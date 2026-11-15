"""Client side of the privilege boundary.

The worker runs as ``librenms`` and has exactly one escalation path:

    sudo -n /usr/local/libexec/librenms-backup/br-helper

Arguments are *never* passed on the command line. The request goes as JSON
over the helper's unix socket, authenticated with a per-process HMAC key read
from a root-owned file in ``/run`` that the librenms group can read.

Every operation is a closed enum.  The helper re-validates everything; this
client validates too, so mistakes fail early and loudly.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import time
import typing as t

from .common import (
    BRException,
    HelperError,
    Logger,
    ValidationError,
    BACKUP_ID_RE,
    run,
)

DEFAULT_SOCKET = "/run/librenms-backup/br-helper.sock"
HMAC_FILE = "/run/librenms-backup/br-helper.hmac"
HELPER_BIN = "/usr/local/libexec/librenms-backup/br-helper"

OPERATIONS = {
    "fs.stat",
    "fs.list",
    "fs.mkdir",
    "fs.unlink",
    "fs.hash",
    "fs.install_tree",     # copy extracted tree into place, fix ownership
    "fs.install_file",
    "service.status",
    "service.exists",
    "service.stop",
    "service.start",
    "service.validate",
    "db.table_list",
    "db.preserve_tables",
    "db.restore_preserved",
    "db.restore",
    "checkpoint.restore",
    "worker.spawn",        # start systemd unit for a job
    "maintenance.on",
    "maintenance.off",
}


class HelperClient:
    def __init__(self, socket_path: str = DEFAULT_SOCKET, logger: Logger | None = None):
        self.socket_path = socket_path
        self.logger = logger or Logger()

    # -- transport ----------------------------------------------------------

    def _secret(self) -> bytes:
        if not os.path.exists(HMAC_FILE):
            raise HelperError(
                f"helper HMAC file missing: {HMAC_FILE} (is br-helper.service running?)"
            )
        with open(HMAC_FILE, "rb") as fh:
            return fh.read().strip()

    def call(self, operation: str, payload: dict | None = None, *,
             timeout: int = 900, response_timeout: int = 900) -> dict:
        if operation not in OPERATIONS:
            raise ValidationError(f"operation not permitted: {operation!r}")
        payload = payload or {}
        body = json.dumps({"op": operation, "payload": payload}, separators=(",", ":")).encode()
        secret = self._secret()
        signature = base64.b64encode(hmac.new(secret, body, hashlib.sha256).digest())

        if not os.path.exists(self.socket_path):
            self._start_helper()

        deadline = time.time() + 10
        last_error: Exception | None = None
        while time.time() < deadline:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                    sock.settimeout(timeout)
                    sock.connect(self.socket_path)
                    header = f"{len(body)} {signature.decode()}\n".encode()
                    sock.sendall(header + body)
                    data = b""
                    while True:
                        block = sock.recv(65536)
                        if not block:
                            break
                        data += block
                        if len(data) > 64 * 1024 * 1024:
                            raise HelperError("helper response too large")
                return self._parse(data)
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                last_error = exc
                time.sleep(0.5)
        raise HelperError(f"cannot reach br-helper: {last_error}")

    @staticmethod
    def _parse(data: bytes) -> dict:
        if not data:
            raise HelperError("empty response from helper")
        try:
            result = json.loads(data)
        except ValueError as exc:
            raise HelperError(f"malformed helper response: {exc}") from exc
        if not isinstance(result, dict) or "ok" not in result:
            raise HelperError("helper response missing status")
        if not result["ok"]:
            raise HelperError(str(result.get("error") or "helper reported failure"))
        return result.get("data") or {}

    def _start_helper(self) -> None:
        """Ask systemd to (re)start the helper; this is the single sudo rule."""
        self.logger.warn("br-helper socket missing - requesting start via systemctl")
        try:
            subprocess.run(
                ["/usr/bin/sudo", "-n", HELPER_BIN, "--ensure-running"],
                check=False, capture_output=True, timeout=30,
            )
        except Exception as exc:  # pragma: no cover
            self.logger.warn("failed to start helper", error=str(exc))
        time.sleep(1)

    # -- typed helpers ------------------------------------------------------

    def hash_file(self, path: str) -> str:
        return self.call("fs.hash", {"path": path})["sha256"]

    def stat(self, path: str) -> dict:
        return self.call("fs.stat", {"path": path})

    def mkdir(self, path: str, mode: int = 0o750) -> None:
        self.call("fs.mkdir", {"path": path, "mode": int(mode)})

    def unlink(self, path: str) -> None:
        self.call("fs.unlink", {"path": path})

    def service_exists(self, unit: str) -> bool:
        try:
            data = self.call("service.exists", {"unit": unit})
        except ValidationError:
            # Older helpers do not implement the op; assume it exists so the
            # caller keeps its original strict behaviour.
            return True
        return bool(data.get("exists"))

    def service(self, action: str, unit: str) -> dict:
        return self.call(f"service.{action}", {"unit": unit})

    def db_preserve_tables(self, *, credentials: dict, tables: list[str]) -> dict:
        return self.call("db.preserve_tables",
                         {"credentials": credentials, "tables": list(tables)}, timeout=900)

    def db_restore_preserved(self, *, credentials: dict, tables: dict) -> dict:
        return self.call("db.restore_preserved",
                         {"credentials": credentials, "tables": tables}, timeout=900)

    def db_restore(self, *, sql_gz: str, database: str, credential_hint: dict) -> dict:
        return self.call(
            "db.restore",
            {"sql_gz": sql_gz, "database": database, "credentials": credential_hint},
            timeout=7200,
        )

    def install_tree(self, *, source: str, target: str, owner: str, group: str,
                     mode: int = 0o750, refuse: t.Sequence[str] = ()) -> dict:
        return self.call(
            "fs.install_tree",
            {"source": source, "target": target, "owner": owner, "group": group,
             "mode": int(mode), "refuse": list(refuse)},
            timeout=3600,
        )

    def maintenance(self, enabled: bool, *, librenms_root: str) -> dict:
        return self.call("maintenance.on" if enabled else "maintenance.off",
                         {"librenms_root": librenms_root}, timeout=120)
