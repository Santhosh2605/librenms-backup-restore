"""Configuration parsing, validation and behavioural contracts."""

from __future__ import annotations

import os

import pytest

from br.common import ValidationError
from br.config import Config


def _conf(tmp_path, body: str) -> str:
    path = tmp_path / "br.conf"
    path.write_text(body, encoding="utf-8")
    return str(path)


MINIMAL = """
[general]
librenms_root = /opt/librenms
backup_root   = /var/backups/librenms
staging_dir   = /var/backups/librenms/staging
archive_dir   = /var/backups/librenms/archives
checkpoint_dir = /var/backups/librenms/checkpoints
run_dir       = /run/librenms-backup
state_dir     = /var/lib/librenms-backup
log_dir       = /var/log/librenms-backup
librenms_user  = librenms
librenms_group = librenms

[archive]
compression = zstd
encryption  = age

[database]
dump_tool = auto

[restore]
confirm_phrase = RESTORE
"""


# -- primitives -------------------------------------------------------------

def test_get_returns_default_when_option_absent(tmp_path):
    cfg = Config(_conf(tmp_path, MINIMAL))
    assert cfg.get("general", "not_there", "fallback") == "fallback"


def test_missing_required_value_raises(tmp_path):
    cfg = Config(_conf(tmp_path, MINIMAL))
    with pytest.raises(ValidationError):
        cfg.get("general", "not_there")


@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("TRUE", True), ("yes", True), ("on", True), ("1", True),
    ("false", False), ("no", False), ("off", False), ("0", False),
])
def test_bool_parsing(tmp_path, raw, expected):
    cfg = Config(_conf(tmp_path, MINIMAL + f"\n[components]\nrrd = {raw}\n"))
    assert cfg.get_bool("components", "rrd") is expected


def test_invalid_bool_rejected(tmp_path):
    cfg = Config(_conf(tmp_path, MINIMAL + "\n[components]\nrrd = maybe\n"))
    with pytest.raises(ValidationError):
        cfg.get_bool("components", "rrd")


def test_int_parsing_and_rejection(tmp_path):
    ok = _conf(tmp_path, MINIMAL.replace("encryption  = age",
                                         "encryption  = age\nzstd_level = 7"))
    assert Config(ok).get_int("archive", "zstd_level") == 7
    bad = _conf(tmp_path, MINIMAL.replace("encryption  = age",
                                          "encryption  = age\nzstd_level = high"))
    with pytest.raises(ValidationError, match="must be an integer"):
        Config(bad)


def test_list_skips_comments_and_blanks(tmp_path):
    body = MINIMAL + "\n[database]\nexclude_tables =\n  ports\n  # cache\n  alert_schedule\n"
    cfg = Config(_conf(tmp_path, body))
    assert cfg.get_list("database", "exclude_tables") == ["ports", "alert_schedule"]


# -- encryption scheme resolution ------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("age", "age"),
    ("AGE", "age"),
    ("none", "none"),
    ("aesgcm-pbkdf2", "aesgcm-pbkdf2"),
    # The documented fallback name must resolve to a real implementation,
    # otherwise a backup fails at the first write.
    ("openssl-sshaes", "aesgcm-pbkdf2"),
])
def test_encryption_aliases_resolve(tmp_path, value, expected):
    body = MINIMAL.replace("encryption  = age", f"encryption = {value}")
    cfg = Config(_conf(tmp_path, body))
    assert cfg.encryption == expected


def test_unknown_encryption_rejected(tmp_path):
    body = MINIMAL.replace("encryption  = age", "encryption = rot13")
    cfg = Config(_conf(tmp_path, body))
    with pytest.raises(ValidationError, match="unsupported encryption scheme"):
        cfg.encryption


# -- components -------------------------------------------------------------

def test_components_default_to_enabled(tmp_path):
    cfg = Config(_conf(tmp_path, MINIMAL))
    assert all(cfg.components.values())


def test_components_can_be_disabled(tmp_path):
    cfg = Config(_conf(tmp_path, MINIMAL + "\n[components]\nrrd = no\nsystem = off\n"))
    components = cfg.components
    assert components["rrd"] is False
    assert components["system"] is False
    assert components["database"] is True


# -- validation -------------------------------------------------------------

def test_validate_rejects_bad_level(tmp_path):
    path = _conf(tmp_path, MINIMAL + "\n[archive]\nzstd_level = 40\n")
    with pytest.raises(ValidationError, match="zstd_level"):
        Config(path)


def test_validate_accepts_minimal_config(tmp_path):
    cfg = Config(_conf(tmp_path, MINIMAL))
    Config(cfg.path)


def test_unknown_section_rejected(tmp_path):
    body = MINIMAL + "\n[surprise]\nkey = value\n"
    with pytest.raises(ValidationError, match="unknown configuration sections"):
        Config(_conf(tmp_path, body))


def test_relative_paths_rejected(tmp_path):
    body = MINIMAL.replace(
        "archive_dir   = /var/backups/librenms/archives",
        "archive_dir   = backups/archives",
    )
    with pytest.raises(ValidationError):
        Config(_conf(tmp_path, body))


def test_unsafe_path_rejected(tmp_path):
    body = MINIMAL.replace(
        "archive_dir   = /var/backups/librenms/archives",
        "archive_dir   = /var/backups/../etc",
    )
    with pytest.raises(ValidationError):
        Config(_conf(tmp_path, body))


def test_get_path_requires_absolute(tmp_path):
    cfg = Config(_conf(tmp_path, MINIMAL))
    cfg._cp.set("archive", "age_recipients_file", "keys/recipients.txt")
    with pytest.raises(ValidationError):
        cfg.get_path("archive", "age_recipients_file")


def test_defaults_applied_for_optional_paths(tmp_path):
    cfg = Config(_conf(tmp_path, MINIMAL))
    assert cfg.librenms_root == "/opt/librenms"
    assert cfg.encryption == "age"
    assert isinstance(cfg.components, dict)
