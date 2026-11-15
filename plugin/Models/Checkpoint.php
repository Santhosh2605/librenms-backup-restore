<?php

namespace App\Plugins\BackupRestore\Models;

use Illuminate\Database\Eloquent\Model;

/**
 * Recovery checkpoint - an automatic, pre-restore safety copy.
 *
 * Checkpoints are intentionally *immutable* from the GUI: no endpoint deletes
 * them. `br_restores` tracks which checkpoint each restore produced so the
 * "Restore checkpoint" action can always be offered after a failed restore.
 */
class Checkpoint extends Model
{
    protected $table = 'br_checkpoints';
    protected $primaryKey = 'checkpoint_id';
    public $incrementing = false;
    protected $keyType = 'string';
    public $timestamps = false;
    protected $guarded = [];

    protected function casts(): array
    {
        return ['created_at' => 'datetime', 'size_bytes' => 'integer'];
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

    public function toPublic(): array
    {
        return [
            'id' => $this->checkpoint_id,
            'label' => $this->label,
            'status' => $this->status,
            'size' => $this->size_bytes,
            'created_at' => self::iso($this->created_at),
            'restore_id' => $this->restore_id,
        ];
    }
}
