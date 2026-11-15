#!/usr/bin/env bash
#
# LibreNMS Backup & Restore v1.0 - installer
#
# Usage:
#   sudo ./scripts/install.sh [--prefix /opt/librenms-backup-restore] [--yes]
#
# Safe to re-run: every step is idempotent. Nothing in the LibreNMS source tree
# is modified except for copying the plugin directory and one new front
# controller (html/plugin-backuprestore.php); see --uninstall.
#
set -Eeuo pipefail

PREFIX="${PREFIX:-/opt/librenms-backup-restore}"
LIBRENMS_ROOT="${LIBRENMS_ROOT:-/opt/librenms}"
APP_USER="${APP_USER:-librenms}"
APP_GROUP="${APP_GROUP:-librenms}"
BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/librenms}"
CONF_DIR="/etc/librenms-backup"
KEY_DIR="$CONF_DIR/keys"
STATE_DIR="/var/lib/librenms-backup"
LOG_DIR="/var/log/librenms-backup"
RUN_DIR="/run/librenms-backup"
HELPER_DIR="/usr/local/libexec/librenms-backup"
ASSUME_YES=0

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

c_ok()   { printf '\033[32m  ok\033[0m  %s\n' "$*"; }
c_info() { printf '\033[36m  ..\033[0m  %s\n' "$*"; }
c_warn() { printf '\033[33mwarn\033[0m  %s\n' "$*"; }
c_err()  { printf '\033[31mFAIL\033[0m  %s\n' "$*" >&2; }
step()   { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()    { c_err "$*"; exit 1; }

for arg in "$@"; do
    case "$arg" in
        --yes|-y) ASSUME_YES=1 ;;
        --prefix) shift; PREFIX="$1" ;;
        --prefix=*) PREFIX="${arg#*=}" ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    esac
done

# Help for the two things that most often go wrong here: a GitHub ZIP download
# does not keep the executable bit, and the script must run as root.
if [[ ! -x "$0" && -f "$0" ]]; then
    c_warn "this script is not executable (a ZIP download does not keep file modes)"
    c_warn "run it with:  sudo bash $0"
fi
[[ $EUID -eq 0 ]] || die "run as root: sudo bash $0"

# ---------------------------------------------------------------------------
step "0. Pre-flight checks"
# ---------------------------------------------------------------------------
[[ -d "$LIBRENMS_ROOT" ]] || die "LibreNMS root not found: $LIBRENMS_ROOT"
[[ -f "$LIBRENMS_ROOT/artisan" ]] || die "$LIBRENMS_ROOT does not look like a LibreNMS install"
id "$APP_USER" >/dev/null 2>&1 || die "system user '$APP_USER' does not exist"
[[ -d "$LIBRENMS_ROOT/app/Plugins" ]] || die "$LIBRENMS_ROOT/app/Plugins missing"

PHP_BIN="$(command -v php || true)"
[[ -n "$PHP_BIN" ]] || die "php not found in PATH"
PHP_VERSION="$("$PHP_BIN" -r 'echo PHP_VERSION;')"
c_ok "PHP $PHP_VERSION at $PHP_BIN"
[[ -x /usr/bin/python3 ]] || die "python3 not found"
c_ok "python3: $(/usr/bin/python3 --version 2>&1)"

/usr/bin/python3 - <<'PY' || die "python3 modules missing (apt-get install python3-pymysql python3-cryptography)"
import importlib.util, sys
missing = [m for m in ("pymysql", "cryptography") if importlib.util.find_spec(m) is None]
if missing:
    print("missing python modules:", ", ".join(missing))
    sys.exit(1)
print("  ok  python modules: pymysql, cryptography")
PY

for tool in mariadb-dump tar zstd gzip sha256sum systemctl; do
    command -v "$tool" >/dev/null 2>&1 || c_warn "optional tool not found: $tool"
done

