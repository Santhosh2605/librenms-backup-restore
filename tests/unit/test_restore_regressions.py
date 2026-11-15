"""Regression tests for defects found during the end-to-end restore rehearsal.

Every test here corresponds to a real failure observed on the rehearsal host,
so a regression means a restore that silently corrupts a live database, dies
mid-state-machine, or reports success while doing nothing. None of these are
hypothetical: each one blocked a restore in practice.
"""

from __future__ import annotations

import json
import os
import sys
import types

import pytest

from br import db, restore as restore_mod
from br.common import BRException, ValidationError


# ---------------------------------------------------------------------- #
# db.sanitised_lines - retarget stripping
# ---------------------------------------------------------------------- #
def _write_dump(tmp_path, text):
    import gzip

    path = tmp_path / "dump.sql.gz"
    with gzip.open(path, "wb") as fh:
        fh.write(text.encode() if isinstance(text, str) else text)
    return str(path)


def test_sanitised_lines_strips_database_retargeting(tmp_path):
    """A --databases dump must never be able to touch the live database.

    Piping such a dump into `mariadb <scratch>` used to import into the LIVE
    database and its leading DROP DATABASE deleted it.
    """
    dump = (
        "-- MariaDB dump\n"
        "/*!40101 SET NAMES utf8mb4 */;\n"
        "CREATE DATABASE /*!32312 IF NOT EXISTS*/ `librenms`;\n"
        "USE `librenms`;\n"
        "DROP TABLE IF EXISTS `devices`;\n"
        "CREATE TABLE `devices` (`device_id` int);\n"
        "DROP DATABASE IF EXISTS `librenms`;\n"
    )
    path = _write_dump(tmp_path, dump)
    kept = b"".join(db.sanitised_dump_lines(path)).decode()
    assert "CREATE DATABASE" not in kept
    assert "DROP DATABASE" not in kept
    assert not any(line.strip().upper().startswith("USE ") for line in kept.splitlines())
    # Real content survives untouched.
    assert "CREATE TABLE `devices`" in kept
    assert "DROP TABLE IF EXISTS `devices`" in kept
    assert "SET NAMES utf8mb4" in kept


def test_sanitised_lines_is_case_and_space_insensitive(tmp_path):
    dump = (
        "  use `librenms`;\n"
        "USE `librenms` ;\n"
        "drop database if exists `librenms`;\n"
        "create database if not exists `librenms`;\n"
        "INSERT INTO t VALUES (1);\n"
    )
    path = _write_dump(tmp_path, dump)
    kept = b"".join(db.sanitised_dump_lines(path)).decode()
    assert kept.strip() == "INSERT INTO t VALUES (1);"


# ---------------------------------------------------------------------- #
# Plugin-owned tables
# ---------------------------------------------------------------------- #
def test_plugin_tables_cover_every_runtime_table():
    """The restore snapshots exactly these tables; a missing one is data loss."""
    for table in ("br_jobs", "br_backups", "br_restores", "br_checkpoints", "br_settings"):
        assert table in db.PLUGIN_TABLES


def test_restore_snapshot_skips_columns_that_vanished(monkeypatch):
    """A plugin table may gain/lose columns between backup and restore."""
    executed: list[str] = []
    monkeypatch.setattr(db, "_table_exists", lambda cred, table: True)
    monkeypatch.setattr(db, "_columns", lambda cred, table: ["job_id", "status"])
    monkeypatch.setattr(db, "_execute", lambda cred, statement: executed.append(statement))

    written = db.restore_snapshot(object(), {
        "br_jobs": {
            "exists": True,
            "columns": ["job_id", "status", "column_that_was_removed"],
            "rows": [["abc", "done", "gone"]],
        }
    })
    assert written == {"br_jobs": 1}
    assert len(executed) == 1
    assert "`column_that_was_removed`" not in executed[0]
    assert "`job_id`, `status`" in executed[0]
    assert "REPLACE INTO `br_jobs`" in executed[0]


def test_restore_snapshot_handles_null_and_quotes(monkeypatch):
    executed: list[str] = []
    monkeypatch.setattr(db, "_table_exists", lambda cred, table: True)
    monkeypatch.setattr(db, "_columns", lambda cred, table: ["a", "b"])
    monkeypatch.setattr(db, "_execute", lambda cred, statement: executed.append(statement))

    db.restore_snapshot(object(), {
        "t": {"exists": True, "columns": ["a", "b"],
              "rows": [["NULL", "it's here"], ["x", "back\\slash"]]},
    })
    statement = executed[0]
    assert "(NULL, 'it''s here')" in statement
    assert "'back\\\\slash'" in statement


def test_restore_snapshot_ignores_tables_absent_from_the_dump(monkeypatch):
    monkeypatch.setattr(db, "_table_exists", lambda cred, table: False)
    monkeypatch.setattr(db, "_execute", lambda cred, statement: pytest.fail("must not write"))
    written = db.restore_snapshot(object(), {
        "br_jobs": {"exists": True, "columns": ["a"], "rows": [["1"]]},
    })
    assert written == {"br_jobs": 0}


