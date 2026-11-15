<?php

namespace App\Plugins\BackupRestore\Models;

use Illuminate\Database\Eloquent\Model;

/**
 * Restore request + state machine snapshot.
 *
 * States follow the documented machine:
 *   QUEUED -> VALIDATING -> CHECKPOINTING -> MAINTENANCE ->
 *   RESTORING_DATABASE -> RESTORING_FILES -> RESTORING_CONFIGURATION ->
 *   FIXING_PERMISSIONS -> VALIDATING_SERVICES -> COMPLETED
 * Any failure moves to FAILED (never silently to COMPLETED).
 */
class Restore extends Model
{
    protected $table = 'br_restores';
    protected $primaryKey = 'restore_id';
    public $incrementing = false;
    protected $keyType = 'string';
    public $timestamps = false;
    protected $guarded = [];

    public const STATES = [
        'QUEUED', 'VALIDATING', 'CHECKPOINTING', 'MAINTENANCE',
        'RESTORING_DATABASE', 'RESTORING_FILES', 'RESTORING_CONFIGURATION',
        'FIXING_PERMISSIONS', 'VALIDATING_SERVICES', 'COMPLETED', 'FAILED',
        'ROLLED_BACK',
    ];

    /** States in which a restore holds the exclusive restore lock. */
    public const ACTIVE = [
        'QUEUED', 'VALIDATING', 'CHECKPOINTING', 'MAINTENANCE',
        'RESTORING_DATABASE', 'RESTORING_FILES', 'RESTORING_CONFIGURATION',
        'FIXING_PERMISSIONS', 'VALIDATING_SERVICES',
    ];

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

    public function toPublic(): array
    {
        return [
            'id' => $this->restore_id,
            'backup_ref' => $this->backup_ref,
            'state' => $this->state,
            'status' => $this->status,
            'checkpoint' => $this->checkpoint_ref,
            'created_at' => self::iso($this->created_at),
            'finished_at' => self::iso($this->finished_at),
            'actor' => $this->actor,
            'error' => $this->error_message,
            'log' => $this->log_tail,
        ];
    }
}
