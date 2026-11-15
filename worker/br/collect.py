"""Filesystem collection: application, RRD, system configuration, integrations.

Every collector returns a small dict describing what it did, which ends up in
the manifest.  Collectors are read-only against the LibreNMS host.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import typing as t

from .common import Logger, ValidationError, du_bytes, validate_abs_path
from .archive import ArchiveWriter

#: Patterns whose *content* is scrubbed from configuration copies.
# Matches `KEY=value`, `KEY = value`, `KEY: value` and `"KEY": "value"` alike.
# Config files in the wild use all four, and a scrubber that only understands
# `=` leaves credentials in the archive.
SECRET_LINE_RE = re.compile(
    r"""^(\s*["']?)                                     # indentation / quote
        (DB_PASSWORD|DB_PASS|APP_KEY|MAIL_PASSWORD|REDIS_PASSWORD|
         SOCIALITE_.*_SECRET|.*_TOKEN|.*_SECRET|.*_PASSWORD|.*_PASSWD|
         .*api_key|api_secret|secret|password)           # key
        (["']?\s*[=:]\s*)                               # separator (= or :)
        (.*)$                                            # value
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: System paths that are captured for reference but never auto-restored.
MACHINE_SPECIFIC = (
    re.compile(r"^etc/netplan/"),
    re.compile(r"^etc/network/interfaces"),
    re.compile(r"^etc/hostname$"),
    re.compile(r"^etc/hosts$"),
    re.compile(r"^etc/machine-id$"),
    re.compile(r"^etc/resolv\.conf$"),
    re.compile(r"^etc/fstab$"),
    re.compile(r"^etc/ssh/ssh_host_"),
    re.compile(r"^etc/cloud/"),
)

DEFAULT_EXCLUDES = (
    re.compile(r"(^|/)\.git(/|$)"),
    re.compile(r"(^|/)node_modules(/|$)"),
    re.compile(r"(^|/)vendor(/|$)"),
    re.compile(r"\.pid$"),
    re.compile(r"\.sock$"),
    re.compile(r"(^|/)\.cache(/|$)"),
    re.compile(r"storage/logs/.*\.log$"),
    # LibreNMS writes its own log to <root>/logs/, which is equally noisy and
    # equally machine-specific; it must not land in a backup.
    re.compile(r"(^|/)logs/.*\.log$"),
    re.compile(r"(^|/)bootstrap/cache/"),
    re.compile(r"storage/framework/(cache|sessions|views)/"),
    re.compile(r"rrd/.*\.rrd$") ,  # RRD is a separate component
    re.compile(r"(^|/)tmp(/|$)"),
)


def is_machine_specific(rel: str) -> bool:
    return any(rx.search(rel) for rx in MACHINE_SPECIFIC)


def should_exclude(rel: str, extra: t.Sequence[str] = ()) -> bool:
    if any(rx.search(rel) for rx in DEFAULT_EXCLUDES):
        return True
    for pattern in extra:
        pattern = pattern.strip()
        if not pattern:
            continue
        if pattern.startswith("**/") and rel.endswith(pattern[3:].lstrip("*")):
            return True
        if rel.startswith(pattern.lstrip("./")):
            return True
    return False


def scrub_text(data: bytes) -> tuple[bytes, int]:
    """Replace secret values with a marker, keeping structure intact.

    The original config.php/.env are ALSO stored encrypted inside the archive;
    the scrubbed copy is what the GUI may safely display.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, 0
    changed = 0
    out_lines = []
    for line in text.splitlines(keepends=True):
        match = SECRET_LINE_RE.match(line.rstrip("\r\n"))
        if match and match.group(4).strip():
            eol = "\r\n" if line.endswith("\r\n") else "\n"
            out_lines.append(f"{match.group(1)}{match.group(2)}{match.group(3)}__REDACTED__{eol}")
            changed += 1
        else:
            out_lines.append(line)
    return "".join(out_lines).encode("utf-8"), changed


# ---------------------------------------------------------------------------
# Application component
# ---------------------------------------------------------------------------

def collect_application(cfg, writer: ArchiveWriter, logger: Logger) -> dict:
    root = cfg.librenms_root
    paths = cfg.get_list("components", "application_paths", "")
    collected: list[dict] = []
    redacted_total = 0

    for rel in paths:
        rel = rel.strip().lstrip("/")
        if not rel:
            continue
        full = os.path.join(root, rel)
        if not os.path.exists(full):
            logger.warn("application path missing (skipped)", path=rel)
            continue
        if os.path.isfile(full):
            data = open(full, "rb").read()
            scrubbed, changed = scrub_text(data)
            redacted_total += changed
            writer.add_bytes(f"application/{rel}", data)
            if changed:
                # Keep a display-safe copy under a clearly separate name.
                writer.add_bytes(f"application-redacted/{rel}", scrubbed)
            collected.append({"path": rel, "bytes": len(data), "kind": "file"})
        else:
            n, size = add_tree(writer, full, f"application/{rel}", logger,
                               extra_excludes=cfg.get_list("components", "system_excludes"))
            collected.append({"path": rel, "bytes": size, "kind": "tree", "files": n})

    # Version metadata so restore can detect incompatibilities.
    writer.add_json("application/version.json", version_info(cfg, logger))
    logger.info("application component collected", entries=len(collected), redacted=redacted_total)
    return {"entries": collected, "redacted_lines": redacted_total}


# ---------------------------------------------------------------------------
# RRD component
# ---------------------------------------------------------------------------

def collect_rrd(cfg, writer: ArchiveWriter, logger: Logger) -> dict:
    root = cfg.librenms_root
    summary: list[dict] = []
    total = 0
    for rel in cfg.get_list("components", "rrd_paths", "rrd"):
        full = os.path.join(root, rel.strip().lstrip("/"))
        if not os.path.isdir(full):
            logger.warn("rrd path missing (skipped)", path=rel)
            continue
        count, size = add_tree(writer, full, f"rrd/{rel.strip().lstrip('/')}", logger,
                               extra_excludes=[r"\.lock$"])
        summary.append({"path": rel, "files": count, "bytes": size})
        total += size
    logger.info("rrd component collected", files=sum(s["files"] for s in summary), bytes=total)
    return {"paths": summary}


# ---------------------------------------------------------------------------
# System configuration component
# ---------------------------------------------------------------------------

def collect_system(cfg, writer: ArchiveWriter, logger: Logger) -> dict:
    extra_excludes = cfg.get_list("components", "system_excludes")
    summary: list[dict] = []
    portable: list[str] = []
    reference_only: list[str] = []

    for path in cfg.get_list("components", "system_paths"):
        try:
            validate_abs_path(path)
        except ValidationError as exc:
            logger.warn("refusing to collect system path", path=path, reason=str(exc))
            continue
        if not os.path.exists(path):
            logger.warn("system path missing (skipped)", path=path)
            continue
        rel = path.lstrip("/")
        if rel.count("/") > 6:
            logger.warn("system path too deep, skipped", path=path)
            continue
        dest = f"system/{rel}"
        if os.path.isdir(path):
            count, size = add_tree(writer, path, dest, logger, extra_excludes=extra_excludes)
            summary.append({"path": path, "files": count, "bytes": size})
        else:
            writer.add_file(path, dest)
            summary.append({"path": path, "files": 1, "bytes": os.path.getsize(path)})

        (reference_only if is_machine_specific(rel) else portable).append(rel)

    # Package inventory makes "restore to a new EC2 instance" reproducible.
    writer.add_json("system/packages.json", package_inventory())
    writer.add_json("system/os-release.json", os_info())
    portability = {
        "portable": sorted(portable),
        "reference_only": sorted(reference_only),
        "note": (
            "reference_only paths are captured for audit/diff purposes and are "
            "NEVER restored automatically onto another host (network identity, "
            "host keys, machine-id, fstab)."
        ),
    }
    writer.add_json("system/portability.json", portability)
    logger.info("system component collected", paths=len(summary))
    return {"paths": summary, "portability": portability}


# ---------------------------------------------------------------------------
# Integrations component
# ---------------------------------------------------------------------------

def collect_integrations(cfg, writer: ArchiveWriter, logger: Logger) -> dict:
    summary: list[dict] = []
    for path in cfg.get_list("components", "integration_paths"):
        try:
            validate_abs_path(path)
        except ValidationError as exc:
            logger.warn("refusing integration path", path=path, reason=str(exc))
            continue
        if not os.path.isdir(path):
            logger.warn("integration path missing (skipped)", path=path)
            continue
        count, size = add_tree(writer, path, f"integrations{path}", logger,
                               extra_excludes=[r"\.pyc$", r"__pycache__"])
        summary.append({"path": path, "files": count, "bytes": size})
    logger.info("integrations component collected", paths=len(summary))
    return {"paths": summary}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def add_tree(writer: ArchiveWriter, src_root: str, dest_root: str, logger: Logger,
             *, extra_excludes: t.Sequence[str] = ()) -> tuple[int, int]:
    """Recursively add ``src_root`` under ``dest_root``. Returns (files, bytes)."""
    files = 0
    size = 0
    for dirpath, dirnames, filenames in os.walk(src_root, followlinks=False):
        dirnames.sort()
        rel_dir = os.path.relpath(dirpath, src_root)
        rel_dir = "" if rel_dir == "." else rel_dir.replace(os.sep, "/")
        keep = []
        for name in dirnames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if os.path.islink(os.path.join(dirpath, name)):
                continue
            if should_exclude(rel + "/", extra_excludes):
                continue
            keep.append(name)
        dirnames[:] = keep

        for name in sorted(filenames):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if should_exclude(rel, extra_excludes):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not os.path.isfile(full):
                continue
            try:
                writer.add_file(full, f"{dest_root}/{rel}")
            except (ValidationError, OSError) as exc:
                logger.warn("file skipped", path=rel, reason=str(exc))
                continue
            files += 1
            size += st.st_size
            if files % 5000 == 0:
                logger.info("collecting files", files=files, bytes=size)
    return files, size


def version_info(cfg, logger: Logger) -> dict:
    """Best-effort version capture, never fatal."""
    info: dict = {"os": os_info()}
    try:
        import subprocess

        root = cfg.librenms_root
        proc = subprocess.run(
            ["/usr/bin/git", "-C", root, "describe", "--tags", "--always"],
            capture_output=True, check=False, timeout=20,
        )
        if proc.returncode == 0:
            info["librenms_git_describe"] = proc.stdout.decode().strip()
        commit = subprocess.run(
            ["/usr/bin/git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, check=False, timeout=20,
        )
        if commit.returncode == 0:
            info["librenms_git_commit"] = commit.stdout.decode().strip()
    except Exception as exc:  # pragma: no cover
        logger.warn("could not determine LibreNMS version from git", error=str(exc))

    release_file = os.path.join(cfg.librenms_root, "LibreNMS/Version.php")
    if os.path.exists(release_file):
        try:
            text = open(release_file, encoding="utf-8").read()
            match = re.search(r"const\s+VERSION\s*=\s*'([^']+)'", text)
            if match:
                info["librenms_version"] = match.group(1)
        except OSError:
            pass
    return info


def os_info() -> dict:
    info = {
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                if "=" in line:
                    key, _, value = line.strip().partition("=")
                    if key in ("PRETTY_NAME", "VERSION_ID", "ID", "VERSION"):
                        info[f"os_{key.lower()}"] = value.strip('"')
    except OSError:
        pass
    return info


def package_inventory() -> dict:
    """dpkg inventory - used to rebuild an identical host."""
    import subprocess

    try:
        proc = subprocess.run(["/usr/bin/dpkg-query", "-W", "-f=${Package}\\t${Version}\\n"],
                              capture_output=True, check=False, timeout=60)
        if proc.returncode != 0:
            return {"available": False}
        packages = {}
        for line in proc.stdout.decode(errors="replace").splitlines():
            if "\t" in line:
                name, ver = line.split("\t", 1)
                packages[name] = ver
        return {"available": True, "count": len(packages), "packages": packages}
    except Exception:
        return {"available": False}
