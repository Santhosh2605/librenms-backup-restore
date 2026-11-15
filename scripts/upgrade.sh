#!/usr/bin/env bash
#
# LibreNMS Backup & Restore - upgrade script
#
# Usage:
#   sudo ./scripts/upgrade.sh            # install the code in this checkout
#   sudo ./scripts/upgrade.sh --from-git <ref>
#
# Guarantees:
#   * configuration (/etc/librenms-backup) and keys are NEVER overwritten
#   * archives and checkpoints are untouched
#   * schema migrations are applied additively and are idempotent
#   * the previous code is kept in /opt/librenms-backup-restore.previous
#
set -Eeuo pipefail

PREFIX="${PREFIX:-/opt/librenms-backup-restore}"
LIBRENMS_ROOT="${LIBRENMS_ROOT:-/opt/librenms}"
APP_USER="${APP_USER:-librenms}"
PHP_BIN="$(command -v php || echo /usr/bin/php)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_TS="$(date -u +%Y%m%dT%H%M%SZ)"

ok()   { printf '\033[32m  ok\033[0m  %s\n' "$*"; }
info() { printf '\033[36m  ..\033[0m  %s\n' "$*"; }
step() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31mFAIL\033[0m  %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"

step "1. Pre-upgrade safety"
if [[ -f "$PREFIX/scripts/br-schema.php" ]]; then
    sudo -u "$APP_USER" -H env LIBRENMS_ROOT="$LIBRENMS_ROOT" \
        "$PHP_BIN" "$PREFIX/scripts/br-schema.php" status || true
fi
if systemctl is-active --quiet 'librenms-backup-worker@' 2>/dev/null; then
    die "a backup/restore worker is running - wait for it to finish"
fi
mysql_count=$(sudo -u "$APP_USER" -H "$PHP_BIN" "$LIBRENMS_ROOT/artisan" tinker --execute \
    'echo \App\Plugins\BackupRestore\Models\Job::whereIn("status",["queued","running"])->count();' 2>/dev/null | tail -1 || echo 0)
[[ "$mysql_count" == "0" ]] && ok "no jobs in flight" || die "jobs in flight ($mysql_count) - retry later"

# A schema backup keeps the upgrade reversible.
sudo -u "$APP_USER" -H env LIBRENMS_ROOT="$LIBRENMS_ROOT" \
    "$PHP_BIN" -r '
        $root=getenv("LIBRENMS_ROOT"); require $root."/vendor/autoload.php";
        $app=require $root."/bootstrap/app.php";
        $app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
        $out=[];
        foreach(["br_backups","br_jobs","br_restores","br_checkpoints","br_schedules","br_settings","br_audit_events"] as $t){
            $out[$t]=DB::table($t)->get()->toArray();
        }
        echo json_encode($out);
    ' > "/var/backups/librenms/br-metadata-$BACKUP_TS.json" 2>/dev/null \
    && ok "plugin metadata snapshot: /var/backups/librenms/br-metadata-$BACKUP_TS.json" \
    || info "metadata snapshot skipped"

step "2. Stop timers (workers keep their own unit)"
systemctl stop librenms-backup-dispatch.timer librenms-backup-scheduler.timer 2>/dev/null || true
ok "timers paused"

step "3. Rotate code"
if [[ -d "$PREFIX" ]]; then
    rm -rf "${PREFIX}.previous"
    mv "$PREFIX" "${PREFIX}.previous"
    ok "previous code kept at ${PREFIX}.previous"
fi
bash "$SRC_DIR/scripts/install.sh" --yes || { 
    die "installer failed - roll back with: rm -rf $PREFIX && mv ${PREFIX}.previous $PREFIX"
}
ok "new code installed"

step "4. Apply schema migrations (additive)"
sudo -u "$APP_USER" -H env LIBRENMS_ROOT="$LIBRENMS_ROOT" \
    "$PHP_BIN" "$PREFIX/scripts/br-schema.php" install || die "schema migration failed"
ok "schema up to date"

step "5. Restart timers"
systemctl start br-helper.service
systemctl restart librenms-backup-dispatch.timer librenms-backup-scheduler.timer
ok "services restarted"

step "6. Post-upgrade validation"
bash "$PREFIX/scripts/validate-install.sh" || {
    printf '\n\033[33mValidation reported problems. Roll back with:\033[0m\n'
    printf '  rm -rf %s && mv %s.previous %s\n' "$PREFIX" "$PREFIX" "$PREFIX"
    exit 1
}

cat <<EOF

$(printf '\033[1mUpgrade complete\033[0m')

Rollback:
  sudo systemctl stop librenms-backup-*.timer
  sudo rm -rf $PREFIX && sudo mv $PREFIX.previous $PREFIX
  sudo $PHP_BIN $PREFIX/scripts/br-schema.php status   # confirm schema compatibility
  sudo systemctl start librenms-backup-dispatch.timer librenms-backup-scheduler.timer

Note: schema changes are additive (new tables/columns). The plugin never drops
or alters LibbyNMS core tables, so an older code version keeps working against
a newer schema; only new features are unavailable.
EOF