def test_restore_snapshot_rejects_unsafe_identifiers(monkeypatch):
    monkeypatch.setattr(db, "_execute", lambda cred, statement: pytest.fail("must not run"))
    written = db.restore_snapshot(object(), {
        "br_jobs; DROP TABLE devices": {"exists": True, "columns": ["a"], "rows": [["1"]]},
    })
    assert written == {}


# ---------------------------------------------------------------------- #
# Restore engine: archive location, phase ordering, rollback
# ---------------------------------------------------------------------- #
class _Store:
    def __init__(self, *, backups=None, checkpoints=None, restore_row=None):
        self._backups = backups or {}
        self._checkpoints = checkpoints or []
        self._restore_row = restore_row

    def available(self):
        return True

    def backup(self, ref):
        return self._backups.get(ref)

    def checkpoints(self):
        return list(self._checkpoints)

    def restore(self, restore_id):
        return self._restore_row

    def setting_get(self, key, default=None):
        return default

    def backup_update(self, *args, **kwargs):
        return None


def _engine(tmp_path, store, **kw):
    cfg = types.SimpleNamespace(
        stage_dir=str(tmp_path / "stage"),
        checkpoint_dir=str(tmp_path / "checkpoints"),
        backup_root=str(tmp_path),
        librenms_root=str(tmp_path / "app"),
        archive_dir=str(tmp_path / "archives"),
        app_user="librenms",
        app_group="librenms",
    )
    cfg.get = lambda section, key, default=None: default
    cfg.encryption = "age"
    cfg.keys_dir = str(tmp_path / "keys")
    cfg.age_recipient = ""
    cfg.age_identity = ""
    cfg.get_path = lambda section, key, default=None: str(tmp_path / "keys" / "recipients.txt")
    worker = types.SimpleNamespace(
        cfg=cfg, store=store,
        log=types.SimpleNamespace(info=lambda *a, **k: None,
                                  warn=lambda *a, **k: None,
                                  error=lambda *a, **k: None),
        helper=types.SimpleNamespace(),
    )
    engine = restore_mod.RestoreEngine(
        worker,
        restore_id=kw.pop("restore_id", "rid-1"),
        backup_ref=kw.pop("backup_ref", "br-1"),
        job_id="job-1",
        **kw,
    )
    engine._note = lambda *a, **k: None
    engine._progress = lambda *a, **k: None
    return engine


def _ok_verify(path, cfg, log, level="standard"):
    return {"ok": True, "members": 1, "checksums_checked": 1, "mismatched": [], "missing": []}


def test_validate_resolves_a_recovery_checkpoint(tmp_path, monkeypatch):
    """Rollback targets live in br_checkpoints, not br_backups."""
    archive = tmp_path / "cp.brarchive"
    archive.write_bytes(b"x")
    store = _Store(checkpoints=[{"checkpoint_id": "cp-1", "status": "complete",
                                 "path": str(archive)}],
                   restore_row={"restore_id": "rid-1", "status": "running", "options": "{}"})
    engine = _engine(tmp_path, store, checkpoint_id="cp-1")

    monkeypatch.setattr(restore_mod.verify_mod, "verify", _ok_verify, raising=True)
    monkeypatch.setattr(restore_mod.manifest_mod, "validate", lambda manifest: None)
    engine._check_compatibility = lambda manifest: None
    engine._read_manifest = lambda path: {
        "encryption": {"scheme": "age"}, "archive": {"size_bytes": 1}, "components": {},
    }
    engine._validate()
    assert engine.archive_path == str(archive)


def test_validate_refuses_an_unknown_checkpoint(tmp_path):
    engine = _engine(tmp_path, _Store(checkpoints=[]), checkpoint_id="cp-nope")
    with pytest.raises(ValidationError):
        engine._validate()


def test_validate_refuses_an_incomplete_checkpoint(tmp_path):
    archive = tmp_path / "cp.brarchive"
    archive.write_bytes(b"x")
    store = _Store(checkpoints=[{"checkpoint_id": "cp-1", "status": "failed",
                                 "path": str(archive)}])
    engine = _engine(tmp_path, store, checkpoint_id="cp-1")
    with pytest.raises(BRException):
        engine._validate()


def test_rollback_takes_no_new_checkpoint(tmp_path, monkeypatch):
    """A rollback must not depend on the very thing it is recovering from."""
    calls = []
    engine = _engine(tmp_path, _Store(), checkpoint_id="cp-1")
    engine._acquire_lock = lambda: None
    engine._validate = lambda: None
    engine._enter_maintenance = lambda: None
    engine._create_checkpoint = lambda: calls.append("checkpoint") or "cp-new"
    for step in ("_restore_database", "_restore_files", "_restore_configuration",
                 "_fix_permissions", "_validate_services"):
        setattr(engine, step, lambda: None)
    engine._leave_maintenance = lambda: None
    engine._release_lock = lambda: None
    engine._state = lambda s: None
    engine.store.restore_finish = lambda *a, **k: None
    engine.store.job_finish = lambda *a, **k: None
    engine.w.adopt_orphans = lambda: 0

    engine.run()
    assert calls == []


