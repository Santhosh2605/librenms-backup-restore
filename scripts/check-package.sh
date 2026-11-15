#!/usr/bin/env bash
#
# LibreNMS Backup & Restore - package integrity check
#
# Usage: ./scripts/check-package.sh [--with-tests]
#
# Validates that a release tarball is structurally complete and internally
# consistent WITHOUT needing LibreNMS, MariaDB, or root. Use it in CI, and after
# packaging, to catch the class of bug where a file is referenced by the
# installer but missing from the archive.
#
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_TESTS=0
[[ "${1:-}" == "--with-tests" ]] && WITH_TESTS=1

FAILED=0
WARNED=0

ok()   { printf '\033[32m  PASS\033[0m  %s\n' "$*"; }
bad()  { printf '\033[31m  FAIL\033[0m  %s\n' "$*"; FAILED=$((FAILED + 1)); }
warn() { printf '\033[33m  WARN\033[0m  %s\n' "$*"; WARNED=$((WARNED + 1)); }
hdr()  { printf '\n\033[1m%s\033[0m\n' "$*"; }

hdr "Layout"
for path in \
    plugin front-controller worker/br config scripts docs tests \
    README.md LICENSE pytest.ini
do
    if [[ -e "$HERE/$path" ]]; then ok "$path"; else bad "missing: $path"; fi
done

hdr "Installer payload (files the installer copies must exist)"
for path in \
    config/br.conf.example config/br-sudoers config/logrotate/librenms-backup \
    docs/DEPLOYMENT-MAP.md \
    docs/INSTALL-STEP-BY-STEP.md \
    docs/GITHUB-DESCRIPTION.md \
    config/systemd/br-helper.service \
    config/systemd/librenms-backup-dispatch.service \
    config/systemd/librenms-backup-dispatch.timer \
    config/systemd/librenms-backup-scheduler.service \
    config/systemd/librenms-backup-scheduler.timer \
    config/systemd/librenms-backup-worker@.service \
    scripts/br-schema.php scripts/install.sh scripts/uninstall.sh \
    scripts/upgrade.sh scripts/validate-install.sh \
    front-controller/plugin-backuprestore.php
do
    if [[ -f "$HERE/$path" ]]; then ok "$path"; else bad "installer needs: $path"; fi
done

hdr "Plugin surface"
for path in \
    plugin/Controllers/BackupRestoreController.php \
    plugin/Services/ApiKernel.php plugin/Services/Guard.php \
    plugin/Services/Settings.php plugin/Services/Formatter.php \
    plugin/Services/Version.php plugin/Menu.php plugin/Page.php \
    plugin/Settings.php \
    plugin/Migrations/2026_11_11_000100_create_backup_restore_tables.php \
    plugin/resources/views/page.blade.php \
    plugin/resources/views/menu.blade.php \
    plugin/resources/views/settings.blade.php \
    plugin/resources/views/assets/backup-restore.js \
    plugin/resources/views/assets/backup-restore.css
do
    if [[ -f "$HERE/$path" ]]; then ok "$path"; else bad "missing: $path"; fi
done

for model in Job Backup Restore Checkpoint Schedule AuditEvent; do
    [[ -f "$HERE/plugin/Models/$model.php" ]] \
        && ok "plugin/Models/$model.php" \
        || bad "missing model: $model.php"
done

hdr "Worker modules"
for mod in worker/br/archive.py worker/br/checksums.py worker/br/collect.py \
           worker/br/common.py worker/br/config.py worker/br/crypto.py \
           worker/br/db.py worker/br/helper.py worker/br/helper_client.py \
           worker/br/manifest.py worker/br/restore.py worker/br/retention.py \
           worker/br/state.py worker/br/verify.py worker/br/worker.py \
           worker/br/writer.py worker/br/__init__.py
do
    if [[ -f "$HERE/$mod" ]]; then ok "$mod"; else bad "missing: $mod"; fi
done

