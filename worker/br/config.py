"""Configuration loading and validation for LibreNMS Backup & Restore."""

from __future__ import annotations

import configparser
import os
import re
import typing as t

from .common import PreflightError, ValidationError, validate_abs_path

DEFAULT_CONF = "/etc/librenms-backup/br.conf"

BOOL_TRUE = {"1", "true", "yes", "on"}
BOOL_FALSE = {"0", "false", "no", "off"}


class Config:
    """Typed, read-only view over ``br.conf``.

    Every setter-ish operation is validated at load time so the worker can
    assume data is sane.  Unknown keys raise, which catches typos during
    upgrades instead of silently ignoring a security-relevant setting.
    """

    def __init__(self, path: str = DEFAULT_CONF):
        self.path = path
        self._cp = configparser.ConfigParser(interpolation=None, strict=False)
        self._cp.optionxform = str  # keep keys case-sensitive as documented
        if not os.path.exists(path):
            raise PreflightError(
                f"configuration file not found: {path} (copy config/br.conf.example)"
            )
        with open(path, "r", encoding="utf-8") as fh:
            try:
                self._cp.read_file(fh)
            except configparser.Error as exc:  # pragma: no cover - defensive
                raise PreflightError(f"invalid configuration: {exc}") from exc
        self._validate()

    # -- primitives ---------------------------------------------------------

    def get(self, section: str, key: str, default: t.Any = None) -> t.Any:
        if not self._cp.has_option(section, key):
            if default is None:
                raise ValidationError(f"missing configuration value [{section}] {key}")
            return default
        return self._cp.get(section, key)

    def get_int(self, section: str, key: str, default: int | None = None) -> int:
        raw = self.get(section, key, default)
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"[{section}] {key} must be an integer, got {raw!r}") from exc

    def get_bool(self, section: str, key: str, default: bool = False) -> bool:
        raw = self.get(section, key, "true" if default else "false")
        text = str(raw).strip().lower()
        if text in BOOL_TRUE:
            return True
        if text in BOOL_FALSE:
            return False
        raise ValidationError(f"[{section}] {key} must be a boolean, got {raw!r}")

    def get_list(self, section: str, key: str, default: str = "") -> list[str]:
        raw = self.get(section, key, default) or ""
        items = [line.strip() for line in str(raw).splitlines()]
        return [i for i in items if i and not i.startswith("#")]

    def get_path(self, section: str, key: str, *, default: str | None = None) -> str:
        raw = self.get(section, key, default)
        return validate_abs_path(str(raw).strip())

    # -- derived namespaces -------------------------------------------------

    @property
    def librenms_root(self) -> str:
        return self.get_path("general", "librenms_root")

    @property
    def backup_root(self) -> str:
        return self.get_path("general", "backup_root")

    @property
    def stage_dir(self) -> str:
        return self.get_path("general", "staging_dir")

    @property
    def archive_dir(self) -> str:
        return self.get_path("general", "archive_dir")

    @property
    def checkpoint_dir(self) -> str:
        return self.get_path("general", "checkpoint_dir")

    @property
    def run_dir(self) -> str:
        return self.get_path("general", "run_dir")

    @property
    def state_dir(self) -> str:
        return self.get_path("general", "state_dir")

    @property
    def log_dir(self) -> str:
        return self.get_path("general", "log_dir")

    @property
    def app_user(self) -> str:
        return str(self.get("general", "librenms_user", "librenms"))

    @property
    def app_group(self) -> str:
        return str(self.get("general", "librenms_group", "librenms"))

    @property
    def components(self) -> dict[str, bool]:
        keys = ("database", "rrd", "application", "system", "integrations")
        return {k: self.get_bool("components", k, True) for k in keys}

    # Accepted spellings -> the scheme name the container actually implements.
    # `openssl-sshaes` is the historical name used in br.conf.example for the
    # passphrase fallback; it is the same AES-256-CTR + HMAC-SHA256 container
    # built on PBKDF2, so both names map to one implementation. Without this
    # mapping the documented fallback passed validation and then failed at the
    # first write, halfway through a backup.
    _ENCRYPTION_ALIASES = {
        "age": "age",
        "none": "none",
        "aesgcm-pbkdf2": "aesgcm-pbkdf2",
        "openssl-sshaes": "aesgcm-pbkdf2",
        "aes256-ctr-hmac-sha256": "aesgcm-pbkdf2",
    }

    @property
    def encryption(self) -> str:
        value = str(self.get("archive", "encryption", "aes256-ctr-hmac-sha256")).strip().lower()
        canonical = self._ENCRYPTION_ALIASES.get(value)
        if canonical is None:
            raise ValidationError(
                f"unsupported encryption scheme: {value!r} "
                f"(expected one of {', '.join(sorted(self._ENCRYPTION_ALIASES))})"
            )
        return canonical

    # -- validation ---------------------------------------------------------

    _ALLOWED_SECTIONS = {
        "general", "archive", "database", "components", "restore",
        "retention", "notifications",
    }

    def _validate(self) -> None:
        unknown = set(self._cp.sections()) - self._ALLOWED_SECTIONS
        if unknown:
            raise ValidationError(f"unknown configuration sections: {sorted(unknown)}")

        # Paths must be absolute, no traversal, and disjoint from webroot.
        librenms_root = self.librenms_root
        for path in (self.stage_dir, self.archive_dir, self.checkpoint_dir):
            if path.startswith(librenms_root.rstrip("/") + "/html") or path.startswith(
                librenms_root.rstrip("/") + "/public"
            ):
                raise ValidationError(
                    f"backup path {path} is inside the LibreNMS web root - refusing to continue"
                )

        if self.get_int("archive", "zstd_level", 3) not in range(1, 20):
            raise ValidationError("zstd_level must be between 1 and 19")

        mode = str(self.get("archive", "verify_after_backup", "always")).strip().lower()
        if mode not in ("always", "scheduled-only", "never"):
            raise ValidationError("verify_after_backup must be always|scheduled-only|never")

        dump_tool = str(self.get("database", "dump_tool", "auto")).strip().lower()
        if dump_tool not in ("auto", "mariadb-dump", "mysqldump"):
            raise ValidationError("dump_tool must be auto|mariadb-dump|mysqldump")

        extra = self.get_list("database", "extra_args")
        for arg in extra:
            # Only long-form flags, no shell metacharacters, no redirection.
            if not re.match(r"^--[a-z0-9][a-z0-9-]*(=[A-Za-z0-9_.-]+)?$", arg):
                raise ValidationError(f"illegal entry in database.extra_args: {arg!r}")

        phrase = str(self.get("restore", "confirm_phrase", "RESTORE"))
        if not re.match(r"^[A-Z]{4,32}$", phrase):
            raise ValidationError("restore.confirm_phrase must be 4-32 upper-case letters")

        for section, key in (
            ("general", "librenms_user"),
            ("general", "librenms_group"),
        ):
            value = str(self.get(section, key))
            if not re.match(r"^[a-z_][a-z0-9_-]{0,31}$", value):
                raise ValidationError(f"[{section}] {key} is not a valid name: {value!r}")


def load(path: str | None = None) -> Config:
    return Config(path or os.environ.get("BR_CONF", DEFAULT_CONF))
