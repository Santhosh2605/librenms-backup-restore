"""Database state store shared by worker and helper.

Same MariaDB database as LibreNMS, but *only* the plugin's own ``br_*``
tables are touched. Connection details are resolved like the backup engine
does (.env / config.php), so operators never duplicate credentials.
"""

from __future__ import annotations

import json
import re
import typing as t

from .common import BRException, Logger, ValidationError


ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")


def mysqldt(value):
    """Convert an ISO-8601 UTC string to a MariaDB DATETIME literal.

    MariaDB rejects 'Z' suffixed timestamps for DATETIME columns, so every
    timestamp we persist goes through here. Non-strings pass through untouched.
    """
    if isinstance(value, str):
        match = ISO_RE.match(value)
        if match:
            return "{}-{}-{} {}:{}:{}".format(*match.groups())
    return value


class Store:
    def __init__(self, cfg, logger: Logger | None = None):
        self.cfg = cfg
        self.logger = logger or Logger()
        self._conn = None

    # -- connection ---------------------------------------------------------

    def _connect(self):
        if self._conn is not None:
            return self._conn
        try:
            import pymysql  # type: ignore
        except ImportError as exc:  # pragma: no cover
            raise BRException(
                "python3-pymysql is required for worker state tracking "
                "(apt-get install python3-pymysql)"
            ) from exc
        from .db import discover_credentials

        cred = discover_credentials(self.cfg)
        self._conn = pymysql.connect(
            host=cred.host,
            port=cred.port,
            user=cred.user,
            password=cred.password,
            database=cred.name,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=10,
        )
        return self._conn

    def available(self) -> bool:
        try:
            self._connect().ping(reconnect=True)
            return True
        except Exception as exc:
            self.logger.warn("state store unavailable", error=str(exc))
            return False

    def execute(self, sql: str, params: t.Sequence[t.Any] = ()) -> int:
        with self._connect().cursor() as cur:
            cur.execute(sql, params)
            return cur.rowcount

    def query(self, sql: str, params: t.Sequence[t.Any] = ()) -> list[dict]:
        with self._connect().cursor() as cur:
            cur.execute(sql, params)
            columns = [c[0] for c in cur.description] if cur.description else []
            return [dict(zip(columns, row)) for row in cur.fetchall()]

    def query_one(self, sql: str, params: t.Sequence[t.Any] = ()) -> dict | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- jobs ---------------------------------------------------------------

    def job_start(self, job_id: str) -> None:
        self.execute(
            "UPDATE br_jobs SET status='running', started_at=UTC_TIMESTAMP(), "
            "heartbeat_at=UTC_TIMESTAMP() WHERE job_id=%s",
            (job_id,),
        )

    def job_progress(self, job_id: str, *, progress: int, stage: str, message: str = "") -> None:
        self.execute(
            "UPDATE br_jobs SET progress=%s, stage=%s, message=%s, "
            "status=IF(status='queued','running',status), heartbeat_at=UTC_TIMESTAMP() "
            "WHERE job_id=%s",
            (max(0, min(100, int(progress))), stage, message[:2000], job_id),
        )

    def job_finish(self, job_id: str, *, status: str, error: str | None = None,
                   backup_id: str | None = None, restore_id: str | None = None) -> None:
        self.execute(
            "UPDATE br_jobs SET status=%s, progress=IF(%s='completed',100,progress), "
            "finished_at=UTC_TIMESTAMP(), heartbeat_at=UTC_TIMESTAMP(), "
            "error_message=%s, backup_id=COALESCE(%s, backup_id), "
            "restore_id=COALESCE(%s, restore_id) WHERE job_id=%s",
            (status, status, (error or None) and error[:4000], backup_id, restore_id, job_id),
        )

    def job(self, job_id: str) -> dict | None:
        return self.query_one("SELECT * FROM br_jobs WHERE job_id=%s", (job_id,))

    # -- backups ------------------------------------------------------------

    def backup_insert(self, data: dict) -> None:
        fields = [
            "backup_ref", "type", "status", "verified_status", "path", "destination",
            "size_bytes", "uncompressed_bytes", "sha256", "encrypted",
            "encryption_scheme", "key_fingerprint", "components", "librenms_version",
            "php_version", "mariadb_version", "os_version", "worker_version", "actor",
            "source", "error_message", "retention_class",
        ]
        values = [mysqldt(data.get(f)) for f in fields]
        placeholders = ",".join(["%s"] * (len(fields) + 1))
        self.execute(
            f"INSERT INTO br_backups ({','.join(fields)}, created_at) "
            f"VALUES ({placeholders})",
            (*values, mysqldt(data.get("created_at"))),
        )

    def backup_update(self, backup_ref: str, **fields) -> None:
        allowed = {
            "status", "verified_status", "path", "size_bytes", "uncompressed_bytes",
            "sha256", "components", "error_message", "verified_at", "deleted_at",
            "encryption_scheme", "key_fingerprint", "retention_class",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{k}=%s" for k in updates)
        self.execute(
            f"UPDATE br_backups SET {assignments} WHERE backup_ref=%s",
            (*[mysqldt(v) for v in updates.values()], backup_ref),
        )

    def backups(self) -> list[dict]:
        return self.query(
            "SELECT * FROM br_backups WHERE status IN ('completed','failed') "
            "ORDER BY created_at DESC"
        )

    def backup(self, backup_ref: str) -> dict | None:
        return self.query_one("SELECT * FROM br_backups WHERE backup_ref=%s", (backup_ref,))

    def backup_revive(self, backup_ref: str, *, path: str, size_bytes: int,
                      sha256: str) -> bool:
        """Restore a soft-deleted backup row after its archive is re-imported.

        The backup reference is unique and the deleted row is retained for
        history, so re-import must revive that row instead of inserting a
        duplicate. The caller must first confirm the old archive is absent.
        """
        return self.execute(
            "UPDATE br_backups SET status='completed', verified_status='unverified', "
            "path=%s, destination='local', size_bytes=%s, sha256=%s, "
            "verified_at=NULL, deleted_at=NULL, error_message=NULL "
            "WHERE backup_ref=%s AND status='deleted'",
            (path, int(size_bytes), sha256, backup_ref),
        ) == 1

    # -- restores -----------------------------------------------------------

    def restore_update_state(self, restore_id: str, state: str, *,
                             status: str | None = None,
                             error: str | None = None) -> None:
        self.execute(
            "UPDATE br_restores SET state=%s, status=COALESCE(%s,status), "
            "error_message=COALESCE(%s,error_message) WHERE restore_id=%s",
            (state, status, error, restore_id),
        )

    def restore_finish(self, restore_id: str, *, state: str, status: str,
                       error: str | None = None, log_tail: str | None = None) -> None:
        self.execute(
            "UPDATE br_restores SET state=%s, status=%s, error_message=%s, "
            "log_tail=%s, finished_at=UTC_TIMESTAMP() WHERE restore_id=%s",
            (state, status, error, (log_tail or "")[-16000:], restore_id),
        )

    def restore(self, restore_id: str) -> dict | None:
        return self.query_one("SELECT * FROM br_restores WHERE restore_id=%s", (restore_id,))

    def active_restore(self) -> dict | None:
        return self.query_one(
            "SELECT * FROM br_restores WHERE status='running' ORDER BY created_at DESC LIMIT 1"
        )

    # -- checkpoints --------------------------------------------------------

    def checkpoint_insert(self, data: dict) -> None:
        self.execute(
            "INSERT INTO br_checkpoints (checkpoint_id, label, status, path, size_bytes, "
            "sha256, created_by, restore_id, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP())",
            (
                data["checkpoint_id"], data.get("label"), data.get("status", "complete"),
                data.get("path"), data.get("size_bytes", 0), data.get("sha256"),
                data.get("created_by", "restore"), data.get("restore_id"),
            ),
        )

    def checkpoints(self) -> list[dict]:
        return self.query("SELECT * FROM br_checkpoints ORDER BY created_at DESC")

    # -- settings -----------------------------------------------------------

    def setting_get(self, key: str, default: t.Any = None) -> t.Any:
        row = self.query_one("SELECT value FROM br_settings WHERE `key`=%s", (key,))
        if not row or row["value"] is None:
            return default
        try:
            return json.loads(row["value"])
        except (TypeError, ValueError):
            return row["value"]

    def setting_set(self, key: str, value: t.Any) -> None:
        self.execute(
            "INSERT INTO br_settings (`key`, value, updated_at) "
            "VALUES (%s,%s,UTC_TIMESTAMP()) ON DUPLICATE KEY UPDATE "
            "value=VALUES(value), updated_at=UTC_TIMESTAMP()",
            (key, json.dumps(value)),
        )

    # -- notifications ------------------------------------------------------

    def notify(self, title: str, body: str, severity: int = 1) -> None:
        """Create a native LibreNMS notification.

        Schema verified against LibreNMS master (2026-11):
            notifications(notifications_id, title, body, severity, source,
                          checksum UNIQUE, datetime)

        ``checksum`` is a UNIQUE key, so we derive a deterministic value from
        (title, body, severity). Repeating the same event therefore updates
        rather than raising a duplicate-key error. A failure here must never
        break a backup or restore, so everything is best-effort.
        """
        try:
            import hashlib

            checksum = hashlib.sha256(f"{title}|{body}|{severity}".encode()).hexdigest()
            self.execute(
                "INSERT INTO notifications "
                "(title, body, severity, source, checksum, datetime) "
                "VALUES (%s,%s,%s,'librenms-backup',%s,UTC_TIMESTAMP()) "
                "ON DUPLICATE KEY UPDATE body=VALUES(body), severity=VALUES(severity), "
                "datetime=UTC_TIMESTAMP()",
                (title[:255], body[:2000], int(severity), checksum),
            )
        except Exception as exc:  # pragma: no cover - never fail a job for this
            self.logger.warn("could not create LibreNMS notification", error=str(exc))
