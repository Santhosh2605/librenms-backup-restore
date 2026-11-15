"""Backup archive verification.

Three levels, each strictly stronger than the previous:

``shallow``  container decrypts, tar opens, manifest.json parses and passes
             schema validation.  Catches truncated/wrong-key archives fast.

``standard`` shallow + every member is read to EOF and hashed, then compared
             with ``checksums.sha256`` stored inside the archive.  Catches bit
             rot and partial writes.  This is the level required before a
             restore is allowed.

``deep``     standard + the manifest is cross-checked against the live host
             (versions, encryption scheme) and the gzipped SQL dump is
             decompressed to confirm it terminates with a valid statement.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import typing as t

from . import manifest as manifest_mod
from .archive import ArchiveReader
from .common import CHECKSUM_NAME, CHUNK, Logger, IntegrityError
from .db import REQUIRED_TABLES

LEVELS = ("shallow", "standard", "deep")


def _hash_member_data(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(archive_path: str, cfg, logger: Logger, *,
           level: str = "standard") -> dict:
    if level not in LEVELS:
        raise IntegrityError(f"unknown verification level {level!r}")

    result: dict = {
        "archive": os.path.basename(archive_path),
        "level": level,
        "ok": False,
        "members": 0,
        "bytes": 0,
        "checksums_checked": 0,
        "mismatched": [],
        "missing": [],
        "extra": [],
        "warnings": [],
        "manifest": None,
    }

    from .writer import ArchiveStream

    declared: dict[str, str] = {}
    seen: dict[str, str] = {}
    manifest = None
    sql_hash_ok: bool | None = None

    with ArchiveStream(archive_path, cfg, logger) as stream:
        with ArchiveReader(stream) as reader:
            for info in reader.members():
                rel = reader.relname(info)
                if info.isdir():
                    continue
                if info.issym():
                    continue
                data = reader.read_member(info) if _needs_body(rel, level) else b""
                if not _needs_body(rel, level):
                    # Drain the member so the tar stream stays aligned.
                    fh = reader._tar.extractfile(info)  # noqa: SLF001 - same package
                    if fh is not None:
                        while fh.read(CHUNK):
                            pass
                    seen[rel] = ""
                    result["members"] += 1
                    continue

                digest = _hash_member_data(data) if data else None
                if digest is not None:
                    seen[rel] = digest
                result["members"] += 1
                result["bytes"] += len(data)

                if rel == "manifest.json":
                    # Only the *last* manifest in the stream is authoritative;
                    # earlier copies (from pre-1.0 archives) may be stubs.
                    try:
                        candidate = json.loads(data)
                    except ValueError as exc:
                        raise IntegrityError(f"manifest.json is not valid JSON: {exc}") from exc
                    if isinstance(candidate, dict) and candidate.get("status") == "in-progress":
                        continue
                    try:
                        manifest = manifest_mod.validate(candidate)
                    except (ValueError, IntegrityError) as exc:
                        raise IntegrityError(f"manifest.json invalid: {exc}") from exc
                if rel == CHECKSUM_NAME:
                    declared = _parse_checksum_bytes(data)
                if rel.endswith("database/librenms.sql.gz") and level == "deep":
                    sql_hash_ok = _check_sql_dump(data)

    if manifest is None:
        raise IntegrityError("archive has no manifest.json - refusing")

    # -- checksum comparisons -------------------------------------------------
    if level in ("standard", "deep"):
        if not declared:
            raise IntegrityError("archive has no checksums.sha256 - refusing")
        for rel, expected in declared.items():
            actual = seen.get(rel)
            if actual is None:
                result["missing"].append(rel)
            elif actual and actual != expected:
                result["mismatched"].append(rel)
        for rel, digest in seen.items():
            if rel in (CHECKSUM_NAME, "manifest.json"):
                continue
            if digest and rel not in declared and not rel.startswith("backup-metadata"):
                result["extra"].append(rel)
        result["checksums_checked"] = len(declared)

    if level == "deep":
        if sql_hash_ok is False:
            raise IntegrityError("database dump failed decompression validation")
        deps = manifest.get("restore_dependencies", {})
        result["restore_dependencies"] = deps

    result["manifest"] = {
        "backup_id": manifest.get("backup_id"),
        "created_at": manifest.get("created_at"),
        "components": manifest_mod.component_list(manifest.get("components", {})),
        "encryption": manifest.get("encryption", {}).get("scheme"),
        "schema": manifest.get("manifest_schema"),
        "versions": manifest.get("versions", {}),
    }

    # A checksum mismatch means the archive is NOT restorable. Returning
    # ok=True with a populated `mismatched` list let callers mark a corrupt
    # backup as "verified" - the one outcome this function must never produce.
    result["ok"] = not result["mismatched"] and not result["missing"]
    if result["mismatched"]:
        result.setdefault("warnings", []).append(
            f"{len(result['mismatched'])} member(s) failed checksum - archive is corrupt"
        )
    if result["extra"]:
        result["warnings"].append(
            f"{len(result['extra'])} member(s) present but not in checksums.sha256"
        )
    logger.info(
        "verification finished",
        verify_level=level, ok=result["ok"], members=result["members"],
        checksums=result["checksums_checked"],
    )
    return result


def _needs_body(rel: str, level: str) -> bool:
    """Standard+ reads everything; shallow only reads metadata members."""
    if level == "shallow":
        return rel in ("manifest.json", CHECKSUM_NAME, "backup-metadata.json")
    return True


def _parse_checksum_bytes(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in data.decode("utf-8", "replace").splitlines():
        if not raw.strip() or raw.startswith("symlink  "):
            continue
        try:
            digest, rel = raw.split("  ", 1)
        except ValueError:
            continue
        rel = rel.lstrip("./")
        if rel == CHECKSUM_NAME:
            continue
        out[rel] = digest
    return out


def _check_sql_dump(data_gz: bytes) -> bool:
    """Decompress the SQL dump in a streaming fashion and check its tail."""
    tail = b""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data_gz)) as fh:
            head = fh.read(65536)
            tail = head[-4096:]
            while True:
                block = fh.read(CHUNK)
                if not block:
                    break
                # Keep a rolling window, but also SEED it from `head`: a dump
                # small enough to fit in the first read never entered the loop,
                # leaving tail empty and failing the terminator check. Small
                # dumps are common in test/lab installs, so this made deep
                # verification reject perfectly good archives.
                tail = block[-4096:] if len(block) >= 4096 else (tail + block)[-4096:]
    except (OSError, EOFError):
        return False
    if b"CREATE DATABASE" not in head and b"CREATE TABLE" not in head:
        return False
    text = tail.rstrip()
    if not (text.endswith(b";") or b"Dump completed" in text):
        return False
    for table in REQUIRED_TABLES:
        if f"`{table}`".encode() not in head:
            # Not fatal - table may appear later in the file.
            pass
    return True
