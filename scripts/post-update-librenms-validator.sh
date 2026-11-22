#!/usr/bin/env bash
# Prepare and apply a fresh validator compatibility patch after LibreNMS updates.
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

[[ $EUID -eq 0 ]] || die "Run with sudo/root: sudo bash scripts/post-update-librenms-validator.sh"
[[ -f "$LIBRENMS_ROOT/daily.sh" && -f "$LIBRENMS_ROOT/validate.php" ]] || die "LibreNMS root not found: $LIBRENMS_ROOT"
[[ -f "$PROJECT_ROOT/scripts/prepare_librenms_validator_patch.py" ]] || die "Patch preparer missing from $PROJECT_ROOT/scripts"
[[ -f "$PROJECT_ROOT/plugin/librenms-schema.json" ]] || die "Plugin schema manifest is missing from the project checkout"

SAVE_DIR="${VALIDATOR_SAVE_DIR:-}"
if [[ -z "$SAVE_DIR" ]]; then
    while IFS= read -r candidate; do
        if [[ -f "$candidate/DAILY_UPDATE_COMPLETED" && -f "$candidate/plugin-files/librenms-schema.json" ]]; then
            SAVE_DIR="$candidate"
            break
        fi
    done < <(find "$BACKUP_ROOT" -mindepth 1 -maxdepth 1 -type d -name '????????T??????Z' -print 2>/dev/null | sort -r)
fi
[[ -n "$SAVE_DIR" && -f "$SAVE_DIR/PRE_UPDATE_READY" ]] || die "No completed pre-update backup found. Set VALIDATOR_SAVE_DIR to its directory."
[[ -f "$SAVE_DIR/plugin-files/librenms-schema.json" ]] || die "Saved plugin manifest missing in $SAVE_DIR"
[[ ! -e "$LIBRENMS_ROOT/$HELPER_REL" ]] || die "$HELPER_REL already exists. Stop and inspect the current/partial patch before continuing."

if [[ ! -f "$SAVE_DIR/DAILY_UPDATE_COMPLETED" ]]; then
    info "The previous daily.sh run did not complete; retrying it before patch preparation"
    (cd "$LIBRENMS_ROOT" && sudo -u "$APP_USER" -H ./daily.sh) || die "daily.sh still fails. Resolve its error and rerun this script. Backup: $SAVE_DIR"
    touch "$SAVE_DIR/DAILY_UPDATE_COMPLETED"
fi

info "Using update backup: $SAVE_DIR"
info "Preparing a patch for the installed LibreNMS version"
python3 "$PROJECT_ROOT/scripts/prepare_librenms_validator_patch.py" --librenms-root "$LIBRENMS_ROOT" || \
    die "Patch preparation failed. No LibreNMS core files were changed; review the version mismatch."

BUNDLE_NAME="$(find "$SUPPORT_ROOT" -mindepth 1 -maxdepth 1 -type d -name '????????T??????Z' -printf '%f\n' | sort | tail -n 1)"
[[ -n "$BUNDLE_NAME" ]] || die "Patch preparer did not create a timestamped bundle."
BUNDLE="$SUPPORT_ROOT/$BUNDLE_NAME"
PATCH_FILE="$BUNDLE/librenms-validator-fix.patch"
[[ -s "$PATCH_FILE" ]] || die "Generated patch is missing or empty: $PATCH_FILE"
info "Generated bundle: $BUNDLE"

info "Dry-running the generated patch"
patch --dry-run -p1 -d "$LIBRENMS_ROOT" < "$PATCH_FILE" || die "Patch dry run failed. No patch was applied; review $PATCH_FILE."

install -D -o "$APP_USER" -g "$APP_USER" -m 0644 \
    "$SAVE_DIR/plugin-files/librenms-schema.json" "$LIBRENMS_ROOT/$MANIFEST_REL"
info "Applying the patch"
patch -p1 -d "$LIBRENMS_ROOT" < "$PATCH_FILE" || die "Patch application failed. Inspect LibreNMS before retrying."
chown "$APP_USER:$APP_USER" "$LIBRENMS_ROOT/$HELPER_REL"
touch "$SAVE_DIR/PATCH_REAPPLIED"

printf '%s\n' '' \
    "Validator compatibility patch applied successfully." \
    "New patch bundle: $BUNDLE" \
    "Pre-update backup: $SAVE_DIR" \
    '' \
    "Run validation with automatic repairs declined:" \
    "  sudo -u $APP_USER $LIBRENMS_ROOT/validate.php" \
    '' \
    "Do not run 'lnms patch:remove --reset'; it removes this local compatibility patch."
