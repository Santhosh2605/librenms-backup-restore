"""SHA-256 checksum manifests for backup archives."""

from __future__ import annotations

import hashlib
import os
import typing as t

import re

from .common import CHECKSUM_NAME, CHUNK, IntegrityError, ValidationError

#: Members excluded from checksums.sha256 (it cannot contain itself).
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

EXCLUDED = {CHECKSUM_NAME, f"./{CHECKSUM_NAME}"}


def build(root: str, output: str | None = None, *, logger=None) -> str:
    """Hash every regular file under ``root`` into a GNU-style checksum file.

    Returns the path of the generated file.
    """
    output = output or os.path.join(root, CHECKSUM_NAME)
    lines: list[str] = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel in EXCLUDED or rel.startswith("../"):
                continue
            if os.path.islink(full):
                target = os.readlink(full)
                lines.append(f"symlink  {rel}  -> {target}")
                continue
            if not os.path.isfile(full):
                continue
            lines.append(f"{_sha256(full)}  {rel}")
            count += 1
            if logger and count % 2000 == 0:
                logger.info("checksums: hashed files", files=count)

    tmp = output + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, output)
    if logger:
        logger.info("checksums written", entries=len(lines), path=os.path.basename(output))
    return output


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def parse(path: str) -> dict[str, str]:
    """Parse a checksum file into ``{relative_path: sha256}``."""
    if not os.path.exists(path):
        raise ValidationError(f"checksum file missing: {path}")
    entries: dict[str, str] = {}
    # errors="replace" rather than strict UTF-8: a corrupted checksum file is
    # an integrity problem to report, not a decoding exception to leak. The
    # resulting line simply fails the split below and raises IntegrityError.
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            if not raw.strip():
                continue
            if raw.startswith("symlink  "):
                continue
            try:
                digest, rel = raw.split("  ", 1)
            except ValueError as exc:
                raise IntegrityError(f"malformed checksum line: {raw[:120]!r}") from exc
            rel = rel.lstrip("./")
            if rel in EXCLUDED:
                continue
            if not HEX64_RE.match(digest):
                raise IntegrityError(f"malformed checksum digest: {raw[:120]!r}")
            entries[rel] = digest
    return entries


def verify_tree(root: str, checksum_path: str | None = None) -> dict[str, t.Any]:
    """Verify an unpacked tree against its checksum file."""
    checksum_path = checksum_path or os.path.join(root, CHECKSUM_NAME)
    expected = parse(checksum_path)
    missing: list[str] = []
    mismatched: list[str] = []
    for rel, digest in expected.items():
        full = os.path.join(root, rel)
        if not os.path.isfile(full):
            missing.append(rel)
            continue
        if _sha256(full) != digest:
            mismatched.append(rel)

    # Files present on disk but absent from the checksum file are reported too.
    # They are not a verification failure on their own (a restore may add
    # runtime files) but they must surface, not stay invisible.
    unexpected: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel in EXCLUDED or rel.startswith("../"):
                continue
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            if rel not in expected:
                unexpected.append(rel)

    return {
        "checked": len(expected),
        "missing": missing[:50],
        "mismatched": mismatched[:50],
        "unexpected": unexpected[:50],
        "unexpected_count": len(unexpected),
        "ok": not missing and not mismatched,
    }


def verify_member(rel: str, data_hash: str, expected: dict[str, str]) -> bool:
    want = expected.get(rel.lstrip("./"))
    return want is not None and want == data_hash
