"""Manifest construction and validation."""

from __future__ import annotations

import copy

import pytest

from br import manifest
from br.common import IntegrityError, ValidationError

BACKUP_ID = "br-20261111T181440Z-a1b2c3"


def _build(br_home, tmp_path, **overrides):
    archive = tmp_path / "archive.brarchive"
    archive.write_bytes(b"x" * 1024)
    kwargs = dict(
        backup_id=BACKUP_ID,
        backup_type="full",
        cfg=br_home,
        components={"database": True, "rrd": True, "application": True,
                    "system": False, "integrations": False},
        versions={"php": "8.4.27", "mariadb": "11.8.6", "python": "3.13.16",
                  "os": {"os_pretty_name": "Debian 13"}},
        collection={"file_hashes": {"a": "b"}, "files": 3},
        checksum_sha256="a" * 64,
        archive_path=str(archive),
        encryption_info={"scheme": "age", "encrypted": True},
        key_fp="age:37bf8854a6",
    )
    kwargs.update(overrides)
    return manifest.build(**kwargs)


def test_build_contains_required_fields(br_home, tmp_path):
    built = _build(br_home, tmp_path)
    for field in manifest.REQUIRED_FIELDS:
        assert field in built, field
    assert built["manifest_schema"] == manifest.CURRENT_SCHEMA
    assert built["backup_id"] == BACKUP_ID
    assert built["archive"]["size_bytes"] == 1024


def test_built_manifest_passes_validation(br_home, tmp_path):
    built = _build(br_home, tmp_path)
    assert manifest.validate(copy.deepcopy(built)) is not None


def test_archive_sha_is_deferred_to_sidecar(br_home, tmp_path):
    built = _build(br_home, tmp_path)
    # The container hash cannot exist before the file is closed; the sidecar is
    # authoritative and the field must stay None rather than lie.
    assert built["checksums"]["archive_sha256"] is None
    assert built["checksums"]["sidecar"].endswith(".sha256")


def test_restore_dependencies_are_recorded(br_home, tmp_path):
    deps = _build(br_home, tmp_path)["restore_dependencies"]
    assert deps["php"] == "8.4.27"
    assert deps["mariadb"] == "11.8.6"
    assert deps["encryption"] == "age"
    assert deps["notes"], "restore caveats must be carried in the manifest"


def test_component_list_sorted(br_home, tmp_path):
    built = _build(br_home, tmp_path)
    assert manifest.component_list(built["components"]) == [
        "application", "database", "rrd"
    ]


# -- validation failures ----------------------------------------------------

def test_rejects_non_dict():
    with pytest.raises(IntegrityError):
        manifest.validate([1, 2, 3])


def test_rejects_missing_field(br_home, tmp_path):
    built = _build(br_home, tmp_path)
    del built["versions"]
    with pytest.raises(IntegrityError, match="missing required fields"):
        manifest.validate(built)


def test_rejects_future_schema(br_home, tmp_path):
    built = _build(br_home, tmp_path)
    built["manifest_schema"] = 99
    with pytest.raises(ValidationError, match="unsupported manifest schema"):
        manifest.validate(built)


def test_rejects_malformed_backup_id(br_home, tmp_path):
    built = _build(br_home, tmp_path)
    built["backup_id"] = "not-a-backup-id"
    with pytest.raises(IntegrityError, match="malformed"):
        manifest.validate(built)


def test_rejects_unknown_component(br_home, tmp_path):
    built = _build(br_home, tmp_path)
    built["components"]["kubernetes"] = True
    with pytest.raises(IntegrityError, match="unknown component"):
        manifest.validate(built)


def test_rejects_non_boolean_component(br_home, tmp_path):
    built = _build(br_home, tmp_path)
    built["components"]["rrd"] = "yes"
    with pytest.raises(IntegrityError, match="must be boolean"):
        manifest.validate(built)


def test_rejects_unknown_checksum_algorithm(br_home, tmp_path):
    built = _build(br_home, tmp_path)
    built["checksums"]["algorithm"] = "md5"
    with pytest.raises(IntegrityError, match="unsupported checksum algorithm"):
        manifest.validate(built)


def test_strict_mode_requires_archive_block(br_home, tmp_path):
    # `archive` is a required field, so it is caught by the field check in both
    # modes; strict mode additionally insists on its shape.
    built = _build(br_home, tmp_path)
    built["archive"] = "not-a-dict"
    with pytest.raises(IntegrityError, match="archive block"):
        manifest.validate(built)
    assert manifest.validate(built, strict=False)
