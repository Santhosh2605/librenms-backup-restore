<?php

namespace App\Plugins\BackupRestore\Models;

use Illuminate\Database\Eloquent\Model;

/**
 * A unit of work executed by the worker (backup, upload import, verify, restore, retention).
 *
 * The GUI never runs work directly: it writes a row here and asks systemd to
 * start `librenms-backup-worker@<job_id>.service`. The worker owns the row's
 * status transitions, which keeps the browser session out of the loop.
 */
class Job extends Model
{
    protected $table = 'br_jobs';
    protected $primaryKey = 'job_id';
    public $incrementing = false;
    protected $keyType = 'string';
    public $timestamps = false;
    protected $guarded = [];

    protected function casts(): array
    {
        return [
            'progress' => 'integer',
            'created_at' => 'datetime',
            'started_at' => 'datetime',
            'finished_at' => 'datetime',
            'heartbeat_at' => 'datetime',
        ];
    }

    /** ISO-8601 rendering that tolerates both Carbon and raw DB strings. */
    private static function iso(mixed $value): ?string
    {
        if ($value === null || $value === '') {
            return null;
        }
        if ($value instanceof \DateTimeInterface) {
            return $value->format('c');
        }

        return (string) $value;
    }

    /** Terminal states. */
    public const FINISHED = ['completed', 'failed', 'cancelled'];

    public const KINDS = ['backup', 'import', 'verify', 'restore', 'retention', 'checkpoint'];

    public function isFinished(): bool
    {
        return in_array($this->status, self::FINISHED, true);
    }

    public function toPublic(): array
    {
        return [
            'id' => $this->job_id,
            'kind' => $this->kind,
            'type' => $this->backup_type,
            'status' => $this->status,
            'stage' => $this->stage,
            'progress' => (int) $this->progress,
            'message' => $this->message,
            'actor' => $this->actor,
            'created_at' => self::iso($this->created_at),
            'started_at' => self::iso($this->started_at),
            'finished_at' => self::iso($this->finished_at),
            'backup_id' => $this->backup_id,
            'restore_id' => $this->restore_id,
            'error' => $this->error_message,
        ];
    }
}
