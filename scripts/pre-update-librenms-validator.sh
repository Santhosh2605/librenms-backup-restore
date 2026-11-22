#!/usr/bin/env bash
# Back up the applied validator patch, restore originals, and run LibreNMS update.
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LIBRENMS_ROOT="${LIBRENMS_ROOT:-/opt/librenms}"
APP_USER="${APP_USER:-librenms}"
SUPPORT_ROOT="$PROJECT_ROOT/plugin/support/librenms-validator"
BACKUP_ROOT="$SUPPORT_ROOT/update-backups"
HELPER_REL="LibreNMS/DB/PluginSchema.php"
MANIFEST_REL="app/Plugins/BackupRestore/librenms-schema.json"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
info() { printf '==> %s\n' "$*"; }

[[ $EUID -eq 0 ]] || die "Run with sudo/root: sudo bash scripts/pre-update-librenms-validator.sh"
[[ -f "$LIBRENMS_ROOT/daily.sh" && -f "$LIBRENMS_ROOT/validate.php" ]] || die "LibreNMS root not found: $LIBRENMS_ROOT"
[[ -d "$SUPPORT_ROOT" ]] || die "Validator bundle directory not found: $SUPPORT_ROOT"

bundle_matches() {
    local candidate="$1" relative count=0 original
    [[ -d "$candidate/backup" && -d "$candidate/patched" ]] || return 1
    [[ -f "$candidate/patched/$HELPER_REL" && -f "$LIBRENMS_ROOT/$HELPER_REL" ]] || return 1
    [[ -f "$LIBRENMS_ROOT/$MANIFEST_REL" ]] || return 1
    while IFS= read -r -d '' original; do
        relative="${original#"$candidate/backup/"}"
        [[ -f "$candidate/patched/$relative" && -f "$LIBRENMS_ROOT/$relative" ]] || return 1
        cmp -s -- "$candidate/patched/$relative" "$LIBRENMS_ROOT/$relative" || return 1
        ((count += 1))
    done < <(find "$candidate/backup" -type f -print0)
    ((count > 0)) && cmp -s -- "$candidate/patched/$HELPER_REL" "$LIBRENMS_ROOT/$HELPER_REL"
}

BUNDLE=""
while IFS= read -r candidate; do
    if bundle_matches "$candidate"; then
        BUNDLE="$candidate"
        break
    fi
done < <(find "$SUPPORT_ROOT" -mindepth 1 -maxdepth 1 -type d -name '????????T??????Z' -print | sort -r)
[[ -n "$BUNDLE" ]] || die "No bundle matches the patch currently installed in LibreNMS. No files were changed."
info "Matching applied patch bundle: $BUNDLE"

SAVE_DIR="$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)"
[[ ! -e "$SAVE_DIR" ]] || die "Backup destination already exists: $SAVE_DIR"
install -d -m 0700 "$SAVE_DIR/current-patched" "$SAVE_DIR/plugin-files"

while IFS= read -r -d '' original; do
    relative="${original#"$BUNDLE/backup/"}"
    current="$LIBRENMS_ROOT/$relative"
    [[ -f "$current" ]] || die "Expected patched file is missing: $current"
    install -D -m 0600 "$current" "$SAVE_DIR/current-patched/$relative"
done < <(find "$BUNDLE/backup" -type f -print0)

install -m 0600 "$LIBRENMS_ROOT/$HELPER_REL" "$SAVE_DIR/plugin-files/PluginSchema.php"
install -m 0600 "$LIBRENMS_ROOT/$MANIFEST_REL" "$SAVE_DIR/plugin-files/librenms-schema.json"
tar -czf "$SAVE_DIR/pre-update-files.tar.gz" -C "$SAVE_DIR" current-patched plugin-files

while IFS= read -r -d '' original; do
    relative="${original#"$BUNDLE/backup/"}"
    install -D -o "$APP_USER" -g "$APP_USER" -m 0644 "$original" "$LIBRENMS_ROOT/$relative"
    cmp -s -- "$original" "$LIBRENMS_ROOT/$relative" || die "Could not verify restored original file: $LIBRENMS_ROOT/$relative"
    if grep -q 'PluginSchema' "$LIBRENMS_ROOT/$relative"; then
        die "Restored file still references PluginSchema: $LIBRENMS_ROOT/$relative. Backup: $SAVE_DIR. daily.sh was not run."
    fi
done < <(find "$BUNDLE/backup" -type f -print0)

mv "$LIBRENMS_ROOT/$HELPER_REL" "$SAVE_DIR/plugin-files/PluginSchema.php"
mv "$LIBRENMS_ROOT/$MANIFEST_REL" "$SAVE_DIR/plugin-files/librenms-schema.json"
touch "$SAVE_DIR/PRE_UPDATE_READY"

info "Backup saved at: $SAVE_DIR"
info "Running LibreNMS daily update as $APP_USER"
if ! (cd "$LIBRENMS_ROOT" && sudo -u "$APP_USER" -H ./daily.sh); then
    die "daily.sh failed. Backup retained at $SAVE_DIR. Resolve the error, then run the post-update script; it will rerun daily.sh before preparing the patch."
fi

touch "$SAVE_DIR/DAILY_UPDATE_COMPLETED"
printf '%s\n' '' "LibreNMS update completed. Backup: $SAVE_DIR" "Next: sudo bash $PROJECT_ROOT/scripts/post-update-librenms-validator.sh"
