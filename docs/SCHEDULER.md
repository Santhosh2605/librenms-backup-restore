# Scheduling and retention

## Two independent clocks

| Timer | Interval | Job |
|---|---|---|
| `librenms-backup-dispatch.timer` | 15 s | Claim queued jobs and start a worker for each |
| `librenms-backup-scheduler.timer` | daily (default 02:00) | Evaluate schedule rows; enqueue due jobs |

Splitting them means a schedule edit takes effect immediately, and a missed
schedule evaluation cannot stall queued work.

## Job lifecycle

```
                 +------------------------------------------+
                 v                                          |
  queued --(dispatch claims it)--> running --> completed ---+
     |                                |
     |                                +--> failed
     +--> cancelled
```

`br_jobs` is the queue. The dispatcher claims a row with a conditional update:

```sql
UPDATE br_jobs SET status='running', started_at=UTC_TIMESTAMP(),
       heartbeat_at=UTC_TIMESTAMP(), systemd_unit=%s
 WHERE job_id=%s AND status='queued'
```

Because the `WHERE` includes `status='queued'`, two dispatchers racing on the
same row cannot both win. No advisory locks, no external queue.

Then it asks the helper to start `librenms-backup-worker@<job_id>.service`. The
unit's instance name *is* the job UUID, so parameters live in the database row
and never in the unit file.

### Why the dispatcher cannot call systemctl

It runs as `librenms`, unprivileged. `systemctl start` returns
`Access denied`. Spawning therefore goes through the helper:

```python
self.helper.call("worker.spawn", {"job_id": job["job_id"]})
```

`worker.spawn` accepts only a UUID (`JOB_ID_RE`) and passes `--no-block`, because
the worker unit is `Type=oneshot` and a full backup can run for hours — waiting
would stall the helper's socket for every other caller.

## Silence is failure

A worker that dies leaves its row `running` forever unless something notices.
The dispatcher liveness check treats a job as alive only if either:

* its `heartbeat_at` is within 10 minutes, **or**
* its systemd unit is still `is-active`.

Otherwise the job is marked:

```
status = failed
error_message = "worker stopped without reporting a final status"
```

and any partial archive is quarantined as `.incomplete`.

## Schedule rows

`br_schedules` describes *when*, in local server time:

| Column | Meaning |
|---|---|
| `name` | Label shown in the UI |
| `frequency` | `daily`, `weekly`, `monthly`, `hourly` |
| `time_of_day` | `HH:MM` |
| `weekday` | `0`–`6` for weekly |
| `day_of_month` | `1`–`31` for monthly |
| `backup_type` | `full`, `database`, `rrd`, `configuration`, `system` |
| `enabled` | Whether evaluation should enqueue it |

Evaluation is idempotent per window: the scheduler records the last-evaluated
window, so restarting the timer at 02:05 does not produce a second 02:00 run.

## Retention (GFS)

Grandfather-father-son, evaluated newest-first over the backup list:

* keep the newest backup of each day, for `keep_daily` days;
* keep the newest backup of each ISO week, for `keep_weekly` weeks;
* keep the newest backup of each month, for `keep_monthly` months;
* always keep anything in `protected` (checkpoints, pinned backups);
* apply `keep_total` as an absolute cap afterwards — protected wins.

```ini
[retention]
keep_daily   = 7
keep_weekly  = 4
keep_monthly = 6
keep_total   = 0      # 0 = unlimited
```

Each tier is evaluated **independently**. Chaining them would let an older backup
from an already-covered day fall through into the weekly tier and be retained as
a "weekly", silently exceeding `keep_total` and defeating daily de-duplication.

When `keep_total` is set, backups inside the cap are kept explicitly even if they
match no tier, so every backup ends up in exactly one of `keep` or `delete` —
never in neither.

### What retention never touches

* **Recovery checkpoints.** They are tagged `retention_class='checkpoint'` and
  are the rollback target for a restore.
* **The newest verified backup.** If the plan would delete every archive it
  keeps at least one.
* **Anything not `type='full'`** is ignored by planning, not deleted.

### Running retention manually

```bash
sudo -u librenms env BR_CONF=/etc/librenms-backup/br.conf \
    PYTHONPATH=/opt/librenms-backup-restore/worker \
    python3 -m br.worker retention --job <uuid>
```

The plan (`keep`, `delete`, and the reason for each decision) is written to
`/var/log/librenms-backup/retention.log` before anything is removed.

## Verifying scheduled backups

Set the policy in `br.conf`:

```ini
[archive]
verify_after_backup = always        # always | scheduled-only | never
verify_deep         = false
```

`always` is the default. A backup that fails verification is recorded as
`failed`, not `completed`, and never becomes a restore candidate.

## Timers in practice

```bash
systemctl list-timers 'librenms-backup*'
systemctl status librenms-backup-scheduler.timer --no-pager
journalctl -u librenms-backup-scheduler -n 50
tail -f /var/log/librenms-backup/scheduler.log
```

To change the hour, edit `config/systemd/librenms-backup-scheduler.timer`
(`OnCalendar=`), reinstall, then `systemctl daemon-reload && systemctl restart
librenms-backup-scheduler.timer`.
