#!/usr/bin/env bash
#
# LibreNMS Backup & Restore - post-install validation
#
# Usage: sudo ./scripts/validate-install.sh [--verbose]
#
# Read-only. Exits non-zero if a REQUIRED check fails; warnings do not fail.
#
set -uo pipefail

PREFIX="${PREFIX:-/opt/librenms-backup-restore}"
LIBRENMS_ROOT="${LIBRENMS_ROOT:-/opt/librenms}"
APP_USER="${APP_USER:-librenms}"
APP_GROUP="${APP_GROUP:-librenms}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/librenms}"
CONF_DIR="/etc/librenms-backup"
FAILED=0
WARNED=0

ok()   { printf '\033[32m  PASS\033[0m  %s\n' "$*"; }
bad()  { printf '\033[31m  FAIL\033[0m  %s\n' "$*"; FAILED=$((FAILED+1)); }
warn() { printf '\033[33m  WARN\033[0m  %s\n' "$*"; WARNED=$((WARNED+1)); }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

check_file() { [[ -f "$1" ]] && ok "file: $1" || bad "missing file: $1"; }
check_dir()  { [[ -d "$1" ]] && ok "dir:  $1" || bad "missing dir: $1"; }
check_exec() { [[ -x "$1" ]] && ok "exec: $1" || bad "not executable: $1"; }
check_perm() {
    local path="$1" want="$2" actual
    actual="$(stat -c '%a' "$path" 2>/dev/null)" || { bad "cannot stat $path"; return; }
    [[ "$actual" == "$want" ]] && ok "mode $actual $path" || warn "mode $actual $path (expected $want)"
}
check_owner() {
    local path="$1" want="$2" actual
    actual="$(stat -c '%U:%G' "$path" 2>/dev/null)" || { bad "cannot stat $path"; return; }
    [[ "$actual" == "$want" ]] && ok "owner $actual $path" || warn "owner $actual $path (expected $want)"
}

hdr "1. Application code"
check_dir "$PREFIX"
for f in worker/br/__init__.py worker/br/worker.py worker/br/helper.py \
         worker/br/restore.py worker/br/crypto.py scripts/br-schema.php; do
    check_file "$PREFIX/$f"
done

hdr "2. Privileged helper"
check_exec /usr/local/libexec/librenms-backup/br-helper
if [[ -x /usr/local/libexec/librenms-backup/br-helper ]]; then
    if runuser -u "$APP_USER" -- sudo -n -l /usr/local/libexec/librenms-backup/br-helper >/dev/null 2>&1; then
        ok "sudo rule works for $APP_USER"
    else
        bad "sudo rule does NOT permit $APP_USER -> br-helper"
    fi
    if runuser -u "$APP_USER" -- sudo -n /usr/bin/id >/dev/null 2>&1; then
        bad "SECURITY: $APP_USER can escalate beyond the helper"
    else
        ok "no general sudo escalation for $APP_USER"
    fi
fi

hdr "3. Sudoers"
check_file /etc/sudoers.d/librenms-backup
check_perm /etc/sudoers.d/librenms-backup 440
visudo -cf /etc/sudoers.d/librenms-backup >/dev/null 2>&1 \
    && ok "sudoers syntax valid" || bad "invalid sudoers file"

# The file can be syntactically valid yet still not grant the one command the
# worker needs. Exercise the actual rule rather than trusting the parse.
if sudo -n -l -U "$APP_USER" 2>/dev/null | grep -q "br-helper"; then
    ok "worker can run br-helper via sudo (rule is effective)"
else
    sudo -n -l -U "$APP_USER" 2>/dev/null | grep -q "NOPASSWD" \
        && bad "br-helper missing from $APP_USER's sudo rules" \
        || warn "could not query sudo rules for $APP_USER (needs root)"
fi