def test_file_stage_uses_single_level_member_prefixes(tmp_path, monkeypatch):
    """The archive stores `application/...`, not `application/application/...`.

    The doubled prefix matched zero members, so the file stage silently
    installed nothing and still reported success.
    """
    seen = []
    engine = _engine(tmp_path, _Store())
    engine.manifest = {"components": {"application": True, "rrd": True, "integrations": True}}

    def _extract(prefix, dest):
        seen.append(prefix)
        os.makedirs(dest, exist_ok=True)
        return 0

    engine._extract_tree = _extract
    engine._install = lambda source, target, note: seen.append((note, target))
    engine._restore_files()

    prefixes = [s for s in seen if isinstance(s, str)]
    assert "application" in prefixes and "application/application" not in prefixes
    assert "rrd" in prefixes and "rrd/rrd" not in prefixes
    # Nothing may target "/" - the helper's path validator rejects it.
    targets = [t for _, t in (s for s in seen if isinstance(s, tuple))]
    assert "/" not in targets


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #


# ---------------------------------------------------------------------- #
# Corrupt-input handling: a damaged archive must raise a typed error, never
# a raw UnicodeDecodeError. Found by fuzzing the BRENC1 header and the
# checksum parser; both used to leak a codec exception to the caller.
# ---------------------------------------------------------------------- #
def test_corrupt_container_header_raises_integrity_error(tmp_path):
    import struct

    from br import crypto
    from br.common import IntegrityError

    path = tmp_path / "bad.brarchive"
    # Magic is valid, header length is valid, but the "JSON" is random bytes
    # that are not UTF-8 decodable.
    path.write_bytes(b"BRENC1" + struct.pack(">I", 8) + b"\xd5\xf6\x8d\xb2\xf2\xa7\xda\xfe")

    with pytest.raises(IntegrityError) as exc:
        with crypto.EncryptedReader(str(path), _StubCfg(tmp_path)):
            pass
    assert "corrupt container header" in str(exc.value)


def test_truncated_container_header_raises_integrity_error(tmp_path):
    import struct

    from br import crypto
    from br.common import IntegrityError

    path = tmp_path / "trunc.brarchive"
    # Header length claims 500 bytes but the file ends after 3.
    path.write_bytes(b"BRENC1" + struct.pack(">I", 500) + b"{}\n")

    with pytest.raises(IntegrityError) as exc:
        with crypto.EncryptedReader(str(path), _StubCfg(tmp_path)):
            pass
    assert "truncated" in str(exc.value)


def test_non_object_container_header_is_rejected(tmp_path):
    import struct

    from br import crypto
    from br.common import IntegrityError

    path = tmp_path / "list.brarchive"
    payload = b"[1, 2, 3]"
    path.write_bytes(b"BRENC1" + struct.pack(">I", len(payload)) + payload)

    with pytest.raises(IntegrityError):
        with crypto.EncryptedReader(str(path), _StubCfg(tmp_path)):
            pass


def test_corrupt_checksum_file_raises_integrity_error(tmp_path):
    from br import checksums
    from br.common import IntegrityError

    path = tmp_path / "checksums.sha256"
    path.write_bytes(b"\xd5\xf6\x8d\xb2 not a checksum line\n")

    with pytest.raises(IntegrityError):
        checksums.parse(str(path))


def test_checksum_file_with_bad_digest_is_rejected(tmp_path):
    from br import checksums
    from br.common import IntegrityError

    path = tmp_path / "checksums.sha256"
    path.write_text("not-a-sha256  application/.env\n")

    with pytest.raises(IntegrityError):
        checksums.parse(str(path))


def test_checksum_file_with_real_format_still_parses(tmp_path):
    """The stricter digest check must not reject the format we actually write."""
    from br import checksums

    digest = "a" * 64
    path = tmp_path / "checksums.sha256"
    path.write_text(
        f"{digest}  application/.env\n"
        "symlink  application/link -> target\n"
        f"{'b' * 64}  database/librenms.sql.gz\n"
    )
    parsed = checksums.parse(str(path))
    assert parsed["application/.env"] == digest
    assert parsed["database/librenms.sql.gz"] == "b" * 64


class _StubCfg:
    """Minimal config for crypto container parsing tests."""

    def __init__(self, tmp_path):
        self.encryption = "age"
        self.keys_dir = str(tmp_path)
        self.age_recipient = ""
        self.age_identity = ""

    def get(self, section, key, default=None):
        return default

    def get_path(self, section, key, default=None):
        return str(self.keys_dir + "/nonexistent")

    def get_int(self, section, key, default=0):
        return default

    def get_bool(self, section, key, default=False):
        return default

    def get_list(self, section, key):
        return []