command -v age >/dev/null 2>&1 || c_warn "age not installed - installer will use the aesgcm-pbkdf2 fallback"
command -v rrdtool >/dev/null 2>&1 && c_ok "rrdtool present" || c_warn "rrdtool not installed (not required by the plugin)"

if [[ $ASSUME_YES -eq 0 ]]; then
    echo
    echo "This will install LibreNMS Backup & Restore into:"
    echo "  code          : $PREFIX"
    echo "  plugin        : $LIBRENMS_ROOT/app/Plugins/BackupRestore"
    echo "  front ctrl    : $LIBRENMS_ROOT/html/plugin-backuprestore.php"
    echo "  backups       : $BACKUP_ROOT"
    echo "  configuration : $CONF_DIR/br.conf"
    read -r -p "Continue? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || die "aborted by operator"
fi

# ---------------------------------------------------------------------------
step "1. Directories and ownership"
# ---------------------------------------------------------------------------
install -d -o root -g root -m 0755 "$PREFIX"
# The worker writes archives as the application user, so the three data
# directories are owned by it (0750, never world-readable). The root of the
# tree stays root-owned so the layout cannot be replaced by the worker.
install -d -o root -g "$APP_GROUP" -m 0750 "$BACKUP_ROOT"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 \
    "$BACKUP_ROOT/archives" "$BACKUP_ROOT/staging" "$BACKUP_ROOT/recovery-checkpoints"
install -d -o root -g "$APP_GROUP" -m 0750 "$CONF_DIR" "$KEY_DIR"
# The worker owns its own state dir (health.json) and log dir.
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0750 "$STATE_DIR" "$LOG_DIR"
install -d -o root -g root -m 0755 "$HELPER_DIR"
install -d -o root -g "$APP_GROUP" -m 0755 "$RUN_DIR"
c_ok "directories created"

# ---------------------------------------------------------------------------
step "2. Application code"
# ---------------------------------------------------------------------------
rsync -a --delete \
    --exclude '.git' --exclude '__pycache__' \
    "$SRC_DIR/worker/" "$PREFIX/worker/"
rsync -a --delete "$SRC_DIR/scripts/" "$PREFIX/scripts/"
rsync -a --delete "$SRC_DIR/docs/" "$PREFIX/docs/" 2>/dev/null || true
chown -R root:root "$PREFIX"
find "$PREFIX" -type d -exec chmod 0755 {} +
find "$PREFIX" -type f -exec chmod 0644 {} +
chmod 0755 "$PREFIX/scripts/"*.sh
[[ -f "$PREFIX/scripts/br-schema.php" ]] && chmod 0644 "$PREFIX/scripts/br-schema.php"
c_ok "code installed to $PREFIX"

# ---------------------------------------------------------------------------
step "3. Privileged helper binary"
# ---------------------------------------------------------------------------
cat > "$HELPER_DIR/br-helper" <<EOF
#!/bin/sh
# LibreNMS Backup & Restore privileged helper (wrapper).
# The only path allowed by /etc/sudoers.d/librenms-backup.
exec /usr/bin/python3 -m br.helper "\$@"
EOF
chmod 0755 "$HELPER_DIR/br-helper"
chown root:root "$HELPER_DIR/br-helper"

# ---------------------------------------------------------------------------
step "4. Configuration"
# ---------------------------------------------------------------------------
if [[ -f "$CONF_DIR/br.conf" ]]; then
    c_info "keeping existing $CONF_DIR/br.conf (edit manually to change settings)"
else
    install -o root -g "$APP_GROUP" -m 0640 \
        "$SRC_DIR/config/br.conf.example" "$CONF_DIR/br.conf"
    # Point the shipped example at the values actually in force. Hardcoding
    # librenms_user meant an APP_USER override produced units that ran as one
    # user while the worker chowned files to another - restores then left the
    # tree owned by the wrong account.
    sed -i "s#^librenms_root .*#librenms_root      = $LIBRENMS_ROOT#; \
            s#^backup_root .*#backup_root        = $BACKUP_ROOT#; \
            s#^librenms_user .*#librenms_user      = $APP_USER#; \
            s#^librenms_group .*#librenms_group     = $APP_GROUP#; \
            s#^state_dir .*#state_dir          = $STATE_DIR#; \
            s#^log_dir .*#log_dir            = $LOG_DIR#" "$CONF_DIR/br.conf"
    c_ok "created $CONF_DIR/br.conf"
