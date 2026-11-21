# LibreNMS Backup & Restore

Encrypted, verified, component-aware backups for LibreNMS — with a restore path
that has actually been rehearsed.

The usual LibreNMS backup advice is a cron job running `mysqldump` and a `tar` of
the install directory. That protects you against almost nothing: nobody tests the
restore, the dump is unencrypted on the same disk, and a half-written archive
looks identical to a good one until the day you need it.

This plugin treats the backup as a product: it is encrypted, it is verified at
three levels, every restore is preceded by a recovery checkpoint, and the restore
itself cannot leave your database empty if it fails.

## Install and set up

Follow these steps on the Linux server that already runs LibreNMS. You need an
administrator account that can use `sudo`. The commands below assume LibreNMS
is installed at `/opt/librenms` and that the LibreNMS service account is named
`librenms`.

### 1. Install the required packages

On Ubuntu or Debian, run:

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-pymysql python3-cryptography \
                        zstd mariadb-client rsync acl
```

### 2. Download the project

```bash
cd /tmp
git clone https://github.com/Santhosh2605/librenms-backup-restore.git
cd librenms-backup-restore
```

### 3. Run the installer

```bash
sudo bash scripts/install.sh
```

When asked `Continue? [y/N]`, type `y` and press Enter. The installer adds the
plugin to LibreNMS, creates its database tables and encryption key, and enables
the background services. It does not replace LibreNMS itself.

If LibreNMS is installed somewhere other than `/opt/librenms`, pass its path:

```bash
sudo LIBRENMS_ROOT=/srv/librenms bash scripts/install.sh
```

### 4. Save the encryption key off the server

The installer uses AES-256 encryption authenticated with HMAC-SHA256 and creates:

```text
/etc/librenms-backup/keys/passphrase
```

Copy the file into a secure password manager or other safe offline storage.
**You need this passphrase to decrypt your backups.** Never commit it to GitHub
or leave your only copy on the LibreNMS server. Do not replace it after backups
have been created. Existing `age` archives remain readable with their original
`identity.txt` key; keep that key if you have older archives.

### 5. Open the plugin and make a backup

1. Sign in to LibreNMS with an administrator account.
2. Open **Plugins → Backup & Restore**.
3. Choose **Run Full Backup Now** and wait for the job to complete.
4. Verify that the backup is marked **verified**.
5. Rehearse a restore on a test or staging server before relying on backups for
   recovery.

To save an archive to your computer, open **Backup History** and choose
**Download**. To restore an archive that is on your computer, open **Restore**,
choose the `.brarchive` file under **Add a backup to this server**, and select
**Upload and verify**. After verification completes, select that backup and run
the pre-flight checks before confirming the restore. Uploads remain on the
server; they are never sent to an external storage service.

The installer grants the PHP-FPM account access only to the upload intake
folder. It assumes the web account is `www-data`; for a custom PHP-FPM user,
install with `sudo WEB_USER=<php-fpm-user> bash scripts/install.sh`.

If the menu item does not appear, refresh LibreNMS's cached plugin list:

```bash
sudo -u librenms php /opt/librenms/artisan config:clear
```

For custom users, configuration options, and troubleshooting, see the
[installation reference](docs/INSTALL.md). For restore and recovery instructions,
see [RESTORE.md](docs/RESTORE.md).

### Optional: LibreNMS database-validator compatibility patch

Some LibreNMS versions report this plugin's `br_*` tables as unexpected and
offer to drop them. MariaDB also reports the plugin's JSON columns with the
`utf8mb4_bin` collation. **Do not use LibreNMS's automatic database repair for
these warnings**; its suggested SQL can delete the plugin's tables.

The project includes a preparer that backs up the current LibreNMS validator
files and generates a reviewable patch without modifying LibreNMS core. From
the project checkout, run:

```bash
sudo python3 scripts/prepare_librenms_validator_patch.py --librenms-root /opt/librenms
```

The command prints a timestamped bundle directory under
`plugin/support/librenms-validator/`. Review its `README.txt`, saved files in
`backup/`, patched copies in `patched/`, and `librenms-validator-fix.patch`.
Replace `<timestamp>` below with the directory name printed by the command:

```bash
PATCH="/opt/librenms-backup-restore/plugin/support/librenms-validator/<timestamp>/librenms-validator-fix.patch"
sudo patch --dry-run -p1 -d /opt/librenms < "$PATCH"
```

Continue only if the dry run succeeds for every file. Applying the patch changes
LibreNMS core files and adds a validator helper. Then install the plugin's
schema manifest and set the new helper's owner:

```bash
sudo patch -p1 -d /opt/librenms < "$PATCH"
sudo install -o librenms -g librenms -m 0644 \
  /opt/librenms-backup-restore/plugin/librenms-schema.json \
  /opt/librenms/app/Plugins/BackupRestore/librenms-schema.json