hdr "Syntax"
if command -v php >/dev/null 2>&1; then
    php_fail=0
    while IFS= read -r f; do
        php -l "$f" >/dev/null 2>&1 || { bad "php syntax: ${f#"$HERE"/}"; php_fail=1; }
    done < <(find "$HERE/plugin" "$HERE/front-controller" "$HERE/scripts" -name '*.php' -type f)
    [[ $php_fail -eq 0 ]] && ok "all PHP files parse"
else
    warn "php not installed - skipping PHP syntax"
fi

if command -v python3 >/dev/null 2>&1; then
    py_fail=0
    while IFS= read -r f; do
        python3 -m py_compile "$f" 2>/dev/null || { bad "python syntax: ${f#"$HERE"/}"; py_fail=1; }
    done < <(find "$HERE/worker" -name '*.py' -type f)
    [[ $py_fail -eq 0 ]] && ok "all Python files compile"
else
    warn "python3 not installed - skipping Python syntax"
fi

bash_fail=0
while IFS= read -r f; do
    bash -n "$f" 2>/dev/null || { bad "bash syntax: ${f#"$HERE"/}"; bash_fail=1; }
done < <(find "$HERE/scripts" -name '*.sh' -type f)
[[ $bash_fail -eq 0 ]] && ok "all shell scripts parse"

hdr "Security invariants"
SUDOERS="$HERE/config/br-sudoers"
if grep -qE '^(www-data|apache|nginx)\b' "$SUDOERS" 2>/dev/null; then
    bad "sudoers grants privileges to a web-server user"
else
    ok "no web-tier sudo entry"
fi

esc=$(grep -E '^librenms\s+ALL=' "$SUDOERS" 2>/dev/null | grep -v 'br-helper' | grep -v 'BR_DIAG' || true)
[[ -z "$esc" ]] && ok "single documented escalation path" \
                 || bad "unexpected sudo escalation: $esc"

# AST-based, not grep: a grep hits docstrings (which document that shell=True is
# banned) and compiled .pyc files. Parse the source and look for a real
# keyword argument.
if python3 - "$HERE/worker" <<'PYCHK'
import ast
import pathlib
import sys

offenders = []
root = pathlib.Path(sys.argv[1])
for path in root.rglob("*.py"):
    if "__pycache__" in path.parts:
        continue
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        continue
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "shell" and isinstance(kw.value, ast.Constant) \
                        and kw.value.value is True:
                    offenders.append(f"{path.name}:{node.lineno}")
if offenders:
    print("shell=True at " + ", ".join(offenders))
    sys.exit(1)
PYCHK
then
    ok "worker never uses a shell (AST check)"
else
    bad "shell=True found in worker (must be argv-only)"
fi

if grep -rn --include='*.py' 'os\.system(' "$HERE/worker" 2>/dev/null | grep -q .; then
    bad "os.system() found in worker"
else
    ok "worker never uses os.system"
fi

helper_unit="$HERE/config/systemd/br-helper.service"
grep -q 'RestrictAddressFamilies=AF_UNIX' "$helper_unit" 2>/dev/null \
    && ok "helper restricted to AF_UNIX" \
    || warn "helper AF_UNIX restriction missing"

grep -q 'NoNewPrivileges=yes' "$helper_unit" 2>/dev/null \
    && ok "helper NoNewPrivileges" \
    || warn "helper NoNewPrivileges missing"

# ProtectSystem=full mounts /etc and /usr read-only. A restore writes /etc, so
# the helper would fail at RESTORING_CONFIGURATION on any systemd host.
if grep -qE '^ProtectSystem=(full|strict|yes)' "$helper_unit" 2>/dev/null; then
    bad "helper unit sets ProtectSystem=$(sed -n 's/^ProtectSystem=//p' "$helper_unit" | head -1)"\
" - /etc must stay writable for restores"
else
    ok "helper unit leaves /etc writable (restore target)"
fi