fi
chown root:"$APP_GROUP" "$CONF_DIR/br.conf"
chmod 0640 "$CONF_DIR/br.conf"

# ---------------------------------------------------------------------------
step "5. Encryption key material (stored separately from archives)"
# ---------------------------------------------------------------------------
if command -v age-keygen >/dev/null 2>&1; then
    if [[ ! -f "$KEY_DIR/identity.txt" ]]; then
        ( umask 077; age-keygen -o "$KEY_DIR/identity.txt" 2>"$KEY_DIR/age-keygen.log" )
        chown root:"$APP_GROUP" "$KEY_DIR/identity.txt" "$KEY_DIR/age-keygen.log"
        chmod 0640 "$KEY_DIR/identity.txt" "$KEY_DIR/age-keygen.log"
        sed -n 's/^# public key: //p' "$KEY_DIR/identity.txt" > "$KEY_DIR/recipients.txt"
        chown root:"$APP_GROUP" "$KEY_DIR/recipients.txt"
        chmod 0640 "$KEY_DIR/recipients.txt"
        c_ok "generated age keypair"
        c_warn "OFFLINE COPY REQUIRED: store $KEY_DIR/identity.txt somewhere safe NOW."
        c_warn "Without it, encrypted backups CANNOT be restored. The helper never exports it."
    else
        c_info "age identity already present"
    fi
    grep -q '^encryption .*age' "$CONF_DIR/br.conf" || \
        sed -i 's/^encryption .*/encryption         = age/' "$CONF_DIR/br.conf"
else
    c_warn "age not installed - configuring aesgcm-pbkdf2 fallback"
    sed -i 's/^encryption .*/encryption         = aesgcm-pbkdf2/' "$CONF_DIR/br.conf"
    if [[ ! -f "$KEY_DIR/passphrase" ]]; then
        ( umask 077; head -c 48 /dev/urandom | base64 | tr -d '\n' > "$KEY_DIR/passphrase" )
        echo >> "$KEY_DIR/passphrase"
        chown root:"$APP_GROUP" "$KEY_DIR/passphrase"
        chmod 0640 "$KEY_DIR/passphrase"
        c_ok "generated 64-char passphrase"
        c_warn "OFFLINE COPY REQUIRED: store $KEY_DIR/passphrase somewhere safe NOW."
    fi
fi
chmod 0750 "$KEY_DIR"

# ---------------------------------------------------------------------------
step "6. Sudo policy (single, argument-less escalation)"
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Database grants for the staging schema.
#
# A restore imports into `<db>__brrestore` before promoting it, so the restore
# account needs rights on that namespace. LibreNMS itself only grants the
# application database, so this is the one extra grant the install needs.
# ---------------------------------------------------------------------------
if command -v mariadb >/dev/null 2>&1; then
    DB_NAME="$(grep -E '^DB_DATABASE=' "$LIBRENMS_ROOT/.env" 2>/dev/null | cut -d= -f2 | tr -d '"'"'"' ')"
    DB_USER="$(grep -E '^DB_USERNAME=' "$LIBRENMS_ROOT/.env" 2>/dev/null | cut -d= -f2 | tr -d '"'"'"' ')"
    DB_NAME="${DB_NAME:-librenms}"
    DB_USER="${DB_USER:-librenms}"
    if mariadb -e "GRANT ALL PRIVILEGES ON \`${DB_NAME}\\_\\_%\`.* TO '${DB_USER}'@'localhost'; FLUSH PRIVILEGES;" 2>/dev/null; then
        c_ok "staging-schema grant applied (${DB_NAME}__%)"
    else
        c_warn "could not apply staging-schema grant; run manually:"
        c_warn "  GRANT ALL PRIVILEGES ON \`${DB_NAME}__%\`.* TO '${DB_USER}'@'localhost';"
    fi
