# GitHub project description

Copy-paste assets for the repository page. Everything here is derived from the
code and the rehearsal results — no aspirational claims.

---

## 1. Description (the "About" field)

Paste this into **Settings → General → Description**. Limit is 350 characters;
this is 249.

```
Encrypted, verified, component-aware backups for LibreNMS with a restore path that has actually been rehearsed. Crash-safe imports into a scratch schema, verified pre-restore checkpoints, and a privilege model where the web tier gets no root at all.
```

Shorter alternative (197 chars):

```
Production-grade backup & restore for LibreNMS: age-encrypted archives, three verification levels, crash-safe restores, recovery checkpoints, and a root helper that keeps the web tier unprivileged.
```

### Website
Leave blank, or point at your docs site. GitHub renders the description above
it.

### Topics (max 20 — paste as space-separated)

```
librenms network-monitoring backup restore disaster-recovery php python mariadb encryption age encryption-at-rest systemd ubuntu monitoring-tool plugin self-hosted infrastructure devops nms
```

Best eight, if you prefer a tighter list:

```
librenms backup restore disaster-recovery php python mariadb encryption
```

---

## 2. README opening

The README's existing first three paragraphs are already the right pitch. If you
want a shorter variant for the top, this is the same argument in half the
length:

> **The usual LibreNMS backup is a cron job running `mysqldump` and a `tar` of
> the install directory.** It protects you against almost nothing: nobody tests
> the restore, the dump sits unencrypted on the same disk, and a half-written
> archive looks exactly like a good one until the day you need it.
>
> This plugin treats the backup as a product rather than a script. Archives are
> encrypted with `age` and verified at three levels. Every restore first takes a
> verified recovery checkpoint, imports into a scratch schema, and promotes it
> only after the import succeeds — so a failed restore cannot leave you with an
> empty database. The restore path has been rehearsed end-to-end against a live
> LibreNMS 26.x install, including rollback.
>
> The privilege model is the other half of the design. Most backup plugins hand
> PHP a `sudo` rule, which turns any PHP RCE into root. Here the web tier holds
> no privilege at all: it writes database rows and nothing else. Work happens in
> a worker running as `librenms`, which may invoke exactly one command — a root
> helper that speaks a closed, HMAC-signed JSON protocol over a unix socket and
> executes argv lists, never a shell.

---

## 3. Release notes for v1.0.1

Suitable for a GitHub Release body.

```markdown
## LibreNMS Backup & Restore v1.0.1

First release. An installable archive that adds a Backup & Restore panel to
LibreNMS, plus a privileged Python worker that does the actual work.

### Highlights

- **Five components, selectable per run** — database, RRD, application, system
  configuration, integrations.
- **`age`-encrypted by default** (X25519 + ChaCha20-Poly1305), with an
  AES-256-CTR + HMAC-SHA256 / PBKDF2 fallback.
- **Three verification levels** — `shallow` opens the container, `standard`
  checks every member's SHA-256, `deep` additionally gunzips the SQL dump.
- **Crash-safe restores** — import lands in `<db>__brrestore` and is promoted by
  `RENAME TABLE` only after it succeeds.
- **Checkpointed restore state machine** — nine named stages, a verified
  pre-restore checkpoint, and rollback to it.
- **GFS retention** with a written reason per archive, and checkpoints excluded
  from cleanup.
- **No LibreNMS core files modified** — one plugin directory and one front
  controller. An upgrade cannot revert the plugin and the plugin cannot break an
  upgrade.

### Verified

Restored a live LibreNMS 26.x database (173 tables) over its own installation:

| Check | Result |
|---|---|
| Restore job | `COMPLETED` at 100 % |
| Canary device / canary user | removed |
| `admin` account | kept |
| Table count | 173 → 173 |
| Plugin state | 21 rows across 6 tables preserved |
| Rollback to pre-restore checkpoint | removed a row written *after* the restore |

`133 unit tests passing` · `21 API contract tests` · `check-package.sh`
reports zero warnings against the shipped tarball.

### Install

```bash
sudo apt-get install -y python3 zstd age python3-cryptography mariadb-client
sudo tar -xzf librenms-backup-restore-1.0.1.tar.gz -C /opt/librenms-backup-restore --strip-components=1
cd /opt/librenms-backup-restore && sudo ./scripts/install.sh
```

Verified on Debian 13 / PHP 8.4.27 / MariaDB 11.8.6. Targets Ubuntu 26.04 /
PHP 8.5.

### Known limitations

- `upgrade.sh` and `uninstall.sh` are syntax-checked and packaged but have not
  been executed against a real installation.
- Fresh install from the tarball onto a clean host has not been rehearsed.
- A restore only installs the paths listed in `application_paths`; it is a
  selective merge, not a clean-room rebuild.
- Offsite/S3 targets are designed for but not implemented.

See [docs/STATUS.md](docs/STATUS.md) for the full phase tracker and
[docs/PROJECT-DOCUMENTATION.html](docs/PROJECT-DOCUMENTATION.html) for the
project report.
```

---

## 4. Repo sidebar snippet (for a pinned issue or discussion)

> **Scope.** This backs up and restores one LibreNMS installation on one host:
> its database, RRD data, configuration, and selected custom files. It is not a
> fleet backup system and does not do offsite replication yet.
>
> **What "rehearsed" means here.** The restore path was run end-to-end against a
> live installation, not just unit-tested — that is how twelve of the twenty-two
> fixed defects were found, including one that wrote a backup over the live
> database.
>
> **The honest caveats** are in the release notes and in `docs/STATUS.md`. Start
> there before deploying.

---

## Notes on tone

Three choices worth flagging, since they are deliberate:

1. **Lead with the problem, not the feature list.** "Nobody tests the restore"
   is the reason someone reads on; "seven tabs" is not.
2. **Publish the caveats, not a feature table of absolutes.** GitHub descriptions
   that claim everything get read as unreliable. Listing what has *not* been
   rehearsed is what makes the rest credible.
3. **The numbers are real.** 173 tables, 133 tests, nine stages — every figure
   here is reproducible from the repo. Avoid rounding them up later.
