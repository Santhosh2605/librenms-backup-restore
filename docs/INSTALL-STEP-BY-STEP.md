# Installation — step by step

This guide assumes **no Linux experience**. If you can copy and paste a command
and press Enter, you can do this.

You will need:

- About **15 minutes**.
- A LibreNMS server you can log in to as an administrator.
- The ability to run commands as `root` (usually by putting `sudo` in front).

> **Before you start:** take a copy of your LibreNMS configuration. This
> installer only adds files — it never edits LibreNMS itself — but a backup you
> already trust costs nothing.

---

## Step 0 — Check which user runs LibreNMS

Run this:

```bash
ls -ld /opt/librenms
```

The first two columns look like `drwxr-xr-x librenms librenms`. The third word
is the **owner** — normally `librenms`. If yours is different, note it down;
you will need it in Step 4.

If the folder does not exist at all, find it with:

```bash
ps aux | grep -i fpm | head -3
```

---

## Step 1 — Install the four prerequisites

The plugin needs a few standard packages. Copy this whole block and paste it as
one command:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-pymysql python3-cryptography \
                        zstd age mariadb-client
```

**What each one does:**

| Package | Why it is needed |
|---|---|
| `python3` | Runs the background worker that does the actual backup |
| `python3-pymysql` | Lets the worker talk to the database |
| `python3-cryptography` | The encryption fallback, if `age` is missing |
| `zstd` | Compresses the archive |
| `age` | Encrypts the archive (modern, small, easy to use) |
| `mariadb-client` | Dumps and restores the database |

Check they all arrived:

```bash
python3 -c "import pymysql, cryptography; print('python modules OK')"
command -v age zstd mariadb-dump tar
```

You should see four paths printed after the `OK` line. If any line says
"not found", repeat Step 1.

---

## Step 2 — Download the code

### Option A — download the ZIP (no git needed)

1. Open the project page on GitHub.
2. Click the green **Code** button, then **Download ZIP**.
3. You now have a file like `librenms-backup-restore-main.zip` in your Downloads
   folder.

Copy it to the server and unpack it:

```bash
sudo apt-get install -y unzip
cd /tmp
sudo unzip -o ~/librenms-backup-restore-main.zip
```

> **Important — two things about ZIP files**
>
> **1. The folder name has `-main` on the end.** A GitHub ZIP unpacks to
> `librenms-backup-restore-main`, while the release tarball unpacks to
> `librenms-backup-restore-1.0.1`. Both contain the same code. Use whichever
> name you actually see — check with `ls /tmp`.
>
> **2. ZIP does not keep file permissions.** After unpacking, the scripts are
> not marked executable, so `./scripts/install.sh` fails with
> **`Permission denied`**. Fix it with:

```bash
sudo chmod +x /tmp/librenms-backup-restore-main/scripts/*.sh
```

### Option B — clone with git

```bash
cd /tmp
sudo git clone https://github.com/<your-user>/librenms-backup-restore.git
```

This keeps file permissions, so the `chmod` line above is not needed.

---

## Step 2.5 — The three users, and why it matters

A standard LibreNMS install on `/opt/librenms` involves **three** user accounts.
You never have to create any of them — the installer uses the existing ones —
but knowing which is which explains every permission message you might see.

| User | What it is | What the plugin uses it for |
|---|---|---|
| `root` | The administrator | The privileged helper (`br-helper.service`) — the only thing that can write to `/opt/librenms` |
| `librenms` | LibreNMS's own account | The worker that builds archives, and the owner of the restored files |
| `www-data` | The web server (Apache/nginx) | Only renders the plugin pages. It **cannot** run backups or touch keys |

The important consequence: **`www-data` is deliberately not in the `librenms`
group.** So any config the plugin needs at page-render time has to come from the
database, not from `/etc/librenms-backup/br.conf`. That file is `0640
root:librenms` — readable by the worker, invisible to the web server, which is
exactly right.

### If your LibreNMS uses a different account

Some installations run LibreNMS as `nmsapp`, `apache`, or something else. Find
out with:

```bash
ls -ld /opt/librenms
```

The third and fourth words are the owner and group. Pass them to the installer:

```bash
sudo APP_USER=nmsapp APP_GROUP=nmsapp ./scripts/install.sh
```

The installer now rewrites the systemd units and `br.conf` to use whatever you
pass — so the service really does run as that account, and restores chown files
to it rather than to `librenms`. (Before version 1.0.2 the units hardcoded
`User=librenms`, which meant the override changed the config but not the
services. That mismatch is fixed.)

The web server account is **not** something you set here. The plugin reads its
settings from LibreNMS's own database, so it works regardless of whether your
web tier runs as `www-data`, `apache`, or `nginx`.

To confirm what got installed:

```bash
grep -E '^User=|^Group=' /etc/systemd/system/librenms-backup-*.service
grep -E '^librenms_(user|group)' /etc/librenms-backup/br.conf
```

Both should show the same account.

---

## Step 3 — Copy it into place

The plugin's own code lives in `/opt/librenms-backup-restore`, **separate from
LibreNMS**, so upgrading one never disturbs the other.

```bash
sudo mkdir -p /opt/librenms-backup-restore
sudo cp -a /tmp/librenms-backup-restore-main/. /opt/librenms-backup-restore/
cd /opt/librenms-backup-restore
ls
```

You should see `README.md`, `scripts`, `plugin`, `worker`, `docs` and a few
others. If you see another folder called `librenms-backup-restore-main` inside,
you copied one level too high — go back and re-run the `cp` line, keeping the
`/.` on the end.

---

## Step 4 — Run the installer

```bash
cd /opt/librenms-backup-restore
sudo ./scripts/install.sh
```

If you get **`Permission denied`**, use this instead (it does the same thing and
works even when permissions were lost in the ZIP):

```bash
sudo bash scripts/install.sh
```

If your LibreNMS is **not** at `/opt/librenms`, say where it is:

```bash
sudo LIBRENMS_ROOT=/srv/librenms bash scripts/install.sh
```

### What you will see

The installer does twelve things and prints each one. Expect `ok` or `warn`
lines. A `warn` is usually fine; `FAIL` is not, and it stops.

At the start it shows a summary and asks:

```
Continue? [y/N]
```

Type `y` and press Enter.

To skip the question entirely (for scripted installs):

```bash
sudo bash scripts/install.sh --yes
```

### Read this when it appears

```
warn  OFFLINE COPY REQUIRED: store /etc/librenms-backup/keys/identity.txt
      somewhere safe NOW.
warn  Without it, encrypted backups CANNOT be restored.
```

This is the single most important thing in the whole install. **Do it now, not
later.** See Step 5.

---

## Step 5 — Save your encryption key somewhere else

Your backups are encrypted. The key that decrypts them lives on the server. If
the server is lost, the key is lost with it — and so are the backups.

### Which file exactly?

The keys directory has up to three files, but **only one of them matters**:

| File | What it is | Back it up? |
|---|---|---|
| `identity.txt` | The **private key**. Decrypts every archive. | **Yes — this is the one.** |
| `recipients.txt` | The matching *public* key. Used to encrypt, not decrypt. | No — not secret, but harmless to keep |
| `passphrase` | Only created if `age` is missing (`encryption = aesgcm-pbkdf2`) | Only if it exists |

Run this to print the key:

```bash
sudo cat /etc/librenms-backup/keys/identity.txt
```

Copy the whole output into your password manager, or write it down and put it in
a safe. Then **delete it from your shell history**:

```bash
history -d $(history 1 | awk '{print $1}')
```

> **Never store this key on the same server as the backups**, and never commit
> it to git. It is the only thing standing between a stolen archive and your
> data.
>
> **Do not run `age-keygen` again.** The installer already created this key. A
> second key would silently make every existing backup unreadable.

---

## Step 6 — Open the plugin

1. Log in to LibreNMS as an administrator.
2. Look in the top menu for **Plugins → Backup & Restore**.
3. You should see a dashboard with seven tabs.

If the menu entry is missing, LibreNMS has cached its plugin list:

```bash
sudo -u librenms php /opt/librenms/artisan config:clear
```

Then reload the page.

If the dashboard loads but says **`helper: unreachable`**, the background
service is not running:

```bash
sudo systemctl status br-helper
sudo systemctl restart br-helper
sudo journalctl -u br-helper -n 30 --no-pager
```

---

## Step 7 — Take your first backup

1. Go to the **Backup Management** tab.
2. Leave the type as **full**.
3. Click **Run backup**.
4. Watch the progress bar. It ends at `completed` with a verification result.

Then click **Backup History** — your archive should be listed as `verified`.

If it is not `verified`, do not rely on it. Check:

```bash
sudo tail -n 30 /var/log/librenms-backup/job.log
```

---

## Step 8 — Prove the restore works

A backup you have never restored is a hope, not a backup.

1. Go to the **Restore** tab and pick the archive you just made.
2. The preflight panel runs seven checks. All seven should be green.
3. Type `RESTORE` to confirm.
4. Watch it go through its stages to `COMPLETED`.

This restores your own data over itself — the safest possible rehearsal. Do it
once now, while everything is calm, rather than discovering a problem during an
outage.

---

## Troubleshooting

| What you see | What it means | What to do |
|---|---|---|
| `Permission denied` running install.sh | ZIP lost the executable bit | `sudo bash scripts/install.sh` |
| `No such file or directory` | You are not in the right folder | `cd /opt/librenms-backup-restore` then retry |
| `LibreNMS root not found: /opt/librenms` | LibreNMS lives elsewhere | `sudo LIBRENMS_ROOT=/your/path bash scripts/install.sh` |
| `unknown setting: 'requiretty'` from visudo | Old version, or sudo-rs quirk | Update to the current release; it is fixed |
| `Unexpected server response (500)` in the GUI | A PHP error while serving the page | Two different logs — see "Where the logs are" below |
| `python3 modules missing` | Step 1 incomplete | `sudo apt-get install -y python3-pymysql python3-cryptography` |
| `helper: unreachable` | Service not started | `sudo systemctl restart br-helper` |
| Menu entry missing | LibreNMS cache | `sudo -u librenms php /opt/librenms/artisan config:clear` |
| `ERROR 1044` during restore | Missing database grant | Re-run the installer; it applies it |
| Backup shows `unverified` | Archive did not verify | Check `job.log`, then delete and retake |

---

## Checking everything is healthy

At any time, run:

```bash
cd /opt/librenms-backup-restore
sudo bash scripts/validate-install.sh
```

It checks twelve things and prints `PASS` / `WARN` / `FAIL` for each. You want
zero `FAIL`s.

---

## Upgrading later

```bash
cd /tmp
sudo unzip -o ~/librenms-backup-restore-main.zip      # or: sudo git pull
sudo bash /tmp/librenms-backup-restore-main/scripts/upgrade.sh
```

Your configuration, your encryption key and your archives are all kept. The
previous version is saved in `/opt/librenms-backup-restore.previous` so you can
go back.

---

## Removing it

```bash
cd /opt/librenms-backup-restore
sudo bash scripts/uninstall.sh
```

That removes the code and the services but **keeps your backups**. To remove
those too, read `scripts/uninstall.sh --help` first — they are deleted
permanently.

---

## Where the logs are (there are two)

These are different files, and searching the wrong one is a common time-waster:

| Log | Path | Use it for |
|---|---|---|
| **Plugin log** | `/var/log/librenms-backup/job.log` | Backups, restores, the worker, the helper |
| **LibreNMS log** | `/opt/librenms/logs/librenms.log` | The plugin's *web pages* failing (500s, blank tables) |

```bash
# backup / restore problems
sudo tail -n 50 /var/log/librenms-backup/job.log

# GUI problems, e.g. "Unexpected server response (500)"
sudo grep -iE "BackupRestore|plugin-backuprestore" /opt/librenms/logs/librenms.log | tail -40
```

Your LibreNMS log path is set by `APP_LOG` in `/opt/librenms/.env`; if that is
not set it defaults to `<librenms_root>/logs/librenms.log`. When the plugin
returns a JSON error it now tells you the exact path it is writing to.

## Where everything lives

| What | Where |
|---|---|
| The plugin's own code | `/opt/librenms-backup-restore` |
| The two files added to LibreNMS | `/opt/librenms/app/Plugins/BackupRestore/` and `/opt/librenms/html/plugin-backuprestore.php` |
| Settings | `/etc/librenms-backup/br.conf` |
| **Encryption key — save a copy** | `/etc/librenms-backup/keys/identity.txt` |
| The backups themselves | `/var/backups/librenms/archives/` |
| Logs | `/var/log/librenms-backup/` |

---

*For the full technical detail — permissions, the database grants, and the
privilege model — see `docs/INSTALL.md` and `docs/DEPLOYMENT-MAP.md`.*
