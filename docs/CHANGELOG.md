# Changelog

All notable changes to this project. Format follows Keep a Changelog; versions
use semantic versioning.

## [1.0.2] — 2026-11-11

### Fixed — user and path overrides were silently ignored

The systemd units hardcoded `User=librenms`, `Group=librenms` and
`/opt/librenms-backup-restore`. Passing `APP_USER=` or `PREFIX=` therefore
changed what the installer *printed* but not what actually got installed: the
worker kept running as `librenms` while writing files chowned to the override,
and a non-default prefix produced units pointing at a directory that did not
exist.

The units are now templates (`@BR_APP_USER@`, `@BR_PREFIX@`, …) rendered from
the values in force, and rendering **fails the install** if any placeholder
survives. `br.conf` is templated the same way, so `librenms_user`,
`librenms_group`, `state_dir` and `log_dir` all follow the override.

Defaults render byte-identically to the previous units, so an install at
`/opt/librenms` owned by `librenms` is unchanged.

### Fixed — the rollback could restore the very file that caused the outage

If `/etc/sudoers.d/librenms-backup` was already invalid (the usual reason to
re-run the installer), the rollback path copied it back and reported "sudo is
working again" — which it would not have been. The previous file is now only
treated as restorable when this host's `visudo` accepts it; otherwise it is kept
aside as `.br-broken` and the install removes the file entirely on failure,
which is the only genuinely safe state.

### Fixed — the error hint named a log file that does not exist

The new 500 handler told operators to check `storage/logs/librenms.log`, and
LibreNMS does not write there. Its channel is
`env('APP_LOG', base_path('logs/librenms.log'))` (config/logging.php), and
`storage/logs/` is empty on a real install. The front controller now resolves
the path from the app's own config, falls back through the locations LibreNMS
has used, and returns it in the JSON (`"log": "/opt/librenms/logs/librenms.log"`)
so nobody has to guess again.

### Fixed — a 500 from the API now says what failed

`GET /plugin-backuprestore.php?r=backups` (Backup History) and `?r=restores`
returned Laravel's HTML error page with a bare HTTP 500, so the GUI could only
say "Unexpected server response (500)" and the cause stayed hidden in
`storage/logs/librenms.log`. The front controller now registers a `renderable`
exception callback: API requests get JSON carrying the exception class, message
and `file:line` inside the plugin. A plain `try/catch` around `handle()` does
**not** work — Laravel's handler converts the throwable to a response first, so
the callback has to be registered with the handler itself.

A missing asset is now reported as 404 rather than 500 (a mistyped filename
looked like a broken install), and framework-internal file paths are withheld.

### Fixed — the validator warned about the correct ownership

`validate-install.sh` reported `WARN owner librenms:librenms .../archives
(expected root:librenms)` on every install. The installer was right and the
validator was wrong: with mode `0750` a `root:librenms` directory grants the
group only `r-x`, so the worker could not write and *every backup would fail*.
An administrator "fixing" the warning would have broken backups. The check now
expects `$APP_USER:$APP_GROUP`, and a write probe as the worker replaces
ownership as the primary signal — that catches ownership and mode together.

### Fixed — "back up your keys" did not say which file