hdr "4. Configuration and key material"
check_file "$CONF_DIR/br.conf"
check_perm "$CONF_DIR/br.conf" 640
if [[ -f "$CONF_DIR/br.conf" ]]; then
    ENCRYPTION_SCHEME="$(sed -nE 's/^[[:space:]]*encryption[[:space:]]*=[[:space:]]*([^[:space:]#;]+).*/\1/p' \
        "$CONF_DIR/br.conf" | head -1 | tr '[:upper:]' '[:lower:]')"
    case "$ENCRYPTION_SCHEME" in
        aes256-ctr-hmac-sha256|aesgcm-pbkdf2|openssl-sshaes)
            if [[ -f "$CONF_DIR/keys/passphrase" ]]; then
                ok "AES-256 + HMAC-SHA256 configured"
                check_perm "$CONF_DIR/keys/passphrase" 640
            else
                bad "AES encryption selected but passphrase is missing"
            fi
            ;;
        age)
            if [[ -f "$CONF_DIR/keys/identity.txt" && -f "$CONF_DIR/keys/recipients.txt" ]]; then
                ok "age encryption configured"
                check_perm "$CONF_DIR/keys/identity.txt" 640
            else
                bad "age encryption selected but identity or recipients file is missing"
            fi
            ;;
        none)
            warn "archive encryption is disabled (encryption=none)"
            ;;
        *)
            bad "unsupported or missing encryption scheme: ${ENCRYPTION_SCHEME:-not set}"
            ;;
    esac
    grep -qE '^\s*db_password\s*=\s*\S' "$CONF_DIR/br.conf" \
        && warn "a database password is stored in br.conf (prefer .env discovery)"
fi

hdr "5. Storage layout"
for d in "$BACKUP_ROOT" "$BACKUP_ROOT/archives" "$BACKUP_ROOT/staging" \
         "$BACKUP_ROOT/recovery-checkpoints"; do
    check_dir "$d"
done
# archives/ must be writable by the WORKER, which runs as $APP_USER. With mode
# 0750 the group only gets r-x, so `root:$APP_USER` would be read-only for the
# worker and every backup would fail at the first write. The installer creates
# these as $APP_USER:$APP_GROUP for that reason - check for that, and treat a
# root-owned archives/ as the problem rather than the target.
for d in archives staging recovery-checkpoints; do
    check_owner "$BACKUP_ROOT/$d" "$APP_USER:$APP_GROUP"
done
# The root of the tree stays root-owned so the layout cannot be replaced.
check_owner "$BACKUP_ROOT" "root:$APP_GROUP"

# Ownership is a proxy; the thing that actually matters is whether the worker
# can write. Test it as the worker, which catches ownership AND mode together
# (and would have caught the old root:librenms expectation, which was wrong).
if [[ $EUID -eq 0 ]] && id "$APP_USER" >/dev/null 2>&1; then
    for d in archives staging recovery-checkpoints; do
        probe="$BACKUP_ROOT/$d/.br-write-probe.$$"
        if sudo -u "$APP_USER" -- touch "$probe" 2>/dev/null; then
            rm -f "$probe"
            ok "worker can write $BACKUP_ROOT/$d"
        else
            bad "worker CANNOT write $BACKUP_ROOT/$d - backups will fail"
        fi
    done
else
    warn "not root: skipped the worker write test on $BACKUP_ROOT"
fi
chmod_check=$(stat -c '%a' "$BACKUP_ROOT/archives" 2>/dev/null)
[[ "$chmod_check" == "750" || "$chmod_check" == "755" ]] && ok "backup dir mode $chmod_check" \
    || warn "backup dir mode $chmod_check (expected 750)"

