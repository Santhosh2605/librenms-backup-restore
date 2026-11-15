"""Deterministic, streaming tar writer/reader.

Why not ``tarfile`` directly?  Backup archives must be *reproducible enough*
to allow member-by-member verification, and restore must be able to stream a
single member out of a (possibly multi-GiB) archive without unpacking
everything.  We therefore write a plain POSIX ustar archive with:

  * names stored relative to the archive root, forward slashes only
  * owner/group recorded as ``librenms`` uid/gid but restore re-applies
    ownership explicitly (never trusting archive metadata)
  * no device nodes, no setuid/setgid bits, no absolute or ``..`` names
  * a strict ``members()`` reader that refuses anything suspicious
"""

from __future__ import annotations

import os
import tarfile
import typing as t

from .common import IntegrityError, ValidationError

#: Members we refuse to restore from an archive, whatever it claims.
FORBIDDEN_PREFIXES = ("/", "../", "..\\")
FORBIDDEN_NAMES = {".", ".."}


class ArchiveWriter:
    """Write a tar stream to ``fileobj`` (any binary writable)."""

    def __init__(self, fileobj, *, arcroot: str = "librenms-backup"):
        self.arcroot = arcroot.strip("/")
        self._tar = tarfile.open(fileobj=fileobj, mode="w|", format=tarfile.PAX_FORMAT)
        self.bytes_written = 0
        self.files = 0
        #: rel -> sha256, populated while writing (single read per file).
        self.hashes: dict[str, str] = {}
        self.sizes: dict[str, int] = {}

    def __enter__(self) -> "ArchiveWriter":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._tar.close()
        except Exception:
            if exc_type is None:
                raise
        return False

    # -- public API ---------------------------------------------------------

    def add_file(self, source: str, dest_rel: str) -> None:
        """Add one regular file. ``dest_rel`` is relative to the archive root."""
        self._validate(dest_rel)
        if not os.path.isfile(source):
            raise ValidationError(f"not a regular file: {source}")
        if os.path.islink(source):
            target = os.readlink(source)
            self.add_symlink(target, dest_rel)
            return
        info = self._tar.gettarinfo(source, arcname=self._member(dest_rel))
        self._sanitise(info)
        from .crypto import _HashingReadable

        with open(source, "rb") as fh:
            hashing = _HashingReadable(fh)
            self._tar.addfile(info, hashing)
            self.hashes[dest_rel] = hashing.digest()
            self.sizes[dest_rel] = info.size
        self.files += 1

    def add_symlink(self, target: str, dest_rel: str) -> None:
        self._validate(dest_rel)
        if target.startswith("/") or ".." in target.split("/"):
            # Absolute/escaping symlinks are recorded as a skipped notice only.
            self.add_bytes(dest_rel + ".symlink", f"skipped-symlink -> {target}\n".encode())
            return
        info = tarfile.TarInfo(self._member(dest_rel))
        info.type = tarfile.SYMTYPE
        info.linkname = target
        info.mode = 0o777
        self._tar.addfile(info)
        self.files += 1

    def add_bytes(self, dest_rel: str, data: bytes) -> None:
        self._validate(dest_rel)
        info = tarfile.TarInfo(self._member(dest_rel))
        info.size = len(data)
        info.mode = 0o600
        import hashlib
        import io

        self._tar.addfile(info, io.BytesIO(data))
        self.hashes[dest_rel] = hashlib.sha256(data).hexdigest()
        self.sizes[dest_rel] = len(data)
        self.files += 1

    def add_directory(self, dest_rel: str) -> None:
        self._validate(dest_rel)
        info = tarfile.TarInfo(self._member(dest_rel))
        info.type = tarfile.DIRTYPE
        info.mode = 0o750
        self._tar.addfile(info)

    def add_json(self, dest_rel: str, data: t.Any) -> None:
        import json

        self.add_bytes(dest_rel, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode())

    # -- internals ----------------------------------------------------------

    def _member(self, rel: str) -> str:
        return f"{self.arcroot}/{rel}" if self.arcroot else rel

    @staticmethod
    def _validate(rel: str) -> None:
        if not rel or rel.startswith("/") or "\\" in rel:
            raise ValidationError(f"illegal archive member name: {rel!r}")
        parts = rel.split("/")
        if any(p in FORBIDDEN_NAMES for p in parts):
            raise ValidationError(f"path traversal in archive member: {rel!r}")
        if ".." in parts:
            raise ValidationError(f"path traversal in archive member: {rel!r}")

    @staticmethod
    def _sanitise(info: tarfile.TarInfo) -> None:
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        info.mode = info.mode & 0o755  # strip setuid/setgid/sticky
        info.pax_headers = {}


class ArchiveReader:
    """Streaming reader with hard safety checks on every member."""

    def __init__(self, fileobj, *, expected_root: str | None = "librenms-backup"):
        self._tar = tarfile.open(fileobj=fileobj, mode="r|")
        self.expected_root = expected_root
        self.count = 0

    def __enter__(self) -> "ArchiveReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._tar.close()
        return False

    def members(self) -> t.Iterator[tarfile.TarInfo]:
        for info in self._tar:
            self._check(info)
            self.count += 1
            yield info

    def _check(self, info: tarfile.TarInfo) -> None:
        name = info.name
        if name.startswith("/") or "\\" in name:
            raise IntegrityError(f"archive member has absolute/backslash name: {name!r}")
        parts = name.split("/")
        if any(p in FORBIDDEN_NAMES for p in parts):
            raise IntegrityError(f"archive member escapes root: {name!r}")
        if self.expected_root and parts[0] != self.expected_root:
            raise IntegrityError(
                f"unexpected archive root {parts[0]!r} (expected {self.expected_root!r})"
            )
        if info.isdev() or info.isfifo():
            raise IntegrityError(f"archive contains device/fifo node: {name!r}")
        if info.issym():
            target = info.linkname
            if target.startswith("/") or ".." in target.split("/"):
                raise IntegrityError(f"archive symlink escapes root: {name!r} -> {target!r}")

    def relname(self, info: tarfile.TarInfo) -> str:
        parts = info.name.split("/")
        return "/".join(parts[1:]) if len(parts) > 1 else ""

    def read_member(self, info: tarfile.TarInfo) -> bytes:
        fh = self._tar.extractfile(info)
        if fh is None:
            return b""
        return fh.read()


def extract_single(archive_path: str, member: str, dest_path: str, *,
                   expected_root: str = "librenms-backup") -> bool:
    """Extract exactly one member (used to pull manifest.json out early)."""
    with open(archive_path, "rb") as fh, ArchiveReader(fh, expected_root=expected_root) as reader:
        for info in reader.members():
            if reader.relname(info) == member and info.isfile():
                data = reader.read_member(info)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                with open(dest_path, "wb") as out:
                    out.write(data)
                return True
    return False