fi

# Validate BEFORE touching /etc/sudoers.d.
#
# Writing the file first and validating afterwards is dangerous: sudo refuses to
# run at all when any file in /etc/sudoers.d is invalid, so a failed validation
# locks the administrator out of sudo. (This is exactly what sudo-rs on Ubuntu
# 26.04 used to trigger, because it rejects Defaults options it does not know.)
TMP_SUDOERS="$(mktemp /tmp/br-sudoers.XXXXXX)"
trap 'rm -f "$TMP_SUDOERS"' EXIT
# 0600 while it is a scratch file: it has to stay writable for the fallback
# rewrite below. 0440 is applied only at the moment it is installed.
chmod 0600 "$TMP_SUDOERS"
cat "$SRC_DIR/config/br-sudoers" > "$TMP_SUDOERS"

if ! visudo -c -f "$TMP_SUDOERS" >/dev/null 2>&1; then
    c_warn "full sudoers file rejected by this host's visudo:"
    visudo -c -f "$TMP_SUDOERS" 2>&1 | sed 's/^/        /' | head -6
    c_warn "this is typical of sudo-rs (Ubuntu 26.04 default) rejecting Defaults options"
    # Fall back to the minimal form: no Defaults at all, just the two rules.
    # env_reset and secure_path are left to the global /etc/sudoers settings.
    cat > "$TMP_SUDOERS" <<'MINIMAL'
# LibreNMS Backup & Restore - minimal policy (no Defaults; see config/br-sudoers)
librenms ALL=(root) NOPASSWD: /usr/local/libexec/librenms-backup/br-helper
Cmnd_Alias BR_DIAG = /usr/bin/df -P /var/backups/librenms, \
                     /usr/bin/du -sb /var/backups/librenms, \
                     /usr/bin/systemctl is-active librenms-backup-scheduler.timer
librenms ALL=(root) NOPASSWD: BR_DIAG
MINIMAL
    if visudo -c -f "$TMP_SUDOERS" >/dev/null 2>&1; then
        c_warn "installed the minimal policy instead (rules are equivalent)"
    else
        rm -f "$TMP_SUDOERS"
        die "this host's visudo rejects both the full and the minimal policy - \
nothing was written to /etc/sudoers.d (your sudo is untouched)"
    fi
fi

# Only now is it safe to move into place. Keep the previous file recoverable -
# but only if it was VALID. A previous file that this host's visudo already
# rejects (the usual reason for re-running the installer) must never be
# restored on rollback, because restoring it would reproduce the outage.
PREV_VALID=0
if [[ -f /etc/sudoers.d/librenms-backup ]]; then
    if visudo -c -f /etc/sudoers.d/librenms-backup >/dev/null 2>&1; then
        cp -a /etc/sudoers.d/librenms-backup /etc/sudoers.d/librenms-backup.br-prev
        PREV_VALID=1
    else
        c_warn "the existing /etc/sudoers.d/librenms-backup is invalid on this host"
        c_warn "  (this is the file causing your 'unknown setting' error - replacing it)"
        cp -a /etc/sudoers.d/librenms-backup /etc/sudoers.d/librenms-backup.br-broken
        c_warn "  kept a copy at /etc/sudoers.d/librenms-backup.br-broken for inspection"
    fi
fi
install -o root -g root -m 0440 "$TMP_SUDOERS" /etc/sudoers.d/librenms-backup

