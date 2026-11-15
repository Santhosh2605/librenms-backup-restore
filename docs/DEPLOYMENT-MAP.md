# Deployment map — files and permissions

Where every file goes on a real LibreNMS server, what mode it carries, and why.
Based on `scripts/install.sh`, which is authoritative: the defaults below are
exactly what it does when you run it with no arguments.

> **Default LibreNMS root is `/opt/librenms`.** The installer will use that
> unless you override it. If your LibreNMS lives elsewhere (the rehearsal host
> used `/srv/librenms`), set `LIBRENMS_ROOT` — see [Overriding paths](#overriding-paths).

---

## 1. Quick answer

```bash
# Extract anywhere; the installer copies what it needs.
sudo tar -xzf librenms-backup-restore-1.0.1.tar.gz -C /usr/local/src/
cd /usr/local/src/librenms-backup-restore-1.0.1
sudo ./scripts/install.sh
```

That command, with no flags, on a host with LibreNMS at `/opt/librenms`:

| What | Where | Owner | Mode |
|---|---|---|---|
| Plugin tree (21 files) | `/opt/librenms/app/Plugins/BackupRestore/` | `librenms:librenms` | `0755` dir / `0644` files |
| Front controller (1 file) | `/opt/librenms/html/plugin-backuprestore.php` | `librenms:librenms` | `0644` |
| Worker + scripts + docs | `/opt/librenms-backup-restore/` | `root:root` | `0755` dir / `0644` files, `0755` for `*.sh` |
| Helper entry point | `/usr/local/libexec/librenms-backup/br-helper` | `root:root` | `0755` |
| Configuration | `/etc/librenms-backup/br.conf` | `root:librenms` | `0640` |
| Encryption keys | `/etc/librenms-backup/keys/` | `root:librenms` | dir `0750`, files `0640` |
| Sudo policy | `/etc/sudoers.d/librenms-backup` | `root:root` | `0440` |
| systemd units | `/etc/systemd/system/` | `root:root` | `0644` |
| logrotate rule | `/etc/logrotate.d/librenms-backup` | `root:root` | `0644` |
| Archives / checkpoints / staging | `/var/backups/librenms/` | `root:librenms` | `0750`; subdirs `librenms:librenms` `0750` |
| Worker state, health, restore lock | `/var/lib/librenms-backup/` | `librenms:librenms` | `0750` |
| Logs | `/var/log/librenms-backup/` | `librenms:librenms` | `0750` |
| Socket + HMAC secret | `/run/librenms-backup/` | `root:librenms` | dir `0755`; secret `0640` |

**Only two locations are added to your LibreNMS tree**: one directory
(`app/Plugins/BackupRestore/`) and one file (`html/plugin-backuprestore.php`).
Nothing existing is edited — the footprint is purely additive.

---

## 2. Into `/opt/librenms` specifically

### 2.1 What goes in, exactly

```
/opt/librenms/
├── app/Plugins/BackupRestore/        ← added (entire plugin tree, 21 files)
│   ├── Menu.php         ← menu entry hook       (MenuEntryHook)
│   ├── Page.php         ← page render hook      (SinglePageHook)
│   ├── Settings.php     ← settings hook         (SettingsHook)
│   ├── Controllers/ Models/ Services/ Migrations/
│   └── resources/views/ (+ assets/backup-restore.{css,js})
└── html/
    └── plugin-backuprestore.php      ← added (front controller, 22 API routes)
```

That is the entire LibreNMS-side footprint: **one directory and one file.**

Only the three top-level classes (`Menu.php`, `Page.php`, `Settings.php`) are
directly visible to LibreNMS — it globs `app/Plugins/*/*.php` for hook classes.
The rest of the tree is pulled in by those classes and by the front controller.
This is why the whole `plugin/` directory is installed rather than two files:
LibreNMS cannot autoload routes or controllers from a local plugin, so the
support classes must ship alongside the hooks.

### 2.2 Why the front controller lives in `html/`

LibreNMS offers local plugins exactly one HTTP surface: the generic
`plugin/{plugin_name}` route. That cannot host 22 endpoints or accept `POST`.
So the installer drops one file into `html/`, which boots the LibreNMS Laravel
application and attaches the plugin's own route table — the same pattern
LibreNMS core uses for `html/api_v0.php`. No core routing file is touched.

```
GET /plugin/BackupRestore            ← core route, renders the shell
GET /plugin-backuprestore.php?r=…    ← plugin routes, data + mutations
GET /plugin-backuprestore.php?r=assets/…  ← plugin CSS/JS
```

### 2.3 What stays outside `/opt/librenms` (and must)

The worker, helper, config and archives deliberately live elsewhere. The worker
needs to survive a restore that overwrites the application tree, and the
encryption keys must never sit inside a directory that gets archived.

---

## 3. Required permissions

### 3.1 The rule that makes it work

Three identities are in play:

| Identity | Runs | Needs |
|---|---|---|
| `www-data` | LibreNMS web tier | **read** the plugin files; **no** shell, **no** sudo |
| `librenms` | worker + dispatcher | **read** config and keys; **write** archives, state, logs |
| `root` | `br-helper` only | write restored trees, restart services, import the database |

The web tier deliberately has **no** privilege. It writes a row to `br_jobs` and
returns. A systemd timer picks the job up. This is why the sudoers file contains
one argument-less grant rather than a list of commands.

### 3.2 Directory ownership at a glance

```
/opt/librenms/app/Plugins/BackupRestore/  librenms:librenms   0755   (0644 files, 21 total)
/opt/librenms/html/plugin-backuprestore.php  librenms:librenms 0644
/opt/librenms-backup-restore              root:root           0755
/usr/local/libexec/librenms-backup        root:root           0755   (br-helper 0755)
/etc/librenms-backup                      root:librenms       0750
/etc/librenms-backup/keys                 root:librenms       0750   (0640 files)
/etc/librenms-backup/br.conf              root:librenms       0640
/etc/sudoers.d/librenms-backup            root:root           0440
/var/backups/librenms                     root:librenms       0750
/var/backups/librenms/{archives,staging,recovery-checkpoints}  librenms:librenms 0750
/var/lib/librenms-backup                  librenms:librenms   0750
/var/log/librenms-backup                  librenms:librenms   0750
/run/librenms-backup                      root:librenms       0755
```

Why `root:librenms 0640` on the config: the worker (as `librenms`) must read it;
nobody else may. `0640` means the group can read and the world cannot. The
`librenms` group is what grants that read.

Why `/var/backups/librenms` is `root:librenms 0750`: the worker may enter and
create subdirectories, but only the specific subdirectories are owned by
`librenms`. That stops a compromise of the worker from rewriting the archive
directory root itself.

### 3.3 The sudoers grant

```
Defaults:librenms env_reset
Defaults:librenms secure_path="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
librenms ALL=(root) NOPASSWD: /usr/local/libexec/librenms-backup/br-helper
```

> **Do not add `!requiretty` or `!logfile`.** Ubuntu 26.04 ships **sudo-rs**,
> which implements a subset of the sudoers grammar and rejects the *entire file*
> if any `Defaults` option is unknown to it. Either line produces
> `unknown setting: 'requiretty'` and blocks the install — and because a
> rejected file in `/etc/sudoers.d` stops `sudo` working altogether, a careless
> copy can lock you out. Both lines are no-ops on sudo 1.9+ (where `requiretty`
> already defaults off and no `logfile` is configured).

One line, no arguments, so a caller cannot influence what runs. The wrapper is
three lines and does nothing but `exec /usr/bin/python3 -m br.helper "$@"` —
that module is at a fixed root-owned path, so `librenms` cannot alter what gets
executed as root.

Verify with `sudo visudo -c` after install.

### 3.4 Filesystem permissions the *restore* needs

This is the part that is easy to get wrong, and it is not about modes — it is
about the helper's own sandbox and allowlist.

**Path allowlist** (`worker/br/helper.py`, `PathPolicy`):

| Class | Roots |
|---|---|
| writable | `backup_root`, `staging_dir`, `checkpoint_dir` |
| restore targets | `librenms_root`, `/etc`, `/var/lib`, `/var/log/librenms-backup` |
| readable | the above + `/var/backups`, `/opt`, `/usr/lib/nagios` |

Any path outside these is refused with `path outside writable roots`.

**systemd hardening** (`config/systemd/br-helper.service`). A restore writes
`/etc` and the application tree, so a read-only system cannot host one:

```ini
ProtectSystem=no
ReadWritePaths=/run/librenms-backup /var/lib/librenms-backup /var/backups/librenms \
               /var/log/librenms-backup /etc /opt/librenms
```

> **Fixed in 1.0.1.** The unit previously shipped `ProtectSystem=full`, which
> mounts `/etc` and `/usr` read-only. A restore would have failed at
> `RESTORING_CONFIGURATION` on any systemd host. Verified with `systemd-run`:
> under the old settings `/etc` rejected writes; under the new ones it accepts
> them. `check-package.sh` now fails the build if `ProtectSystem` is tightened
> again or a required path drops out of `ReadWritePaths`.
>
> If your LibreNMS root is not `/opt/librenms`, add yours to `ReadWritePaths`.

### 3.5 Database grants

The restore imports into a scratch schema before promoting it, so the LibreNMS
database user needs rights on `<db>__%`, not just `<db>`:

```sql
GRANT ALL PRIVILEGES ON `librenms`.*    TO 'librenms'@'localhost';
GRANT ALL PRIVILEGES ON `librenms\_\_%`.* TO 'librenms'@'localhost';
FLUSH PRIVILEGES;
```

`install.sh` applies both automatically and prints the statement if it cannot.
Missing the second grant is the cause of the 1044 error during restores.

### 3.6 SELinux / AppArmor

Not required on a stock Ubuntu or Debian install. If you run SELinux in
enforcing mode, the helper needs write access to the paths in §3.4 — either
label them, or add a local policy. Nothing in the plugin requires disabling
either system.

---

## 3.7 What a restore actually overwrites

A restore is a **selective merge, not a wipe**. Understanding this matters
before you rely on it to recover from a compromise.

| Surface | Restored? | Controlled by |
|---|---|---|
| Database | yes — full replacement of the schema | always |
| `config.php`, `.env`, `bootstrap/cache/config.php` | yes | `application_paths` |
| `includes/definitions/custom`, `html/includes/custom`, `resources/views/overrides` | yes (if present) | `application_paths` |
| `rrd/` | yes | `rrd_paths` |
| `/etc/...` captured trees | yes | `system_paths` |
| Integration dirs (`/usr/lib/nagios/plugins`, …) | yes | `integration_paths` |
| **Anything else in `html/`, `app/`, `includes/`** | **no — left as-is** | not collected in the first place |

The default `application_paths` is deliberately narrow:

```ini
application_paths = config.php
                    .env
                    bootstrap/cache/config.php
                    includes/definitions/custom
                    html/includes/custom
                    resources/views/overrides
```

So a file dropped into `html/` after the backup survives the restore. That is
by design — the archive only contains what it collected — but state it plainly
rather than let anyone assume a restore is a clean-room rebuild.

**If your LibreNMS has bespoke files under `html/` or `app/`**, add those paths
to `application_paths` before relying on the backup. Whole-directory entries are
captured recursively; note that adding a large tree increases archive size and
backup time proportionally.

---

## 4. Overriding paths

Every path is a variable at the top of `install.sh`, overridable by environment:

```bash
sudo LIBRENMS_ROOT=/srv/librenms ./scripts/install.sh
```

| Variable | Default | Meaning |
|---|---|---|
| `LIBRENMS_ROOT` | `/opt/librenms` | Existing LibreNMS installation |
| `PREFIX` | `/opt/librenms-backup-restore` | Where the worker and scripts live |
| `APP_USER` / `APP_GROUP` | `librenms` | Application user and group |
| `BACKUP_ROOT` | `/var/backups/librenms` | Archives, checkpoints, staging |

`CONF_DIR`, `KEY_DIR`, `STATE_DIR`, `LOG_DIR`, `RUN_DIR` and `HELPER_DIR` are
fixed in the script — the systemd units and the sudoers rule reference them by
absolute path, so changing them means editing those files too.

**After installing to a non-default root, you must also:**

1. Set `librenms_root` in `/etc/librenms-backup/br.conf` (the installer does
   this for you when it creates the file, but not if the file already existed).
2. Add that root to `ReadWritePaths=` in
   `/etc/systemd/system/br-helper.service`.
3. Update `integration_paths` in `br.conf` — it ships pointing at
   `/opt/librenms/includes/services`.
4. `sudo systemctl daemon-reload && sudo systemctl restart br-helper`.

Step 2 is not optional: omit it and restores of the application tree fail with
a read-only filesystem error.

---

## 5. Verifying a deployment

```bash
# 13 sections: code, helper, sudoers, config+keys, storage, web exposure,
# plugin registration, DB schema, systemd units, worker health, core integrity,
# HTTP surface
sudo /opt/librenms-backup-restore/scripts/validate-install.sh

# ownership and modes only
sudo stat -c '%U:%G %04a %n' \
  /opt/librenms/app/Plugins/BackupRestore/Page.php \
  /opt/librenms/html/plugin-backuprestore.php \
  /etc/librenms-backup/br.conf \
  /etc/librenms-backup/keys/identity.txt \
  /etc/sudoers.d/librenms-backup \
  /var/backups/librenms /var/lib/librenms-backup

# helper alive and answering
sudo -u librenms -H env BR_CONF=/etc/librenms-backup/br.conf \
  PYTHONPATH=/opt/librenms-backup-restore/worker \
  /usr/bin/python3 -m br.worker health

# sudoers still parses
sudo visudo -c | tail -2

# API reachable and authenticated
curl -s -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1/plugin-backuprestore.php?r=br/status
```

Then load **Administration → Backup & Restore** (menu entry under **Plugins**).
An empty Dashboard with `helper: reachable` means the install is sound.

`validate-install.sh` exits non-zero if anything fails, so it doubles as a
post-deployment gate in configuration management.

---

## 6. Common permission failures

| Symptom | Cause | Fix |
|---|---|---|
| `helper: unreachable` on the Dashboard | helper not started, or socket group wrong | `systemctl status br-helper`; socket must be `root:librenms 0660` |
| `path outside writable roots` | restore staging outside `BACKUP_ROOT` | keep `staging_dir` under `backup_root` |
| `path outside readable roots` | integration path outside the allowlist | add it to `ReadWritePaths` and the helper policy |
| `Read-only file system` during restore | `ProtectSystem=full`, or root missing from `ReadWritePaths` | see §3.4 |
| `ERROR 1044` on restore | missing `<db>__%` grant | see §3.5 |
| Plugin loads but API returns 404 | front controller not in `html/` | re-run `install.sh`, or copy it manually |
| Menu entry missing | LibreNMS cached its plugin list | `sudo -u librenms php /opt/librenms/artisan config:clear` |
| Dashboard shows zero backups after a rebuild | `br_backups` is not derived from disk | `... -m br.worker adopt` |

---

## 7. Files the plugin never touches

For audit purposes, a complete install modifies none of these:

```
/opt/librenms/app/**            (except adding app/Plugins/BackupRestore/)
/opt/librenms/includes/**
/opt/librenms/routes/**
/opt/librenms/config/**
/opt/librenms/composer.json
```

A LibreNMS upgrade therefore cannot revert the plugin, and the plugin cannot
break an upgrade. The only upgrade-time step is re-running `install.sh` if the
plugin itself is being upgraded — `scripts/upgrade.sh` handles that, keeping the
previous version in `/opt/librenms-backup-restore.previous`.

---

*See also: `docs/INSTALL.md` for the full procedure, `docs/SECURITY.md` for the
threat model, and `docs/PROJECT-DOCUMENTATION.html` for the project report.*
