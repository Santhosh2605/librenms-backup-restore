"""Crypto container tests: round-trip, tamper detection, scheme reporting."""

from __future__ import annotations

import os

import pytest

from br import crypto
from br.common import IntegrityError, PreflightError, ValidationError


PAYLOAD = (b"librenms backup payload\n" * 5000) + bytes(range(256))


def _read_all(reader) -> bytes:
    chunks = []
    while True:
        block = reader.read(64 * 1024)
        if not block:
            return b"".join(chunks)
        chunks.append(block)


# -- scheme: none -----------------------------------------------------------

def test_none_scheme_roundtrip(br_home, tmp_path):
    dest = str(tmp_path / "a.brarchive")
    with crypto.EncryptedWriter(dest, br_home) as writer:
        writer.write(PAYLOAD)
    assert os.path.getsize(dest) > len(PAYLOAD)

    with crypto.EncryptedReader(dest, br_home) as reader:
        assert _read_all(reader) == PAYLOAD


def test_container_has_magic_and_readable_header(br_home, tmp_path):
    import json
    import struct

    dest = str(tmp_path / "b.brarchive")
    with crypto.EncryptedWriter(dest, br_home) as writer:
        writer.write(b"x")

    with open(dest, "rb") as fh:
        assert fh.read(len(crypto.MAGIC)) == crypto.MAGIC
        (hlen,) = struct.unpack(">I", fh.read(4))
        header = json.loads(fh.read(hlen))
    assert header["scheme"] == "none"
    assert header["encrypted"] is False


# -- scheme: aesgcm-pbkdf2 --------------------------------------------------

def test_aesgcm_roundtrip(make_cfg, tmp_path):
    pytest.importorskip("cryptography")
    cfg = make_cfg(encryption="aesgcm-pbkdf2")
    # 600k PBKDF2 iterations are correct for production but slow in tests.
    import br.crypto as c

    original = c.PBKDF2_ITERATIONS
    c.PBKDF2_ITERATIONS = 1000
    try:
        dest = str(tmp_path / "c.brarchive")
        with c.EncryptedWriter(dest, cfg) as writer:
            writer.write(PAYLOAD)

        raw = open(dest, "rb").read()
        assert PAYLOAD[:512] not in raw, "ciphertext must not contain plaintext"

        with c.EncryptedReader(dest, cfg) as reader:
            assert _read_all(reader) == PAYLOAD
    finally:
        c.PBKDF2_ITERATIONS = original


def test_aesgcm_detects_tampered_ciphertext(make_cfg, tmp_path):
    pytest.importorskip("cryptography")
    cfg = make_cfg(encryption="aesgcm-pbkdf2")
    import br.crypto as c

    original = c.PBKDF2_ITERATIONS
    c.PBKDF2_ITERATIONS = 1000
    try:
        dest = str(tmp_path / "d.brarchive")
        with c.EncryptedWriter(dest, cfg) as writer:
            writer.write(PAYLOAD)

        data = bytearray(open(dest, "rb").read())
        data[len(data) // 2] ^= 0xFF
        open(dest, "wb").write(bytes(data))

        with pytest.raises(IntegrityError):
            with c.EncryptedReader(dest, cfg) as reader:
                _read_all(reader)
    finally:
        c.PBKDF2_ITERATIONS = original


def test_aesgcm_wrong_passphrase_fails(make_cfg, tmp_path):
    pytest.importorskip("cryptography")
    cfg = make_cfg(encryption="aesgcm-pbkdf2")
    import br.crypto as c

    original = c.PBKDF2_ITERATIONS
    c.PBKDF2_ITERATIONS = 1000
    try:
        dest = str(tmp_path / "e.brarchive")
        with c.EncryptedWriter(dest, cfg) as writer:
            writer.write(PAYLOAD)

        other = os.path.join(os.path.dirname(cfg.home["passphrase_file"]), "other")
        with open(other, "w") as fh:
            fh.write("a-completely-different-passphrase\n")
        os.chmod(other, 0o600)

        cfg._cp.set("archive", "openssl_passphrase_file", other)
        with pytest.raises(IntegrityError):
            with c.EncryptedReader(dest, cfg) as reader:
                _read_all(reader)
    finally:
        c.PBKDF2_ITERATIONS = original


# -- passphrase policy ------------------------------------------------------

def test_missing_passphrase_file_is_preflight_error(make_cfg, br_home):
    cfg = make_cfg(encryption="aesgcm-pbkdf2")
    cfg._cp.set("archive", "openssl_passphrase_file", "/nonexistent/passphrase")
    with pytest.raises(PreflightError):
        crypto.read_passphrase(cfg)


def test_world_readable_passphrase_rejected(make_cfg, br_home):
    cfg = make_cfg(encryption="aesgcm-pbkdf2")
    path = cfg.get_path("archive", "openssl_passphrase_file")
    os.chmod(path, 0o644)
    with pytest.raises(PreflightError):
        crypto.read_passphrase(cfg)


def test_short_passphrase_rejected(make_cfg, br_home):
    cfg = make_cfg(encryption="aesgcm-pbkdf2")
    path = cfg.get_path("archive", "openssl_passphrase_file")
    with open(path, "w") as fh:
        fh.write("tooshort\n")
    os.chmod(path, 0o600)
    with pytest.raises(ValidationError):
        crypto.read_passphrase(cfg)


# -- container integrity ----------------------------------------------------

def test_non_container_rejected(br_home, tmp_path):
    bogus = tmp_path / "bogus.brarchive"
    bogus.write_bytes(b"NOTACONTAINER" + b"\x00" * 32)
    with pytest.raises(IntegrityError):
        with crypto.EncryptedReader(str(bogus), br_home) as reader:
            reader.read(10)


def test_corrupt_header_length_rejected(br_home, tmp_path):
    import struct

    bogus = tmp_path / "corrupt.brarchive"
    bogus.write_bytes(crypto.MAGIC + struct.pack(">I", 0xFFFFFF) + b"{}")
    with pytest.raises(IntegrityError):
        with crypto.EncryptedReader(str(bogus), br_home) as reader:
            reader.read(10)


def test_missing_archive_is_preflight_error(br_home, tmp_path):
    with pytest.raises(PreflightError):
        with crypto.EncryptedReader(str(tmp_path / "absent.brarchive"), br_home):
            pass


# -- age --------------------------------------------------------------------

def test_age_roundtrip(make_cfg, age_identity, tmp_path):
    cfg = make_cfg(encryption="age")
    cfg._cp.set("archive", "age_identity_file", age_identity["identity"])
    cfg._cp.set("archive", "age_recipients_file", str(tmp_path / "keys" / "recipients.txt"))

    dest = str(tmp_path / "age.brarchive")
    with crypto.EncryptedWriter(dest, cfg) as writer:
        writer.write(PAYLOAD)

    with crypto.EncryptedReader(dest, cfg) as reader:
        assert _read_all(reader) == PAYLOAD


def test_age_rejects_non_age_recipient(make_cfg, tmp_path):
    cfg = make_cfg(encryption="age")
    recips = tmp_path / "keys" / "recipients.txt"
    recips.parent.mkdir(parents=True, exist_ok=True)
    recips.write_text("ssh-ed25519 AAAAnot-an-age-key\n", encoding="utf-8")
    cfg._cp.set("archive", "age_recipients_file", str(recips))

    with pytest.raises(ValidationError):
        with crypto.EncryptedWriter(str(tmp_path / "x.brarchive"), cfg) as writer:
            writer.write(b"x")
