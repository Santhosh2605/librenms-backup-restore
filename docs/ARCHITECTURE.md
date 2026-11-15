# Architecture

## Design goals

1. **A backup tool must never be able to destroy the system it protects.** Every
   failure mode is biased toward doing nothing.
2. **The web tier is assumed hostile.** PHP never gets a privileged handle. It
   writes database rows and nothing else.
3. **Restores are rehearsed, not hoped for.** A backup that has never been
   restored is a hypothesis.
4. **No shell, ever.** Every external command is an argv list.

## Process model

```
                       browser
                          |
                          | HTTPS, session cookie, CSRF
                          v
   +------------------------------------------------------------+
   |  PHP-FPM / Apache            as user: librenms             |
   |    plugin/    Blade shell, JSON API                         |
   |    front-controller/plugin-backuprestore.php  (route table) |
   |    Writes: DB rows only.  No sudo. No filesystem mutation.   |
   +------------------------------------------------------------+
                          |
                          | shared MariaDB
                          v
   +------------------------------------------------------------+
   |  systemd: librenms-backup-dispatch.timer   (every 15s)      |
   |           librenms-backup-scheduler.timer  (daily)          |
   |               gives the worker exactly what it needs        |
   |           librenms-backup-worker@<job>.service             |
   |               runs once per job as user: librenms           |
   +------------------------------------------------------------+
                          |
                          | unix socket + HMAC, one operation at a time
                          v
   +------------------------------------------------------------+
   |  br-helper.service            as user: root                |
   |    br.helper  - closed JSON protocol over                   |
   |                 /run/librenms-backup/br-helper.sock         |
   |    Validates every field; path allowlist; argv lists only.   |
   +------------------------------------------------------------+
                          |
                          v
          mariadb-dump / mariadb   zstd   age   systemctl   rsync
```

The web tier is deliberately **not** in the privilege chain. If an attacker
achieves RCE in PHP, they can insert rows — and the worker will refuse every
job that fails validation — but they cannot read `/etc/shadow`, stop services,
or run a restore.

## Why a front controllers exists at all

LibreNMS local plugins are discovered through a hook/glob mechanism and cannot
register routes or service providers. Its `routes/web.php` also ends with:

```php
Route::any('{path?}', InstallationController::class.'invalid')->where('path', '.*');
```

That catch-all matches everything, so plugin URLs 404 unless the plugin's routes
are *prepended*. Symfonys matcher returns the first match, not the best match.

`front-controller/plugin-backuprestore.php` therefore:

1. boots the LibreNMS application,
2. collects its own routes into `$brRoutes`,
3. rebuilds the `RouteCollection` with plugin routes first and core routes
   after, then calls `setRoutes()`,
4. dispatches through the kernel.

### Transport: `?r=<resource>`

The API addresses resources as `plugin-backuprestore.php?r=backups`. This looks
unusual, and it is — for a reason:

* PHP's built-in server will not execute a `.php` file when extra path segments
  follow the script name; it falls through to `index.php`. PATH_INFO is
  therefore unusable for local development.
* Laravel strips `SCRIPT_NAME` from `Request::path()`, so `/plugin-backuprestore.php/br/status`
  and `/br/status` collapse to the same route path — splitting the route table
  across two forms is a maintenance trap.

A single query parameter works identically under Apache, nginx+FastCGI,
`artisan serve` and the PHP built-in server. It is validated with
`#[^A-Za-z0-9._/-]#` plus a `..` check before it is used to rewrite
`REQUEST_URI`.

Assets are addressed the same way (`?r=assets/backup-restore.js`) but are
registered at the top level rather than under the `br/` namespace, and the
rewrite applies the `br/` prefix only to API resources.

## Data model

Seven tables, created by `scripts/br-schema.php` — LibreNMS local plugins cannot
register migrations, so the schema is installed by a versioned script rather than
Laravel's migrator.

| Table | Role |
|---|---|
| `br_backups` | One row per archive. `backup_ref`, type, status, verification, size, components, versions, `retention_class` (daily/weekly/monthly/checkpoint) |
| `br_jobs` | Queue and state machine. `job_id` (UUID, doubles as the systemd instance name), kind, stage, progress, heartbeat, `systemd_unit` |
| `br_restores` | Restore runs. `restore_id`, state (`QUEUED`…`DONE`/`FAILED`), checkpoint reference, options, log tail |
| `br_checkpoints` | Recovery points captured immediately before a restore overwrites anything |
| `br_schedules` | frequency, time-of-day, weekday, day-of-month, enabled |
| `br_settings` | Plugin settings (single row keyed `plugin_name`) |
| `br_audit_events` | Append-only record of every privileged action and refusal |

