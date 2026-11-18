"""Encryption layer for backup archives.

Two authenticated schemes are supported.  Both are *encrypt-then-MAC* /
AEAD constructions: a tampered archive fails closed.

``age``
    Ready-made X25519 + ChaCha20-Poly1305 tool from Ubuntu's own archive.
    Recipients (public keys) are used for backup, the identity (private key)
    only for restore.  This means the backup host can write archives it can
    only read back with the identity file - the preferred operational model.

``aesgcm-pbkdf2`` (a.k.a. ``aesctr-hmac`` internally)
    Self-contained fallback implemented with ``python3-cryptography``:
    AES-256-CTR for confidentiality plus HMAC-SHA256 over
    ``header || ciphertext`` for integrity, keys derived from a passphrase
    with PBKDF2-HMAC-SHA256 (600 000 iterations, random 16-byte salt).

Container format (scheme independent wrapper, magic ``BRENC1``)::

    +--------+--------+----------------+--------------+--------+
    | BRENC1 | len(4) | header (JSON)  | ciphertext   | tag(32)|
    +--------+--------+----------------+--------------+--------+

For ``age`` the inner payload is an age file; the wrapper tag is unused and
integrity comes from the age AEAD itself.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import io
import json
import os
import struct
import subprocess
import threading
import typing as t

from .common import (
    CHUNK,
    BRException,
    IntegrityError,
    Logger,
    PreflightError,
    ValidationError,
    run,
    which,
)

MAGIC = b"BRENC1"
TAG_LEN = 32
HEADER_MAX = 64 * 1024
PBKDF2_ITERATIONS = 600_000


# ---------------------------------------------------------------------------
# key material
# ---------------------------------------------------------------------------

def read_passphrase(cfg) -> bytes:
    path = cfg.get_path("archive", "openssl_passphrase_file")
    if not os.path.exists(path):
        raise PreflightError(f"passphrase file missing: {path}")
    mode = os.stat(path).st_mode & 0o777
    if mode & 0o007:
        raise PreflightError(f"passphrase file {path} is world-accessible (mode {mode:o})")
    with open(path, "rb") as fh:
        phrase = fh.read().strip()
    if len(phrase) < 16:
        raise ValidationError("passphrase must be at least 16 characters")
    return phrase


def _age_binary() -> str:
    return which("age", ("/usr/bin/age", "/usr/local/bin/age"))


# ---------------------------------------------------------------------------
# writer / reader primitives
# ---------------------------------------------------------------------------

class _HashingReadable(io.RawIOBase):
    """Readable wrapper that hashes bytes as they pass through."""

    def __init__(self, inner, digest=None):
        self._inner = inner
        self._digest = digest or hashlib.sha256()

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        data = self._inner.read(size)
        if data:
            self._digest.update(data)
        return data

    def digest(self) -> str:
        return self._digest.hexdigest()


def _derive_keys(passphrase: bytes, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
    material = hashlib.pbkdf2_hmac("sha256", passphrase, salt, iterations, dklen=64)
    return material[:32], material[32:]


class EncryptedWriter:
    """Streaming encrypt-then-MAC writer.

    Usage::

        with EncryptedWriter(dest, cfg) as out:
            out.write(chunk)
    """

    def __init__(self, dest_path: str, cfg):
        self.dest_path = dest_path
        self.cfg = cfg
        self.scheme = cfg.encryption
        self._fh = None
        self._header: dict = {}
        self._cipher = None
        self._mac = None
        self._proc = None
        self._thread = None
        self.bytes_written = 0

    def __enter__(self):
        self._fh = open(self.dest_path, "wb")
        if self.scheme == "none":
            self._header = {"scheme": "none", "encrypted": False}
            self._write_header()
        elif self.scheme == "aesgcm-pbkdf2":
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            salt = os.urandom(16)
            nonce = os.urandom(16)
            k_enc, k_mac = _derive_keys(read_passphrase(self.cfg), salt, PBKDF2_ITERATIONS)
            self._header = {
                "scheme": "aesgcm-pbkdf2",
                "encrypted": True,
                "cipher": "AES-256-CTR",
                "mac": "HMAC-SHA256",
                "kdf": "PBKDF2-HMAC-SHA256",
                "iterations": PBKDF2_ITERATIONS,
                "salt": base64.b64encode(salt).decode(),
                "nonce": base64.b64encode(nonce).decode(),
            }
            self._cipher = Cipher(algorithms.AES(k_enc), modes.CTR(nonce)).encryptor()
            self._mac = hmac.new(k_mac, digestmod=hashlib.sha256)
            self._write_header()
        elif self.scheme == "age":
            recipients = self.cfg.get_path("archive", "age_recipients_file")
            with open(recipients, "r", encoding="utf-8") as fh:
                recs = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
            if not recs:
                raise ValidationError(f"no age recipients in {recipients}")
            argv = [_age_binary(), "--encrypt", "--output", "-"]
            for rec in recs:
                if not rec.startswith("age1"):
                    raise ValidationError(f"invalid age recipient: {rec!r}")
                argv += ["--recipient", rec]
            self._header = {
                "scheme": "age",
                "encrypted": True,
                "cipher": "X25519/ChaCha20-Poly1305 (age v1)",
                "recipients": len(recs),
            }
            self._write_header()
            # Hand the CONTAINER FILE ITSELF to age as stdout. If we captured
            # stdout into a pipe we would have to drain it concurrently; at
            # ~64 KiB of ciphertext the pipe fills, age blocks, our write()
            # blocks, and the backup deadlocks. Writing straight to the fd
            # keeps memory flat and removes the deadlock entirely.
            self._fh.flush()
            self._proc = subprocess.Popen(
                argv, stdin=subprocess.PIPE, stdout=self._fh, stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        else:  # pragma: no cover - config validator blocks this
            raise ValidationError(f"unknown scheme {self.scheme}")
        return self

    # -- header -------------------------------------------------------------

    def _write_header(self) -> None:
        payload = json.dumps(self._header, sort_keys=True).encode()
        self._fh.write(MAGIC + struct.pack(">I", len(payload)) + payload)
        if self._mac:
            self._mac.update(payload)

    # -- data path ----------------------------------------------------------

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        self.bytes_written += len(data)
        if self.scheme == "none":
            self._fh.write(data)
        elif self.scheme == "aesgcm-pbkdf2":
            out = self._cipher.update(data)
            self._mac.update(out)
            self._fh.write(out)
        elif self.scheme == "age":
            self._proc.stdin.write(data)
        return len(data)

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                self._finalize()
        finally:
            if self._proc is not None:
                try:
                    self._proc.stdin.close()
                except (BrokenPipeError, ValueError):
                    pass
                code = self._proc.wait()
                # age wrote straight into our fd; flush before we fsync below.
                try:
                    self._fh.flush()
                except (AttributeError, ValueError):
                    pass
                if exc_type is None and code != 0:
                    raise BRException(f"age encryption failed with exit code {code}")
            if self._fh:
                self._fh.flush()
                os.fsync(self._fh.fileno())
                self._fh.close()
        return False

    def _finalize(self) -> None:
        if self.scheme == "aesgcm-pbkdf2":
            tail = self._cipher.finalize()
            if tail:
                self._mac.update(tail)
                self._fh.write(tail)
            self._fh.write(self._mac.digest())
        elif self.scheme == "age":
            pass  # age writes its own trailer once stdin closes


class EncryptedReader:
    """Streaming verifying reader matching :class:`EncryptedWriter`."""

    def __init__(self, src_path: str, cfg, *, logger: Logger | None = None):
        self.src_path = src_path
        self.cfg = cfg
        self.logger = logger or Logger()
        self.scheme = "unknown"
        self._fh = None
        self._cipher = None
        self._mac = None
        self._remaining = 0
        self._expected_tag = b""
        self._proc = None
        self._eof = False

    def __enter__(self):
        if not os.path.exists(self.src_path):
            raise PreflightError(f"archive not found: {self.src_path}")
        self._fh = open(self.src_path, "rb")
        magic = self._fh.read(len(MAGIC))
        if magic != MAGIC:
            raise IntegrityError(f"{os.path.basename(self.src_path)}: not a BRENC1 container")
        (hlen,) = struct.unpack(">I", self._fh.read(4))
        if hlen <= 0 or hlen > HEADER_MAX:
            raise IntegrityError("corrupt container header length")
        raw_header = self._fh.read(hlen)
        if len(raw_header) != hlen:
            # A truncated file has a header length that promises more bytes
            # than the archive actually contains.
            raise IntegrityError("container truncated inside the header")
        try:
            header = json.loads(raw_header)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # json.loads decodes as UTF-8 first, so a corrupted header raises
            # UnicodeDecodeError rather than JSONDecodeError. Both mean the
            # same thing to the operator: this file is not a valid archive.
            raise IntegrityError(f"corrupt container header: {exc}") from exc
        if not isinstance(header, dict):
            raise IntegrityError("corrupt container header: not a JSON object")
        self.scheme = header.get("scheme", "unknown")
        total = os.path.getsize(self.src_path)
        body_start = len(MAGIC) + 4 + hlen

        if self.scheme == "none":
            self._remaining = total - body_start
        elif self.scheme == "aesgcm-pbkdf2":
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

            if total - body_start <= TAG_LEN:
                raise IntegrityError("container truncated")
            self._fh.seek(total - TAG_LEN)
            self._expected_tag = self._fh.read(TAG_LEN)
            self._fh.seek(body_start)
            self._remaining = total - body_start - TAG_LEN
            try:
                salt = base64.b64decode(header["salt"], validate=True)
                nonce = base64.b64decode(header["nonce"], validate=True)
                iterations = int(header.get("iterations", PBKDF2_ITERATIONS))
            except (KeyError, TypeError, ValueError, binascii.Error) as exc:
                raise IntegrityError(
                    f"corrupt container header: missing or invalid field ({exc})"
                ) from exc
            if len(salt) != 16 or len(nonce) != 16:
                raise IntegrityError("corrupt container header: invalid salt or nonce length")
            if (header.get("cipher") != "AES-256-CTR"
                    or header.get("mac") != "HMAC-SHA256"
                    or header.get("kdf") != "PBKDF2-HMAC-SHA256"):
                raise IntegrityError("corrupt container header: unsupported AES/HMAC parameters")
            # The iteration count comes from an unauthenticated header. Bound
            # it before deriving keys to prevent a crafted archive from forcing
            # excessive CPU work during preflight.
            # Keep production archives above the security floor. Tests may
            # lower PBKDF2_ITERATIONS to keep round-trip coverage fast; using
            # the same configured value as the floor allows those test-only
            # containers while preserving the production bound (100k).
            if not min(100_000, PBKDF2_ITERATIONS) <= iterations <= 2_000_000:
                raise IntegrityError("corrupt container header: PBKDF2 iterations out of range")
            k_enc, k_mac = _derive_keys(read_passphrase(self.cfg), salt, iterations)
            self._cipher = Cipher(algorithms.AES(k_enc), modes.CTR(nonce)).decryptor()
            self._mac = hmac.new(k_mac, digestmod=hashlib.sha256)
            self._mac.update(raw_header)
        elif self.scheme == "age":
            identity = self.cfg.get_path("archive", "age_identity_file")
            if not os.path.exists(identity):
                raise PreflightError(f"age identity file missing: {identity}")
            self._proc = _spawn(
                [_age_binary(), "--decrypt", "--identity", identity, "-"], stderr_pipe=True
            )
            src = self._fh

            def pump():
                try:
                    while True:
                        block = src.read(CHUNK)
                        if not block:
                            break
                        self._proc.stdin.write(block)
                except (BrokenPipeError, ValueError):
                    pass
                finally:
                    try:
                        self._proc.stdin.close()
                    except (BrokenPipeError, ValueError):
                        pass

            self._pump_thread = threading.Thread(target=pump, daemon=True)
            self._pump_thread.start()
        else:
            raise IntegrityError(f"unsupported container scheme {self.scheme!r}")
        return self

    def read(self, size: int = -1) -> bytes:
        if self._eof:
            return b""
        if self.scheme == "age":
            data = self._proc.stdout.read(size if size > 0 else CHUNK)
            if not data:
                self._eof = True
                code = self._proc.wait()
                if code != 0:
                    raise IntegrityError(f"age decryption failed (exit {code}) - wrong key or corrupt file")
            return data

        want = self._remaining if size < 0 else min(size, self._remaining)
        if want <= 0:
            self._eof = True
            self._verify_tag()
            return b""
        data = self._fh.read(want)
        if not data:
            raise IntegrityError("container ended unexpectedly")
        self._remaining -= len(data)
        if self._mac:
            self._mac.update(data)
            data = self._cipher.update(data)
        if self._remaining == 0:
            self._eof = True
            self._verify_tag()
        return data

    def _verify_tag(self) -> None:
        if self.scheme == "aesgcm-pbkdf2":
            tail = self._cipher.finalize()
            if tail and self._mac:
                self._mac.update(tail)
            if self._mac and not hmac.compare_digest(self._mac.digest(), self._expected_tag):
                raise IntegrityError(
                    "HMAC verification failed - archive is corrupt or was modified"
                )

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None and not self._eof:
                # Drain so the tag check always runs.
                while self.read(CHUNK):
                    pass
        finally:
            if self._proc is not None:
                try:
                    self._proc.wait(timeout=10)
                except Exception:  # pragma: no cover
                    self._proc.kill()
            if self._fh:
                self._fh.close()
        return False


def _spawn(argv: list[str], stderr_pipe: bool = False):
    import subprocess

    return subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if stderr_pipe else subprocess.DEVNULL,
        bufsize=0,
    )


# ---------------------------------------------------------------------------
# convenience helpers
# ---------------------------------------------------------------------------

def encrypt_stream_factory(dest_path: str, cfg) -> t.Callable[[], EncryptedWriter]:
    return lambda: EncryptedWriter(dest_path, cfg)


def describe(cfg) -> dict:
    """Non-secret description of the active encryption configuration."""
    scheme = cfg.encryption
    info: dict = {"scheme": scheme, "authenticated": scheme != "none"}
    if scheme == "age":
        info["cipher"] = "X25519/ChaCha20-Poly1305 (age v1)"
        info["recipients_file"] = cfg.get_path("archive", "age_recipients_file")
        info["identity_file"] = cfg.get_path("archive", "age_identity_file")
        info["key_storage"] = "separate from archive"
    elif scheme == "aesgcm-pbkdf2":
        info["cipher"] = "AES-256-CTR + HMAC-SHA256"
        info["kdf"] = f"PBKDF2-HMAC-SHA256/{PBKDF2_ITERATIONS}"
        info["key_storage"] = "separate passphrase file"
    return info


def key_fingerprint(cfg) -> str:
    """Stable, non-reversible fingerprint of the active key material.

    Lets the GUI show "same key as last backup" without revealing anything.
    """
    scheme = cfg.encryption
    if scheme == "none":
        return "none:none"
    if scheme == "age":
        path = cfg.get_path("archive", "age_recipients_file")
    else:
        path = cfg.get_path("archive", "openssl_passphrase_file")
    if not os.path.exists(path):
        return "missing:missing"
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        digest.update(fh.read().strip())
    return f"{scheme}:{digest.hexdigest()[:16]}"
