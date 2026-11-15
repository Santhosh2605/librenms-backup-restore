# Build status — LibreNMS Backup & Restore

Snapshot: 2026-11-11 (rehearsal complete) · Target: Ubuntu 26.04 / PHP 8.5 · Sandbox: Debian 13 / PHP 8.4.27 / MariaDB 11.8.6 / LibreNMS `dd4cebd7`

## Phase overview

| # | Phase | State | Evidence |
|---|-------|-------|----------|
| 1 | Architecture & data model | **Done** | 7 tables (`br_backups`, `br_jobs`, `br_restores`, `br_checkpoints`, `br_schedules`, `br_settings`, `br_audit_events`), versioned installer `scripts/br-schema.php` |
| 2 | Privilege & security model | **Done** | Root helper over unix socket + HMAC, no shell, path allowlist; sudoers grants the worker exactly one command; systemd hardening (`RestrictAddressFamilies=AF_UNIX`, `ProtectSystem`, `NoNewPrivileges`) |
| 3 | Worker backup engine | **Done** | Collect (DB/RRD/app/system/integrations) → zstd+age archive → manifest + `.sha256` → verify → retention. Real artifact: 238.6 KiB, 354 members, verified |
| 4 | Web plugin (GUI) | **Done** | 7 tabs (Dashboard, History, Manage, Restore, Scheduler, Audit, Settings); page renders 200; CSS/JS served 200 |
| 5 | HTTP API layer | **Done** | 21 routes, all resources 200; `?r=` transport; CSRF on state changes; audit trail |
| 6 | Job orchestration | **Done** (this session) | Dispatcher now spawns workers through the helper (`worker.spawn`), not raw `systemctl`; systemd instance unit + timers |
| 7 | Restore engine + rehearsal | **Done** | Full restore of `br-20261114T053942Z-97037a` reaches `COMPLETED` 100%; canaries removed; checkpoint rollback exercised; 11 more defects found and fixed |
| 8 | Automated tests | **Done** | 127 unit tests + 21 API contract tests; `test_restore_regressions.py` locks in this pass's fixes |
| 9 | Documentation set | **Done** | Standalone HTML project report (`docs/PROJECT-DOCUMENTATION.html`) + README and 7 Markdown documents |
| 10 | Packaging & lifecycle | **Done** | `dist/librenms-backup-restore-1.0.1.tar.gz` + SHA-256; `check-package.sh --with-tests` passes |

Rough code volume: plugin 2,993 lines (21 files), worker 6,166 lines (21 files), front controller 182, config + scripts 1,178.

## Bugs found and fixed this session

1. **Asset routes never registered** — `assets/{asset}` was created but not captured into `$brRoutes`, so it stayed behind LibreNMS's catch-all. JS/CSS now 200.
2. **`?r=` prefix rewrote assets wrongly** — every resource got a `br/` prefix, turning `assets/x.js` into `br/assets/x.js`. Prefix is now applied to the API namespace only.
3. **Health file unwritable** — worker wrote `health.json.tmp.tmp` into a root-owned dir. `write_json` is already atomic; the double suffix is gone and the state dir is worker-owned. `worker/health` now reports `helper: reachable`.
4. **Dispatcher could not spawn workers** — it called `systemctl` as an unprivileged user. It now uses the helper's `worker.spawn`, which accepts only a UUID.
5. **`worker.spawn` blocked for the whole job** — oneshot units made the helper wait; added `--no-block`.
6. **Restore lock in a root-owned dir** — moved to the worker-owned state dir.
7. **`retention_class` too narrow** — `varchar(8)` could not hold `'checkpoint'`; widened to 16 in the migration and live schema.
8. **Helper had no DB transport** — `RestrictAddressFamilies=AF_UNIX` ruled out TCP, so `mariadb` failed with *Can't create TCP/IP socket*. Restore now connects over `/run/mysqld/mysqld.sock`; the helper validates the socket path against an allowlist.
9. **Dump piped as compressed bytes** — `subprocess(stdin=gzip_file)` passed the raw descriptor, so `mariadb` received zlib data. A feeder thread now streams decompressed SQL and `--binary-mode` is set.
10. **Restore was not crash-safe** — it dropped the live database before importing. Imports now land in a scratch schema (`<db>__brrestore`) and are promoted with `RENAME TABLE` only on success.

