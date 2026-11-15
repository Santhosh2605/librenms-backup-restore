"""Shared pytest fixtures for the worker test suite.

The worker has no third-party runtime dependencies except ``cryptography``
(only for the aesgcm-pbkdf2 scheme) and the ``zstd``/``age`` binaries. Tests
that need an external binary skip cleanly when it is absent, so the suite is
runnable on a bare checkout.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

WORKER_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "worker"))
if WORKER_ROOT not in sys.path:
    sys.path.insert(0, WORKER_ROOT)

from br.config import Config  # noqa: E402


def _write(path: str, text: str, mode: int = 0o600) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(path, mode)
    return path


@pytest.fixture()
def br_home(tmp_path):
    """A complete, self-consistent directory layout plus a Config bound to it."""
    root = tmp_path / "app"
    (root / "includes").mkdir(parents=True)
    (root / "config.php").write_text("<?php\n$config['db_host']='127.0.0.1';\n")

    keys = tmp_path / "keys"
    keys.mkdir()
    passphrase = _write(str(keys / "passphrase"), "correct-horse-battery-staple\n")

    home = {
        "librenms_root": str(root),
        "backup_root": str(tmp_path / "backups"),
        "archive_dir": str(tmp_path / "backups" / "archives"),
        "staging_dir": str(tmp_path / "backups" / "staging"),
        "checkpoint_dir": str(tmp_path / "backups" / "checkpoints"),
        "run_dir": str(tmp_path / "run"),
        "state_dir": str(tmp_path / "state"),
        "log_dir": str(tmp_path / "log"),
        "keys": str(keys),
        "passphrase_file": passphrase,
    }
    for key in ("backup_root", "archive_dir", "staging_dir", "checkpoint_dir",
                "run_dir", "state_dir", "log_dir"):
        os.makedirs(home[key], exist_ok=True)

    conf = _write(
        str(tmp_path / "br.conf"),
        f"""
[general]
librenms_root  = {home['librenms_root']}
backup_root    = {home['backup_root']}
staging_dir    = {home['staging_dir']}
archive_dir    = {home['archive_dir']}
checkpoint_dir = {home['checkpoint_dir']}
run_dir        = {home['run_dir']}
state_dir      = {home['state_dir']}
log_dir        = {home['log_dir']}
librenms_user  = librenms
librenms_group = librenms
min_free_mib   = 1
headroom_mib   = 0

[archive]
compression             = {{compression}}
zstd_level              = 1
encryption              = {{encryption}}
age_recipients_file     = {home['keys']}/recipients.txt
age_identity_file       = {home['keys']}/identity.txt
openssl_passphrase_file = {home['passphrase_file']}

[database]
dump_tool = auto

[components]
database     = true
rrd          = false
application  = true
system       = false
integrations = false
""".replace("{compression}", "zstd").replace("{encryption}", "none"),
    )

    cfg = Config(conf)
    cfg.home = home  # convenience handle for tests
    return cfg


@pytest.fixture()
def make_cfg(br_home, tmp_path):
    """Factory: same layout, overriding archive options."""

    def _make(**overrides):
        text = open(br_home.path, "r", encoding="utf-8").read()
        for key, value in overrides.items():
            needle = f"{key}"
            lines = []
            replaced = False
            for line in text.splitlines():
                if line.strip().startswith(needle) and "=" in line:
                    pad = " " * (len(line) - len(line.lstrip()))
                    lines.append(f"{pad}{key} = {value}")
                    replaced = True
                else:
                    lines.append(line)
            assert replaced, f"option {key} not present in template config"
            text = "\n".join(lines) + "\n"
        target = tmp_path / f"br-{abs(hash(tuple(sorted(overrides.items()))))}.conf"
        target.write_text(text, encoding="utf-8")
        os.chmod(target, 0o600)
        fresh = Config(str(target))
        fresh.home = br_home.home
        return fresh

    return _make


requires_zstd = pytest.mark.skipif(
    shutil.which("zstd") is None, reason="zstd binary not installed"
)


@pytest.fixture()
def age_identity(tmp_path):
    """Real age keypair, generated on the fly (needs the age binary)."""
    if shutil.which("age-keygen") is None:
        pytest.skip("age-keygen not installed")
    import subprocess

    ident = tmp_path / "keys" / "identity.txt"
    ident.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["age-keygen", "-o", str(ident)], capture_output=True, text=True, check=True
    )
    # age-keygen prints the public key to stderr on some releases and stdout on
    # others, so scan both.
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    recipient = ""
    for line in combined.splitlines():
        if "public key:" in line.lower():
            # age-keygen capitalises "Public key:" on stderr.
            recipient = line.split(":", 1)[1].strip().split()[0]
    assert recipient.startswith("age1"), combined
    os.chmod(ident, 0o600)
    recipients = tmp_path / "keys" / "recipients.txt"
    recipients.write_text(recipient + "\n", encoding="utf-8")
    return {"identity": str(ident), "recipient": recipient}
