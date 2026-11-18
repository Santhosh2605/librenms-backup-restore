# Restore runbook

A restore overwrites a working installation. Read the whole page before you
start one, and rehearse on a copy first — a backup you have never restored is a
hypothesis, not a backup.

## Before you begin

1. **Confirm which backup.** The dashboard lists reference, size, age and
   verification state. Pick one marked `verified`.
2. **Confirm you have the decryption key.** New backups need the offline AES
   passphrase. Older `age` backups need `identity.txt`. If the only copy is on
   this host and this host is the one you are restoring, take the offline copy
   first.
3. **Confirm the rollback path.** A restore takes a fresh recovery checkpoint
   automatically, but that checkpoint lands on this host's disk. If the disk is
   the problem, the checkpoint is not a rollback.
4. **Expect downtime.** The application is put into maintenance mode and Apache /
   php-fpm are stopped for the duration.

To restore a file that is not already listed, open the plugin's **Restore** tab,
upload the `.brarchive` file, and wait for server-side validation and verification
to finish. The verified archive will then appear in the restore selector. The
upload is kept on the server; no cloud service is involved.

## Rehearse first

The rehearsal is the same code path as the real thing, on a disposable target.

```bash
# 1. Take a backup on the source host and copy it out
ls -l /var/backups/librenms/archives/
scp root@src:/var/backups/librenms/archives/br-*.brarchive* /tmp/

# 2. On a scratch host with LibreNMS installed and this plugin installed,
#    place the archive where the worker can see it
sudo install -o librenms -g librenms -m 0640 \
    /tmp/br-20261111T181440Z-1a5cd8.brarchive \
    /var/backups/librenms/archives/

# 3. Register it and verify
sudo -u librenms env BR_CONF=/etc/librenms-backup/br.conf \
    PYTHONPATH=/opt/librenms-backup-restore/worker \
    python3 -m br.worker verify \
        --job <uuid> \
        --backup /var/backups/librenms/archives/br-20261111T181440Z-1a5cd8.brarchive \
        --level deep
```

Then run the restore from the GUI. What "success" looks like:

| Stage | Expected |
|---|---|
| Preflight | All checks green: archive present, hashes match, key available, dependencies compatible |
| Confirmation | Typed phrase accepted (`RESTORE` by default) |
| Checkpoint | `cp-<timestamp>-<restore-id>` created and verified |
| Maintenance | LibreNMS in maintenance mode; `apache2` and `php-fpm` stopped |
| Database | Dump imported into a scratch schema, then promoted; table count reported |
| Files | `application`, `rrd`, and configured `integrations` trees installed |
| Config | `config.php` / `.env` restored; certificate/key trees may be restored; general `/etc` service configuration is skipped for safety |
| Permissions | Ownership normalised to `librenms:librenms` |
| Services | Services validated and restarted |
| Result | Job `completed`, state `DONE` |

If any stage fails, the restore aborts and reports the failing stage. The live
database is untouched until the import has already succeeded in a scratch
schema, so a failure before promotion is recoverable by simply fixing the cause.

## Performing a real restore

1. **Stop polling.** LibreNMS → Settings → Poller: disable, or
   `sudo systemctl stop librenms-scheduler.timer`.
2. **Note the current state.** `php artisan --version`, the LibreNMS git commit,
   and the current backup reference. You will want these if you have to compare.
3. **Take a fresh backup** and copy it off-host. Do not rely on the checkpoint
   alone for this.
4. Open **Plugins → Backup & Restore → Restore**, select the archive, read the
   preflight report, type the confirmation phrase, submit.
5. Watch the job. Progress and stage are reported every few seconds; the log tail
   is available while it runs.
6. On completion, verify: log in, check device count, run a poll cycle manually,
   and confirm graphs render.

## Rolling back a restore

Every restore creates a checkpoint first, before anything is modified. To go
back:

1. **Plugins → Backup & Restore → Restore**, choose the checkpoint
   (`cp-…`), and restore it the same way.
2. Or from the CLI:

   ```bash
   sudo -u librenms -H env BR_CONF=/etc/librenms-backup/br.conf \
     PYTHONPATH=/opt/librenms-backup-restore/worker \
     /usr/bin/python3 -m br.worker restore \
       --job "$(uuidgen)" --restore "$(uuidgen)" \
       --backup "" --checkpoint cp-20261114T055617Z-2d3d78d4
   ```

   A checkpoint is resolved from `br_checkpoints`, not `br_backups`; the
   backup reference is intentionally empty in rollback mode.

