"""Verification levels and collect-time scrubbing."""

from __future__ import annotations

import os

import pytest

from br import collect, verify
from br.common import Logger
from br.writer import ArchiveBuild


def _logger(tmp_path):
    return Logger(os.path.join(str(tmp_path), "verify.log"))


def _build_archive(cfg, tmp_path, *, manifest=None, break_checksum=False):
    dest = str(tmp_path / "verify.brarchive")
    log = _logger(tmp_path)
    payloads = {
        "manifest.json": None,
        "checksums.sha256": None,
        "application/config.php": b"<?php\n",
        "database/librenms.sql.gz": b"-- sql\n",
    }
    import hashlib

    with ArchiveBuild(dest, cfg, log) as writer:
        hashes = {}
        for rel, data in payloads.items():
            if data is None:
                continue
            writer.add_bytes(rel, data)
            hashes[rel] = hashlib.sha256(data).hexdigest()
        if breaks := break_checksum:
            hashes["database/librenms.sql.gz"] = "0" * 64
        writer.add_bytes("checksums.sha256",
                         ("\n".join(f"{v}  {k}" for k, v in hashes.items()) + "\n").encode())
        doc = manifest or {
            "manifest_schema": 1,
            "backup_id": "br-20261111T181440Z-abc123",
            "status": "complete",
            "created_at": "2026-11-11T18:14:40Z",
            "versions": {"php": "8.4.27", "mariadb": "11.8.6"},
            "components": {"database": True, "application": True},
            "encryption": {"scheme": "none"},
            "checksums": {"algorithm": "sha256"},
            "archive": {"size_bytes": 1},
        }
        writer.add_json("manifest.json", doc)
    return dest


def test_levels_are_ordered():
    assert verify.LEVELS == ("shallow", "standard", "deep")


def test_shallow_verification_passes(br_home, tmp_path):
    dest = _build_archive(br_home, tmp_path)
    result = verify.verify(dest, br_home, _logger(tmp_path), level="shallow")
    assert result["ok"] is True


def test_standard_verification_checks_payload_hashes(br_home, tmp_path):
    dest = _build_archive(br_home, tmp_path)
    result = verify.verify(dest, br_home, _logger(tmp_path), level="standard")
    assert result["ok"] is True
    assert result["members"] > 0


def test_standard_verification_catches_bad_checksum(br_home, tmp_path):
    """A corrupted member must never be reported as verified."""
    dest = _build_archive(br_home, tmp_path, break_checksum=True)
    result = verify.verify(dest, br_home, _logger(tmp_path), level="standard")
    assert result["ok"] is False
    assert result["mismatched"]


def test_deep_verification_re_reads_bodies(br_home, tmp_path):
    """Deep mode additionally proves the SQL dump really gunzips."""
    import gzip

    # Must look like a real dump: a schema statement up front and a
    # terminator at the tail, which is what deep verification checks for.
    body = (b"-- LibreNMS dump\nCREATE DATABASE `librenms`;\n"
            + b"INSERT INTO `devices` VALUES (1,'router');\n" * 200)
    dump = gzip.compress(body + b"-- Dump completed\n")
    dest = str(tmp_path / "deep.brarchive")
    log = _logger(tmp_path)
    import hashlib

    hashes = {
        "database/librenms.sql.gz": hashlib.sha256(dump).hexdigest(),
        "application/config.php": hashlib.sha256(b"<?php\n").hexdigest(),
    }
    with ArchiveBuild(dest, br_home, log) as writer:
        writer.add_bytes("database/librenms.sql.gz", dump)
        writer.add_bytes("application/config.php", b"<?php\n")
        writer.add_bytes("checksums.sha256",
                         ("\n".join(f"{v}  {k}" for k, v in hashes.items()) + "\n").encode())
        writer.add_json("manifest.json", {
            "manifest_schema": 1,
            "backup_id": "br-20261111T181440Z-abc123",
            "status": "complete",
            "created_at": "2026-11-11T18:14:40Z",
            "versions": {"php": "8.4.27"},
            "components": {"database": True, "application": True},
            "encryption": {"scheme": "none"},
            "checksums": {"algorithm": "sha256"},
            "archive": {"size_bytes": 1},
            "restore_dependencies": {"php": "8.4.27", "encryption": "none"},
        })

    result = verify.verify(dest, br_home, log, level="deep")
    assert result["ok"] is True
    assert result["restore_dependencies"]["php"] == "8.4.27"


def test_missing_manifest_is_a_failure(br_home, tmp_path):
    dest = str(tmp_path / "nomanifest.brarchive")
    log = _logger(tmp_path)
    with ArchiveBuild(dest, br_home, log) as writer:
        writer.add_bytes("application/config.php", b"<?php\n")
    with pytest.raises(Exception):
        verify.verify(dest, br_home, log, level="standard")


def test_unsupported_level_rejected(br_home, tmp_path):
    dest = _build_archive(br_home, tmp_path)
    with pytest.raises(Exception):
        verify.verify(dest, br_home, _logger(tmp_path), level="paranoid")


# -- collect-time secret scrubbing ----------------------------------------

@pytest.mark.parametrize("line", [
    "DB_PASSWORD=supersecret",
    "APP_KEY=base64:abcdefghijklmnop",
    "auth_token = abc123def456",
    "api_secret: zzz",
])
def test_scrub_text_redacts_secret_assignments(line):
    scrubbed, count = collect.scrub_text((line + "\n").encode())
    assert count >= 1, f"not redacted: {line}"
    assert b"supersecret" not in scrubbed
    assert b"abcdefghijklmnop" not in scrubbed
    assert b"abc123def456" not in scrubbed


def test_scrub_text_leaves_ordinary_lines():
    text = b"<?php\n$config['db_host'] = 'localhost';\n"
    scrubbed, count = collect.scrub_text(text)
    assert count == 0
    assert scrubbed == text


def test_scrub_text_handles_binary():
    blob = bytes(range(256)) * 10
    scrubbed, _count = collect.scrub_text(blob)
    assert isinstance(scrubbed, bytes)


# -- exclude rules ---------------------------------------------------------

@pytest.mark.parametrize("rel", [
    "application/storage/framework/cache/data",
    "application/logs/librenms.log",
    "application/bootstrap/cache/config.php",
])
def test_machine_specific_paths_excluded(rel):
    assert collect.should_exclude(rel) or collect.is_machine_specific(rel)


def test_normal_paths_are_not_excluded():
    assert not collect.should_exclude("application/config.php")
    assert not collect.should_exclude("database/librenms.sql.gz")
