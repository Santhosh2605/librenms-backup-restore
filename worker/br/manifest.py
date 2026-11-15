"""Backup manifest construction and validation.

The manifest is the contract between backup and restore.  It is written
*into* the archive as ``manifest.json`` and a copy is kept outside the archive
(``<archive>.manifest.json``) for fast GUI display - the outside copy is
tamper-evident because the restore path always re-validates the inside copy
against ``checksums.sha256``.
"""

from __future__ import annotations

import os
import typing as t

from . import common
from .common import IntegrityError, ValidationError

CURRENT_SCHEMA = 1

#: Manifest fields that must exist for a restore to be attempted.
REQUIRED_FIELDS = (
    "manifest_schema", "backup_id", "created_at", "components",
    "archive", "encryption", "versions", "checksums",
)

#: Major versions we know how to restore from.
SUPPORTED_SCHEMA = (1,)


def build(
    *,
    backup_id: str,
    backup_type: str,
    cfg,
    components: dict,
    versions: dict,
    collection: dict,
    checksum_sha256: str,
    archive_path: str,
    encryption_info: dict,
    key_fp: str,
    logger=None,
) -> dict:
    archive_size = os.path.getsize(archive_path) if os.path.exists(archive_path) else 0
    manifest = {
        "manifest_schema": CURRENT_SCHEMA,
        "product": "librenms-backup-restore",
        "worker_version": common.VERSION,
        "backup_id": backup_id,
        "backup_type": backup_type,
        "created_at": common.utc_now(),
        "host": {
            "hostname": _hostname(),
            "librenms_root": cfg.librenms_root,
        },
        "versions": versions,
        "components": components,
        "collection": collection,
        "archive": {
            "file": os.path.basename(archive_path),
            "size_bytes": archive_size,
            "size_human": common.human(archive_size),
            "compression": cfg.get("archive", "compression", "zstd"),
            "container": "BRENC1",
            "inner_format": "POSIX tar (PAX)",
        },
        "encryption": encryption_info,
        "key_fingerprint": key_fp,
        "checksums": {
            "algorithm": "sha256",
            "entry_count": len(collection.get("file_hashes", {})),
            # The archive's own hash cannot be known before the container is
            # closed, so it is recorded in <archive>.sha256 sidecar, in the
            # DB row and in backup-metadata.json. Inside the archive the
            # per-member checksums in checksums.sha256 are authoritative.
            "archive_sha256": None,
            "sidecar": os.path.basename(archive_path) + ".sha256",
        },
        "restore_dependencies": {
            "php": versions.get("php", ""),
            "mariadb": versions.get("mariadb", ""),
            "os": versions.get("os", {}).get("os_pretty_name", ""),
            "python": versions.get("python", ""),
            "encryption": encryption_info.get("scheme"),
            "notes": [
                "Restore requires the same encryption scheme and key material.",
                "Database restore recreates the schema from the dump.",
                "Machine-specific network configuration is NOT restored automatically.",
            ],
        },
    }
    return manifest


def validate(manifest: t.Any, *, strict: bool = True) -> dict:
    """Validate structure and refuse archives from incompatible versions."""
    if not isinstance(manifest, dict):
        raise IntegrityError("manifest.json is not a JSON object")

    missing = [k for k in REQUIRED_FIELDS if k not in manifest]
    if missing:
        raise IntegrityError(f"manifest.json missing required fields: {missing}")

    schema = manifest.get("manifest_schema")
    if schema not in SUPPORTED_SCHEMA:
        raise ValidationError(
            f"unsupported manifest schema {schema!r}; this worker supports {SUPPORTED_SCHEMA}"
        )

    bid = manifest.get("backup_id")
    if not isinstance(bid, str) or not common.BACKUP_ID_RE.match(bid):
        raise IntegrityError(f"manifest backup_id is malformed: {bid!r}")

    comps = manifest.get("components")
    if not isinstance(comps, dict) or not comps:
        raise IntegrityError("manifest components missing")
    for key, value in comps.items():
        if key not in common.COMPONENTS:
            raise IntegrityError(f"manifest lists unknown component {key!r}")
        if not isinstance(value, bool):
            raise IntegrityError(f"manifest component {key!r} must be boolean")

    enc = manifest.get("encryption")
    if not isinstance(enc, dict) or "scheme" not in enc:
        raise IntegrityError("manifest encryption block missing")

    checks = manifest.get("checksums")
    if not isinstance(checks, dict) or "algorithm" not in checks:
        raise IntegrityError("manifest checksums block missing")
    if checks["algorithm"] != "sha256":
        raise IntegrityError(f"unsupported checksum algorithm {checks['algorithm']!r}")

    if strict:
        arch = manifest.get("archive")
        if not isinstance(arch, dict) or "size_bytes" not in arch:
            raise IntegrityError("manifest archive block missing")

    return manifest


def component_list(components: dict) -> list[str]:
    return sorted([k for k, v in components.items() if v])


def _hostname() -> str:
    try:
        import socket

        return socket.gethostname()
    except Exception:  # pragma: no cover
        return "unknown"