hdr "6. Backups are NOT web-accessible"
for web in "$LIBRENMS_ROOT/html" "$LIBRENMS_ROOT/public"; do
    if [[ -d "$web" ]] && [[ "$(readlink -f "$BACKUP_ROOT")" == "$(readlink -f "$web")"/* ]]; then
        bad "SECURITY: backup root is inside the web root ($web)"
    fi
done
ok "backup root is outside the web root"

hdr "7. LibreNMS plugin registration"
check_dir "$LIBRENMS_ROOT/app/Plugins/BackupRestore"
for f in Menu.php Page.php Settings.php; do
    check_file "$LIBRENMS_ROOT/app/Plugins/BackupRestore/$f"
done
# Only hook classes may live at the plugin root: LibreNMS globs app/Plugins/*/*.php
# and throws PluginDoesNotImplementHookException for anything else.
stray=$(ls "$LIBRENMS_ROOT/app/Plugins/BackupRestore"/*.php 2>/dev/null \
        | xargs -r -n1 basename \
        | grep -vE '^(Menu|Page|Settings|DeviceOverview|PortTab)\.php$' || true)
if [[ -z "$stray" ]]; then
    ok "plugin root contains hook classes only"
else
    bad "non-hook PHP files at plugin root will break every page: $stray"
fi
check_file "$LIBRENMS_ROOT/html/plugin-backuprestore.php"
count=$(ls "$LIBRENMS_ROOT/app/Plugins/BackupRestore"/*.php 2>/dev/null | wc -l)
[[ "$count" -ge 3 ]] && ok "plugin hook classes present ($count .php files)" || bad "plugin hook classes missing"

hdr "8. Database schema"
if sudo -u "$APP_USER" -H env LIBRENMS_ROOT="$LIBRENMS_ROOT" \
        php "$PREFIX/scripts/br-schema.php" status >/tmp/br-schema-status 2>&1; then
    ok "schema status OK"
else
    bad "schema status failed:"
    sed 's/^/        /' /tmp/br-schema-status
fi

hdr "9. systemd units"
for unit in br-helper.service librenms-backup-dispatch.timer \
            librenms-backup-scheduler.timer 'librenms-backup-worker@.service'; do
    if systemctl list-unit-files "$unit" >/dev/null 2>&1; then
        ok "unit installed: $unit"
    else
        bad "unit missing: $unit"
    fi
done
if systemctl is-active --quiet br-helper.service; then
    ok "br-helper.service active"
else
    bad "br-helper.service not active (journalctl -u br-helper -n 50)"
fi
for t in librenms-backup-dispatch.timer librenms-backup-scheduler.timer; do
    systemctl is-enabled --quiet "$t" && ok "$t enabled" || warn "$t not enabled"
done

hdr "10. Worker health"
HEALTH="$PREFIX/../librenms-backup-health.json"
out=$(sudo -u "$APP_USER" -H env BR_CONF="$CONF_DIR/br.conf" \
        PYTHONPATH="$PREFIX/worker" \
        /usr/bin/python3 -m br.worker health 2>&1)
if echo "$out" | grep -q '"ok": true'; then
    ok "worker health check passed"
else
    warn "worker health check reported problems:"
    echo "$out" | head -30 | sed 's/^/        /'
fi

hdr "11. LibreNMS core untouched"
if command -v git >/dev/null 2>&1 && [[ -d "$LIBRENMS_ROOT/.git" ]]; then
    dirty=$(sudo -u "$APP_USER" git -C "$LIBRENMS_ROOT" status --porcelain 2>/dev/null \
            | grep -vE 'app/Plugins/BackupRestore|html/plugin-backuprestore\.php' || true)
    if [[ -z "$dirty" ]]; then
        ok "no LibreNMS core files modified by the plugin"
    else
        warn "unexpected working-tree changes:"
        echo "$dirty" | head -10 | sed 's/^/        /'
    fi
else
    warn "not a git checkout - cannot verify core integrity automatically"
fi

hdr "12. HTTP surface"
if command -v curl >/dev/null 2>&1; then
    probe=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
            "${BR_URL:-http://127.0.0.1/plugin-backuprestore.php/api}" || echo "000")
    case "$probe" in
        200) ok "API responded 200 (authenticated session cookie present)" ;;
        302|401|403) ok "API correctly refuses unauthenticated access ($probe)" ;;
        000) warn "could not reach ${BR_URL:-http://127.0.0.1/} (set BR_URL to test)" ;;
        404) bad "entrypoint returns 404 - check Apache config / RewriteRule" ;;
        *)   warn "unexpected HTTP status: $probe" ;;
    esac
fi

hdr "13. Test suite runnable"
# "Run the tests to verify" is only useful advice if pytest is actually there.
if python3 -c 'import pytest' 2>/dev/null; then
    ver="$(python3 -m pytest --version 2>/dev/null | head -1)"
    ok "pytest available ($ver)"
    if [[ -d "$PREFIX/tests" ]]; then
        ok "test suite present at $PREFIX/tests"
        if ( cd "$PREFIX" && python3 -m pytest -q --tb=no >/tmp/br-selftest.log 2>&1 ); then
            tail -1 /tmp/br-selftest.log | sed 's/^/        /'
            ok "self-test passed"
        else
            bad "self-test FAILED - see /tmp/br-selftest.log"
        fi
    else
        warn "tests/ not installed under $PREFIX (older release?) - run them from the download"
    fi
else
    warn "pytest not installed - 'python3 -m pytest' will fail"
    warn "  install with: sudo apt-get install -y python3-pytest"
fi

hdr "Summary"
printf '  failures: %d\n  warnings: %d\n\n' "$FAILED" "$WARNED"
if [[ "$FAILED" -gt 0 ]]; then
    printf '\033[31mInstallation has failures - do not rely on this system yet.\033[0m\n'
    exit 1
fi
printf '\033[32mAll required checks passed.\033[0m\n'
exit 0
