#!/usr/bin/env python3
"""Prepare a reviewable LibreNMS core patch without writing to the core tree.

Run from the plugin checkout, for example:
    python3 scripts/prepare_librenms_validator_patch.py --librenms-root /opt/librenms

The script snapshots the current core files under plugin/support/, then writes
patched copies and a unified diff under the same plugin-owned directory. It
never writes into --librenms-root.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPOSITORY_ROOT / "plugin"
SUPPORT_ROOT = PLUGIN_ROOT / "support" / "librenms-validator"

CORE_FILES = (
    Path("LibreNMS/Validations/Database/CheckSchemaStructure.php"),
    Path("LibreNMS/Validations/Database/CheckSchemaCollation.php"),
    Path("database/migrations/2021_02_09_122930_migrate_to_utf8mb4.php"),
)
PLUGIN_SCHEMA = PLUGIN_ROOT / "librenms-schema.json"


PLUGIN_SCHEMA_PHP = r'''<?php

namespace LibreNMS\DB;

/**
 * Reads exact table and JSON-column declarations from local plugins.
 * Manifests are data-only and do not support wildcards.
 */
final class PluginSchema
{
    /** @var array<string, mixed>|null */
    private static $schema = null;

    /** @return array<string, bool> */
    public static function ownedTables(): array
    {
        $tables = [];
        foreach (self::load()['owned_tables'] as $table) {
            $tables[$table] = true;
        }

        return $tables;
    }

    public static function isJsonColumn(string $table, string $column): bool
    {
        return in_array($column, self::load()['json_columns'][$table] ?? [], true);
    }

    /** @return array<string, mixed> */
    private static function load(): array
    {
        if (self::$schema !== null) {
            return self::$schema;
        }

        $schema = ['owned_tables' => [], 'json_columns' => []];
        $files = glob(base_path('app/Plugins/*/librenms-schema.json')) ?: [];
        foreach ($files as $file) {
            $manifest = json_decode(file_get_contents($file), true, 512, JSON_THROW_ON_ERROR);
            if (! is_array($manifest) || ($manifest['schema_version'] ?? null) !== 1) {
                throw new \UnexpectedValueException("Invalid plugin database schema manifest: $file");
            }

            foreach (($manifest['owned_tables'] ?? []) as $table) {
                if (! is_string($table) || ! preg_match('/\A[A-Za-z0-9_]+\z/D', $table)) {
                    throw new \UnexpectedValueException("Invalid table name in plugin schema manifest: $file");
                }
                $schema['owned_tables'][$table] = true;
            }

            foreach (($manifest['json_columns'] ?? []) as $table => $columns) {
                if (! is_string($table) || ! preg_match('/\A[A-Za-z0-9_]+\z/D', $table) || ! is_array($columns)) {
                    throw new \UnexpectedValueException("Invalid JSON-column declaration in plugin schema manifest: $file");
                }
                foreach ($columns as $column) {
                    if (! is_string($column) || ! preg_match('/\A[A-Za-z0-9_]+\z/D', $column)) {
                        throw new \UnexpectedValueException("Invalid JSON column name in plugin schema manifest: $file");
                    }
                    $schema['json_columns'][$table][$column] = true;
                }
            }
        }

        $schema['owned_tables'] = array_keys($schema['owned_tables']);
        foreach ($schema['json_columns'] as $table => $columns) {
            $schema['json_columns'][$table] = array_keys($columns);
        }

        self::$schema = $schema;

        return self::$schema;
    }
}
'''


def replace_once(source: str, old: str, new: str, label: str) -> str:
    count = source.count(old)
    if count != 1:
        raise ValueError(f"Expected one match for {label}; found {count}. Core files were not changed.")
    return source.replace(old, new, 1)


def patch_structure(source: str) -> str:
    source = replace_once(
        source,
        "use LibreNMS\\DB\\Eloquent;\nuse LibreNMS\\DB\\Schema;",
        "use LibreNMS\\DB\\Eloquent;\nuse LibreNMS\\DB\\PluginSchema;\nuse LibreNMS\\DB\\Schema;",
        "schema checker imports",
    )
    source = replace_once(
        source,
        "        foreach ($current_schema as $table => $data) {\n            $this->descriptions[] = \"Database: extra table ($table)\";",
        "        $plugin_tables = PluginSchema::ownedTables();\n\n"
        "        foreach ($current_schema as $table => $data) {\n"
        "            if (isset($plugin_tables[$table])) {\n"
        "                continue; // plugin-owned tables are not LibreNMS core extras\n"
        "            }\n\n"
        "            $this->descriptions[] = \"Database: extra table ($table)\";",
        "extra-table repair loop",
    )
    return source


def patch_collation(source: str) -> str:
    source = replace_once(
        source,
        "use LibreNMS\\DB\\Eloquent;",
        "use LibreNMS\\DB\\Eloquent;\nuse LibreNMS\\DB\\PluginSchema;",
        "collation checker imports",
    )
    source = replace_once(
        source,
        "        $collation_columns = Eloquent::DB()->select($column_collation_sql);\n"
        "        if (empty($collation_columns) !== true) {",
        "        $collation_columns = Eloquent::DB()->select($column_collation_sql);\n"
        "        // MariaDB implements JSON as LONGTEXT with utf8mb4_bin. Accept that\n"
        "        // collation only for exact JSON columns declared by local plugins.\n"
        "        $collation_columns = array_values(array_filter($collation_columns, static function ($row): bool {\n"
        "            return ! (\n"
        "                $row->CHARACTER_SET_NAME === 'utf8mb4'\n"
        "                && $row->COLLATION_NAME === 'utf8mb4_bin'\n"
        "                && PluginSchema::isJsonColumn($row->TABLE_NAME, $row->COLUMN_NAME)\n"
        "            );\n"
        "        }));\n\n"
        "        if (empty($collation_columns) !== true) {",
        "collation column check",
    )
    return source


def patch_charset_migration(source: str) -> str:
    source = replace_once(
        source,
        "use Illuminate\\Support\\Facades\\DB;",
        "use Illuminate\\Support\\Facades\\DB;\nuse LibreNMS\\DB\\PluginSchema;",
        "charset migration imports",
    )
    source = replace_once(
        source,
        "        foreach ($columns as $column) {\n            $null = $column->IS_NULLABLE == 'YES' ? 'NULL' : 'NOT NULL';",
        "        foreach ($columns as $column) {\n"
        "            // Preserve MariaDB JSON aliases, whose expected collation is utf8mb4_bin.\n"
        "            if ($column->CHARACTER_SET_NAME === 'utf8mb4'\n"
        "                && $column->COLLATION_NAME === 'utf8mb4_bin'\n"
        "                && PluginSchema::isJsonColumn($column->TABLE_NAME, $column->COLUMN_NAME)) {\n"
        "                continue;\n"
        "            }\n\n"
        "            $null = $column->IS_NULLABLE == 'YES' ? 'NULL' : 'NOT NULL';",
        "charset migration column loop",
    )
    return source


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--librenms-root",
        required=True,
        type=Path,
        help="LibreNMS root to read the current core files from (read-only)",
    )
    args = parser.parse_args()

    core_root = args.librenms_root.resolve(strict=True)
    if not (core_root / "validate.php").is_file() or not (core_root / "app").is_dir():
        parser.error("--librenms-root must point to the LibreNMS application root")
    if not PLUGIN_SCHEMA.is_file():
        parser.error(f"plugin schema manifest is missing: {PLUGIN_SCHEMA}")

    try:
        manifest = json.loads(PLUGIN_SCHEMA.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        parser.error(f"cannot read plugin schema manifest: {exc}")
    if manifest.get("schema_version") != 1:
        parser.error("unsupported plugin schema manifest version")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = SUPPORT_ROOT / stamp
    if output.exists():
        parser.error(f"output already exists: {output}")
    try:
        output.resolve().relative_to(PLUGIN_ROOT.resolve())
    except ValueError:
        parser.error("output path resolves outside the plugin directory")

    transforms = {
        CORE_FILES[0]: patch_structure,
        CORE_FILES[1]: patch_collation,
        CORE_FILES[2]: patch_charset_migration,
    }

    originals: dict[Path, str] = {}
    patched: dict[Path, str] = {}
    try:
        for relative, transform in transforms.items():
            source_path = core_root / relative
            original = source_path.read_text(encoding="utf-8")
            originals[relative] = original
            patched[relative] = transform(original)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("No files were written. Core files were never modified.", file=sys.stderr)
        return 2

    new_file = Path("LibreNMS/DB/PluginSchema.php")
    if (core_root / new_file).exists():
        print(f"ERROR: core already has {new_file}; review it before generating this patch.", file=sys.stderr)
        return 2
    patched[new_file] = PLUGIN_SCHEMA_PHP

    try:
        for relative, content in originals.items():
            write_text(output / "backup" / relative, content)
        for relative, content in patched.items():
            write_text(output / "patched" / relative, content)

        diff_lines: list[str] = []
        for relative in (*CORE_FILES, new_file):
            before = originals.get(relative, "")
            after = patched[relative]
            diff_lines.extend(difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=f"a/{relative.as_posix()}" if before else "/dev/null",
                tofile=f"b/{relative.as_posix()}",
            ))
        write_text(output / "librenms-validator-fix.patch", "".join(diff_lines))

        report = [
            "Prepared LibreNMS validator patch (not applied)",
            f"LibreNMS source: {core_root}",
            "Core writes: none",
            "Backups:",
            *[f"  backup/{p.as_posix()}" for p in CORE_FILES],
            "Patched copies:",
            *[f"  patched/{p.as_posix()}" for p in patched],
            "Unified diff: librenms-validator-fix.patch",
            "Review the diff before applying it to a LibreNMS checkout.",
        ]
        write_text(output / "README.txt", "\n".join(report) + "\n")
    except OSError as exc:
        print(f"ERROR writing plugin-side artifacts: {exc}", file=sys.stderr)
        print("The LibreNMS core tree was not modified.", file=sys.stderr)
        return 2

    print(f"Patch bundle prepared under: {output}")
    print("Original core files are backed up there; no LibreNMS core files were changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