The installer's closing summary said `Keys : /etc/librenms-backup/keys/
(BACK THESE UP OFFLINE)`. Only `identity.txt` can decrypt archives;
`recipients.txt` is the public half. The summary and the step-by-step guide now
name the file explicitly and say what the other two are.

### Fixed — documentation still printed the rejected line

`docs/DEPLOYMENT-MAP.md` and `docs/PROJECT-DOCUMENTATION.html` both showed
`Defaults:librenms !requiretty` in a copyable snippet. Copying from either
reproduced the exact `unknown setting` failure. Both now print only the portable
form, with the reason spelled out. `check-package.sh` fails if any doc prints the
line again.

### Documented — the three-user model

`docs/INSTALL-STEP-BY-STEP.md` now explains `root` / `librenms` / `www-data`
and why the web tier deliberately cannot read `br.conf` or the encryption key.

## [1.0.1] — 2026-11-11

Rehearsal-driven release. Every item below was found by running a real restore
against a real LibreNMS 26.x install, and each one either corrupted data, hid a
failure, or reported success while doing nothing.

### Fixed — data safety
- **Restores wrote into the live database.** `mariadb-dump --databases` emits
  `DROP DATABASE` / `CREATE DATABASE` / `USE`, so piping the dump into
  `mariadb <scratch_db>` imported into the LIVE schema and then deleted it.
  `db.sanitised_dump_lines()` now strips all three statement forms
  (case-insensitive, comment-wrapped and indented forms included) before the
  client sees them. Proven: the scratch schema received 173 tables and the
  live database was untouched.
- **Plugin tables were lost across a restore.** The dump is the user's
  LibreNMS database as it was at backup time and does not contain `br_*`
  tables. The restore now snapshots them (`db.preserve_tables`), imports, and
  rewrites them (`db.restore_preserved`), skipping columns that no longer
  exist. Without this, the restore's own job and restore rows vanished
  mid-flight, so no restore could ever be marked `COMPLETED`.

### Fixed — restore completion
- **Maintenance mode ran after the checkpoint.** A full backup was taken while
  polling was still writing. Order is now VALIDATING → MAINTENANCE →
  CHECKPOINTING → …
- **A checkpoint row could outlive its file.** `_create_checkpoint` now marks a
  pathless or missing checkpoint `failed` instead of leaving it dangling.
- **Restore resolved the wrong archive.** `br_jobs.backup_id` is overwritten by
  the checkpoint backup the restore itself runs; `br_restores.backup_ref` is
  authoritative and is now preferred, with a warning on disagreement.
- **File restore extracted from the wrong member prefix.** Members are stored
  as `application/...`, `rrd/...`, `system/...`; the restore asked for
  `application/application/...`, matched zero members, and installed nothing
  while reporting success. Prefixes corrected; `system/` likewise.
- **Integration payloads installed to `/`.** The helper's path validator
  rejects `"/"`, aborting the restore at 74%. Integration roots are now
  installed per absolute tree (`/opt`, `/usr`, `/etc`, `/var`).
- **Permission repair could not read its own source.** `fs.install_tree` used
  `policy.writable()` on the source, but the permission-repair step
  legitimately uses the live app tree as a source. Sources are now checked
  against readable roots.
- **Missing services failed a good restore.** `apache2`/`php8.4-fpm` were
  required to reach `active` even when the host does not run them at all
  (`systemctl is-active` reports `inactive` for a unit that does not exist).
  Added `service.exists` (`systemctl cat`) and treat a genuinely absent unit as
  a warning rather than a failure.

### Fixed — protocol and reporting
- **`adopt` corrupted the helper protocol.** It printed to stdout, which is the
  helper's line-delimited HMAC channel; the caller saw a malformed reply after
  an otherwise successful restore. Logging only, and the `Logger` now echoes to
  stderr.
- **Post-restore registry was empty.** A restore replaces `br_backups` with its
  backup-time contents, so the archive just restored disappeared from the UI.
  The engine now reconciles from disk (`adopt_orphans`) before completing.
- **`adopt` exit status.** The command returned the adoption *count*, so
  "0 archives to adopt" exited non-zero.

### Fixed — reported from a real Ubuntu 26.04 install

The first install on the actual target platform failed at step 6. Recorded
verbatim so the cause is not re-derived.

- **`visudo` rejected the sudoers file: `unknown setting: 'requiretty'`.**
  Ubuntu 26.04 ships **sudo-rs** as the default `sudo`, which implements a
  subset of the sudoers grammar and rejects the *entire file* when it meets a
  `Defaults` option it does not know. Both `!requiretty` and `!logfile` are
  unsupported there. Both lines were also no-ops — `requiretty` defaults to off
  in sudo 1.9+, and no `logfile` is set anywhere by default — so they are simply
  removed. The two remaining Defaults (`env_reset`, `secure_path`) are supported
  by sudo-rs and by sudo 1.9.x.
- **The installer wrote the sudoers file before validating it.** Any invalid
  file in `/etc/sudoers.d` makes `sudo` refuse to run at all, so a failed
  validation locks the administrator out. Validation now happens in a scratch
  file; only a file that `visudo` accepts is moved into place, and the previous
  file is kept until the combined `/etc/sudoers` parses cleanly.
- **Automatic fallback.** If a host rejects the full policy, the installer
  retries with a minimal form containing no `Defaults` at all — identical rules,
  nothing to object to. If that also fails it aborts with `sudo` untouched.
- **`br-schema.php` could exit 0 with tables missing.** The status was
  `($didWork || $allPresent)`, so a run that "did work" reported success even if
  a table was still absent. Now the exit status reflects the end state, and
  `install.sh` follows up with an independent `br-schema.php status` check.
- **The helper's socket was never verified at install time.** systemd reporting
  `active` is not the same as the socket being usable. `install.sh` now waits
  for `/run/librenms-backup/br-helper.sock` and says so plainly if it never
  appears.

`check-package.sh` gained a "Sudo policy portability" section: it fails if
`requiretty`/`logfile` return to `config/br-sudoers`, or if validation stops
happening before the file is installed.

### Fixed — found by the pre-release audit

A dedicated audit pass (static analysis plus input fuzzing) after the rehearsal
found three further defects that no existing test reached.

- **Corrupt archive leaked a codec exception.** `json.loads()` decodes UTF-8
  before parsing, so a damaged `BRENC1` header raised `UnicodeDecodeError`,
  which the `json.JSONDecodeError` handler did not catch. A truncated header
  raised nothing at all and produced a bogus scheme instead. Both now raise
  `IntegrityError` with an operator-readable message. 300 fuzzed headers
  produced zero typed-error escapes, against 401 crashes before.
- **Corrupt checksum file leaked a codec exception.** `checksums.parse()`
  opened with strict UTF-8 and indexed `header["salt"]` style fields unguarded.
  Now decoded with `errors="replace"` and digests are validated as 64 hex
  characters, so a damaged file reports `IntegrityError`. Verified against a
  real archive: all 296 lines of the shipped `checksums.sha256` still parse.
- **A restored application tree was unreadable to the web server.** Restore
  installed the tree `0750`, and `install_tree` never corrected the target root
  (`os.walk` does not yield the root), so the web server — a different user,
  deliberately not in the `librenms` group — could not traverse it. A restore
  would have left the site returning 403. Both the install mode and the
  permission-repair pass now apply `0755`, with `config.php`/`.env` kept at
  `0640` and `librenms/` kept at `0750`. Reproduced and fixed on the rehearsal
  host: `www-data` could not read the tree before, can read it after.

### Fixed — deployment
- **`ProtectSystem=full` made restores impossible on a real systemd host.**
  The helper unit mounted `/etc` and `/usr` read-only, but `/etc` is a restore
  target, so a restore would fail at `RESTORING_CONFIGURATION`. Changed to
  `ProtectSystem=no` with an explicit `ReadWritePaths` allowlist covering
  `/etc`, the LibreNMS root, and the state/log/backup trees. Verified with
  `systemd-run`: under the old settings `/etc` rejected writes, under the new
  ones every restore target accepts them. The rehearsal had run the helper as a
  bare process, so it never exercised the sandbox.
  `check-package.sh` now fails if `ProtectSystem` is tightened again or a
  required path drops out of `ReadWritePaths`.

### Added
- **`docs/DEPLOYMENT-MAP.md`** — where every file goes, its owner and mode, the
  sudoers grant, the database grants, restore-path requirements, overrides for a
  non-default LibreNMS root, and a troubleshooting table.
- **Checkpoint rollback.** `worker restore --checkpoint <cp-id>` (and a
  `{"rollback": "<cp-id>"}` option on a restore request) reuses the restore
  state machine against a recovery checkpoint. A rollback deliberately takes no
  new checkpoint — depending on the safety net you are recovering from is how
  you get stuck.
- `service.exists` helper operation; `db.table_list`, `db.preserve_tables` and
  `db.restore_preserved` helper operations.
- `tests/unit/test_restore_regressions.py` — 12 regressions covering retarget
  stripping, plugin-table preservation, checkpoint resolution, rollback
  semantics and member prefixes. Suite: **127 passed, 21 skipped**.

### Verified end to end
- Restore of `br-20261114T053942Z-97037a` (227,318 B, 173 tables) reaches
  `COMPLETED` at 100%.
- Canaries planted immediately before the restore — device `9999`
  `canary-pre-restore`, user `canary_user`, `/srv/librenms/html/canary.html` —
  are all gone afterwards; `admin` and the plugin rows survive.
- Rollback to the pre-restore checkpoint removes a row written *after* the
  restore, proving the checkpoint is a real recovery point.

## [1.0.0] — 2026-11-11

First release.

### Added

**Backup engine**
- Component-wise collection: `database`, `rrd`, `application`, `system`,
  `integrations`, selectable per run via backup type (`full`, `database`,
  `rrd`, `configuration`, `system`).
- Streaming pipeline `tar → zstd|gzip → age|AES-CTR+HMAC → .brarchive` with
  flat memory use; compressor and encryptor subprocesses are drained
  concurrently.
- Per-file SHA-256 computed as bytes pass through, so each file is read once.
- Manifest with schema version, component inventory, tool versions, restore
  dependencies and machine-specific caveats.
- Three verification levels (`shallow`, `standard`, `deep`), including a real
  gunzip of the SQL dump at `deep`.
- GFS retention with per-decision reasons, protected set, and an absolute cap.
- Config scrubbing before archiving: `KEY=value`, `KEY: value` and
  `"KEY": "value"` forms of passwords, tokens, secrets and `APP_KEY`.

**Restore engine**
- Crash-safe import: the dump lands in a scratch schema
  (`<db>__brrestore`) and is promoted with `RENAME TABLE` only after it
  succeeds, so a failed restore cannot leave the database empty.
- Automatic recovery checkpoint (a verified backup) before anything is
  overwritten.
- Maintenance mode, service stop/start and validation through the helper.
- Machine-specific network configuration captured but never auto-restored.
- Checkpoint rollback path, excluded from retention.

**Orchestration**
- `br_jobs` as a lock-free queue; claiming is a conditional `UPDATE`.
- One systemd oneshot unit per job, instance name = job UUID, so parameters
  never enter the unit file.
- Separate dispatch (15 s) and scheduler (daily) timers.
- Heartbeat-based liveness: silence past the timeout marks a job failed and
  quarantines any partial archive.

**Web UI and API**
- Seven-tab interface: Dashboard, History, Manage, Restore, Scheduler, Audit,
  Settings.
- 21 API routes under a single front controller, with a uniform
  `{status, data | message}` envelope.
- `?r=<resource>` transport that behaves identically under Apache,
  nginx+FastCGI, `artisan serve` and the PHP built-in server.
- Assets served through the same front controller with explicit MIME types and
  `X-Content-Type-Options: nosniff`.
- Server-side typed-phrase confirmation for restores; CSRF enforced (and
  re-checked) on every non-GET verb.
- Append-only audit trail, including refusals.

**Security**
- Root helper over a unix socket with per-message HMAC-SHA256, a closed
  operation set, argv-only execution and path allowlists.
- The web tier has no sudo entry at all.
- systemd hardening on both helper and worker, including
  `RestrictAddressFamilies=AF_UNIX` and `MemoryDenyWriteExecute`.

**Operations**
- `scripts/install.sh` (idempotent), `uninstall.sh` (with `--purge`),
  `upgrade.sh`, `validate-install.sh`, `br-schema.php`.
- Versioned schema installer for the seven `br_*` tables plus the `plugins` row.
- 115 unit tests, 21 API contract tests.

### Fixed during development

Bugs found by testing rather than by reading, each with a regression test:

- **Retention tiers chained with `continue`.** An older backup from an
  already-covered day fell through into the weekly tier, was retained as a
  "weekly", exceeded `keep_total` and defeated daily de-duplication. Tiers are
  now evaluated independently.
- **`keep_total` could strand backups.** Rows inside the cap that matched no
  tier appeared in neither `keep` nor `delete`. They are now kept explicitly.
- **`openssl-sshaes` resolved to no implementation.** The documented
  passphrase fallback passed configuration validation, then failed at the first
  write — halfway through a backup. Both spellings now map to the AES container.
- **Checksum mismatches were not failures.** Verification returned `ok=True`
  with a populated `mismatched` list, so a corrupt archive could be recorded as
  verified. A mismatch now sets `ok=False`.
- **Deep verification rejected small dumps.** The tail check seeded an empty
  window when the whole dump fitted in the first read, so every small archive
  failed validation.
- **Verification ignored unexpected members.** Files present on disk but absent
  from `checksums.sha256` were invisible; they are now reported.
- **Config scrubbing missed `KEY: value` and quoted JSON.** Credentials in
  `.env`-style and JSON-style config survived into the archive.
- **LibreNMS's own log and `bootstrap/cache/` were archived.** Both are
  machine-specific and noisy.

Pre-release fixes to the API and worker transport:

- Plugin routes were appended and lost to LibreNMS's trailing catch-all
  `Route::any('{path?}')`; the route collection is now rebuilt plugin-first.
- The asset route was registered but never captured into the prepended set.
- The `?r=` rewrite prefixed `br/` onto assets, turning `assets/x.js` into
  `br/assets/x.js`.
- The dispatcher called `systemctl` unprivileged and could not spawn workers;
  it now goes through the helper's `worker.spawn`.
- `worker.spawn` blocked for the duration of a oneshot job; `--no-block` added.
- The restore lock lived in a root-owned directory; moved to the worker-owned
  state dir.
- The health file was written as `health.json.tmp.tmp` into a root-owned
  directory, so the UI always reported `helper: unreachable`.
- `retention_class` was `varchar(8)`, which cannot hold `checkpoint` (10 chars).
- The helper is `AF_UNIX`-only, so `mariadb` failed with *Can't create TCP/IP
  socket*; restores now use `/run/mysqld/mysqld.sock` with an allowlist check.
- `subprocess(stdin=gzip_file)` handed the raw descriptor to the client, so
  `mariadb` received compressed bytes instead of SQL. A feeder thread now
  streams decoded SQL, with `--binary-mode`.
- The restore dropped the live database before importing; replaced with
  scratch-schema import plus promotion.

### Known limitations

- `worker_version` reports `null` in the health payload; version drift is
  therefore not yet visible in the UI.
- Offsite/S3 targets are designed for but not implemented; the archive format is
  stream-shaped for that purpose.
- The GUI has no archive-level "download to my machine" progress indicator for
  multi-GiB archives.
- Restore of `rrd` on a target with a different RRD structure is not
  reconciled; files are installed as-is.
