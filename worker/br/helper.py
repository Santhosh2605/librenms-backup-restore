"""br-helper - the ONLY privileged component.

Runs as root under systemd (``br-helper.service``). It:

  * listens on a unix socket (``/run/librenms-backup/br-helper.sock``) with
    mode 0660 root:librenms,
  * authenticates every request with an HMAC-SHA256 key stored in a 0640
    root:librenms file, so only the librenms user (and root) can sign,
  * accepts a *closed set* of operations, each with a strict schema,
  * never invokes a shell: subprocess argv lists only,
  * refuses every path outside an allowlist rooted at the configured
    backup/librenms directories,
  * logs every accepted and rejected request to syslog.

Design rule: the helper trusts nothing from the client except identifiers it
can validate.  Even if the GUI and worker were fully compromised into
submitting arbitrary JSON, the helper cannot be made to run an unintended
command.
"""

from __future__ import annotations

import base64
import grp
import hashlib
import hmac
import json
import os
import pwd
import shutil
import socket
import socketserver
import stat
import subprocess
import sys
import threading
import typing as t

from .common import (
    ALLOWED_UNITS,
    BACKUP_ID_RE,
    BRException,
    HelperError,
    Logger,
    VERSION,
    ValidationError,
    safe_join,
    validate_abs_path,
)

DEFAULT_SOCKET = "/run/librenms-backup/br-helper.sock"
HMAC_FILE = "/run/librenms-backup/br-helper.hmac"
SOCKET_GROUP = "librenms"
MAX_REQUEST = 4 * 1024 * 1024
SAFE_SYSTEM_RESTORE_ROOTS = ("/etc/ssl/certs", "/etc/ssl/private")

#: Paths the helper may read/write. Populated from the config at startup.
class PathPolicy:
    def __init__(self, *, backup_root: str, checkpoint_dir: str, librenms_root: str,
                 stage_dir: str, system_paths: t.Sequence[str] = (),
                 integration_paths: t.Sequence[str] = (), app_user: str = "librenms",
                 app_group: str = "librenms"):
        self.writable_roots = [os.path.abspath(p) for p in (backup_root, stage_dir, checkpoint_dir)]
        self.app_user = app_user
        self.app_group = app_group
        # Only permit targets that the configured backup can legitimately
        # restore. Never grant the worker blanket access to /etc or /var/lib.
        safe_system_paths = [os.path.abspath(p) for p in system_paths
                             if any(os.path.abspath(p) == root or
                                    os.path.abspath(p).startswith(root + os.sep)
                                    for root in SAFE_SYSTEM_RESTORE_ROOTS)]
        self.restore_targets = [os.path.abspath(librenms_root), *safe_system_paths,
                                *(os.path.abspath(p) for p in integration_paths)]
        # The app tree is a legitimate SOURCE as well as a target: the
        # permission-repair step re-applies ownership over the installed tree,
        # and requiring staging-only sources made that step impossible.
        self.readable_roots = (self.writable_roots + self.restore_targets
                               + [os.path.abspath(p) for p in integration_paths]
                               + ["/var/backups"])

    def writable(self, path: str) -> str:
        validate_abs_path(path)
        resolved = os.path.realpath(path)
        for root in self.writable_roots:
            if resolved == root or resolved.startswith(root + os.sep):
                return resolved
        raise ValidationError(f"path outside writable roots: {path!r}")

    def restorable(self, path: str) -> str:
        validate_abs_path(path)
        resolved = os.path.realpath(path)
        for root in self.restore_targets:
            if resolved == root or resolved.startswith(root + os.sep):
                return resolved
        raise ValidationError(f"restore target not permitted: {path!r}")

    def readable(self, path: str) -> str:
        validate_abs_path(path)
        resolved = os.path.realpath(path)
        for root in self.readable_roots:
            if resolved == root or resolved.startswith(root + os.sep):
                return resolved
        raise ValidationError(f"path outside readable roots: {path!r}")