`retention_class` is `varchar(16)`: recovery checkpoints are tagged
`checkpoint`, which does not fit in 8 characters.

## Backup pipeline

```
collect          tar members            zstd/gzip          age / AES       file
database  ->  ArchiveWriter  ->  _Compressor  ->  EncryptedWriter  ->  .brarchive
rrd                                   (pipe drained concurrently)
application
system
integrations
```

* **Compression and encryption are separate stages.** The encryption layer only
  ever sees compressed bytes, so it stays fast, and a future S3 uploader can
  stream a finished file unchanged.
* **Compressor stdout is drained by a pump thread.** Without concurrent draining
  a multi-GiB bundle deadlocks once the 64 KiB pipe buffer fills.
* **`age` writes directly to the container fd.** Capturing its stdout into a pipe
  would reintroduce the same deadlock at the encryption stage.
* **Per-file hashes are computed as bytes pass through** (`_HashingReadable`), so
  each file is read exactly once.

Output is four files: the archive, `.manifest.json`, `.sha256`, and a DB row.
The archive's own hash cannot exist before the container is closed, so it lives
in the sidecar and the DB row — the in-archive `checksums.sha256` is the
authority for member-level integrity.

## Verification

| Level | What it proves |
|---|---|
| `shallow` | Container opens, header parses, manifest validates |
| `standard` | Plus: every member's SHA-256 matches `checksums.sha256` |
| `deep` | Plus: the SQL dump actually gunzips, carries a schema statement and terminates cleanly; restore dependencies are surfaced |

A checksum mismatch sets `ok = False`. Nothing else is meaningful if a member
does not hash to its recorded value.

## Restore pipeline

```
preflight -> typed confirmation -> recovery checkpoint -> maintenance mode
   -> stop app services -> restore database -> restore files -> restore config
   -> fix ownership -> validate services -> maintenance off -> DONE
```

Two properties matter more than speed:

* **Crash safety.** The live database is never dropped before the import is known
  to work. The dump is imported into a scratch schema (`<db>__brrestore`) and the
  schemas are swapped with `RENAME TABLE` only on success.
* **A recovery checkpoint first.** Before anything is overwritten, a fresh
  verified backup is taken. If the restore then fails, the operator has a
  same-day archive to roll back to.

The privileged work is done by the helper, which accepts only an allowlisted
socket path, validates identifiers against `^[A-Za-z_][A-Za-z0-9_]{0,63}$`, and
refuses to restore a file that is not `*.sql.gz`.

## Scheduling

`librenms-backup-dispatch.timer` fires every 15 s and claims queued jobs by
`UPDATE ... WHERE status='queued'` (a conditional update, so two dispatchers
cannot double-claim). It then asks the helper to start
`librenms-backup-worker@<job_id>.service`.

The dispatcher must not call `systemctl` itself: it runs unprivileged, and
`systemctl start` is denied. The helper's `worker.spawn` operation accepts only a
UUID — the unit name is never built from user input — and passes `--no-block`,
because a oneshot unit that runs for hours would otherwise stall the helper's
socket.

Silence is treated as failure: a job whose heartbeat is older than the timeout
and whose unit is inactive is marked failed rather than left running forever.

## Configuration and keys

`/etc/librenms-backup/br.conf` (`root:librenms 0640`) holds paths and policy —
never secrets. Key material lives separately:

| Path | Contents | Mode |
|---|---|---|
| `/etc/librenms-backup/keys/recipients.txt` | age public keys | 0640 |
| `/etc/librenms-backup/keys/identity.txt` | age private key | 0640 |
| `/etc/librenms-backup/keys/passphrase` | AES container passphrase (≥16 chars) | 0640 |

The passphrase file is rejected at read time if it is world-accessible or shorter
than 16 characters.

## Failure modes considered

| Failure | Behaviour |
|---|---|
| Disk full mid-backup | Archive quarantined as `.incomplete`, never registered |
| Worker killed mid-job | Heartbeat expires → job marked failed, archive quarantined |
| Corrupt member in archive | Verification fails, backup not marked verified |
| Restore import fails | Live database untouched (scratch schema); checkpoint drives rollback |
| Helper unreachable | Jobs stay queued; the API reports `helper: unreachable` |
| Two restores requested | Second is refused with 409 while one is active |
| Plugin disabled | Every route returns the plugin's own 403, not a core page |
