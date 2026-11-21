# Updating LibreNMS with the local validator patch

The validator fix changes LibreNMS core files locally. Before updating, save
the current patched files, restore the pre-patch core files so `daily.sh` can
update cleanly, then generate a fresh patch against the updated LibreNMS code.

**Do not run `lnms patch:remove --reset`.** It discards local changes and can
delete the new `PluginSchema.php` helper. Never copy old patched files over a
newer LibreNMS version; that can overwrite upstream changes.

## 1. Back up the current patched files

Start a root shell and keep it open for the following commands:

```bash
sudo -i
PROJECT=/opt/librenms-backup-restore
LIBRENMS=/opt/librenms
PREP_BUNDLE="$PROJECT/plugin/support/librenms-validator/20261121T162855Z"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_DIR="$PROJECT/plugin/support/librenms-validator/update-backups/$STAMP"
install -d -o root -g root -m 0700 "$BACKUP_DIR"
```

If you applied a different preparer bundle, set `PREP_BUNDLE` to that bundle.
Save the currently installed patch, schema manifest, and LibreNMS revision:

```bash
mkdir -p "$BACKUP_DIR/current/LibreNMS/Validations/Database" \
         "$BACKUP_DIR/current/LibreNMS/DB" \
         "$BACKUP_DIR/current/database/migrations" \
         "$BACKUP_DIR/current/app/Plugins/BackupRestore"
cp -a "$LIBRENMS/LibreNMS/Validations/Database/CheckSchemaCollation.php" "$BACKUP_DIR/current/LibreNMS/Validations/Database/"
cp -a "$LIBRENMS/LibreNMS/Validations/Database/CheckSchemaStructure.php" "$BACKUP_DIR/current/LibreNMS/Validations/Database/"
cp -a "$LIBRENMS/database/migrations/2021_02_09_122930_migrate_to_utf8mb4.php" "$BACKUP_DIR/current/database/migrations/"
cp -a "$LIBRENMS/LibreNMS/DB/PluginSchema.php" "$BACKUP_DIR/current/LibreNMS/DB/"
cp -a "$LIBRENMS/app/Plugins/BackupRestore/librenms-schema.json" "$BACKUP_DIR/current/app/Plugins/BackupRestore/"
sudo -u librenms git -C "$LIBRENMS" rev-parse HEAD > "$BACKUP_DIR/librenms-head.txt"
sudo -u librenms git -C "$LIBRENMS" status --short > "$BACKUP_DIR/git-status-before-update.txt"
```

## 2. Return core files to their pre-patch state

Use the original files saved by the preparer. Move the new helper and manifest
out of the LibreNMS checkout as well. This preserves them in `BACKUP_DIR` while
allowing the normal updater to work with a clean core tree.

```bash
install -o librenms -g librenms -m 0644 "$PREP_BUNDLE/backup/LibreNMS/Validations/Database/CheckSchemaCollation.php" "$LIBRENMS/LibreNMS/Validations/Database/CheckSchemaCollation.php"
install -o librenms -g librenms -m 0644 "$PREP_BUNDLE/backup/LibreNMS/Validations/Database/CheckSchemaStructure.php" "$LIBRENMS/LibreNMS/Validations/Database/CheckSchemaStructure.php"
install -o librenms -g librenms -m 0644 "$PREP_BUNDLE/backup/database/migrations/2021_02_09_122930_migrate_to_utf8mb4.php" "$LIBRENMS/database/migrations/2021_02_09_122930_migrate_to_utf8mb4.php"
mv "$LIBRENMS/LibreNMS/DB/PluginSchema.php" "$BACKUP_DIR/PluginSchema.php"
mv "$LIBRENMS/app/Plugins/BackupRestore/librenms-schema.json" "$BACKUP_DIR/librenms-schema.json"
sudo -u librenms git -C "$LIBRENMS" status --short
```

Check the status output. If any of the three validator files are still listed,
stop and investigate. Do not clear the status with a broad reset.

## 3. Update LibreNMS

Run the regular updater as the `librenms` account:

```bash
cd "$LIBRENMS"
sudo -u librenms ./daily.sh
```

If the update fails, stop and keep its output. Do not copy the old patched files
over an updated LibreNMS revision.

## 4. Regenerate and apply the patch for the updated version

After `daily.sh` completes, prepare a fresh patch from the project checkout:

```bash
cd "$PROJECT"
python3 scripts/prepare_librenms_validator_patch.py --librenms-root "$LIBRENMS"
```

Use the new timestamped directory printed by the command. Review its
`README.txt`, `backup/`, `patched/`, and `librenms-validator-fix.patch`. Then
replace `<new timestamp>` and dry-run the patch:

```bash
NEW_BUNDLE="$PROJECT/plugin/support/librenms-validator/<new timestamp>"
PATCH="$NEW_BUNDLE/librenms-validator-fix.patch"
patch --dry-run -p1 -d "$LIBRENMS" < "$PATCH"
```

Apply only if every file passes the dry run:

```bash
patch -p1 -d "$LIBRENMS" < "$PATCH"
install -o librenms -g librenms -m 0644 "$PROJECT/plugin/librenms-schema.json" "$LIBRENMS/app/Plugins/BackupRestore/librenms-schema.json"
chown librenms:librenms "$LIBRENMS/LibreNMS/DB/PluginSchema.php"
sudo -u librenms "$LIBRENMS/validate.php"
```

If the preparer says its source patterns do not match, or if the dry run has a
failed hunk, stop. The LibreNMS code changed and the patch needs review for that
version.

## If an update fails and you need to restore the old patched files

Restore the saved patched files only if the LibreNMS Git revision still matches
`librenms-head.txt`. If the revision changed, do not put old core files over the
updated version; prepare a patch for that version instead.

```bash
cmp "$BACKUP_DIR/librenms-head.txt" <(sudo -u librenms git -C "$LIBRENMS" rev-parse HEAD)
```

If `cmp` reports no difference, restore the saved files:

```bash
install -o librenms -g librenms -m 0644 "$BACKUP_DIR/current/LibreNMS/Validations/Database/CheckSchemaCollation.php" "$LIBRENMS/LibreNMS/Validations/Database/CheckSchemaCollation.php"
install -o librenms -g librenms -m 0644 "$BACKUP_DIR/current/LibreNMS/Validations/Database/CheckSchemaStructure.php" "$LIBRENMS/LibreNMS/Validations/Database/CheckSchemaStructure.php"
install -o librenms -g librenms -m 0644 "$BACKUP_DIR/current/database/migrations/2021_02_09_122930_migrate_to_utf8mb4.php" "$LIBRENMS/database/migrations/2021_02_09_122930_migrate_to_utf8mb4.php"
install -o librenms -g librenms -m 0644 "$BACKUP_DIR/current/LibreNMS/DB/PluginSchema.php" "$LIBRENMS/LibreNMS/DB/PluginSchema.php"
install -o librenms -g librenms -m 0644 "$BACKUP_DIR/current/app/Plugins/BackupRestore/librenms-schema.json" "$LIBRENMS/app/Plugins/BackupRestore/librenms-schema.json"
sudo -u librenms "$LIBRENMS/validate.php"
```