def _cred_from_payload(payload: dict):
    """Build DbCredentials from a helper payload, mirroring db.restore rules."""
    from .db import DbCredentials

    creds = payload.get("credentials") or {}
    database = _valid_identifier(str(creds.get("database") or ""))
    user = _valid_identifier(str(creds.get("user") or ""))
    socket_path = str(creds.get("socket") or "")
    if socket_path and socket_path not in ("/run/mysqld/mysqld.sock",
                                           "/var/run/mysqld/mysqld.sock"):
        raise ValidationError(f"database socket not allowed: {socket_path!r}")
    return DbCredentials(
        host=str(creds.get("host") or "127.0.0.1"),
        port=int(creds.get("port") or 3306),
        name=database,
        user=user,
        password=str(creds.get("password") or ""),
        socket=socket_path or None,
    )


class Helper:
    def __init__(self, policy: PathPolicy, logger: Logger):
        self.policy = policy
        self.logger = logger
        self._lock = threading.Lock()

    # -- dispatch -----------------------------------------------------------

    def handle(self, operation: str, payload: dict) -> dict:
        handler = getattr(self, "op_" + operation.replace(".", "_"), None)
        if handler is None:
            raise ValidationError(f"unsupported operation: {operation!r}")
        with self._lock:  # serialise destructive operations
            return handler(payload)

    # -- filesystem ---------------------------------------------------------

    def op_fs_stat(self, payload: dict) -> dict:
        path = self.policy.readable(_require(payload, "path"))
        st = os.stat(path)
        return {
            "path": path,
            "size": st.st_size,
            "mode": stat.S_IMODE(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "mtime": int(st.st_mtime),
            "is_dir": stat.S_ISDIR(st.st_mode),
        }

    def op_fs_hash(self, payload: dict) -> dict:
        path = self.policy.readable(_require(payload, "path"))
        digest = hashlib.sha256()
        with open(path, "rb") as fh:
            for block in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(block)
        return {"path": path, "sha256": digest.hexdigest()}

    def op_fs_mkdir(self, payload: dict) -> dict:
        path = self.policy.writable(_require(payload, "path"))
        mode = int(payload.get("mode", 0o750))
        if mode & 0o007:
            raise ValidationError("refusing to create world-accessible directory")
        os.makedirs(path, exist_ok=True)
        os.chmod(path, mode)
        return {"path": path}

    def op_fs_unlink(self, payload: dict) -> dict:
        path = self.policy.writable(_require(payload, "path"))
        if os.path.isdir(path) and not os.path.islink(path):
            raise ValidationError("refusing to unlink a directory through fs.unlink")
        try:
            os.unlink(path)
        except FileNotFoundError:
            return {"path": path, "existed": False}
        return {"path": path, "existed": True}

    def op_fs_list(self, payload: dict) -> dict:
        path = self.policy.readable(_require(payload, "path"))
        if not os.path.isdir(path):
            raise ValidationError("not a directory")
        entries = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            st = os.lstat(full)
            entries.append({
                "name": name,
                "size": st.st_size,
                "is_dir": stat.S_ISDIR(st.st_mode),
                "mtime": int(st.st_mtime),
            })
        return {"path": path, "entries": entries}

    def op_fs_install_tree(self, payload: dict) -> dict:
        """Copy an extracted tree into its final location and fix ownership.

        Used by restore. ``refuse`` lists path components never to overwrite
        (machine identity files).
        """
        source = self.policy.readable(_require(payload, "source"))
        target = self.policy.restorable(_require(payload, "target"))
        owner = str(payload.get("owner", self.policy.app_user))
        group = str(payload.get("group", self.policy.app_group))
        mode = int(payload.get("mode", 0o750))
        refuse = [str(x) for x in payload.get("refuse", [])]

        if owner != self.policy.app_user or group != self.policy.app_group:
            raise ValidationError("restore ownership must use the configured LibreNMS account")
        if mode not in (0o755, 0o775):
            raise ValidationError("restore mode must be 0755 or 0775")
        # Caller-supplied refuse lists must never be able to weaken protection
        # for host identity and access-control files.
        refuse.extend(("/etc/machine-id", "machine-id", "shadow", "gshadow",
                       "sudoers", "sudoers.d/", "ssh/", "ssl/private/"))

        uid = _uid(owner)
        gid = _gid(group)
        if not os.path.isdir(source):
            raise ValidationError(f"source tree missing: {source}")

        # Fix the target root itself. os.walk never yields the root as a
        # relative path, so without this the tree root keeps whatever mode it
        # had — and a 0750 root blocks the web server from traversing into a
        # perfectly readable tree, which takes the site down after a restore.
        os.makedirs(target, exist_ok=True)
        os.chown(target, uid, gid)
        os.chmod(target, mode)

        copied = 0
        skipped = []
        for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
            rel = os.path.relpath(dirpath, source)
            rel = "" if rel == "." else rel
            dest_dir = safe_join(target, rel) if rel else target
            os.makedirs(dest_dir, exist_ok=True)
            os.chown(dest_dir, uid, gid)
            os.chmod(dest_dir, mode)
            for name in filenames:
                rel_file = f"{rel}/{name}" if rel else name
                if any(_matches_refuse(rel_file, pattern) for pattern in refuse):
                    skipped.append(rel_file)
                    continue
                src = os.path.join(dirpath, name)
                dst = safe_join(target, rel_file)
                if os.path.islink(src):
                    link_target = os.readlink(src)
                    if link_target.startswith("/") or ".." in link_target.split("/"):
                        skipped.append(rel_file)
                        continue
                    if os.path.lexists(dst):
                        os.unlink(dst)
                    os.symlink(link_target, dst)
                    copied += 1
                    continue
                if not os.path.isfile(src):
                    continue
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                tmp = dst + ".br-tmp"
                shutil.copy2(src, tmp)
                os.chown(tmp, uid, gid)
                shutil.copymode(src, tmp)
                # Never preserve setuid/setgid.
                current = stat.S_IMODE(os.stat(tmp).st_mode)
                os.chmod(tmp, current & 0o755)
                os.replace(tmp, dst)
                copied += 1
                if copied % 500 == 0:
                    self.logger.info("install_tree progress", copied=copied)
        return {"target": target, "copied": copied, "skipped": skipped[:200]}

    def op_fs_install_file(self, payload: dict) -> dict:
        return self.op_fs_install_tree(payload)

    # -- services -----------------------------------------------------------

    def _unit(self, payload: dict) -> str:
        unit = str(_require(payload, "unit"))
        if unit not in ALLOWED_UNITS:
            raise ValidationError(f"unit not permitted: {unit!r}")
        return unit

    def op_service_status(self, payload: dict) -> dict:
        unit = self._unit(payload)
        proc = _systemctl(["is-active", unit])
        return {"unit": unit, "active": proc.stdout.decode().strip(), "code": proc.returncode}

    def op_db_table_list(self, payload: dict) -> dict:
        """List base tables in the LibreNMS database."""
        from .db import list_tables

        cred = _cred_from_payload(payload)
        names = list_tables(cred)
        return {"tables": names, "count": len(names)}

    def op_db_preserve_tables(self, payload: dict) -> dict:
        """Capture the plugin's own tables before their schema is replaced.

        The dump is the *user's* database as it was at backup time; it may be
        older than, or entirely predate, this plugin's tables. Without this
        snapshot the restore's own job/restore rows vanish mid-flight (nothing
        can then mark them complete) and the archive registry rolls back.
        """
        from .db import snapshot_tables

        cred = _cred_from_payload(payload)
        tables = [str(x) for x in payload.get("tables") or []]
        return {"tables": snapshot_tables(cred, tables)}

    def op_db_restore_preserved(self, payload: dict) -> dict:
        """Re-write previously captured plugin rows after an import."""
        from .db import restore_snapshot

        cred = _cred_from_payload(payload)
        return {"restored": restore_snapshot(cred, payload.get("tables") or {})}

    def op_service_exists(self, payload: dict) -> dict:
        """Whether systemd actually knows about this unit.

        `is-active` answers "inactive" for a unit that does not exist at all,
        so it cannot be used to decide whether a service is REQUIRED after a
        restore. `systemctl cat` exits non-zero only for unknown units.
        """
        unit = self._unit(payload)
        proc = subprocess.run(["/usr/bin/systemctl", "cat", "--no-pager", unit],
                              capture_output=True, check=False, timeout=30)
        return {"unit": unit, "exists": proc.returncode == 0,
                "code": proc.returncode}

    def op_service_stop(self, payload: dict) -> dict:
        unit = self._unit(payload)
        proc = _systemctl(["stop", unit])
        return {"unit": unit, "code": proc.returncode,
                "output": proc.stdout.decode(errors="replace")[-2000:]}

    def op_service_start(self, payload: dict) -> dict:
        unit = self._unit(payload)
        proc = _systemctl(["start", unit])
        return {"unit": unit, "code": proc.returncode,
                "output": proc.stdout.decode(errors="replace")[-2000:]}

    def op_service_validate(self, payload: dict) -> dict:
        """Read-only validation: config test where one exists, else is-active."""
        unit = self._unit(payload)
        if unit == "apache2":
            proc = subprocess.run(["/usr/sbin/apache2ctl", "configtest"],
                                  capture_output=True, check=False, timeout=120)
            return {"validator": "apache2ctl configtest", "code": proc.returncode,
                    "output": (proc.stdout + proc.stderr).decode(errors="replace")[-4000:]}
        if unit == "mariadb":
            proc = subprocess.run(["/usr/bin/mariadb", "-e", "SELECT 1"],
                                  capture_output=True, check=False, timeout=60)
            return {"validator": "mariadb SELECT 1", "code": proc.returncode,
                    "output": (proc.stdout + proc.stderr).decode(errors="replace")[-4000:]}
        if unit.endswith("-fpm"):
            binary = "/usr/sbin/php-fpm8.4"
            version = unit.replace("php", "").replace("-fpm", "")
            candidate = f"/usr/sbin/php-fpm{version}"
            if os.path.exists(candidate):
                binary = candidate
            if not os.path.exists(binary):
                return {"validator": "none", "code": 0, "output": "php-fpm binary not found"}
            proc = subprocess.run([binary, "-t"], capture_output=True, check=False, timeout=120)
            return {"validator": f"{os.path.basename(binary)} -t", "code": proc.returncode,
                    "output": (proc.stdout + proc.stderr).decode(errors="replace")[-4000:]}
        proc = _systemctl(["is-active", unit])
        return {"validator": "is-active", "code": 0 if proc.returncode == 0 else 3,
                "output": proc.stdout.decode(errors="replace").strip()}

    # -- database -----------------------------------------------------------

    def op_db_restore(self, payload: dict) -> dict:
        """Restore a gzipped SQL dump. Only paths inside staging are accepted."""
        sql_gz = self.policy.readable(_require(payload, "sql_gz"))
        if not sql_gz.endswith(".sql.gz"):
            raise ValidationError("refusing to restore a file that is not *.sql.gz")
        creds = payload.get("credentials") or {}
        database = _valid_identifier(str(creds.get("database") or ""))
        user = _valid_identifier(str(creds.get("user") or ""))
        password = str(creds.get("password") or "")
        host = str(creds.get("host") or "127.0.0.1")
        port = int(creds.get("port") or 3306)
        # RestrictAddressFamilies=AF_UNIX means no TCP: the local socket is the
        # only transport available to this process.
        db_socket = str(creds.get("socket") or "")
        if db_socket:
            allowed_sockets = ("/run/mysqld/mysqld.sock", "/var/run/mysqld/mysqld.sock")
            if db_socket not in allowed_sockets:
                raise ValidationError(f"database socket not allowed: {db_socket!r}")
        else:
            raise ValidationError("a local database socket is required (this helper has no TCP)")

        from .config import Config
        from .common import Logger as L
        from .db import DbCredentials, restore as db_restore

        cfg = Config(os.environ.get("BR_CONF", "/etc/librenms-backup/br.conf"))
        cred = DbCredentials(host=host, port=port, name=database, user=user,
                             password=password, socket=db_socket or None)
        result = db_restore(cred, sql_gz, self.logger, create=True)
        return {"database": database, **result}

    def op_checkpoint_restore(self, payload: dict) -> dict:
        raise ValidationError(
            "checkpoint restores are executed through worker.restore with checkpoint=<id>; "
            "this operation is intentionally unimplemented"
        )

    # -- maintenance --------------------------------------------------------

    def op_maintenance_on(self, payload: dict) -> dict:
        root = self.policy.restorable(str(_require(payload, "librenms_root")))
        flag = os.path.join(root, "storage/framework/down")
        os.makedirs(os.path.dirname(flag), exist_ok=True)
        with open(flag, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"time": _utc(), "message": "LibreNMS restore in progress",
                                 "retry": 300}))
        _chown_app(root, flag)
        # Pause the monitoring scheduler; polling timers resume after restore.
        paused = []
        for unit in ("librenms-scheduler.timer",):
            if _systemctl(["stop", unit]).returncode == 0:
                paused.append(unit)
        return {"flag": flag, "paused": paused}

    def op_maintenance_off(self, payload: dict) -> dict:
        root = self.policy.restorable(str(_require(payload, "librenms_root")))
        flag = os.path.join(root, "storage/framework/down")
        try:
            os.unlink(flag)
        except FileNotFoundError:
            pass
        resumed = []
        for unit in ("librenms-scheduler.timer",):
            if _systemctl(["start", unit]).returncode == 0:
                resumed.append(unit)
        return {"resumed": resumed}

    # -- worker spawning ----------------------------------------------------

    def op_worker_spawn(self, payload: dict) -> dict:
        """Start ``librenms-backup-worker@<job>.service``. Identity only."""
        job_id = str(_require(payload, "job_id"))
        from .common import JOB_ID_RE

        if not JOB_ID_RE.match(job_id):
            raise ValidationError("job_id must be a UUID")
        # --no-block matters: the unit is Type=oneshot and a full backup can run
        # for hours, so waiting here would stall the helper (and its socket).
        proc = _systemctl(["--no-block", "start", f"librenms-backup-worker@{job_id}.service"])
        return {"job_id": job_id, "code": proc.returncode,
                "output": (proc.stdout + proc.stderr).decode(errors="replace")[-2000:]}