# Every path a restore writes must appear in ReadWritePaths. The unit is a
# template now, so compare against the RENDERED default form rather than the
# raw file, and check the placeholder it should use.
rendered_helper="$(sed -e 's#@BR_PREFIX@#/opt/librenms-backup-restore#g' \
    -e 's#@BR_LIBRENMS_ROOT@#/opt/librenms#g' -e 's#@BR_APP_USER@#librenms#g' \
    -e 's#@BR_APP_GROUP@#librenms#g' -e 's#@BR_BACKUP_ROOT@#/var/backups/librenms#g' \
    -e 's#@BR_STATE_DIR@#/var/lib/librenms-backup#g' -e 's#@BR_LOG_DIR@#/var/log/librenms-backup#g' \
    -e 's#@BR_RUN_DIR@#/run/librenms-backup#g' "$helper_unit")"
for need in /etc /var/backups/librenms /var/lib/librenms-backup /var/log/librenms-backup; do
    if printf '%s\n' "$rendered_helper" \
        | awk '/^ReadWritePaths=/{f=1} f{printf "%s", $0} f&&!/\\$/{exit}' \
        | grep -q -- "$need"; then
        ok "ReadWritePaths covers $need"
    else
        bad "ReadWritePaths missing $need"
    fi
done

# Templating must cover the user, so an APP_USER override is honoured.
hdr "systemd unit templating"
for tpl in @BR_APP_USER@ @BR_APP_GROUP@ @BR_PREFIX@ @BR_LIBRENMS_ROOT@; do
    if grep -q -- "$tpl" "$HERE"/config/systemd/librenms-backup-worker@.service \
       || grep -q -- "$tpl" "$HERE"/config/systemd/br-helper.service; then
        ok "units use $tpl"
    else
        bad "units hardcode the value for $tpl (overrides would be ignored)"
    fi
done
if grep -qE '^User=librenms$' "$HERE"/config/systemd/*.service; then
    bad "a unit still hardcodes User=librenms"
else
    ok "no unit hardcodes User=librenms"
fi
if grep -q 'render_unit' "$HERE/scripts/install.sh"; then
    ok "install.sh renders the unit templates"
else
    bad "install.sh does not render unit templates"
fi

hdr "Sudo policy portability"
# sudo-rs (Ubuntu 26.04 default) rejects the whole file if any Defaults option
# is unknown to it. !requiretty and !logfile are both unsupported there, and a
# rejected file in /etc/sudoers.d can lock the administrator out of sudo.
for opt in requiretty logfile; do
    if grep -qE "^Defaults:[^ ]+ !?${opt}\b" "$HERE/config/br-sudoers"; then
        bad "br-sudoers uses '$opt' - rejected by sudo-rs on Ubuntu 26.04"
    else
        ok "br-sudoers avoids '$opt' (sudo-rs compatible)"
    fi
done

# Docs are copied by hand, so a bad snippet in a doc recreates the outage just
# as effectively as a bad config file. Flag any doc that still PRINTS the line.
doc_hits="$(grep -rnE "^Defaults:[^ ]+ !?(requiretty|logfile)\b" \
    "$HERE"/docs "$HERE"/README.md 2>/dev/null || true)"
if [[ -n "$doc_hits" ]]; then
    bad "documentation still shows a sudo-rs-incompatible Defaults line"
    printf '        %s\n' "$doc_hits"
else
    ok "no documentation prints requiretty/logfile as a snippet"
fi
# The install must validate before writing, never after.
if grep -q 'visudo -c -f "$TMP_SUDOERS"' "$HERE/scripts/install.sh"; then
    ok "install.sh validates sudoers in a scratch file first"
else
    bad "install.sh does not pre-validate sudoers (a bad file can break sudo)"
fi

hdr "README architecture diagram"
if grep -q '^```mermaid$' "$HERE/README.md"; then
    ok "README has a mermaid diagram"

    # The diagram states counts. If the wording changes so the count can no
    # longer be found, that is drift too - so "missing" is a failure, not a skip.
    check_count() {
        local label="$1" expect="$2" real="$3"
        if ! grep -q -- "$label" "$HERE/README.md"; then
            bad "README no longer states '$label' (diagram drift)"
        elif [[ "$real" == "$expect" ]]; then
            ok "README '$label' matches code ($real)"
        else
            bad "README claims '$label' but code has $real"
        fi
    }
    check_count "22 routes" "22" \
        "$(grep -c 'Route::get\|Route::post' "$HERE/front-controller/plugin-backuprestore.php")"
    check_count "20 operations" "20" \
        "$(sed -n '/^OPERATIONS = {/,/^}/p' "$HERE/worker/br/helper_client.py" \
           | grep -oE '"[a-z_.]+"' | wc -l)"

    # Every code path named in the component table must exist.
    missing=0
    while read -r path; do
        [[ -z "$path" ]] && continue
        [[ -e "$HERE/$path" ]] || { bad "diagram references missing path: $path"; missing=1; }
    done < <(sed -n '/^### Component reference/,/^###/p' "$HERE/README.md" \
             | grep -oE '`(worker|plugin|front-controller)/[A-Za-z0-9_./-]+`' | tr -d '`' | sort -u)
    [[ $missing -eq 0 ]] && ok "diagram component paths all exist"
else
    bad "README has no mermaid architecture diagram"
fi

hdr "Version consistency"
PY_VER="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$HERE/worker/br/common.py" | head -1)"
PHP_VER="$(sed -n "s/.*const VERSION = '\(.*\)';.*/\1/p" "$HERE/plugin/Services/Version.php" | head -1)"
if [[ -z "$PY_VER" || -z "$PHP_VER" ]]; then
    bad "could not read a version from worker/br/common.py or plugin/Services/Version.php"
