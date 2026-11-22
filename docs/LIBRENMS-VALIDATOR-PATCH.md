# LibreNMS validator patch preparation

This project includes `scripts/prepare_librenms_validator_patch.py` to prepare a
reviewable patch for LibreNMS core. The script is intentionally read-only with
respect to the LibreNMS installation: it reads the three core files, stores
their originals under `plugin/support/librenms-validator/<UTC timestamp>/backup/`,
and writes patched copies plus a unified diff under that same plugin directory.
It does not install or apply the patch.

The plugin manifest `plugin/librenms-schema.json` declares the seven exact
`br_*` tables owned by this plugin and the two MariaDB JSON alias columns.
When installed, it belongs at
`/opt/librenms/app/Plugins/BackupRestore/librenms-schema.json`.

Run from the project checkout:

```bash
python3 scripts/prepare_librenms_validator_patch.py --librenms-root /opt/librenms
```

The generated patch targets these LibreNMS files:

- `LibreNMS/Validations/Database/CheckSchemaStructure.php`
- `LibreNMS/Validations/Database/CheckSchemaCollation.php`
- `database/migrations/2021_02_09_122930_migrate_to_utf8mb4.php`
- new file `LibreNMS/DB/PluginSchema.php`

The structural validator will preserve only tables listed in plugin manifests;
unknown extra tables remain visible and repairable. The collation validator
will accept `utf8mb4_bin` only for declared JSON columns. The charset migration
will preserve those same JSON columns when it reruns.

The script fails closed if expected source sections do not match the installed
LibreNMS version. Review `librenms-validator-fix.patch` and the saved originals
before applying anything to a separate LibreNMS checkout.

For the step-by-step update and reapply procedure, see the README section
“Updating LibreNMS later.” If a command or validation result differs from the
expected output, see
[LIBRENMS-VALIDATOR-TROUBLESHOOTING.md](LIBRENMS-VALIDATOR-TROUBLESHOOTING.md).
