#!/usr/bin/env bash
#
# LibreNMS Backup & Restore v1.0 - uninstaller / rollback
#
# Usage:
#   sudo ./scripts/uninstall.sh              # remove code + units, KEEP data
#   sudo ./scripts/uninstall.sh --purge      # also drop br_* tables
#   sudo ./scripts/uninstall.sh --purge --destroy-archives
#
# Order matters: the GUI is removed first so nothing can queue work while the
# worker is being retired.
#
set -Eeuo pipefail

PREFIX="${PREFIX:-/opt/librenms-backup-restore}"
LIBRENMS_ROOT="${LIBRENMS_ROOT:-/opt/librenms}"
APP_USER="${APP_USER:-librenms}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/librenms}"
CONF_DIR="/etc/librenms-backup"
PHP_BIN="$(command -v php || echo /usr/bin/php)"
PURGE=0
DESTROY=0

for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=1 ;;
        --destroy-archives) DESTROY=1 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    esac
done

c_ok()   { printf '\033[32m  ok\033[0m  %s\n' "$*"; }
c_info() { printf '\033[36m  ..\033[0m  %s\n' "$*"; }
c_warn() { printf '\033[33mwarn\033[0m  %s\n' "$*"; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "run as root"; exit 1; }

step "1. Stop timers, workers and helper"
systemctl disable --now librenms-backup-scheduler.timer 2>/dev/null || true
systemctl disable --now librenms-backup-dispatch.timer  2>/dev/null || true
systemctl stop 'librenms-backup-worker@*' 2>/dev/null || true
systemctl disable --now br-helper.service 2>/dev/null || true
c_ok "units stopped"

step "2. Remove systemd units / sudoers / logrotate"
rm -f /etc/systemd/system/br-helper.service \
      /etc/systemd/system/librenms-backup-worker@.service \
      /etc/systemd/system/librenms-backup-dispatch.service \
      /etc/systemd/system/librenms-backup-dispatch.timer \
      /etc/systemd/system/librenms-backup-scheduler.service \
      /etc/systemd/system/librenms-backup-scheduler.timer
systemctl daemon-reload
rm -f /etc/sudoers.d/librenms-backup
rm -f /etc/logrotate.d/librenms-backup
rm -rf /usr/local/libexec/librenms-backup
c_ok "system integration removed"

step "3. Remove the LibreNMS GUI surface"
rm -f "$LIBRENMS_ROOT/html/plugin-backuprestore.php"
rm -rf "$LIBRENMS_ROOT/app/Plugins/BackupRestore"
sudo -u "$APP_USER" -H "$PHP_BIN" "$LIBRENMS_ROOT/artisan" config:clear  >/dev/null 2>&1 || true
sudo -u "$APP_USER" -H "$PHP_BIN" "$LIBRENMS_ROOT/artisan" view:clear    >/dev/null 2>&1 || true
c_ok "plugin files and front controller removed"

step "4. Database"
if [[ $PURGE -eq 1 ]]; then
    sudo -u "$APP_USER" -H env LIBRENMS_ROOT="$LIBRENMS_ROOT" \
        "$PHP_BIN" "$PREFIX/scripts/br-schema.php" uninstall --force || c_warn "schema uninstall reported errors"
    c_ok "br_* tables dropped"
else
    c_info "keeping br_* tables (run with --purge to drop them)"
    c_info "the plugin row remains inactive; reactivate by reinstalling"
fi

step "5. Application code"
rm -rf "$PREFIX"
c_ok "removed $PREFIX"

step "6. Data"
if [[ $DESTROY -eq 1 ]]; then
    echo
    c_warn "About to DELETE all archives and recovery checkpoints in $BACKUP_ROOT"
    c_warn "and the encryption key material in $CONF_DIR/keys."
    read -r -p "Type DESTROY to continue: " reply
    if [[ "$reply" == "DESTROY" ]]; then
        rm -rf "$BACKUP_ROOT" "$CONF_DIR/keys"
        c_ok "archives, checkpoints and keys deleted"
    else
        c_info "cancelled - data retained"
    fi
else
    c_info "archives retained in $BACKUP_ROOT"
    c_info "keys retained in $CONF_DIR/keys (KEEP THESE - they decrypt your archives)"
    c_info "configuration retained: $CONF_DIR/br.conf"
fi

cat <<'EOF'

Uninstall finished.

Rollback note: a full backup can still be restored WITHOUT this plugin by
following docs/RESTORE.md section "Bare-metal recovery (no plugin)". The
archive is a standard tar stream inside a BRENC1 container; the manual
procedure documents the exact commands.

LibreNMS core files were never modified, so no LibreNMS rollback is required.
EOF
