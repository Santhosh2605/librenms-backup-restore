<?php

namespace App\Plugins\BackupRestore\Models;

use Illuminate\Database\Eloquent\Model;

/**
 * A completed (or failed) backup archive.
 */
class Backup extends Model
{
    protected $table = 'br_backups';
    protected $primaryKey = 'backup_id';
    public $timestamps = false;
    protected $guarded = [];

    protected function casts(): array
    {
        return [
            'created_at' => 'datetime',
            'verified_at' => 'datetime',
            'size_bytes' => 'integer',
            'uncompressed_bytes' => 'integer',
            'encrypted' => 'boolean',
        ];
    }

    public const STATUSES = ['pending', 'running', 'completed', 'failed', 'deleted'];
    public const VERIFICATION = ['unverified', 'verified', 'failed'];

    public function scopeCompleted($q)
    {
        return $q->where('status', 'completed');
    }

    public function isRestorable(): bool
    {
        return $this->status === 'completed'
            && $this->verified_status === 'verified'
            && $this->path !== null;
    }

    /**
     * Public representation. Deliberately omits absolute paths' internals and
     * every secret field.
     */
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
            'id' => $this->backup_id,
            'backup_ref' => $this->backup_ref,
            'type' => $this->type,
            'status' => $this->status,
            'verification' => $this->verified_status,
            'size' => $this->size_bytes,
            'size_human' => \App\Plugins\BackupRestore\Services\Formatter::bytes($this->size_bytes),
            'created_at' => $this->created_at instanceof \DateTimeInterface
                ? $this->created_at->format('Y-m-d H:i:s T')
                : (string) $this->created_at,
            'created_iso' => self::iso($this->created_at),
            'components' => json_decode($this->components ?? '[]', true) ?: [],
            'destination' => $this->destination,
            'encrypted' => (bool) $this->encrypted,
            'encryption' => $this->encryption_scheme,
            'librenms_version' => $this->librenms_version,
            'worker_version' => $this->worker_version,
            'error' => $this->error_message,
        ];
    }
}
