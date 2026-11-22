"""Backup & restore worker.

Entry point::

    br-worker backup   --job <uuid> [--type full|database|rrd|configuration|system]
    br-worker verify   --job <uuid> --backup <ref> [--level standard]
    br-worker import   --job <uuid>   (queued browser archive upload)
    br-worker restore  --job <uuid> --restore <uuid> --backup <ref>
    br-worker retention --job <uuid>
    br-worker scheduler-tick          (systemd timer)
    br-worker health

Runs as the ``librenms`` user.  Escalation happens only through
:class:`br.helper_client.HelperClient`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sys
import time
import traceback
import typing as t

from . import checksums, collect, crypto, db, manifest as manifest_mod, retention, verify as verify_mod
from .common import (
    BACKUP_ID_RE,
    BRException,
    IntegrityError,
    Interrupted,
    JOB_ID_RE,
    Logger,
    PreflightError,
    ValidationError,
    du_bytes,
    free_bytes,
    human,
    utc_now,
)
from .config import Config
from .helper_client import HelperClient
from .state import Store
from .writer import ArchiveBuild

#: Progress percentage announced when a component *starts*.
COMPONENT_PROGRESS = {
    "database": 5,
    "rrd": 45,
    "application": 80,
    "system": 88,
    "integrations": 94,
}

TYPE_COMPONENTS = {
    "full": ("database", "rrd", "application", "system", "integrations"),
    "database": ("database",),
    "rrd": ("rrd",),
    "configuration": ("application", "system", "integrations"),
    "system": ("system", "integrations"),
}


class Worker:
    def __init__(self, cfg: Config, logger: Logger, helper: HelperClient | None = None):
        self.cfg = cfg
        self.log = logger
        self.store = Store(cfg, logger)
        self.helper = helper or HelperClient(logger=logger)
        self._abort = False

    # -- signals ------------------------------------------------------------

    def install_signal_handlers(self) -> None:
        def _handle(signum, _frame):
            self.log.warn("signal received - aborting at next checkpoint", signal=signum)
            self._abort = True

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _handle)

    # ------------------------------------------------------------------ #
    # BACKUP
    # ------------------------------------------------------------------ #

    def backup(self, job_id: str, backup_type: str = "full") -> int:
        if backup_type not in TYPE_COMPONENTS:
            raise ValidationError(f"unknown backup type {backup_type!r}")
        self._preflight(backup_type)

        job = self.store.job(job_id) if self.store.available() else None
        actor = (job or {}).get("actor") or "system"
        source = "scheduled" if (job or {}).get("actor") == "scheduler" else "manual"

        backup_id = _new_backup_id()
        archive_path = os.path.join(self.cfg.archive_dir, f"{backup_id}.brarchive")
        started = time.time()

        self.log.info("backup starting", job=job_id, backup_id=backup_id, type=backup_type)
        self._progress(job_id, 2, "PREPARING", f"backup {backup_id} starting")

        components_requested = {c: (c in TYPE_COMPONENTS[backup_type]) and self.cfg.components[c]
                                for c in self.cfg.components}
        collection: dict = {"components": {}}
        versions = self._versions()
        self._progress(job_id, 4, "PREPARING", "collecting versions")

        try:
            with ArchiveBuild(archive_path, self.cfg, self.log) as writer:
                if components_requested["database"]:
                    self._progress(job_id, COMPONENT_PROGRESS["database"], "DATABASE", "collecting database")
                    cred = db.discover_credentials(self.cfg)
                    staging_sql = os.path.join(self.cfg.stage_dir, f"{backup_id}-librenms.sql.gz")
                    info = db.dump(cred, self.cfg, staging_sql, self.log)
                    writer.add_file(staging_sql, "database/librenms.sql.gz")
                    collection["components"]["database"] = info
                    os.unlink(staging_sql)
                    versions["mariadb"] = db.version(cred)

                if components_requested["rrd"]:
                    self._progress(job_id, COMPONENT_PROGRESS["rrd"], "RRD", "collecting rrd")
                    collection["components"]["rrd"] = collect.collect_rrd(
                        self.cfg, writer, self.log)

                if components_requested["application"]:
                    self._progress(job_id, COMPONENT_PROGRESS["application"], "APPLICATION", "collecting application")
                    collection["components"]["application"] = collect.collect_application(
                        self.cfg, writer, self.log)

                if components_requested["system"]:
                    self._progress(job_id, COMPONENT_PROGRESS["system"], "SYSTEM", "collecting system")
                    collection["components"]["system"] = collect.collect_system(
                        self.cfg, writer, self.log)

                if components_requested["integrations"]:
                    self._progress(job_id, COMPONENT_PROGRESS["integrations"], "INTEGRATIONS", "collecting integrations")
                    collection["components"]["integrations"] = collect.collect_integrations(
                        self.cfg, writer, self.log)

                if self._abort:
                    raise Interrupted("backup aborted by signal")

                # -- final manifest + checksums -----------------------------
                file_hashes = dict(writer.hashes)
                collection["file_hashes"] = file_hashes
                checksum_body = _checksum_text(file_hashes)
                writer.add_bytes(checksums.CHECKSUM_NAME, checksum_body)

                final_manifest = manifest_mod.build(
                    backup_id=backup_id,
                    backup_type=backup_type,
                    cfg=self.cfg,
                    components=components_requested,
                    versions=versions,
                    collection=collection,
                    checksum_sha256=None,
                    archive_path=archive_path,
                    encryption_info=crypto.describe(self.cfg),
                    key_fp=crypto.key_fingerprint(self.cfg),
                    logger=self.log,
                )
                # The manifest is written LAST so it is only present in an
                # archive that reached completion.
                writer.add_json("manifest.json", final_manifest)
                writer.add_json("backup-metadata.json", {
                    "backup_id": backup_id,
                    "worker_version": __import__("br.common", fromlist=["VERSION"]).VERSION,
                    "created_at": utc_now(),
                    "type": backup_type,
                    "encryption": crypto.describe(self.cfg),
                })

            self._progress(job_id, 97, "SEALING", "computing archive checksum")
            archive_sha = _sha256(archive_path)
            _write_sidecar(archive_path, archive_sha)
            self._write_manifest_sidecar(archive_path, final_manifest)

            size = os.path.getsize(archive_path)
            self.log.info("archive sealed", archive=os.path.basename(archive_path),
                          size=human(size), sha256=archive_sha[:16])

            # -- verification ------------------------------------------------
            verified = "unverified"
            if str(self.cfg.get("archive", "verify_after_backup", "always")).lower() == "always":
                self._progress(job_id, 98, "VERIFYING", "verifying archive")
                result = verify_mod.verify(archive_path, self.cfg, self.log, level="standard")
                verified = "verified" if result["ok"] else "failed"
                if not result["ok"]:
                    raise BRException(
                        f"post-backup verification failed: mismatched={len(result['mismatched'])} "
                        f"missing={len(result['missing'])}"
                    )

            # -- record ----------------------------------------------------
            self._record_backup(
                backup_id=backup_id, backup_type=backup_type, path=archive_path,
                size=size, verified=verified, manifest=final_manifest, sha=archive_sha,
                components=components_requested, versions=versions, actor=actor,
                source=source,
                uncompressed=sum(writer.sizes.values()),
                retention_class=(
                    "checkpoint"
                    if os.path.abspath(archive_path).startswith(
                        os.path.abspath(self.cfg.checkpoint_dir))
                    else None
                ),
            )

            # Remember the key that produced this archive. The restore
            # preflight compares a backup's fingerprint against this value, and
            # without it every first backup makes that check report "key
            # mismatch" - a red light on the one screen where a false alarm is
            # least affordable.
            try:
                self.store.setting_set("key_fingerprint", crypto.key_fingerprint(self.cfg))
            except Exception as exc:  # noqa: BLE001 - never fail a good backup
                self.log.warn("could not record key fingerprint", error=str(exc))

            elapsed = int(time.time() - started)
            self.log.info("backup completed", backup_id=backup_id, seconds=elapsed,
                          size=human(size), verification=verified)
            self._finish(job_id, "completed", backup_id=backup_id)

            if self.store.available():
                self.store.notify(
                    f"Backup completed: {backup_id}",
                    f"Full backup finished in {elapsed}s, {human(size)}, "
                    f"{len(components_requested)} components, verification={verified}.",
                    1,
                )
                self._run_retention_silent(job_id)
            return 0

        except Interrupted as exc:
            self.log.warn("backup interrupted", reason=str(exc))
            _quarantine(archive_path, self.log)
            self._record_failure(backup_id, backup_type, exc, actor, source, path=None)
            self._finish(job_id, "failed", error="interrupted")
            return 130
        except Exception as exc:
            self.log.error("backup failed", error=str(exc))
            _quarantine(archive_path, self.log)
            self._record_failure(backup_id, backup_type, exc, actor, source, path=None)
            self._finish(job_id, "failed", error=str(exc))
            self._notify_failure(backup_id, exc)
            return 1

    # ------------------------------------------------------------------ #
    # VERIFY
    # ------------------------------------------------------------------ #

    def verify_backup(self, job_id: str, backup_ref: str, level: str = "standard") -> int:
        """Verify a backup by reference, or by path when the ref is a filename.

        The path form matters for disaster recovery: docs/RESTORE.md tells the
        operator to verify a .brarchive before restoring it, and on a rebuilt
        host that archive is not registered in the database yet. Requiring a DB
        row here made the documented recovery path impossible.
        """
        row = self.store.backup(backup_ref) if self.store.available() else None

        if not row and (backup_ref.endswith(".brarchive") or os.path.sep in backup_ref):
            candidate = backup_ref if os.path.isabs(backup_ref) else os.path.join(
                self.cfg.archive_dir, os.path.basename(backup_ref)
            )
            if not os.path.exists(candidate):
                raise ValidationError(f"archive not found: {candidate}")
            # Unregistered archive: verify it, report the result, but do not
            # invent a database row for it.
            self.log.info("verifying unregistered archive", path=candidate, level=level)
            result = verify_mod.verify(candidate, self.cfg, self.log, level=level)
            self._progress(
                job_id, 100, "DONE",
                f"unregistered archive {'verified' if result['ok'] else 'FAILED'}: "
                f"{result['members']} members",
            )
            self._finish(job_id, "completed" if result["ok"] else "failed",
                         error=None if result["ok"] else "checksum mismatch")
            return 0 if result["ok"] else 3

        if not row:
            raise ValidationError(f"unknown backup {backup_ref}")
        path = row.get("path")
        if not path or not os.path.exists(path):
            self.store.backup_update(backup_ref, verified_status="failed",
                                      error_message="archive missing on disk")
            self._finish(job_id, "failed", error="archive missing")
            return 1
        self._progress(job_id, 20, "VERIFYING", f"verifying {backup_ref} ({level})")
        try:
            result = verify_mod.verify(path, self.cfg, self.log, level=level)
        except Exception as exc:
            self.log.error("verification error", error=str(exc))
            self.store.backup_update(backup_ref, verified_status="failed",
                                      error_message=str(exc))
            self._finish(job_id, "failed", error=str(exc))
            return 1

        ok = result["ok"]
        self.store.backup_update(
            backup_ref,
            verified_status="verified" if ok else "failed",
            verified_at=utc_now() if ok else None,
            error_message=None if ok else (
                f"mismatched={result['mismatched'][:5]} missing={result['missing'][:5]}"
            ),
        )
        self._progress(job_id, 100, "DONE", f"verification {'passed' if ok else 'FAILED'}")
        self._finish(job_id, "completed" if ok else "failed",
                     error=None if ok else "checksum mismatch")
        return 0 if ok else 3

    def import_uploaded_archive(self, job_id: str, upload_token: str) -> int:
        """Validate, register, and verify an archive uploaded through the GUI."""
        import hashlib
        import tempfile
        from .common import JOB_ID_RE

        if not JOB_ID_RE.fullmatch(upload_token):
            self._finish(job_id, "failed", error="invalid upload token")
            return 2
        incoming_dir = os.path.join(self.cfg.librenms_root, "storage", "app", "br-uploads")
        incoming = os.path.join(incoming_dir, f"{upload_token}.upload")
        temp_path = None
        try:
            self._progress(job_id, 5, "IMPORTING", "validating uploaded archive")
            real_dir = os.path.realpath(incoming_dir)
            real_input = os.path.realpath(incoming)
            if (real_input != os.path.join(real_dir, f"{upload_token}.upload")
                    or not os.path.isfile(real_input)):
                raise ValidationError("uploaded archive is missing or invalid")
            manifest = manifest_mod.validate(_manifest_from_archive(real_input, self.cfg, self.log))
            backup_ref = manifest["backup_id"]
            destination = os.path.join(self.cfg.archive_dir, f"{backup_ref}.brarchive")
            existing = self.store.backup(backup_ref)
            if existing and existing.get("status") != "deleted":
                raise ValidationError(f"backup {backup_ref} already exists on this server")
            previous_path = str((existing or {}).get("path") or "")
            if os.path.exists(destination) or (previous_path and os.path.exists(previous_path)):
                raise ValidationError(f"backup {backup_ref} archive file still exists on this server")
            upload_size = os.path.getsize(real_input)
            reserve = self.cfg.get_int("general", "headroom_mib", 512) * 1024 * 1024
            if shutil.disk_usage(self.cfg.archive_dir).free < upload_size + reserve:
                raise ValidationError("not enough archive-store space to import this upload safely")

            digest = hashlib.sha256()
            fd, temp_path = tempfile.mkstemp(prefix=".upload-", suffix=".tmp",
                                             dir=self.cfg.archive_dir)
            with os.fdopen(fd, "wb") as out, open(real_input, "rb") as src:
                for block in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(block)
                    out.write(block)
                out.flush()
                os.fsync(out.fileno())
            os.chmod(temp_path, 0o640)
            os.replace(temp_path, destination)
            temp_path = None
            _write_sidecar(destination, digest.hexdigest())
            self._write_manifest_sidecar(destination, manifest)
            if existing:
                revived = self.store.backup_revive(
                    backup_ref,
                    path=destination,
                    size_bytes=upload_size,
                    sha256=digest.hexdigest(),
                )
                if not revived:
                    os.unlink(destination)
                    try:
                        os.unlink(destination + ".sha256")
                    except FileNotFoundError:
                        pass
                    raise BRException("deleted backup record could not be restored")
                self.log.info("restored deleted backup record", backup=backup_ref)
            else:
                self.adopt_orphans()
            if not self.store.backup(backup_ref):
                raise BRException("uploaded archive could not be registered")
            self.log.info("uploaded archive registered", backup=backup_ref)
            return self.verify_backup(job_id, backup_ref, "standard")
        except Exception as exc:
            self.log.error("uploaded archive import failed", error=str(exc))
            self._finish(job_id, "failed", error=str(exc))
            return 1
        finally:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
            try:
                os.unlink(incoming)
            except FileNotFoundError:
                pass

    # ------------------------------------------------------------------ #
    # RESTORE
    # ------------------------------------------------------------------ #

    def restore(self, job_id: str, restore_id: str, backup_ref: str,
                checkpoint_id: str | None = None) -> int:
        """Controlled state machine - see docs/RESTORE.md.

        With ``checkpoint_id`` set the same machine runs in rollback mode: it
        restores the pre-restore checkpoint instead of a user backup.
        """
        from .restore import RestoreEngine

        engine = RestoreEngine(self, restore_id=restore_id, backup_ref=backup_ref,
                               job_id=job_id, checkpoint_id=checkpoint_id)
        return engine.run()

    # ------------------------------------------------------------------ #
    # RETENTION
    # ------------------------------------------------------------------ #

    def retention(self, job_id: str) -> int:
        # Explicit single-archive deletion requested from the GUI.
        forced = self.store.setting_get(f"force_delete_{job_id}")
        if forced:
            return self._force_delete(job_id, str(forced))

        rows = self.store.backups()
        backups = [
            {
                "backup_id": r["backup_ref"],
                "created_at": (r["created_at"].strftime("%Y-%m-%dT%H:%M:%SZ")
                               if hasattr(r["created_at"], "strftime")
                               else str(r["created_at"])),
                "type": r["type"],
                "path": r["path"],
            }
            for r in rows if r["status"] == "completed"
        ]
        protected = {c["checkpoint_id"] for c in self.store.checkpoints()
                     if c.get("status") == "complete"}
        # Checkpoints reference backups through br_backups.retention_class notes;
        # anything used by a restore row is protected explicitly.
        protected |= {r["backup_ref"] for r in self.store.query(
            "SELECT backup_ref FROM br_restores WHERE status IN ('running','completed')")}
        protected |= {str(self.store.setting_get("last_backup_ref", ""))}

        plan = retention.plan(
            backups,
            keep_daily=self.cfg.get_int("retention", "keep_daily", 7),
            keep_weekly=self.cfg.get_int("retention", "keep_weekly", 4),
            keep_monthly=self.cfg.get_int("retention", "keep_monthly", 6),
            keep_total=self.cfg.get_int("retention", "keep_total", 0),
            protected=protected,
        )
        self.log.info("retention plan", keep=len(plan["keep"]), delete=len(plan["delete"]))
        self._progress(job_id, 30, "RETENTION", f"deleting {len(plan['delete'])} archive(s)")

        deleted = []
        for backup_id in plan["delete"]:
            row = self.store.backup(backup_id)
            if not row:
                continue
            path = row.get("path")
            if path and os.path.exists(path):
                try:
                    self.helper.unlink(self.cfg.archive_dir + "/" + os.path.basename(path))
                except BRException:
                    if os.path.exists(path):
                        os.unlink(path)
                for suffix in (".sha256", ".manifest.json"):
                    side = path + suffix
                    if os.path.exists(side):
                        os.unlink(side)
            self.store.backup_update(backup_id, status="deleted", deleted_at=utc_now())
            deleted.append(backup_id)
            self.log.info("retention: archive deleted", backup_id=backup_id,
                          reason=plan["reasons"].get(backup_id))
        self._finish(job_id, "completed")
        return 0

    def _force_delete(self, job_id: str, backup_ref: str) -> int:
        """Delete exactly one archive, after re-checking protection rules."""
        row = self.store.backup(backup_ref)
        if not row:
            self._finish(job_id, "failed", error="backup not found")
            return 1
        if self._is_protected(backup_ref, row):
            self.log.warn("delete refused: archive is protected", backup=backup_ref)
            self._finish(job_id, "failed", error="archive is protected by a restore record")
            return 1
        path = row.get("path")
        self._progress(job_id, 50, "RETENTION", f"deleting {backup_ref}")
        if path and os.path.exists(path):
            try:
                os.unlink(path)
            except OSError as exc:
                self._finish(job_id, "failed", error=f"could not unlink archive: {exc}")
                return 1
            for suffix in (".sha256", ".manifest.json"):
                side = path + suffix
                if os.path.exists(side):
                    try:
                        os.unlink(side)
                    except OSError:
                        pass
        self.store.backup_update(backup_ref, status="deleted", deleted_at=utc_now())
        self.store.execute("DELETE FROM br_settings WHERE `key`=%s",
                           (f"force_delete_{job_id}",))
        self.log.info("archive deleted on request", backup=backup_ref)
        self._finish(job_id, "completed")
        return 0

    def _is_protected(self, backup_ref: str, row: dict) -> bool:
        if self.store.query_one(
                "SELECT restore_id FROM br_restores WHERE backup_ref=%s LIMIT 1", (backup_ref,)):
            return True
        if row.get("retention_class") == "checkpoint":
            return True
        path = str(row.get("path") or "")
        return path.startswith(str(self.cfg.checkpoint_dir))

    # ------------------------------------------------------------------ #
    # SCHEDULER TICK (systemd timer, not LibreNMS scheduler)
    # ------------------------------------------------------------------ #

    def scheduler_tick(self) -> int:
        """Called by librenms-backup-scheduler.timer.

        Decides whether a scheduled backup is due and, if so, queues a job and
        asks systemd to start the worker unit. Uses the *plugin's own* tables,
        never LibreNMS' scheduler tables.
        """
        import subprocess

        if not self.store.available():
            self.log.warn("scheduler tick: state store unavailable")
            return 1
        active = self.store.query_one(
            "SELECT job_id FROM br_jobs WHERE status IN ('queued','running') LIMIT 1")
        if active:
            self.log.info("scheduler tick: job already active, skipping", job=active["job_id"])
            return 0

        schedule = self.store.query_one(
            "SELECT * FROM br_schedules WHERE enabled=1 ORDER BY schedule_id LIMIT 1")
        if not schedule:
            self.log.info("scheduler tick: no enabled schedule")
            return 0

        next_run = schedule.get("next_run")
        now = utc_now()
        if next_run and next_run > now:
            self.log.info("scheduler tick: not due", next_run=next_run)
            return 0

        job_id = _new_job_id()
        self.store.execute(
            "INSERT INTO br_jobs (job_id, kind, backup_type, status, actor, created_at) "
            "VALUES (%s,'backup',%s,'queued','scheduler',UTC_TIMESTAMP())",
            (job_id, schedule.get("backup_type") or "full"),
        )
        # The queue is drained by librenms-backup-dispatch.timer; nothing to spawn here.
        self._advance_next_run(schedule)
        self.log.info("scheduler tick: backup queued", job=job_id)
        return 0

    def _advance_next_run(self, schedule: dict) -> None:
        from datetime import datetime, timedelta, timezone

        freq = schedule.get("frequency") or "daily"
        time_of_day = schedule.get("time_of_day") or "02:30"
        hour, _, minute = time_of_day.partition(":")
        now = datetime.now(timezone.utc)
        candidate = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        if freq == "weekly":
            weekday = int(schedule.get("weekday") or 0)
            while candidate.weekday() != weekday:
                candidate += timedelta(days=1)
        elif freq == "monthly":
            dom = int(schedule.get("day_of_month") or 1)
            while candidate.day != dom:
                candidate += timedelta(days=1)
        self.store.execute(
            "UPDATE br_schedules SET next_run=%s, updated_at=UTC_TIMESTAMP() WHERE schedule_id=%s",
            (candidate.strftime("%Y-%m-%dT%H:%M:%SZ"), schedule["schedule_id"]),
        )

    # ------------------------------------------------------------------ #
    # RUN-JOB (systemd instance entrypoint)
    # ------------------------------------------------------------------ #

    def adopt_orphans(self) -> int:
        """Rebuild database rows for archives that exist on disk but are unknown.

        Needed after disaster recovery, and after any event that replaces the
        database while the archive tree survives: the `.brarchive` holds its own
        manifest, so every field the UI needs is recoverable without a DB row.
        Without this, a perfectly valid archive is invisible to the restore
        screen exactly when it is most needed.

        Read-only with respect to the archive: nothing is rewritten or moved.
        """
        if not self.store.available():
            self.log.error("adopt: state store unavailable")
            return 1
        adopted = 0
        skipped = 0
        archive_dir = self.cfg.archive_dir
        for entry in sorted(os.listdir(archive_dir)):
            if not entry.endswith(".brarchive"):
                continue
            ref = entry[: -len(".brarchive")]
            if self.store.backup(ref):
                skipped += 1
                continue
            path = os.path.join(archive_dir, entry)
            try:
                manifest = _manifest_from_archive(path, self.cfg, self.log)
                manifest_mod.validate(manifest)
            except Exception as exc:  # noqa: BLE001 - report and continue
                self.log.warn("adopt: unreadable archive", path=entry, error=str(exc))
                continue
            sidecar = path + ".sha256"
            sha = None
            if os.path.exists(sidecar):
                with open(sidecar, "r", encoding="utf-8") as fh:
                    sha = (fh.read().split() or [None])[0]
            versions = manifest.get("versions") or {}
            comps = manifest.get("components") or {}
            self.store.backup_insert({
                "backup_ref": ref,
                "type": manifest.get("backup_type") or "full",
                "status": "completed",
                "verified_status": "unverified",
                "path": path,
                "destination": "local",
                "size_bytes": os.path.getsize(path),
                "uncompressed_bytes": 0,
                "sha256": sha,
                "encrypted": bool((manifest.get("encryption") or {}).get("encrypted")),
                "encryption_scheme": (manifest.get("encryption") or {}).get("scheme"),
                "key_fingerprint": manifest.get("key_fingerprint"),
                "components": json.dumps([c for c, on in comps.items() if on]),
                "librenms_version": versions.get("librenms"),
                "php_version": versions.get("php"),
                "mariadb_version": versions.get("mariadb"),
                "os_version": (versions.get("os") or {}).get("os_pretty_name"),
                "worker_version": manifest.get("worker_version"),
                "actor": "adopted",
                "source": "manual",
                "created_at": manifest.get("created_at") or utc_now(),
            })
            adopted += 1
            self.log.info("adopted orphan archive", backup=ref, path=entry)
        # Log, never print: this method is also called from inside the helper
        # process, where stdout is the line-delimited HMAC response channel.
        # A stray print() there corrupts the protocol and the caller sees a
        # malformed reply after an otherwise successful restore.
        self.log.info("adopt finished", adopted=adopted, already_registered=skipped)
        self._last_adopted = adopted
        return adopted

    def run_job(self, job_id: str) -> int:
        """Execute the job named by the systemd instance (%i).

        The unit is started by the dispatcher; this method re-reads the row so
        the parameters always come from the database, never from the unit file.
        """
        from .common import JOB_ID_RE

        if not JOB_ID_RE.match(job_id):
            self.log.error("run-job: invalid job id", job=job_id)
            return 2
        job = self.store.job(job_id)
        if not job:
            self.log.error("run-job: job not found", job=job_id)
            return 2
        kind = job["kind"]
        self.install_signal_handlers()
        self.log.info("run-job starting", job=job_id, kind=kind)

        if kind == "backup":
            return self.backup(job_id, job.get("backup_type") or "full")
        if kind == "verify":
            return self.verify_backup(job_id, job.get("backup_id") or "",
                                      str(self.store.setting_get(f"verify_level_{job_id}") or "standard"))
        if kind == "import":
            return self.import_uploaded_archive(job_id, job.get("backup_id") or "")
        if kind == "restore":
            restore_id = job.get("restore_id") or ""
            rollback_of = ""
            # Resolution order matters. `br_jobs.backup_id` is bookkeeping and
            # is overwritten by the checkpoint backup the restore itself runs,
            # so trusting it alone makes the restore try to promote its own
            # checkpoint. `br_restores.backup_ref` is written once, at request
            # time, and is authoritative.
            backup_ref = ""
            restore_row = self.store.restore(restore_id) if restore_id else None
            if restore_row:
                backup_ref = restore_row.get("backup_ref") or ""
            backup_ref = backup_ref or job.get("backup_id") or ""
            if restore_row and job.get("backup_id") and job["backup_id"] != backup_ref:
                self.log.warn(
                    "restore: job backup_id disagrees with the restore request",
                    job=job_id, job_backup=job["backup_id"], restore_backup=backup_ref,
                )
            if restore_row:
                # A rollback is requested as a restore whose options carry
                # {"rollback": "<checkpoint_id>"}. The schema has no
                # restore_type column, and `checkpoint_ref` is written BY the
                # restore, so it cannot also be the request.
                options = restore_row.get("options")
                if isinstance(options, str):
                    try:
                        import json as _json

                        options = _json.loads(options or "{}")
                    except ValueError:
                        options = {}
                rollback_of = str((options or {}).get("rollback") or "")
                if rollback_of:
                    self.log.info("restore: rollback requested", checkpoint=rollback_of)
            return self.restore(job_id, restore_id, backup_ref,
                                checkpoint_id=rollback_of or None)
        if kind == "retention":
            return self.retention(job_id)
        if kind == "checkpoint":
            return self.backup(job_id, "full")
        self.store.job_finish(job_id, status="failed", error=f"unknown job kind {kind}")
        return 2

    # ------------------------------------------------------------------ #
    # DISPATCH (queue consumer - runs unprivileged, no sudo)
    # ------------------------------------------------------------------ #

    def dispatch(self) -> int:
        """Claim queued jobs and detach one worker process per job.

        Runs from ``librenms-backup-dispatch.timer`` every 15 seconds as the
        ``librenms`` user. This is the *only* thing that starts workers, so the
        web tier never needs the ability to execute anything.
        """
        import subprocess

        if not self.store.available():
            return 1
        limit = max(1, self.cfg.get_int("general", "max_parallel_jobs", 1))
        running = self.store.query(
            "SELECT job_id, pid, heartbeat_at, started_at FROM br_jobs WHERE status='running'")
        alive = [r for r in running if _job_is_alive(r)]
        if len(alive) >= limit:
            self.log.info("dispatch: worker slot busy", running=len(alive))
            self._write_health()
            return 0

        queued = self.store.query(
            "SELECT * FROM br_jobs WHERE status='queued' ORDER BY created_at LIMIT %s",
            (limit - len(alive),),
        )
        spawned = 0
        for job in queued:
            # systemd instance unit: parameters come from the DB row, so the
            # unit file never encodes user input.
            unit = f"librenms-backup-worker@{job['job_id']}.service"
            # The dispatcher runs unprivileged; it cannot call systemctl itself.
            # Spawning goes through the root helper, whose op_worker_spawn only
            # accepts a UUID - the unit name is never built from user input.
            try:
                result = self.helper.call("worker.spawn", {"job_id": job["job_id"]},
                                          timeout=60, response_timeout=60)
            except Exception as exc:  # noqa: BLE001 - reported to the job row
                self.log.error("dispatch: helper worker.spawn failed",
                               job=job["job_id"], error=str(exc))
                self.store.job_finish(job["job_id"], status="failed",
                                      error=f"could not start worker unit: {exc}")
                continue
            if result.get("code") not in (0, None):
                err = str(result.get("output") or "")[-500:]
                self.log.error("dispatch: worker.spawn returned non-zero",
                               job=job["job_id"], error=err)
                self.store.job_finish(job["job_id"], status="failed",
                                      error=f"could not start worker unit: {err}")
                continue
            self.store.execute(
                "UPDATE br_jobs SET status='running', started_at=UTC_TIMESTAMP(), "
                "heartbeat_at=UTC_TIMESTAMP(), systemd_unit=%s "
                "WHERE job_id=%s AND status='queued'",
                (unit, job["job_id"]),
            )
            self.log.info("dispatch: worker unit started", job=job["job_id"],
                          kind=job["kind"], unit=unit)
            spawned += 1

        # Reap rows whose worker died without updating state.
        for row in running:
            if not _job_is_alive(row):
                self.store.job_finish(row["job_id"], status="failed",
                                      error="worker stopped without reporting a final status")
                self.log.warn("dispatch: reaped dead worker", job=row["job_id"])

        self._write_health()
        return 0

    def _write_health(self) -> None:
        """Publish a read-only health file the GUI can consume without root."""
        path = os.path.join(self.cfg.state_dir, "health.json")
        try:
            os.makedirs(self.cfg.state_dir, exist_ok=True)
            report = self.health()
            report["helper"] = "reachable" if report["checks"].get("helper", {}).get("ok") else "unreachable"
            report["timers"] = {
                "librenms-backup-scheduler.timer": "unknown",
                "librenms-backup-dispatch.timer": "unknown",
            }
            from .common import write_json
            write_json(path, report)      # write_json is atomic (tmp + rename)
            os.chmod(path, 0o644)
        except Exception as exc:  # pragma: no cover
            self.log.warn("could not write health file", error=str(exc))

    # ------------------------------------------------------------------ #
    # HEALTH
    # ------------------------------------------------------------------ #

    def health(self) -> dict:
        checks = {}
        try:
            cred = db.discover_credentials(self.cfg)
            checks["database"] = {"ok": db.server_reachable(cred),
                                  "version": db.version(cred) if db.server_reachable(cred) else None}
        except BRException as exc:
            checks["database"] = {"ok": False, "error": str(exc)}
        checks["archive_dir"] = {"ok": os.path.isdir(self.cfg.archive_dir),
                                 "path": self.cfg.archive_dir}
        checks["free_space"] = {
            "ok": free_bytes(self.cfg.backup_root) > self.cfg.get_int("general", "min_free_mib", 1024) * 1024 * 1024,
            "bytes": free_bytes(self.cfg.backup_root),
            "human": human(free_bytes(self.cfg.backup_root)),
        }
        checks["encryption"] = {"ok": crypto.key_fingerprint(self.cfg).endswith("missing") is False,
                               "fingerprint": crypto.key_fingerprint(self.cfg),
                               "scheme": self.cfg.encryption}
        try:
            self.helper.call("fs.stat", {"path": self.cfg.archive_dir})
            checks["helper"] = {"ok": True}
        except BRException as exc:
            checks["helper"] = {"ok": False, "error": str(exc)}
        checks["store"] = {"ok": self.store.available()}
        return {"ok": all(c.get("ok") for c in checks.values()), "checks": checks,
                "at": utc_now()}

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    def _preflight(self, backup_type: str) -> None:
        if not os.path.isdir(self.cfg.librenms_root):
            raise PreflightError(f"LibreNMS root not found: {self.cfg.librenms_root}")
        if not os.path.isdir(self.cfg.backup_root):
            raise PreflightError(f"backup root not found: {self.cfg.backup_root}")
        os.makedirs(self.cfg.stage_dir, exist_ok=True)
        os.makedirs(self.cfg.archive_dir, exist_ok=True)
        free = free_bytes(self.cfg.backup_root)
        minimum = self.cfg.get_int("general", "min_free_mib", 1024) * 1024 * 1024
        if free < minimum:
            raise PreflightError(
                f"insufficient free space on {self.cfg.backup_root}: "
                f"{human(free)} available, {human(minimum)} required"
            )
        cred = db.discover_credentials(self.cfg)
        if not db.server_reachable(cred):
            raise PreflightError(f"cannot connect to MariaDB at {cred.host}:{cred.port}")
        # Encryption material must be present *before* we start writing.
        if self.cfg.encryption == "age":
            for key in ("age_recipients_file", "age_identity_file"):
                path = self.cfg.get_path("archive", key)
                if not os.path.exists(path):
                    raise PreflightError(f"encryption key material missing: {path}")
        elif self.cfg.encryption == "aesgcm-pbkdf2":
            crypto.read_passphrase(self.cfg)
        self.log.info("preflight passed", type=backup_type, free=human(free))

    def _versions(self) -> dict:
        import platform

        info = collect.version_info(self.cfg, self.log)
        return {
            "librenms": info.get("librenms_version") or info.get("librenms_git_describe") or "unknown",
            "librenms_git": info.get("librenms_git_commit"),
            "php": _php_version(),
            "python": platform.python_version(),
            "worker": _worker_version(),
            "os": info.get("os", {}),
        }

    def _progress(self, job_id: str, percent: int, stage: str = "", message: str = "", **_) -> None:
        if not job_id or not self.store.available():
            return
        try:
            self.store.job_progress(job_id, progress=percent, stage=stage, message=message)
        except Exception as exc:  # pragma: no cover
            self.log.warn("could not update job progress", error=str(exc))

    def _finish(self, job_id: str, status: str, *, error: str | None = None,
                backup_id: str | None = None) -> None:
        if not job_id or not self.store.available():
            return
        try:
            self.store.job_finish(job_id, status=status, error=error, backup_id=backup_id)
        except Exception as exc:  # pragma: no cover
            self.log.warn("could not finish job", error=str(exc))

    def _record_backup(self, **kw) -> None:
        if not self.store.available():
            return
        self.store.backup_insert({
            "backup_ref": kw["backup_id"],
            "type": kw["backup_type"],
            "status": "completed",
            "verified_status": kw["verified"],
            "path": kw["path"],
            "destination": "local",
            "size_bytes": kw["size"],
            "uncompressed_bytes": int(kw.get("uncompressed") or 0),
            "sha256": kw["sha"],
            "encrypted": self.cfg.encryption != "none",
            "encryption_scheme": self.cfg.encryption,
            "key_fingerprint": crypto.key_fingerprint(self.cfg),
            "components": json.dumps(
                [c for c, on in kw["components"].items() if on]),
            "librenms_version": kw["versions"].get("librenms"),
            "php_version": kw["versions"].get("php"),
            "mariadb_version": kw["versions"].get("mariadb"),
            "os_version": (kw["versions"].get("os") or {}).get("os_pretty_name"),
            "worker_version": _worker_version(),
            "actor": kw["actor"],
            "source": kw["source"],
            "retention_class": kw.get("retention_class"),
            "created_at": utc_now(),
        })
        self.store.setting_set("last_backup_ref", kw["backup_id"])
        self.store.setting_set("last_backup_status", "completed")

    def _record_failure(self, backup_id, backup_type, exc, actor, source, path) -> None:
        if not self.store.available():
            return
        try:
            self.store.backup_insert({
                "backup_ref": backup_id,
                "type": backup_type,
                "status": "failed",
                "verified_status": "unverified",
                "path": path,
                "destination": "local",
                "size_bytes": 0,
                "uncompressed_bytes": 0,
                "sha256": None,
                "encrypted": self.cfg.encryption != "none",
                "encryption_scheme": self.cfg.encryption,
                "key_fingerprint": crypto.key_fingerprint(self.cfg),
                "components": "[]",
                "worker_version": _worker_version(),
                "actor": actor,
                "source": source,
                "error_message": str(exc)[:4000],
                "created_at": utc_now(),
            })
            self.store.setting_set("last_backup_status", "failed")
        except Exception:  # pragma: no cover
            pass

    def _notify_failure(self, backup_id: str, exc: Exception) -> None:
        if not self.store.available():
            return
        try:
            self.store.notify(f"Backup FAILED: {backup_id}",
                              f"The backup job failed: {str(exc)[:500]}", 2)
        except Exception:  # pragma: no cover
            pass

    def _run_retention_silent(self, job_id: str) -> None:
        try:
            self.retention(job_id)
        except Exception as exc:  # pragma: no cover - retention must not fail a backup
            self.log.warn("retention run failed", error=str(exc))

    def _write_manifest_sidecar(self, archive_path: str, manifest: dict) -> None:
        from .common import write_json

        try:
            write_json(archive_path + ".manifest.json", manifest)
        except OSError as exc:  # pragma: no cover
            self.log.warn("could not write manifest sidecar", error=str(exc))


# ---------------------------------------------------------------------------
# module helpers
# ---------------------------------------------------------------------------

def _checksum_text(hashes: dict[str, str]) -> bytes:
    return ("\n".join(f"{digest}  {rel}" for rel, digest in sorted(hashes.items())) + "\n").encode()


def _sha256(path: str) -> str:
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_sidecar(archive_path: str, digest: str) -> None:
    with open(archive_path + ".sha256", "w", encoding="utf-8") as fh:
        fh.write(f"{digest}  {os.path.basename(archive_path)}\n")


def _quarantine(path: str, log: Logger) -> None:
    if not os.path.exists(path):
        return
    bad = path + ".incomplete"
    try:
        os.replace(path, bad)
        log.warn("incomplete archive quarantined", path=bad)
    except OSError as exc:
        log.warn("could not quarantine incomplete archive", error=str(exc))


def _job_is_alive(row: dict) -> bool:
    """A job is alive if its worker unit is active or its heartbeat is fresh."""
    import subprocess
    from datetime import datetime, timedelta, timezone

    pid = row.get("pid")
    if pid and _pid_alive(pid):
        return True
    heartbeat = row.get("heartbeat_at")
    if heartbeat is not None:
        if isinstance(heartbeat, str):
            try:
                heartbeat = datetime.fromisoformat(heartbeat.replace("Z", "+00:00"))
            except ValueError:
                heartbeat = None
        if heartbeat is not None:
            if heartbeat.tzinfo is None:
                heartbeat = heartbeat.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - heartbeat < timedelta(minutes=10):
                return True
    unit = row.get("systemd_unit")
    if unit:
        proc = subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", unit],
                              check=False, timeout=30)
        if proc.returncode == 0:
            return True
    return False


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (ProcessLookupError, ValueError, PermissionError, TypeError):
        return False


def _new_backup_id() -> str:
    import secrets
    from datetime import datetime, timezone

    return f"br-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}"


def _new_job_id() -> str:
    import uuid

    return str(uuid.uuid4())


def _php_version() -> str:
    try:
        proc = __import__("subprocess").run(["/usr/bin/php", "-r", "echo PHP_VERSION;"],
                                            capture_output=True, check=False, timeout=20)
        return proc.stdout.decode().strip() or "unknown"
    except Exception:  # pragma: no cover
        return "unknown"


def _manifest_from_archive(archive_path: str, cfg, logger) -> dict:
    """Read the authoritative manifest.json out of an archive on disk."""
    from .archive import ArchiveReader
    from .writer import ArchiveStream

    found: dict | None = None
    with ArchiveStream(archive_path, cfg, logger) as stream:
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


def _worker_version() -> str:
    from .common import VERSION

    return VERSION


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="br-worker", description="LibreNMS Backup & Restore worker")
    parser.add_argument("--conf", default=os.environ.get("BR_CONF", "/etc/librenms-backup/br.conf"))
    parser.add_argument("--log-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_backup = sub.add_parser("backup")
    p_backup.add_argument("--job", required=True)
    p_backup.add_argument("--type", default="full", choices=sorted(TYPE_COMPONENTS))

    p_verify = sub.add_parser("verify")
    p_verify.add_argument("--job", required=True)
    p_verify.add_argument("--backup", required=True)
    p_verify.add_argument("--level", default="standard", choices=list(verify_mod.LEVELS))

    p_restore = sub.add_parser("restore")
    p_restore.add_argument("--job", required=True)
    p_restore.add_argument("--restore", required=True)
    p_restore.add_argument("--backup", required=True)
    p_restore.add_argument("--checkpoint", default=None,
                           help="roll back to this recovery checkpoint instead of a backup")

    p_retention = sub.add_parser("retention")
    p_retention.add_argument("--job", required=True)

    p_run = sub.add_parser("run-job", help="resolve a queued job by id and execute it")
    p_run.add_argument("--job", required=True)

    sub.add_parser("scheduler-tick")
    sub.add_parser("dispatch")
    sub.add_parser("health")
    sub.add_parser("adopt", help="register archives that exist on disk but not in the DB")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config(args.conf)
    log_dir = args.log_dir or cfg.log_dir
    log_name = {
        "backup": "backup.log", "verify": "verify.log", "restore": "restore.log",
        "retention": "retention.log", "scheduler-tick": "scheduler.log",
        "dispatch": "dispatch.log", "health": "health.log", "run-job": "job.log",
        "adopt": "adopt.log",
    }[args.command]
    logger = Logger(os.path.join(log_dir, log_name), echo=(args.command == "health"))
    worker = Worker(cfg, logger)

    try:
        if args.command == "backup":
            worker.install_signal_handlers()
            return worker.backup(args.job, args.type)
        if args.command == "verify":
            return worker.verify_backup(args.job, args.backup, args.level)
        if args.command == "restore":
            worker.install_signal_handlers()
            return worker.restore(args.job, args.restore, args.backup,
                                  checkpoint_id=args.checkpoint)
        if args.command == "retention":
            return worker.retention(args.job)
        if args.command == "scheduler-tick":
            rc = worker.scheduler_tick()
            worker._write_health()
            return rc
        if args.command == "dispatch":
            return worker.dispatch()
        if args.command == "run-job":
            return worker.run_job(args.job)
        if args.command == "adopt":
            # The method returns the adoption COUNT for callers that reconcile
            # after a restore; as a CLI verb, the exit status must be success.
            adopted = worker.adopt_orphans()
            print(f"adopted {adopted} archive(s)")
            return 0
        if args.command == "health":
            print(json.dumps(worker.health(), indent=2))
            return 0
    except BRException as exc:
        logger.error("worker failed", command=args.command, error=str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover
        logger.error("unhandled exception", command=args.command,
                     error=repr(exc), trace=traceback.format_exc()[-4000:])
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