elif [[ "$PY_VER" == "$PHP_VER" ]]; then
    ok "worker and plugin agree on version $PY_VER"
else
    bad "version drift: worker=$PY_VER plugin=$PHP_VER"
fi
# The README must document BOTH ways to get the code: the release tarball
# (carries the version) and the GitHub ZIP (folder name has no version).
if grep -q "librenms-backup-restore-$PY_VER.tar.gz\|librenms-backup-restore-main" \
    "$HERE/README.md"; then
    ok "README documents how to obtain the code"
else
    bad "README does not document tarball or ZIP download"
fi
if grep -q -- "-main" "$HERE/README.md"; then
    ok "README covers the GitHub ZIP folder name (-main)"
else
    bad "README does not warn about the ZIP folder name"
fi
if grep -q "chmod +x" "$HERE/README.md"; then
    ok "README covers restoring the executable bit lost by ZIP"
else
    bad "README does not mention chmod +x for ZIP downloads"
fi
if sed -n '/^## \[/p' "$HERE/docs/CHANGELOG.md" | head -1 | grep -q "\[$PY_VER\]"; then
    ok "CHANGELOG has a $PY_VER section"
else
    warn "CHANGELOG has no top [$PY_VER] section"
fi

hdr "Documentation"
for doc in docs/INSTALL.md docs/RESTORE.md docs/SECURITY.md \
           docs/ARCHITECTURE.md docs/SCHEDULER.md docs/CHANGELOG.md
do
    if [[ -f "$HERE/$doc" ]]; then
        lines=$(wc -l <"$HERE/$doc")
        [[ $lines -gt 40 ]] && ok "$doc ($lines lines)" || warn "$doc looks stubby ($lines lines)"
    else
        bad "missing doc: $doc"
    fi
done

hdr "Tests"
count=$(find "$HERE/tests" -name 'test_*.py' -type f | wc -l | tr -d ' ')
[[ $count -gt 0 ]] && ok "$count test module(s) present" || bad "no tests found"

if [[ $WITH_TESTS -eq 1 ]]; then
    if python3 -c 'import pytest' 2>/dev/null; then
        if (cd "$HERE" && python3 -m pytest -q >/tmp/br-pytest.log 2>&1); then
            ok "pytest: $(tail -1 /tmp/br-pytest.log)"
        else
            bad "pytest failed - see /tmp/br-pytest.log"
        fi
    else
        warn "pytest not installed - skipping test run"
    fi
fi

hdr "Result"
if [[ $FAILED -eq 0 ]]; then
    printf '\033[32mPackage OK\033[0m (%d warning(s))\n' "$WARNED"
    exit 0
fi
printf '\033[31m%d check(s) failed\033[0m (%d warning(s))\n' "$FAILED" "$WARNED"
exit 1