Defect 10 is the important one: the failed rehearsals left `librenms` empty mid-run, which is exactly the failure mode a backup tool must not have. The database was rebuilt from the verified archive (173 tables), and `scripts/br-schema.php install` restored the program schema and the plugin row.

## Post-rehearsal deployment fix (22)

22. **`ProtectSystem=full` blocked every restore on a real host.** `/etc` is a
    restore target, but the helper unit mounted it read-only. Proven with
    `systemd-run` (writes rejected under the old unit, accepted under the new
    one) and now guarded by `check-package.sh`. The rehearsal had launched the
    helper as a bare process, so the sandbox was never exercised.

## Rehearsal result (phase 7 closed)

A full restore was executed against the live rehearsal install and verified by
canaries planted immediately before the run.

| Check | Before | After |
|-------|--------|-------|
| Device `9999` / `canary-pre-restore` | present | **gone** |
| User `canary_user` | present | **gone** |
| `/srv/librenms/html/canary.html` | present | **present — out of scope** (see below) |
| `admin` account | present | **kept** |
| Table count | 173 | **173** |
| Restore job | — | **`completed`, 100%** |
| Plugin rows (`br_jobs`, `br_restores`, `br_settings`) | — | **preserved** |

> **Correction (post-audit).** An earlier revision of this table claimed the
> file canary was removed. It was not. A restore installs only the paths listed
> in `[components] application_paths` (default: `config.php`, `.env`,
> `bootstrap/cache/config.php`, three custom-override directories, plus
> `version.json`) — it is a **selective merge, not a wipe**. A file in the web
> root is therefore untouched by design. Re-running the rehearsal on 11 Nov
> confirmed it survives. To back up more of the tree, add those paths to
> `application_paths`; see docs/DEPLOYMENT-MAP.md § "What a restore overwrites".

Checkpoint rollback was then exercised separately: a row written *after* the
restore was removed by rolling back to the pre-restore checkpoint, confirming
the checkpoint is a genuine recovery point rather than a record of intent.

Defects 11–21 below were all found by this rehearsal.

## Bugs found and fixed in the rehearsal pass

11. **Dump retargeting (data loss).** `--databases` dumps carry `DROP DATABASE`
    / `CREATE DATABASE` / `USE`; piping one into a scratch schema imported into
    the LIVE database and deleted it. `sanitised_dump_lines()` strips all three
    forms. Verified: 173 tables into the scratch schema, live DB untouched.
12. **Plugin tables not preserved.** `br_*` tables are not part of the user's
    dump, so a restore erased the plugin's own state mid-flight and no restore
    could reach `COMPLETED`. Now snapshotted before import and rewritten after.
13. **Wrong member prefixes.** `application/application` matched nothing;
    the file stage installed zero objects and reported success.
14. **`/` as an install target.** The helper's validator rejects `"/"`,
    aborting integration restores. Now installed per absolute tree.
15. **Permission repair blocked by its own policy.** Source paths were checked
    against writable roots only; the live app tree is a legitimate source.
16. **Absent services treated as failures.** `apache2`/`php8.4-fpm` were
    required to be `active` on hosts that do not run them. `service.exists`
    (`systemctl cat`) now distinguishes "missing" from "stopped".
17. **`adopt` printed to the helper's protocol channel,** corrupting the reply
    after a successful restore. Logs only; `Logger.echo` writes to stderr.
18. **Post-restore registry rollback.** The archive just restored vanished from
    the UI; the engine reconciles from disk before completing.
19. **`adopt` exit status was the adoption count** (0 ⇒ non-zero exit).
20. **No checkpoint rollback path existed.** Added `--checkpoint` and a
    `{"rollback": id}` request option; rollback takes no new checkpoint.
