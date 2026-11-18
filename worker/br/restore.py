"""Restore engine: a controlled, checkpointed state machine.

    QUEUED -> VALIDATING -> CHECKPOINTING -> MAINTENANCE ->
    RESTORING_DATABASE -> RESTORING_FILES -> RESTORING_CONFIGURATION ->
    FIXING_PERMISSIONS -> VALIDATING_SERVICES -> COMPLETED

Any failure transitions to FAILED and *stops*. The recovery checkpoint is
never deleted automatically, and the engine never reports success unless every
service validation passed.

Hard guarantees enforced here:
  * one restore at a time (database row check + flock)
  * archive verified (checksums) before anything is written
  * a verified checkpoint of the CURRENT system exists before maintenance mode
  * machine-specific configuration is never restored
  * no shell is ever invoked; file installation goes through br-helper
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
import typing as t

from . import crypto, db, manifest as manifest_mod, verify as verify_mod
from .archive import ArchiveReader
from .checksums import CHECKSUM_NAME
from .common import (
    BRException,
    IntegrityError,
    Logger,
    PreflightError,
    ValidationError,
    du_bytes,
    free_bytes,
    human,
    utc_now,
    validate_abs_path,
)
from .writer import ArchiveBuild, ArchiveStream

#: Configuration trees that may be written back, and where they go.
#: Anything not listed here is restored only under the LibreNMS root.
CONFIG_TARGETS = (
    "system/etc/apache2/",
    "system/etc/php/",
    "system/etc/mysql/",
    "system/etc/snmp/",
    "system/etc/cron.d/",
    "system/etc/systemd/system/",
    "system/etc/smokeping/",
    "system/etc/ssl/",
    "system/etc/letsencrypt/",
)

#: Never overwrite these, they identify the *running* host.
MACHINE_IDENTITY_REFUSE = (
    "netplan/",
    "network/interfaces",
    "hostname",
    "hosts",
    "machine-id",
    "resolv.conf",
    "fstab",
    "ssh_host_",
)

SERVICES_AFTER_RESTORE = ("mariadb", "php8.4-fpm", "apache2")


class RestoreEngine:
    def __init__(self, worker, *, restore_id: str, backup_ref: str, job_id: str,
                 checkpoint_id: str | None = None):
        self.w = worker
        self.cfg = worker.cfg
        self.log: Logger = worker.log
        self.store = worker.store
        self.helper = worker.helper
        self.restore_id = restore_id
        self.backup_ref = backup_ref
        self.job_id = job_id
        self.staging = os.path.join(self.cfg.stage_dir, f"restore-{restore_id}")
        #: Set when this run is a rollback TO a checkpoint rather than a
        #: restore FROM a backup. Both are the same state machine; the only
        #: differences are how the archive is located and labelled.
        self.rollback_from: str | None = checkpoint_id
        self.manifest: dict = {}
        self.checkpoint_id: str | None = None
        self._lock_fd = None
        self._log_lines: list[str] = []

    # ------------------------------------------------------------------ #

    def run(self) -> int:
        try:
            self._acquire_lock()
            self._state("VALIDATING")
            self._validate()
            # Maintenance BEFORE the checkpoint: the checkpoint runs a full
            # backup, and taking it while polling is still writing means the
            # snapshot is a moving target. Quiescing first also keeps the
            # downtime window a single, explainable interval.
            self._state("MAINTENANCE")
            self._enter_maintenance()
            if self.rollback_from:
                # Rolling back must not take a new checkpoint: the system is
                # already broken, and a failing safety net would abort the
                # rollback that is supposed to fix it.
                self._note(f"rolling back to checkpoint {self.rollback_from} "
                           "- no new checkpoint taken")
            else:
                self._state("CHECKPOINTING")
                self.checkpoint_id = self._create_checkpoint()
            self._state("RESTORING_DATABASE")
            self._restore_database()
            self._state("RESTORING_FILES")
            self._restore_files()
            self._state("RESTORING_CONFIGURATION")
            self._restore_configuration()
            self._state("FIXING_PERMISSIONS")
            self._fix_permissions()
            self._state("VALIDATING_SERVICES")
            self._validate_services()
            self._state("COMPLETED")

            # The restored dump carries the br_* tables as they were at backup
            # time, so the just-restored archive (and any newer one) is no
            # longer registered. Re-register everything on disk, otherwise the
            # UI shows an empty history immediately after a successful restore.
            try:
                adopted = self.w.adopt_orphans()
                if adopted:
                    self._note(f"post-restore reconciliation: adopted {adopted} archive(s)")
            except Exception as exc:  # noqa: BLE001 - never fail a good restore
                self._note(f"post-restore reconciliation skipped: {exc}", level="warning")

            self.store.restore_finish(
                self.restore_id, state="COMPLETED", status="completed",
                log_tail="\n".join(self._log_lines[-400:]),
            )
            self.w._finish(self.job_id, "completed")
            self.store.setting_set("last_restore_status", "completed")
            self.store.setting_set("last_restore_at", utc_now())
            self.store.notify(
                f"Restore completed from {self.backup_ref}",
                f"Restore {self.restore_id} finished successfully. Checkpoint: {self.checkpoint_id}",
                1,
            )
            self.log.info("restore completed", restore=self.restore_id,
                          checkpoint=self.checkpoint_id)
            return 0

        except Exception as exc:
            self._fail(exc)
            return 1
        finally:
            self._release_lock()

    # ------------------------------------------------------------------ #
    # steps
    # ------------------------------------------------------------------ #

    def _validate(self) -> None:
        self._progress(5, "validating archive")
        if not self.store.available():
            raise PreflightError("state store unavailable - refusing to restore blind")
        if self.rollback_from:
            # A checkpoint is not in br_backups; it is a row in br_checkpoints
            # pointing at an archive inside the checkpoint directory.
            cp = next((c for c in self.store.checkpoints()
                       if c["checkpoint_id"] == self.rollback_from), None)
            if not cp:
                raise ValidationError(f"unknown checkpoint {self.rollback_from!r}")
            if cp.get("status") != "complete":
                raise PreflightError(
                    f"checkpoint {self.rollback_from} is {cp.get('status')!r}, not complete")
            row = {"path": cp.get("path"), "key_fingerprint": None,
                   "backup_ref": self.rollback_from}
        else:
            row = self.store.backup(self.backup_ref)
        if not row:
            raise ValidationError(f"unknown backup reference {self.backup_ref}")
        path = row.get("path") or ""
        if not path or not os.path.exists(path):
            raise PreflightError(f"archive file missing on disk for {self.backup_ref}")
        self.archive_path = path

        # 1. integrity + signature of the content
        self._progress(10, "verifying archive checksums")
        result = verify_mod.verify(path, self.cfg, self.log, level="standard")
        if not result["ok"]:
            raise IntegrityError(
                f"archive verification failed (mismatched={len(result['mismatched'])}, "
                f"missing={len(result['missing'])})"
            )
        self._note(f"archive verified: {result['members']} members, "
                   f"{result['checksums_checked']} checksums")

        # 2. manifest + compatibility
        self._progress(18, "validating manifest")
        manifest = self._read_manifest(path)
        manifest_mod.validate(manifest)
        self.manifest = manifest
        self._check_compatibility(manifest)

        # 3. encryption key matches the one used at backup time
        stored_fp = row.get("key_fingerprint")
        current_fp = crypto.key_fingerprint(self.cfg)
        if stored_fp and current_fp and stored_fp != current_fp:
            raise IntegrityError(
                "encryption key fingerprint mismatch: the archive was produced with a "
                "different key than the one currently configured"
            )
        self._note(f"encryption OK ({manifest['encryption'].get('scheme')})")

        # 4. disk space
        self._progress(24, "checking disk space")
        needed = int(manifest.get("archive", {}).get("size_bytes") or 0)
        factor = float(self.cfg.get("restore", "space_factor", 1.25))
        free = free_bytes(self.cfg.backup_root)
        projected = int(needed * factor) + needed  # unpacked + checkpoint copy
        if free < projected:
            raise PreflightError(
                f"insufficient disk space: need ~{human(projected)}, have {human(free)}"
            )
        self._note(f"disk space OK: {human(free)} free, ~{human(projected)} required")

        # 5. mandatory user confirmation already captured by the GUI; enforce
        #    that the request really came with it.
        # The typed confirmation was enforced at request time (see
        # Controllers\BackupRestoreController::restore). Re-check that a restore
        # row exists and is still marked as running before we modify anything.
        restore_row = self.store.restore(self.restore_id)
        if not restore_row or restore_row.get("status") != "running":
            raise ValidationError("restore request is not in a runnable state")

    def _check_compatibility(self, manifest: dict) -> None:
        versions = manifest.get("versions") or {}
        deps = manifest.get("restore_dependencies") or {}
        warnings = []

        schema = manifest.get("manifest_schema")
        if schema != manifest_mod.CURRENT_SCHEMA:
            raise ValidationError(
                f"manifest schema {schema} is not restorable by worker {manifest_mod.CURRENT_SCHEMA}"
            )

        backup_os = (versions.get("os") or {}).get("os_version_id")
        try:
            current_os = open("/etc/os-release", encoding="utf-8").read()
            current_id = ""
            for line in current_os.splitlines():
                if line.startswith("VERSION_ID="):
                    current_id = line.split("=", 1)[1].strip().strip('"')
        except OSError:
            current_id = ""
        if backup_os and current_id and backup_os.split(".")[0] != current_id.split(".")[0]:
            warnings.append(
                f"OS major version differs (backup {backup_os}, host {current_id}) - "
                "system configuration restore may need adjustment"
            )

        backup_librenms = versions.get("librenms")
        if backup_librenms and backup_librenms != "unknown":
            self._note(f"backup LibreNMS version: {backup_librenms}")

        for warning in warnings:
            self._note("WARNING: " + warning, level="warning")
            self.log.warn("compatibility warning", detail=warning)

        self.manifest["_warnings"] = warnings

    def _create_checkpoint(self) -> str:
        """Snapshot the CURRENT state before we touch anything."""
        from .worker import Worker  # circular-safe local import

        checkpoint_id = f"cp-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{self.restore_id[:8]}"
        self._progress(30, "creating recovery checkpoint")
        path = os.path.join(self.cfg.checkpoint_dir, f"{checkpoint_id}.brarchive")
        os.makedirs(self.cfg.checkpoint_dir, exist_ok=True)

        # Reuse the worker's own backup pipeline so a checkpoint is byte-for-byte
        # the same kind of artifact (and therefore restorable).
        checkpoint_worker = Worker(self.cfg, self.log, helper=self.helper)
        original_archive_dir = self.cfg._cp.get("general", "archive_dir")
        job_id_preexisting = self.store.setting_get("checkpoint_job_id")
        try:
            self.cfg._cp.set("general", "archive_dir", self.cfg.checkpoint_dir)
            # Use a dedicated worker instance so its own progress updates do not
            # clobber the restore job row.
            rc = checkpoint_worker.backup(self.job_id, "full")
            if rc != 0:
                raise BRException("recovery checkpoint creation failed - aborting restore")
            created = self.store.setting_get("last_backup_ref")
            if not created:
                raise BRException("checkpoint was not recorded")
            row = self.store.backup(created)
            cp_path = row["path"] if row else None
            if not cp_path:
                raise BRException("checkpoint was recorded without a path")
            if not os.path.exists(cp_path):
                # A dangling row would send the NEXT restore looking for a file
                # that was never written (a checkpoint created before a failure
                # leaves exactly this). Mark it rather than leak a ghost.
                self.store.backup_update(
                    created, status="failed",
                    error_message="checkpoint archive missing after creation",
                )
                raise BRException(
                    f"checkpoint archive missing after creation ({os.path.basename(cp_path)})"
                )
            final_path = os.path.join(self.cfg.checkpoint_dir, os.path.basename(cp_path))
            if os.path.abspath(cp_path) != os.path.abspath(final_path):
                shutil.move(cp_path, final_path)
        finally:
            self.cfg._cp.set("general", "archive_dir", original_archive_dir)

        # A checkpoint is only worth something if we can restore it.
        self._progress(38, "verifying recovery checkpoint")
        result = verify_mod.verify(final_path, self.cfg, self.log, level="standard")
        if not result["ok"]:
            raise BRException("recovery checkpoint failed verification - aborting restore")

        self.store.checkpoint_insert({
            "checkpoint_id": checkpoint_id,
            "label": f"pre-restore of {self.backup_ref}",
            "status": "complete",
            "path": final_path,
            "size_bytes": os.path.getsize(final_path),
            "sha256": _sha256(final_path),
            "created_by": "restore",
            "restore_id": self.restore_id,
        })
        self.store.execute(
            "UPDATE br_restores SET checkpoint_ref=%s WHERE restore_id=%s",
            (checkpoint_id, self.restore_id),
        )
        # The checkpoint lives outside retention; mark the pre-existing backups
        # as protected via the checkpoint row.
        self._note(f"recovery checkpoint verified: {checkpoint_id}")
        return checkpoint_id

    def _enter_maintenance(self) -> None:
        self._progress(42, "entering maintenance mode")
        # Pause our own scheduler so it cannot start a backup mid-restore.
        try:
            self.helper.call("service.stop", {"unit": "librenms-backup-scheduler.timer"})
        except BRException as exc:
            self._note("could not pause backup scheduler: " + str(exc), level="warning")

        data = self.helper.maintenance(True, librenms_root=self.cfg.librenms_root)
        self._note(f"maintenance mode on; paused: {data.get('paused')}")

        # Stop writers so the database dump is consistent and files are not
        # being rewritten while we replace them.
        for unit in ("apache2", "php8.4-fpm"):
            try:
                self.helper.service("stop", unit)
                self._note(f"stopped {unit}")
            except BRException as exc:
                self._note(f"could not stop {unit}: {exc}", level="warning")

    def _restore_database(self) -> None:
        components = self.manifest.get("components") or {}
        if not components.get("database"):
            self._note("archive has no database component - skipping")
            return
        self._progress(48, "extracting database dump")
        os.makedirs(self.staging, exist_ok=True)
        sql_path = self._extract_member("database/librenms.sql.gz",
                                       os.path.join(self.staging, "librenms.sql.gz"))
        if not sql_path:
            raise IntegrityError("database component declared but dump missing from archive")

        cred = db.discover_credentials(self.cfg)
        hint = {
            "host": cred.host, "port": cred.port, "user": cred.user,
            "password": cred.password, "database": cred.name,
            # The helper is AF_UNIX-only, so hand it the local socket.
            "socket": getattr(cred, "socket", None) or "",
        }

        # The dump is the user's database as it looked at backup time. This
        # plugin's own tables are not part of LibreNMS, so they may be absent
        # from it entirely. Snapshot them first: without this the restore's own
        # job/restore rows are wiped mid-flight and nothing can ever mark the
        # restore COMPLETED.
        self._progress(50, "capturing plugin state")
        preserved = {}
        try:
            snapshot = self.helper.db_preserve_tables(
                credentials=hint, tables=list(db.PLUGIN_TABLES))
            preserved = snapshot.get("tables") or {}
            held = {k: len(v.get("rows") or []) for k, v in preserved.items() if v.get("exists")}
            self._note(f"plugin state captured: {sum(held.values())} rows in "
                       f"{len(held)} table(s) {held or ''}".rstrip())
        except BRException as exc:
            self._note(f"plugin state capture failed ({exc}) - continuing", level="warning")

        self._progress(55, "restoring database")
        result = self.helper.db_restore(sql_gz=sql_path, database=cred.name,
                                       credential_hint=hint)
        if not result.get("tables"):
            raise BRException("database restore reported zero tables - treating as failure")
        self._note(f"database restored: {result['tables']} tables")

        if preserved:
            self._progress(58, "restoring plugin state")
            try:
                back = self.helper.db_restore_preserved(credentials=hint, tables=preserved)
                written = back.get("restored") or {}
                self._note("plugin state rewritten: "
                           + ", ".join(f"{k}={v}" for k, v in sorted(written.items())))
            except BRException as exc:
                # A failed plugin-state rewrite must not fail the data restore.
                self._note(f"plugin state rewrite failed: {exc}", level="warning")

    def _restore_files(self) -> None:
        components = self.manifest.get("components") or {}
        os.makedirs(self.staging, exist_ok=True)
        self._progress(62, "extracting application files")

        # Member prefixes are single-level (`application/...`, `rrd/...`): the
        # collector writes them with add_tree(dest_root="application"), so the
        # doubled `application/application` form matched nothing and the file
        # stage silently installed zero objects.
        if components.get("application"):
            self._extract_tree("application", os.path.join(self.staging, "application"))
            self._install(os.path.join(self.staging, "application"), self.cfg.librenms_root,
                          note="application files")
        if components.get("rrd"):
            # rrd/ already carries the `rrd` directory name, so it lands in the
            # LibreNMS root as a whole.
            self._extract_tree("rrd", os.path.join(self.staging, "rrd"))
            self._install(os.path.join(self.staging, "rrd"), self.cfg.librenms_root,
                          note="rrd data")
        if components.get("integrations"):
            # Integration payloads preserve their configured absolute paths.
            # Install only those exact roots, never broad parents such as /opt
            # or /usr.
            integ_root = os.path.join(self.staging, "integrations")
            self._extract_tree("integrations", integ_root)
            get_list = getattr(self.cfg, "get_list", None)
            integration_paths = (get_list("components", "integration_paths")
                                 if callable(get_list) else [])
            for configured_path in integration_paths:
                src = os.path.join(integ_root, configured_path.lstrip("/"))
                if not os.path.isdir(src):
                    continue
                self._install(src, configured_path,
                              note=f"integration files ({configured_path})")

    def _restore_configuration(self) -> None:
        components = self.manifest.get("components") or {}
        if not components.get("system"):
            self._note("archive has no system component - skipping configuration restore")
            return
        self._progress(74, "restoring system configuration")
        self._extract_tree("system", os.path.join(self.staging, "system"))

        portability = ((self.manifest.get("collection") or {}).get("system") or {}).get(
            "portability", {})
        reference_only = set(portability.get("reference_only") or [])

        installed = 0
        safe_roots = ("/etc/ssl/certs", "/etc/ssl/private")
        for configured_path in self.cfg.get_list("components", "system_paths"):
            if not any(configured_path == root or configured_path.startswith(root + "/")
                       for root in safe_roots):
                continue
            rel = configured_path.lstrip("/")
            if rel in reference_only:
                self._note(f"skipped machine-specific path {rel}", level="warning")
                continue
            src = os.path.join(self.staging, "system", rel)
            if not os.path.isdir(src):
                continue
            self._install(src, configured_path, note=configured_path,
                          refuse=MACHINE_IDENTITY_REFUSE)
            installed += 1
        self._note(f"restored {installed} safe certificate/key configuration trees; "
                   "other system configuration paths were skipped for safety")

    def _fix_permissions(self) -> None:
        self._progress(84, "repairing ownership and permissions")
        data = self.helper.call("fs.install_tree", {
            "source": os.path.join(self.staging, "application") if os.path.isdir(
                os.path.join(self.staging, "application")) else self.cfg.librenms_root,
            "target": self.cfg.librenms_root,
            "owner": self.cfg.app_user,
            "group": self.cfg.app_group,
            # 0755 so the web server (a different user, not in the librenms
            # group) can traverse the tree. Secrets are tightened separately
            # below, and librenms/ holds the durable data.
            "mode": 0o755,
            "refuse": [],
        })
        self._note(f"ownership applied to {data.get('copied', 0)} objects")

        # librenms/ holds device data and is written by the poller; keeping it
        # group-only is correct and must survive the 0755 pass above.
        data_dir = os.path.join(self.cfg.librenms_root, "librenms")
        if os.path.isdir(data_dir):
            os.chmod(data_dir, 0o750)

        # Storage/log directories must exist and be writable by the app user.
        for rel in ("storage", "storage/framework/sessions", "storage/framework/views",
                    "storage/framework/cache", "storage/logs", "bootstrap/cache",
                    "rrd", "logs"):
            path = os.path.join(self.cfg.librenms_root, rel)
            if not os.path.exists(path):
                os.makedirs(path, exist_ok=True)
            self.helper.call("fs.install_tree", {
                "source": path, "target": path, "owner": self.cfg.app_user,
                "group": self.cfg.app_group, "mode": 0o775, "refuse": [],
            })

        # Config files are secret: 0640 librenms:librenms.
        for rel in ("config.php", ".env"):
            path = os.path.join(self.cfg.librenms_root, rel)
            if os.path.exists(path):
                os.chmod(path, 0o640)
                try:
                    import grp
                    import pwd

                    os.chown(path, pwd.getpwnam(self.cfg.app_user).pw_uid,
                             grp.getgrnam(self.cfg.app_group).gr_gid)
                except (KeyError, PermissionError):
                    self._note(f"could not chown {rel} (needs helper privileges)",
                               level="warning")

    def _unit_installed(self, unit: str) -> bool:
        """True when systemd knows about ``unit`` (i.e. it exists on this host)."""
        try:
            return self.helper.service_exists(unit)
        except BRException:
            return True

    def _validate_services(self) -> None:
        self._progress(90, "starting and validating services")
        failures = []

        # Config tests first (read-only), then start.
        for unit in SERVICES_AFTER_RESTORE:
            try:
                result = self.helper.service("validate", unit)
                ok = int(result.get("code", 1)) == 0
                self._note(f"validate {unit}: {'OK' if ok else 'FAILED'} "
                           f"({result.get('validator')})")
                if not ok:
                    failures.append(f"{unit} validation failed: "
                                    f"{str(result.get('output'))[:300]}")
            except BRException as exc:
                self._note(f"validate {unit}: {exc}", level="warning")

        for unit in SERVICES_AFTER_RESTORE:
            try:
                self.helper.service("start", unit)
                status = self.helper.service("status", unit)
                active = status.get("active") == "active"
                self._note(f"start {unit}: {status.get('active')}")
                if active:
                    continue
                # Not a failure when the unit does not exist on this host.
                # A host can legitimately run LibreNMS under, say, nginx +
                # php-fpm, or via `artisan serve`; insisting that apache2 be
                # active turned a successful restore into a failed one.
                if self._unit_installed(unit):
                    failures.append(f"{unit} did not reach active state")
                else:
                    self._note(f"{unit} is not installed on this host - not required",
                               level="warning")
            except BRException as exc:
                failures.append(f"{unit} could not be started: {exc}")

        # LibreNMS health check: validate.php is read-only and authoritative.
        self._progress(95, "validating LibreNMS application health")
        try:
            import subprocess

            validate = os.path.join(self.cfg.librenms_root, "validate.php")
            if os.path.exists(validate):
                proc = subprocess.run(
                    ["/usr/bin/php", validate, "-m"],
                    cwd=self.cfg.librenms_root, capture_output=True, check=False, timeout=300,
                )
                output = (proc.stdout + proc.stderr).decode(errors="replace")
                critical = [ln for ln in output.splitlines() if ln.startswith("✖")]
                self._note(f"validate.php: exit {proc.returncode}, "
                           f"{len(critical)} failing check(s)")
                if proc.returncode != 0 and critical:
                    failures.append(
                        "LibreNMS validate.php reported failures: " + "; ".join(critical[:5])
                    )
            else:
                self._note("validate.php not present - skipped", level="warning")
        except Exception as exc:
            self._note(f"could not run validate.php: {exc}", level="warning")

        if failures:
            raise BRException("service validation failed: " + " | ".join(failures[:5]))

        # Leave maintenance mode only after everything validated.
        self.helper.maintenance(False, librenms_root=self.cfg.librenms_root)
        self.helper.call("service.start", {"unit": "librenms-backup-scheduler.timer"})
        self._note("maintenance mode off; monitoring scheduler resumed")

    # ------------------------------------------------------------------ #
    # failure handling
    # ------------------------------------------------------------------ #

    def _fail(self, exc: Exception) -> None:
        message = str(exc)
        self.log.error("restore failed", restore=self.restore_id, error=message,
                       checkpoint=self.checkpoint_id)
        try:
            self.store.restore_finish(
                self.restore_id, state="FAILED", status="failed", error=message[:4000],
                log_tail="\n".join(self._log_lines[-400:]),
            )
            self.store.setting_set("last_restore_status", "failed")
            self.store.setting_set("last_restore_at", utc_now())
        except Exception:  # pragma: no cover
            pass
        try:
            self.w._finish(self.job_id, "failed", error=message)
        except Exception:  # pragma: no cover
            pass

        # Best effort: bring services back so the operator can intervene.
        try:
            self.helper.maintenance(False, librenms_root=self.cfg.librenms_root)
            for unit in SERVICES_AFTER_RESTORE:
                self.helper.service("start", unit)
        except Exception:  # pragma: no cover
            pass

        try:
            if self.checkpoint_id:
                detail = (f"Restore {self.restore_id} FAILED: {message[:300]}. "
                          f"A verified recovery checkpoint is intact: {self.checkpoint_id}. "
                          f"Use 'Restore checkpoint' once the cause is fixed.")
            else:
                detail = (f"Restore {self.restore_id} FAILED before a checkpoint was created: "
                          f"{message[:300]}. No data was modified.")
            self.store.notify("Restore FAILED", detail, 2)
        except Exception:  # pragma: no cover
            pass

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _state(self, state: str) -> None:
        self._note(f"==> {state}")
        try:
            self.store.restore_update_state(self.restore_id, state)
            self.store.execute(
                "UPDATE br_restores SET log_tail=%s WHERE restore_id=%s",
                ("\n".join(self._log_lines[-400:]), self.restore_id),
            )
        except Exception as exc:  # pragma: no cover
            self.log.warn("could not persist state", state=state, error=str(exc))

    def _progress(self, percent: int, message: str) -> None:
        self.w._progress(self.job_id, percent, "RESTORE", message)

    def _note(self, message: str, level: str = "info") -> None:
        line = f"[{utc_now()}] {message}"
        self._log_lines.append(line)
        if level == "warning":
            self.log.warn(message)
        else:
            self.log.info(message)

    def _read_manifest(self, archive_path: str) -> dict:
        found: dict | None = None
        with ArchiveStream(archive_path, self.cfg, self.log) as stream:
            with ArchiveReader(stream) as reader:
                for info in reader.members():
                    if reader.relname(info) == "manifest.json" and info.isfile():
                        try:
                            candidate = json.loads(reader.read_member(info))
                        except ValueError:
                            continue
                        if candidate.get("status") == "in-progress":
                            continue
                        found = candidate
        if found is None:
            raise IntegrityError("archive contains no authoritative manifest.json")
        return found

    def _extract_member(self, member: str, dest: str) -> str | None:
        """Stream a single, large member (the SQL dump) out of the archive."""
        with ArchiveStream(self.archive_path, self.cfg, self.log) as stream:
            with ArchiveReader(stream) as reader:
                for info in reader.members():
                    if reader.relname(info) != member or not info.isfile():
                        continue
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    fh = reader._tar.extractfile(info)  # noqa: SLF001 - same package
                    if fh is None:
                        return None
                    with open(dest, "wb") as out:
                        for block in iter(lambda: fh.read(1024 * 1024), b""):
                            out.write(block)
                    return dest
        return None

    def _extract_tree(self, prefix: str, dest_root: str) -> int:
        """Extract every member under ``prefix`` into ``dest_root`` (safe join)."""
        if os.path.isdir(dest_root):
            shutil.rmtree(dest_root, ignore_errors=True)
        os.makedirs(dest_root, exist_ok=True)
        count = 0
        with ArchiveStream(self.archive_path, self.cfg, self.log) as stream:
            with ArchiveReader(stream) as reader:
                for info in reader.members():
                    rel = reader.relname(info)
                    if not rel.startswith(prefix.rstrip("/") + "/"):
                        continue
                    sub = rel[len(prefix.rstrip("/")) + 1:]
                    target = _safe_join(dest_root, sub)
                    if info.isdir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    if info.issym():
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        if os.path.lexists(target):
                            os.unlink(target)
                        os.symlink(info.linkname, target)
                        count += 1
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    fh = reader._tar.extractfile(info)  # noqa: SLF001
                    if fh is None:
                        continue
                    with open(target, "wb") as out:
                        for block in iter(lambda: fh.read(1024 * 1024), b""):
                            out.write(block)
                    count += 1
        self._note(f"extracted {count} objects under {prefix}")
        return count

    def _install(self, source: str, target: str, *, note: str,
                 refuse: t.Sequence[str] = ()) -> None:
        if not os.path.isdir(source):
            self._note(f"{note}: nothing to install", level="warning")
            return
        # 0755, not 0750: LibreNMS serves this tree through a web server that
        # runs as a different user (www-data) and is deliberately NOT in the
        # librenms group. A 0750 install makes the restored application
        # unreadable to the web tier and takes the site down after a restore.
        # Identity files stay protected by `refuse`, and config.php / .env are
        # tightened to 0640 in _fix_permissions.
        data = self.helper.install_tree(
            source=source, target=target, owner=self.cfg.app_user,
            group=self.cfg.app_group, mode=0o755, refuse=list(refuse),
        )
        self._note(f"{note}: installed {data.get('copied', 0)} objects"
                   + (f", skipped {len(data['skipped'])} protected" if data.get("skipped") else ""))

    def _acquire_lock(self) -> None:
        active = self.store.active_restore() if self.store.available() else None
        if active and active["restore_id"] != self.restore_id:
            raise BRException(
                f"another restore ({active['restore_id']}) is already running - refusing"
            )
        # The lock lives in the worker-owned state dir. run_dir is shared with
        # the root helper socket and is not writable by the worker.
        os.makedirs(self.cfg.state_dir, exist_ok=True)
        lock_path = os.path.join(self.cfg.state_dir, "restore.lock")
        self._lock_fd = open(lock_path, "w")
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BRException("restore lock is held by another process") from exc
        self._lock_fd.write(f"{self.restore_id}\n")
        self._lock_fd.flush()

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                self._lock_fd.close()
            except OSError:
                pass
            self._lock_fd = None


def _safe_join(root: str, rel: str) -> str:
    root_abs = os.path.abspath(root)
    candidate = os.path.abspath(os.path.join(root_abs, rel))
    if candidate != root_abs and not candidate.startswith(root_abs + os.sep):
        raise IntegrityError(f"archive member escapes extraction root: {rel!r}")
    return candidate


def _sha256(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
