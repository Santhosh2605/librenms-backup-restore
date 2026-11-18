# Installation

Target: **Ubuntu 26.04 / PHP 8.5**, also validated on Debian 13 / PHP 8.4.
Existing LibreNMS install assumed (validated against `dd4cebd7`).

## sudo-rs on Ubuntu 26.04

Ubuntu 26.04 defaults to **sudo-rs**, which accepts only a subset of the sudoers
grammar. It rejects the whole file when it meets a `Defaults` option it does not
implement — `requiretty` and `logfile` among them — and any invalid file in
`/etc/sudoers.d` stops `sudo` working entirely.

`config/br-sudoers` therefore uses only `env_reset` and `secure_path`, and
`install.sh` validates the policy in a scratch file before installing it. If your
host still rejects it, the installer falls back to a minimal policy with no
`Defaults` at all and the same two rules.

Check yours with:

```bash
sudo -V | head -2        # "Sudo version 1.9.x" = original sudo
                         # sudo-rs reports "sudo-rs <version>"
visudo -c -f /etc/sudoers.d/librenms-backup
```

## 1. Requirements

| Component | Minimum | Notes |
|---|---|---|
| Python | 3.11 (3.13 tested) | Worker and helper |
| PHP | 8.2 (8.4/8.5 tested) | Runs the plugin |
| MariaDB | 10.6 (11.8 tested) | Same instance LibreNMS uses |
| `zstd` | any | Compression |
| systemd | 245+ | Timers and instance units |
| `python3-cryptography` | any | AES-256 encryption and HMAC-SHA256 |
| `acl` | any | Restricts PHP-FPM write access to the archive upload intake folder |

```bash
sudo apt-get install -y acl python3 zstd python3-cryptography \
                        mariadb-client rsync
```

## 2. Unpack

```bash
sudo mkdir -p /opt/librenms-backup-restore
sudo tar -xzf librenms-backup-restore-*.tar.gz -C /opt/librenms-backup-restore --strip-components=1
sudo chown -R root:root /opt/librenms-backup-restore
```

The tree must be root-owned: the worker reads code from it and must not be able to
modify it.

## 3. Run the installer

```bash
sudo ./scripts/install.sh
```

It is idempotent. It performs these steps:

1. Creates `/etc/librenms-backup/{keys}`, `/var/backups/librenms/{archives,staging,recovery-checkpoints}`,
   `/var/lib/librenms-backup`, `/var/log/librenms-backup`, `/run/librenms-backup`.
2. Installs `br.conf` from `config/br.conf.example` (does not overwrite an
   existing file).
3. Installs `br-sudoers` to `/etc/sudoers.d/librenms-backup` (`0440`, validated
   with `visudo -c`).
4. Installs the helper wrapper to `/usr/local/libexec/librenms-backup/br-helper`.
5. Installs and enables the systemd units, then `daemon-reload`.
6. Copies the plugin to `<librenms>/app/Plugins/BackupRestore/` and the front
   controller to `<librenms>/html/plugin-backuprestore.php`.
7. Creates the database schema (`scripts/br-schema.php install`).
8. Creates `/run/librenms-backup` with the right ownership for the helper socket.
9. Creates a private archive upload folder readable by the worker and writable
   by the PHP-FPM user (`www-data` by default). For a different PHP-FPM user,
   run `sudo WEB_USER=<php-fpm-user> bash scripts/install.sh`.

Set PHP `upload_max_filesize` and `post_max_size` above the largest archive you
plan to upload (the plugin limit is 50 GiB). If nginx fronts PHP-FPM, also set
`client_max_body_size` to that size. Restart PHP-FPM and the web server after
changing their limits.

### Ownership decisions the installer makes

| Path | Owner | Mode | Why |
|---|---|---|---|
| `/var/backups/librenms` | `root:librenms` | `0750` | Layout cannot be replaced by the worker |
| `.../archives`, `.../staging`, `.../recovery-checkpoints` | `librenms:librenms` | `0750` | Worker writes here |
| `/var/lib/librenms-backup` | `librenms:librenms` | `0750` | Worker owns its state (health file, restore lock) |
| `/var/log/librenms-backup` | `librenms:librenms` | `0750` | Worker writes logs |
| `/run/librenms-backup` | `root:librenms` | `0755` | Helper socket; worker must reach it, not own it |
| `/etc/librenms-backup` | `root:librenms` | `0750` | Config readable by worker, writable only by root |