A rollback runs the same state machine as a restore, with two differences: the
archive comes from the checkpoint directory, and **no new checkpoint is
taken** — a rollback must never depend on the safety net it is recovering from.

Rollback is a real recovery point, not a record of intent. In the rehearsal,
a device row written *after* a completed restore was gone after rolling back to
the pre-restore checkpoint, and the pre-restore contents were back.

Checkpoints are excluded from retention — routine cleanup will never delete one.
A checkpoint is also only usable while its archive exists; a row whose file has
gone missing is marked `failed` rather than left looking valid.

## What a restore does to this plugin's own state

The dump is your LibreNMS database as it was at backup time. It does not
contain the plugin's own `br_*` tables, because those are created by the plugin
installer, not by LibreNMS. A restore therefore:

1. snapshots `br_jobs`, `br_backups`, `br_restores`, `br_checkpoints`,
   `br_settings`, `br_schedules` and `br_audit_events`;
2. imports the dump;
3. writes the snapshot back with `REPLACE INTO`, skipping any column that no
   longer exists (so upgrading the plugin across a restore is safe);
4. re-registers archives found on disk but absent from `br_backups`.

Step 4 matters after a disaster: `br_backups` is not derived from disk, so
without reconciliation the UI shows an empty backup history even though the
archives are sitting in `/var/backups/librenms/archives/`. `br.worker adopt`
performs the same reconciliation on demand.

## Manual recovery (no GUI available)

If LibreNMS will not start, the CLI path still works.

```bash
# 1. Inspect the archive without restoring it
sudo -u librenms env BR_CONF=/etc/librenms-backup/br.conf \
    PYTHONPATH=/opt/librenms-backup-restore/worker \
    python3 -m br.worker verify --job <uuid> \
        --backup /var/backups/librenms/archives/<ref>.brarchive --level deep

# 2. Recreate the database from the dump, by hand, on the socket
sudo mariadb -e "DROP DATABASE IF EXISTS librenms;
                 CREATE DATABASE librenms CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
#    (extract database/librenms.sql.gz from the archive first)
sudo zcat librenms.sql.gz | sudo mariadb --binary-mode librenms

# 3. Re-install the program schema and plugin row
sudo -u librenms LIBRENMS_ROOT=/opt/librenms \
    php /opt/librenms-backup-restore/scripts/br-schema.php install

# 4. Start services
sudo systemctl start php8.4-fpm apache2
```

Two traps in this path, both previously observed:

* piping a `.sql.gz` through `mariadb` without `--binary-mode` fails on dumps
  containing NUL bytes;
* restoring over TCP fails when the helper is sandboxed to `AF_UNIX` — use the
  socket.

## What is not restored

Machine-specific configuration is captured for reference but never applied
automatically:

```
/etc/netplan/            /etc/fstab
/etc/network/interfaces  /etc/ssh/ssh_host_*
/etc/hostname            /etc/cloud/
/etc/hosts               /etc/machine-id
/etc/resolv.conf
```

Restoring these blindly across hosts breaks networking and duplicates SSH
identity. Review them by hand if the target is a bare-metal rebuild.

## Timing expectations

Rough figures for a lab install (173 tables, ~240 KiB archive):

| Stage | Time |
|---|---|
| Preflight | < 2 s |
| Checkpoint | 5–30 s (runs a full backup) |
| Database | 1–30 s |
| Files + config | 1–10 s |
| Services | 5–30 s |
| **Total** | **~1 minute** |

Production figures scale with database size and RRD volume. Budget for the
checkpoint to take as long as your normal backup.

## Rehearsal checklist

Copy this into your change record for every restore:

- [ ] Archive verified at `deep` level
- [ ] Private key confirmed available off-host
- [ ] Off-host copy of the current state taken
- [ ] Rehearsed against a scratch host
- [ ] Downtime window announced
- [ ] Polling stopped
- [ ] Restore completed, job `DONE`
- [ ] Login verified, device count matches expectation
- [ ] Poll cycle run successfully
- [ ] Graphs render
- [ ] Checkpoint retained until the next confirmed-good backup