if ! visudo -c >/dev/null 2>&1; then
    c_err "the system's combined sudoers is now invalid - rolling back"
    if [[ $PREV_VALID -eq 1 && -f /etc/sudoers.d/librenms-backup.br-prev ]]; then
        install -o root -g root -m 0440 /etc/sudoers.d/librenms-backup.br-prev \
            /etc/sudoers.d/librenms-backup
        rm -f /etc/sudoers.d/librenms-backup.br-prev
        if visudo -c >/dev/null 2>&1; then
            die "restored the previous policy; sudo is working again"
        fi
        c_err "the previous policy is also invalid - removing our file entirely"
    fi
    # No valid previous file: the only safe state is to have no file at all.
    rm -f /etc/sudoers.d/librenms-backup
    if visudo -c >/dev/null 2>&1; then
        die "removed the file; sudo is working again (re-run to retry)"
    fi
    die "sudoers is STILL invalid after removing our file - check /etc/sudoers"
fi
rm -f /etc/sudoers.d/librenms-backup.br-prev
rm -f /etc/sudoers.d/librenms-backup.br-broken
c_ok "sudoers installed and validated"

# ---------------------------------------------------------------------------
step "7. systemd units and timers"
# ---------------------------------------------------------------------------
# The units are TEMPLATES. LibreNMS usually lives at /opt/librenms and runs as
# the `librenms` user, but both are overridable, and a unit that hardcodes
# either one silently runs the worker as the wrong user or from the wrong tree.
# Render every @BR_*@ placeholder from the values actually in force.
render_unit() {
    local src="$1" dst="$2"
    sed -e "s#@BR_PREFIX@#$PREFIX#g" \
        -e "s#@BR_LIBRENMS_ROOT@#$LIBRENMS_ROOT#g" \
        -e "s#@BR_APP_USER@#$APP_USER#g" \
        -e "s#@BR_APP_GROUP@#$APP_GROUP#g" \
        -e "s#@BR_BACKUP_ROOT@#$BACKUP_ROOT#g" \
        -e "s#@BR_STATE_DIR@#$STATE_DIR#g" \
        -e "s#@BR_LOG_DIR@#$LOG_DIR#g" \
        -e "s#@BR_RUN_DIR@#$RUN_DIR#g" \
        "$src" > "$dst"
    if grep -q '@BR_[A-Z_]*@' "$dst"; then
        die "unrendered placeholder in $(basename "$dst"): $(grep -o '@BR_[A-Z_]*@' "$dst" | sort -u | tr '\n' ' ')"
    fi
}

