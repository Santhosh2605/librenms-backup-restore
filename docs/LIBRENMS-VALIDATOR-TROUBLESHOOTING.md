# LibreNMS validator patch troubleshooting

This guide covers the optional validator compatibility patch described in the
[README](../README.md#optional-librenms-database-validator-compatibility-patch).
It does not replace the backup and review steps in that section.

## The patch path contains a timestamp placeholder

Angle-bracket values such as `<timestamp>` are placeholders and should not be
copied literally. The following command selects the newest generated bundle
without asking the user to enter a timestamp:

Select the newest generated bundle automatically; no timestamp needs to be
entered by hand:

```bash
PROJECT=/opt/librenms-backup-restore
BUNDLE=$(find "$PROJECT/plugin/support/librenms-validator" -mindepth 1 -maxdepth 1 -type d -name '????????T??????Z' -print | sort | tail -n 1)
test -n "$BUNDLE" && test -f "$BUNDLE/librenms-validator-fix.patch" || { echo "Validator patch bundle not found"; exit 1; }
printf 'Selected bundle: %s\n' "$BUNDLE"
PATCH="$BUNDLE/librenms-validator-fix.patch"
```

Use the same shell session for commands that rely on `BUNDLE`, `PATCH`,
`NEW_BUNDLE`, or `SAVE`, or assign the variables again. Before applying a patch,
run the dry run and confirm every file succeeds:

```bash
sudo patch --dry-run -p1 -d /opt/librenms < "$PATCH"
```

If the shell reports “No such file or directory” for the patch, stop. Check
`printf '%s\n' "$PATCH"` and `ls -l "$PATCH"`; do not run the apply command
until the path points to an existing patch and the dry run passes.

## Patch command did not apply, or `PluginSchema.php` is missing

The patch output should list the three validator/migration files and the new
`LibreNMS/DB/PluginSchema.php`. If patch application failed because the path was
wrong, LibreNMS remains unpatched. An ensuing `chown` message saying
`PluginSchema.php` cannot be found confirms that the helper was not created.

Re-select the correct bundle, dry-run it, then apply only after a successful dry
run:

```bash
sudo patch --dry-run -p1 -d /opt/librenms < "$PATCH"
sudo patch -p1 -d /opt/librenms < "$PATCH"
sudo install -o librenms -g librenms -m 0644 \
  /opt/librenms-backup-restore/plugin/librenms-schema.json \
  /opt/librenms/app/Plugins/BackupRestore/librenms-schema.json
sudo chown librenms:librenms /opt/librenms/LibreNMS/DB/PluginSchema.php
sudo -u librenms /opt/librenms/validate.php
```

If the dry run reports rejected hunks, do not force the patch. The LibreNMS
files differ from the version used to prepare it; review the change and prepare
a new patch for the installed LibreNMS version.

## `Class "LibreNMS\\DB\\PluginSchema" not found`

The validator files still call the helper, but the helper is missing from
`/opt/librenms/LibreNMS/DB/PluginSchema.php`. This can happen if patched
validator files were left in place while the helper was moved to the plugin's
update backup directory.

First check the current state:

```bash
grep -n PluginSchema /opt/librenms/LibreNMS/Validations/Database/CheckSchemaCollation.php
ls -l /opt/librenms/LibreNMS/DB/PluginSchema.php
```

For a normal LibreNMS update, restore the original validator files from the
matching bundle before running `daily.sh`. The README's restore block locates
all original files under the bundle's `backup/` folder, restores them while
preserving their LibreNMS paths, and automatically stops if the restored
collation validator still references `PluginSchema`. After the update, prepare
and review a new patch, dry-run it, apply it, and install the manifest as
described in the README. Do not copy an old helper or old patched validator
files over a newer LibreNMS version. If the matching original files or bundle
are unavailable, stop and recover them from the saved update backup or bundle
before proceeding.

## Validation again reports the plugin's `br_*` tables or JSON columns

These are the warnings the compatibility patch is intended to address. Check
that the current validators contain the patch and that both the helper and
manifest exist:

```bash
grep -n PluginSchema /opt/librenms/LibreNMS/Validations/Database/CheckSchemaCollation.php
test -f /opt/librenms/LibreNMS/DB/PluginSchema.php && echo "helper present"
test -f /opt/librenms/app/Plugins/BackupRestore/librenms-schema.json && echo "manifest present"
```

If the files are present but validation still reports the plugin's own tables,
check that the manifest declares the expected tables and JSON columns and that
the helper and manifest are owned/readable as expected. Do not accept
LibreNMS's automatic repair for these reports: its suggested SQL may drop the
plugin's tables. Answer `n` to the repair prompt while diagnosing.

## Validation says the schema and collations are correct, but lists modified files

This is expected while the compatibility patch is applied: it modifies three
LibreNMS validator/migration files and adds the `PluginSchema.php` helper.
Do not run `lnms patch:remove --reset`; it would remove the local patch. Keep the
bundle and update backup so the patch can be reviewed and regenerated after a
LibreNMS update.

The separate “install is over 24 hours out of date” warning is not caused by
this patch. Follow LibreNMS's normal update guidance when ready, using the
README's backup and reapply procedure for the local validator patch.