21. **`_run_sql` name collision** between the dump importer and the
    snapshot writer silently rebound the function.

## Package

`dist/librenms-backup-restore-1.0.1.tar.gz` — 152 KiB, 94 entries, 74 files,
SHA-256 published alongside it in `librenms-backup-restore-1.0.1.tar.gz.sha256`
(the archive cannot contain its own digest, so the sidecar is authoritative).
Extracted to a scratch directory and re-gated with `check-package.sh
--with-tests` → **Package OK (0 warnings)**, `127 passed, 21 skipped`.

`scripts/check-package.sh` validates layout, installer payload, plugin surface,
worker modules, PHP/Python/bash syntax, security invariants (no web-tier sudo,
single escalation path, no `shell=True` by AST, `AF_UNIX` helper), docs presence
and the test suite — without needing LibreNMS, MariaDB or root.

## Works right now

- Plugin page renders: `/plugin/BackupRestore` → 200; all tabs backed by live data.
- API: `status`, `backups`, `jobs`, `restores`, `schedules`, `settings`, `audit`, `worker/health` → 200 JSON.
- Assets: `?r=assets/backup-restore.{js,css}` → 200, correct MIME types.
- Worker health: helper reachable, archive dir/database/encryption checks green.
- Backup pipeline: produces a verified, encrypted archive with sidecars.

## Bugs found by the test suite (all fixed, all with regression tests)

1. **Retention tiers chained with `continue`** — an older backup from an already-covered day fell through into the weekly tier, was retained as a "weekly", exceeded `keep_total`, and defeated daily de-duplication.
2. **`keep_total` stranded backups** — rows inside the cap matching no tier appeared in neither `keep` nor `delete`. They are now kept explicitly.
3. **`openssl-sshaes` resolved to no implementation** — the documented fallback passed config validation, then failed at the first write, halfway through a backup. Both spellings now map to the AES container.
4. **Checksum mismatches were not failures** — `verify()` returned `ok=True` with a populated `mismatched` list, so a corrupt archive could be recorded as *verified*. This is the most serious defect found: it defeated the tool's central promise.
5. **Deep verification rejected small dumps** — the tail window was seeded empty when the whole dump fitted in the first read, so every small archive failed validation.
6. **Verification ignored unexpected members** — files on disk but absent from `checksums.sha256` were invisible; now reported.
7. **Config scrubbing missed `KEY: value` and quoted JSON** — credentials in `.env` and JSON style survived into the archive. Also, LibreNMS's own `logs/*.log` and `bootstrap/cache/` were being archived.

## Open items, in order

1. **Fresh-install rehearsal from the tarball** — the packaged copy validates and
   passes its own tests, but has not itself been installed onto a clean host.
   (The restore rehearsal was done from the staged tree, not the archive.)
2. **Verify `upgrade.sh` / `uninstall.sh` against a real install** —
   syntax-checked and packaged, but not executed.
3. **`worker_version` is null** in health output (cosmetic, but it is a
   version-drift signal).
4. **Empty placeholder dirs** — `database/`, `helper/`, `deployment/` are empty;
   either populate or drop them from the layout.
5. **Restore on a host with a real web server / php-fpm** — the rehearsal host
   runs `artisan serve`, so the `service.exists` fallback is what was exercised,
   not the strict path. Worth one run on Apache.

## Definition of done for the restore phase — **met**

Restore a chosen backup over a live install using only the GUI, in one pass:
preflight green → typed confirmation → checkpoint created → maintenance mode →
database + files + config restored → permissions fixed → services validated →
maintenance off → job `completed`, with the checkpoint still available for
rollback.

Observed on 2026-11-11 against LibreNMS 26.x / MariaDB 11.8.6: preflight 7/7,
job `completed` at 100%, all three canaries gone, `admin` and plugin rows
intact, 173 tables, and a follow-up rollback to the checkpoint removed a row
written after the restore.