# ---------------------------------------------------------------------------
# socket server
# ---------------------------------------------------------------------------

class _Handler(socketserver.BaseRequestHandler):
    helper: Helper
    secret: bytes

    def handle(self) -> None:  # noqa: C901
        try:
            header = b""
            while not header.endswith(b"\n"):
                chunk = self.request.recv(1)
                if not chunk:
                    return
                header += chunk
                if len(header) > 512:
                    return
            length_str, _, signature_b64 = header.decode().strip().partition(" ")
            length = int(length_str)
            if length <= 0 or length > MAX_REQUEST:
                return
            body = b""
            while len(body) < length:
                chunk = self.request.recv(min(65536, length - len(body)))
                if not chunk:
                    return
                body += chunk

            expected = hmac.new(self.secret, body, hashlib.sha256).digest()
            try:
                provided = base64.b64decode(signature_b64)
            except Exception:
                provided = b""
            if not hmac.compare_digest(expected, provided):
                self.helper.logger.error("helper: HMAC mismatch - request rejected")
                self._respond({"ok": False, "error": "authentication failed"})
                return

            request = json.loads(body)
            operation = request.get("op")
            payload = request.get("payload") or {}
            if not isinstance(operation, str) or not isinstance(payload, dict):
                raise ValidationError("malformed request")

            self.helper.logger.info("helper: op", op=operation)
            data = self.helper.handle(operation, payload)
            self._respond({"ok": True, "data": data})
        except BRException as exc:
            self.helper.logger.warn("helper: rejected", error=str(exc))
            self._respond({"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - unexpected
            self.helper.logger.error("helper: internal error", error=repr(exc))
            self._respond({"ok": False, "error": "internal error"})

    def _respond(self, obj: dict) -> None:
        try:
            self.request.sendall(json.dumps(obj).encode())
        except OSError:
            pass


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(socket_path: str = DEFAULT_SOCKET, conf_path: str | None = None) -> int:
    if os.geteuid() != 0:
        print("br-helper must run as root", file=sys.stderr)
        return 1
    from .config import Config

    cfg = Config(conf_path or os.environ.get("BR_CONF", "/etc/librenms-backup/br.conf"))
    log = Logger(os.path.join(cfg.log_dir, "helper.log"), echo=False)

    policy = PathPolicy(
        backup_root=cfg.backup_root,
        checkpoint_dir=cfg.checkpoint_dir,
        librenms_root=cfg.librenms_root,
        stage_dir=cfg.stage_dir,
        system_paths=cfg.get_list("components", "system_paths"),
        integration_paths=cfg.get_list("components", "integration_paths"),
        app_user=cfg.app_user,
        app_group=cfg.app_group,
    )
    _Handler.helper = Helper(policy, log)
    _Handler.secret = _ensure_secret(socket_path, log)

    os.makedirs(os.path.dirname(socket_path), exist_ok=True)
    if os.path.exists(socket_path):
        os.unlink(socket_path)
    server = _Server(socket_path, _Handler)
    gid = _gid(SOCKET_GROUP)
    os.chown(socket_path, 0, gid)
    os.chmod(socket_path, 0o660)
    log.info("br-helper listening", socket=socket_path, version=VERSION)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        server.server_close()
    return 0


def _ensure_secret(socket_path: str, log: Logger) -> bytes:
    path = HMAC_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, "rb") as fh:
            secret = fh.read().strip()
        if len(secret) >= 32:
            os.chown(path, 0, _gid(SOCKET_GROUP))
            os.chmod(path, 0o640)
            return secret
    secret = base64.b64encode(os.urandom(48))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "wb") as fh:
        fh.write(secret + b"\n")
    os.chown(path, 0, _gid(SOCKET_GROUP))
    log.info("br-helper: generated new HMAC secret")
    return secret


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _require(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValidationError(f"missing required field: {key}")
    return value


def _valid_identifier(value: str) -> str:
    import re

    if not re.match(r"^[A-Za-z0-9_$]{1,64}$", value):
        raise ValidationError(f"invalid SQL identifier: {value!r}")
    return value


def _uid(name: str) -> int:
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError as exc:
        raise ValidationError(f"unknown user {name!r}") from exc


def _gid(name: str) -> int:
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError as exc:
        raise ValidationError(f"unknown group {name!r}") from exc


def _systemctl(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["/usr/bin/systemctl", *argv], capture_output=True, check=False,
                          timeout=180)


def _matches_refuse(rel: str, pattern: str) -> bool:
    if pattern.endswith("/") and rel.startswith(pattern):
        return True
    return rel == pattern or rel.endswith("/" + pattern)


def _chown_app(root: str, path: str) -> None:
    try:
        st = os.stat(root)
        os.chown(path, st.st_uid, st.st_gid)
    except OSError:
        pass


def _utc() -> str:
    from .common import utc_now

    return utc_now()


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if "--ensure-running" in argv:
        proc = _systemctl(["start", "br-helper.service"])
        return proc.returncode
    return serve()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
