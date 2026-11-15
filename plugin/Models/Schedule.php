<?php

namespace App\Plugins\BackupRestore\Models;

use Illuminate\Database\Eloquent\Model;

/**
 * Backup schedule (daily / weekly / monthly) materialised to a systemd timer
 * by the installer + `scripts/render-systemd-timer.sh`.
 */
class Schedule extends Model
{
    protected $table = 'br_schedules';
    protected $primaryKey = 'schedule_id';
    public $timestamps = false;
    protected $guarded = [];

    public const FREQUENCIES = ['daily', 'weekly', 'monthly'];

    protected function casts(): array
    {
        return [
            'enabled' => 'boolean',
            'keep_daily' => 'integer',
            'keep_weekly' => 'integer',
            'keep_monthly' => 'integer',
        ];
    }

    public function toPublic(): array
    {
        return [
            'id' => $this->schedule_id,
            'name' => $this->name,
            'frequency' => $this->frequency,
            'time' => $this->time_of_day,
            'weekday' => $this->weekday,
            'day_of_month' => $this->day_of_month,
            'enabled' => (bool) $this->enabled,
            'retention' => [
                'keep_daily' => $this->keep_daily,
                'keep_weekly' => $this->keep_weekly,
                'keep_monthly' => $this->keep_monthly,
            ],
            'next_run' => $this->next_run,
        ];
    }
}