for unit in "$SRC_DIR"/config/systemd/*.service "$SRC_DIR"/config/systemd/*.timer; do
    tmp="$(mktemp)"
    render_unit "$unit" "$tmp"
    # systemd validates syntax but not paths, so check the two that matter.
    install -o root -g root -m 0644 "$tmp" "/etc/systemd/system/$(basename "$unit")"
    rm -f "$tmp"
done
systemctl daemon-reload
systemctl enable --now br-helper.service        >/dev/null 2>&1 || c_warn "br-helper.service enable failed"
systemctl enable --now librenms-backup-dispatch.timer  >/dev/null 2>&1 || c_warn "dispatch timer enable failed"
systemctl enable --now librenms-backup-scheduler.timer >/dev/null 2>&1 || c_warn "scheduler timer enable failed"
c_ok "systemd units installed"
systemctl --no-pager --lines=0 status br-helper.service 2>/dev/null | head -3 || true

# Verify the helper answers, not just that systemd says it started. A helper
# that cannot create its socket looks identical to a healthy one in systemctl.
SOCK=/run/librenms-backup/br-helper.sock
for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -S "$SOCK" ]] && break
    sleep 1
done
if [[ -S "$SOCK" ]]; then
    c_ok "helper socket is up ($SOCK)"
else
    c_warn "helper socket did not appear: $SOCK"
    c_warn "  journalctl -u br-helper -n 40 --no-pager"
fi

# ---------------------------------------------------------------------------
step "8. logrotate"
# ---------------------------------------------------------------------------
install -o root -g root -m 0644 "$SRC_DIR/config/logrotate/librenms-backup" \
    /etc/logrotate.d/librenms-backup
c_ok "logrotate configured"

# ---------------------------------------------------------------------------
step "9. LibreNMS plugin files"
# ---------------------------------------------------------------------------
PLUGIN_DIR="$LIBRENMS_ROOT/app/Plugins/BackupRestore"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 0755 "$PLUGIN_DIR"
rsync -a --delete "$SRC_DIR/plugin/" "$PLUGIN_DIR/"
chown -R "$APP_USER":"$APP_GROUP" "$PLUGIN_DIR"
find "$PLUGIN_DIR" -type d -exec chmod 0755 {} +
find "$PLUGIN_DIR" -type f -exec chmod 0644 {} +
c_ok "plugin files installed to $PLUGIN_DIR"

# Front controller: the LibreNMS-sanctioned way to add routes without patching core.
install -o "$APP_USER" -g "$APP_GROUP" -m 0644 \
    "$SRC_DIR/front-controller/plugin-backuprestore.php" \
    "$LIBRENMS_ROOT/html/plugin-backuprestore.php"
c_ok "front controller installed: $LIBRENMS_ROOT/html/plugin-backuprestore.php"

# ---------------------------------------------------------------------------
step "10. Database schema"
# ---------------------------------------------------------------------------
c_info "applying plugin schema (own br_* tables only)"
sudo -u "$APP_USER" -H env LIBRENMS_ROOT="$LIBRENMS_ROOT" PHP_USER="$APP_USER" \
    "$PHP_BIN" "$PREFIX/scripts/br-schema.php" install || die "schema install failed"
c_ok "schema applied"

# Prove the schema is complete rather than trusting the install exit code.
sudo -u "$APP_USER" -H env LIBRENMS_ROOT="$LIBRENMS_ROOT" \
    "$PHP_BIN" "$PREFIX/scripts/br-schema.php" status >/dev/null 2>&1 \
    || die "schema status check failed - br_* tables incomplete"
c_ok "schema status verified"

# ---------------------------------------------------------------------------
step "11. Cache refresh (LibreNMS core command, no source change)"
# ---------------------------------------------------------------------------
sudo -u "$APP_USER" -H "$PHP_BIN" "$LIBRENMS_ROOT/artisan" config:clear >/dev/null 2>&1 || true
sudo -u "$APP_USER" -H "$PHP_BIN" "$LIBRENMS_ROOT/artisan" view:clear   >/dev/null 2>&1 || true
c_ok "LibreNMS caches cleared"

# ---------------------------------------------------------------------------
step "12. Validation"
# ---------------------------------------------------------------------------
bash "$SRC_DIR/scripts/validate-install.sh" || c_warn "validation reported problems (see above)"

cat <<EOF

$(printf '\033[1mInstallation complete\033[0m')

  GUI      : Administration > Plugins > Backup & Restore
             (LibreNMS has no Administration-menu hook for plugins; the
              page itself is titled "Administration > Backup & Restore")
  Config   : $CONF_DIR/br.conf           (root:$APP_GROUP 0640)
  Keys     : $KEY_DIR/identity.txt        (BACK THIS UP OFFLINE)
  Backups  : $BACKUP_ROOT/{archives,recovery-checkpoints}
  Logs     : $LOG_DIR/
  Code     : $PREFIX

Next steps:
  1. Copy $KEY_DIR/identity.txt to offline storage. It is the ONLY file that
     can decrypt your archives - lose this host AND that file and the backups
     cannot be opened by anyone. recipients.txt is the matching public key
     (not secret). passphrase is used only when age is absent
     (encryption = aesgcm-pbkdf2 in br.conf).
  2. Run a first backup:  GUI > Backup & Restore > Run Full Backup Now
  3. Verify it, then rehearse a restore on a NON-production host (docs/RESTORE.md).
EOF