sudo chown librenms:librenms /opt/librenms/LibreNMS/DB/PluginSchema.php
sudo -u librenms /opt/librenms/validate.php
```

The validator should then accept the declared plugin tables and JSON columns.
It will still report unrelated unexpected tables or actual collation problems.
LibreNMS will warn that these core files are locally modified. **Do not run
`lnms patch:remove --reset`**; it discards local patches, including the helper.

#### Updating LibreNMS later

Keep the timestamped bundle from the preparer. It contains the original core
files and the patch. Before updating, replace `<timestamp>` below with that
bundle's name, then back up the currently working patch, restore the original
core files, and move the helper and manifest out of LibreNMS:

```bash
PROJECT=/opt/librenms-backup-restore
LIBRENMS=/opt/librenms
BUNDLE="$PROJECT/plugin/support/librenms-validator/<timestamp>"
SAVE="$PROJECT/plugin/support/librenms-validator/update-backups/$(date -u +%Y%m%dT%H%M%SZ)"
sudo install -d -m 0700 "$SAVE"
sudo cp -a "$LIBRENMS/LibreNMS/Validations/Database/CheckSchemaCollation.php" "$SAVE/"
sudo cp -a "$LIBRENMS/LibreNMS/Validations/Database/CheckSchemaStructure.php" "$SAVE/"
sudo cp -a "$LIBRENMS/database/migrations/2021_02_09_122930_migrate_to_utf8mb4.php" "$SAVE/"
sudo cp -a "$LIBRENMS/LibreNMS/DB/PluginSchema.php" "$SAVE/"
sudo cp -a "$LIBRENMS/app/Plugins/BackupRestore/librenms-schema.json" "$SAVE/"
sudo install -o librenms -g librenms -m 0644 "$BUNDLE/backup/LibreNMS/Validations/Database/CheckSchemaCollation.php" "$LIBRENMS/LibreNMS/Validations/Database/CheckSchemaCollation.php"
sudo install -o librenms -g librenms -m 0644 "$BUNDLE/backup/LibreNMS/Validations/Database/CheckSchemaStructure.php" "$LIBRENMS/LibreNMS/Validations/Database/CheckSchemaStructure.php"
sudo install -o librenms -g librenms -m 0644 "$BUNDLE/backup/database/migrations/2021_02_09_122930_migrate_to_utf8mb4.php" "$LIBRENMS/database/migrations/2021_02_09_122930_migrate_to_utf8mb4.php"
sudo mv "$LIBRENMS/LibreNMS/DB/PluginSchema.php" "$SAVE/"
sudo mv "$LIBRENMS/app/Plugins/BackupRestore/librenms-schema.json" "$SAVE/"
sudo -u librenms git -C "$LIBRENMS" status --short
```

Confirm the three validator files no longer appear as modified. If any local
LibreNMS files remain listed, stop and review them before updating. The `SAVE`
directory contains the working patch files in case you need to restore them.
If the update advances LibreNMS to a new revision, do not copy these old files
back over it; regenerate and review the patch for the new version.

Then update:

```bash
cd /opt/librenms
sudo -u librenms ./daily.sh
```

After the update, generate a **new** patch against the updated LibreNMS files.
Review it, dry-run it, and apply it using the steps above. Do not copy the old
patched files onto the updated core. If patch preparation or the dry run fails,
stop; the LibreNMS code changed and the patch needs review for the new version.

## What it does

- **Five components, selectable per run** — database, RRD, application, system,
  integrations. Back up everything, or just the config, or just the database.
- **Encrypted by default** — AES-256-CTR authenticated with HMAC-SHA256;
  keys are derived from the offline passphrase using PBKDF2-HMAC-SHA256.
- **Verified, not assumed** — `shallow` opens the container, `standard` checks
  every member's SHA-256, `deep` additionally gunzips the SQL dump and checks its
  schema statement and terminator.
- **Crash-safe restores** — the dump is imported into a scratch schema and
  promoted only after it succeeds. A failed restore cannot empty your database.
- **Rollback built in** — every restore first takes a verified recovery
  checkpoint, and retention never deletes one.
- **A real UI** — seven tabs: dashboard, history, manage, restore, scheduler,
  audit, settings. REST API underneath, so it is scriptable.

## Architecture

The browser talks to the web plugin, which queues work and accepts uploads only
into a dedicated ACL-protected intake folder. The unprivileged worker validates
uploads and handles archive storage. Only tightly scoped system actions pass
through the root helper.

![Architecture diagram showing browser, web app, worker, root helper, and local storage](docs/architecture.svg)

### Component reference

| Component | Runs as | Responsibility | Code |
|---|---|---|---|
| **Admin UI** | browser | 7 tabs, REST calls, no server-side logic | `plugin/resources/views/` |
| **Front controller** | `www-data` | Boots LibreNMS's Laravel app and attaches the plugin's route table | `front-controller/` |
| **22 API routes** | `www-data` | Read endpoints, mutations, CSRF, `admin` gate | `plugin/Controllers/` |
| **Eloquent models** | `www-data` | The 7 `br_*` tables | `plugin/Models/` |
| **Dispatcher** | `librenms` | Timer-driven; claims queued jobs and spawns one worker each | `worker/br/worker.py` |
| **Worker** | `librenms` | Backup, verify, restore, retention | `worker/br/` |
| **Archive pipeline** | `librenms` | `tar → zstd → AES-256 + HMAC-SHA256 → BRENC1`, streaming, constant memory | `worker/br/writer.py`, `crypto.py` |
| **Root helper** | `root` | 20 operations over a unix socket, HMAC-signed | `worker/br/helper.py` |
| **Path allowlist** | `root` | Rejects traversal, `/` as a target, and anything outside the configured roots | `helper.py` (`PathPolicy`) |

### Data flow, in words

1. **Backup** — the UI `POST`s a request; the web tier writes one row to
   `br_jobs` and returns immediately. The dispatcher timer claims the row and
   starts a worker. The worker collects components, streams them through
   compression and encryption, writes the archive and its sidecars, verifies it,
   and records the result.
2. **Upload/download** — uploads land in the ACL-protected intake folder and
   the worker validates and verifies them before registration. Authorized admins
   can download registered archives through the plugin.
3. **Restore** — the UI `POST`s with a typed confirmation; preflight runs seven
   checks, then the worker drives the nine-stage state machine. Privileged steps
   (import the database, install trees, restart services) go through the helper.
   A verified checkpoint is taken before anything is modified.
4. **Verify** — retrieves the archive, checks the container, every member's
   SHA-256, and at `deep` level gunzips the SQL dump to confirm its schema
   statements and terminator.

## Why the privilege model is unusual

Most backup plugins give PHP-FPM a sudo rule, which turns any PHP RCE into root.
Here the web tier has **no root privilege**: it can queue jobs and write uploads
only to the restricted intake folder. Actual work happens in a worker running
as `librenms`, which can invoke exactly one command:

```sudoers
librenms ALL=(root) NOPASSWD: /usr/local/libexec/librenms-backup/br-helper
```

That helper speaks a closed JSON protocol over a unix socket, signs every message
with HMAC-SHA256, validates every field, and executes argv lists — never a shell.
See [docs/SECURITY.md](docs/SECURITY.md) for the full threat model, including what
it deliberately does *not* protect against.

## Documentation

| Document | Contents |
|---|---|
| [INSTALL.md](docs/INSTALL.md) | Requirements, installer, keys, verification, troubleshooting table |
| [LIBRENMS-VALIDATOR-PATCH.md](docs/LIBRENMS-VALIDATOR-PATCH.md) | Prepare, review, and apply the optional LibreNMS schema-validator compatibility patch |
| [RESTORE.md](docs/RESTORE.md) | Restore runbook, rehearsal checklist, manual recovery, rollback |
| [SECURITY.md](docs/SECURITY.md) | Privilege model, helper protocol, secret handling, threat boundaries |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Process model, routes, data model, pipelines, failure modes |
| [SCHEDULER.md](docs/SCHEDULER.md) | Timers, job lifecycle, GFS retention |
| [STATUS.md](docs/STATUS.md) | Current build status by phase |
| [CHANGELOG.md](docs/CHANGELOG.md) | Release notes and known limitations |

## Testing

```bash
python3 -m pytest                     # 115 unit tests, no services needed
```

Integration tests exercise the API contract and self-skip unless you point them
at a live instance:

```bash
export BR_TEST_BASE_URL=https://librenms.example.test
export BR_TEST_USER=admin BR_TEST_PASS='...'
python3 -m pytest tests/integration -v
```

## CLI

The worker is usable without the web UI, which matters when the UI is what is
broken:

```bash
python3 -m br.worker backup    --job <uuid> --type full
python3 -m br.worker verify    --job <uuid> --backup <archive> --level deep
python3 -m br.worker restore   --job <uuid> --restore <id> --backup <archive>
python3 -m br.worker retention --job <uuid>
python3 -m br.worker health
```

`BR_CONF` selects the configuration file (`/etc/librenms-backup/br.conf` by
default); `PYTHONPATH=/opt/librenms-backup-restore/worker`.

## Requirements

Python 3.11+ · PHP 8.2+ · MariaDB 10.6+ · systemd 245+ · `zstd`.
Validated on Debian 13 / PHP 8.4 / MariaDB 11.8 against LibreNMS `dd4cebd7`;
target platform is Ubuntu 26.04 / PHP 8.5.

## License

See [LICENSE](LICENSE).
