<?php

namespace App\Plugins\BackupRestore\Models;

use Illuminate\Database\Eloquent\Model;
use Illuminate\Support\Facades\Request;

/**
 * Append-only audit trail.
 *
 * Written for every privileged action: backup created, verified, downloaded,
 * deleted, restore requested/started/completed/failed, settings changed,
 * authorisation denied. Never updated or deleted by the application.
 */
class AuditEvent extends Model
{
    protected $table = 'br_audit_events';
    protected $primaryKey = 'event_id';
    public $timestamps = false;
    protected $guarded = [];

    protected function casts(): array
    {
        return ['created_at' => 'datetime'];
    }

    public static function record(string $action, array $attributes = []): self
    {
        return self::create([
            'action' => $action,
            'actor' => $attributes['actor'] ?? (auth()->user()->username ?? 'system'),
            'target' => $attributes['target'] ?? null,
            'detail' => $attributes['detail'] ?? null,
            'ip' => $attributes['ip'] ?? Request::ip(),
            'result' => $attributes['result'] ?? 'ok',
            'created_at' => now(),
        ]);
    }

    public function toPublic(): array
    {
        return [
            'id' => $this->event_id,
            'action' => $this->action,
            'actor' => $this->actor,
            'target' => $this->target,
            'detail' => $this->detail,
            'ip' => $this->ip,
            'result' => $this->result,
            'at' => $this->created_at?->toIso8601String(),
        ];
    }
}