If you install by hand, get these right — a root-owned state dir means the
worker cannot write `health.json` and the UI reports `helper: unreachable`.

## 4. Configure

Edit `/etc/librenms-backup/br.conf`. The values you are most likely to change:

```ini
[general]
librenms_root = /opt/librenms

[archive]
encryption = aes256-ctr-hmac-sha256
compression = zstd        # zstd | gzip

[components]
rrd = true                # set false if you do not want RRD in the archive
```

The file is read by the worker and the helper only. **Never put secrets in it.**

## 5. Keys

### AES-256 and HMAC-SHA256 (default)

```bash
sudo cat /etc/librenms-backup/keys/passphrase
```

Save the generated passphrase in secure offline storage. It is required to
decrypt these backups. Keep any existing `age` identity file as well if you
need to restore archives created by older versions.

`age` remains supported for reading older archives. To create an `age` archive
with a legacy installation, set `encryption = age` and keep the matching
recipient and identity files. New installs should keep the AES setting above.

## 6. Enable the plugin

```bash
sudo -u librenms php /opt/librenms-backup-restore/scripts/br-schema.php install
```

This creates the seven `br_*` tables **and** inserts/reactivates the `plugins`
row. If you have just restored a database from a backup, re-run it: a restored
`plugins` table may not contain this plugin.

## 7. Start the services

```bash
sudo systemctl enable --now br-helper.service
sudo systemctl enable --now librenms-backup-dispatch.timer
sudo systemctl enable --now librenms-backup-scheduler.timer
sudo systemctl status br-helper.service --no-pager
```

## 8. Verify

```bash
sudo ./scripts/validate-install.sh
```

It checks, in order: directory ownership and modes, sudoers validity, helper
socket and HMAC file, systemd unit states, database schema version, plugin row,
key material permissions, and a real end-to-end backup in a scratch directory.

If everything passes, open **LibreNMS → Plugins → Backup & Restore**. The
dashboard should show a green worker status with `helper: reachable`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `helper: unreachable` | Socket missing, or no permission to the HMAC file | `systemctl restart br-helper`; check `ls -l /run/librenms-backup/` |
| `health file not readable` | `/var/lib/librenms-backup` not worker-owned | `chown librenms:librenms /var/lib/librenms-backup` |
| Jobs stay `queued` | Dispatcher timer not running, or spawn denied | `systemctl list-timers`, then `journalctl -u librenms-backup-dispatch` |
| `Can't create TCP/IP socket` | Helper is `AF_UNIX`-only and the DB was reached over TCP | Ensure `/run/mysqld/mysqld.sock` exists; the worker picks it up automatically |
| `Access denied` starting worker | Something calls `systemctl` unprivileged | Jobs must spawn through the helper's `worker.spawn` |
| Plugin page 404s | Front controller missing, or routes not prepended | Check `<librenms>/html/plugin-backuprestore.php` exists and is `0644 librenms:librenms` |
| Assets 404 | Same as above; assets use `?r=assets/...` | Verify with `curl '<base>/plugin-backuprestore.php?r=assets/backup-restore.css'` |
| `Data too long for column 'retention_class'` | Old schema (`varchar(8)`) | Re-run `br-schema.php install` |
| Backup verified but restore fails at `RESTORING_DATABASE` | Stale dump or socket mismatch | Check `basename` of the failure in `job.log`; verify with `deep` level |

Logs: `/var/log/librenms-backup/{backup,verify,restore,dispatch,scheduler,health}.log`
and `journalctl -u br-helper`.

## Uninstall

```bash
sudo ./scripts/uninstall.sh          # keeps archives and config
sudo ./scripts/uninstall.sh --purge  # removes archives and config too
```

It refuses to run if a restore is in progress.
