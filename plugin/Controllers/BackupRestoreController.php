<?php

namespace App\Plugins\BackupRestore\Controllers;

use App\Plugins\BackupRestore\Models\AuditEvent;
use App\Plugins\BackupRestore\Models\Backup;
use App\Plugins\BackupRestore\Models\Checkpoint;
use App\Plugins\BackupRestore\Models\Job;
use App\Plugins\BackupRestore\Models\Restore;
use App\Plugins\BackupRestore\Models\Schedule;
use App\Plugins\BackupRestore\Services\ApiKernel;
use App\Plugins\BackupRestore\Services\Guard;
use App\Plugins\BackupRestore\Services\Settings;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;

/**
 * JSON API for the Backup & Restore GUI.
 *
 * Reached only through the explicitly registered entrypoint
 * html/plugin-backuprestore.php (see docs/INSTALL.md "Why not routes?").
 *
 * Contract:
 *   GET  endpoints -> read-only, `view` ability
 *   POST endpoints -> mutate, `operate`/`restore`/`configure` ability + CSRF
 *
 * This class NEVER runs a shell command. It writes a job row and asks the
 * helper to start a systemd unit. Secrets are never serialised to the client.
 */
class BackupRestoreController
{
    public function __construct()
    {
        \Illuminate\Support\Facades\Date::setLocale('en');
    }

    /**
     * Serve this plugin's own static assets.
     *
     * Only two filenames exist; anything else is a 404. No path concatenation
     * from user input, no directory listing, no LibreNMS core assets.
     */
    public function asset(string $asset = '')
    {
        $allowed = [
            'backup-restore.js' => 'application/javascript',
            'backup-restore.css' => 'text/css',
        ];
        if (! isset($allowed[$asset])) {
            abort(404);
        }
        $path = base_path('app/Plugins/BackupRestore/resources/views/assets/' . $asset);
        if (! is_file($path)) {
            abort(404);
        }

        return response()->file($path, [
            'Content-Type' => $allowed[$asset] . '; charset=utf-8',
            'Cache-Control' => 'private, max-age=300',
            'X-Content-Type-Options' => 'nosniff',
        ]);
    }

    /** Capability index - lets the client adapt to the installed version. */
    public function apiIndex(Request $request): JsonResponse
    {
        Guard::check($request, 'view');

        return ApiKernel::ok([
            'product' => 'librenms-backup-restore',
            'version' => \App\Plugins\BackupRestore\Services\Version::VERSION,
            'abilities' => Guard::ABILITIES,
            'backup_types' => ['full', 'database', 'rrd', 'configuration', 'system'],
            'restore_states' => Restore::STATES,
            'endpoints' => [
                'GET api/status', 'GET api/backups', 'GET api/backups/{ref}',
                'GET api/backups/{ref}/download', 'GET api/jobs', 'GET api/jobs/{job}',
                'GET api/restores', 'GET api/restores/preflight/{ref}',
                'GET api/schedules', 'GET api/settings', 'GET api/audit',
                'POST api/backups', 'POST api/backups/{ref}/verify',
                'POST api/backups/{ref}/delete', 'POST api/restores',
                'POST api/restores/checkpoint', 'POST api/schedules', 'POST api/settings',
            ],
        ]);
    }

    // ------------------------------------------------------------------ //
    // Dashboard
    // ------------------------------------------------------------------ //

    public function status(Request $request): JsonResponse
    {
        Guard::check($request, 'view');

        $total = Backup::where('status', 'completed')->count();
        $lastOk = Backup::where('status', 'completed')->orderByDesc('created_at')->first();
        $lastFail = Backup::where('status', 'failed')->orderByDesc('created_at')->first();
        $lastRestore = Restore::orderByDesc('created_at')->first();
        $activeJob = Job::whereIn('status', ['queued', 'running'])->orderByDesc('created_at')->first();

        $storage = $this->storageSnapshot();
        $next = $this->nextScheduled();

        return ApiKernel::ok([
            'totals' => [
                'backups' => $total,
                'verified' => Backup::where('verified_status', 'verified')->count(),
                'failed' => Backup::where('status', 'failed')->count(),
                'storage_bytes' => (int) Backup::where('status', 'completed')->sum('size_bytes'),
            ],
            'last_backup' => $lastOk?->toPublic(),
            'last_failed_backup' => $lastFail?->toPublic(),
            'last_restore' => $lastRestore?->toPublic(),
            'next_scheduled' => $next,
            'storage' => $storage,
            'current_job' => $activeJob?->toPublic(),
            'worker' => $this->workerHealthSnapshot(),
            'encryption' => [
                'scheme' => Settings::get('encryption_scheme', 'unknown'),
                'key_fingerprint' => Settings::get('key_fingerprint', 'unknown'),
            ],
        ]);
    }

    // ------------------------------------------------------------------ //
    // Backups
    // ------------------------------------------------------------------ //

    public function backups(Request $request): JsonResponse
    {
        Guard::check($request, 'view');

        $query = Backup::query()->orderByDesc('created_at');
        if ($type = $request->query('type')) {
            if (in_array($type, ['full', 'database', 'rrd', 'configuration', 'system'], true)) {
                $query->where('type', $type);
            }
        }
        if ($status = $request->query('status')) {
            $query->where('status', $status);
        }
        $limit = min(200, max(1, (int) $request->query('limit', 50)));

        return ApiKernel::ok([
            'items' => $query->limit($limit)->get()->map->toPublic()->all(),
            'count' => Backup::count(),
        ]);
    }

    public function backupDetail(Request $request, string $ref): JsonResponse
    {
        Guard::check($request, 'view');
        $backup = Backup::where('backup_ref', $ref)->first();
        if (! $backup) {
            return ApiKernel::error('backup not found', 404);
        }
        $manifest = $this->manifestSidecar($backup);

        return ApiKernel::ok([
            'backup' => $backup->toPublic(),
            'manifest' => $manifest,
            'can_restore' => $backup->isRestorable(),
        ]);
    }

    /**
     * Queue a backup. Body: {"type": "full"}.
     */
    public function createBackup(Request $request): JsonResponse
    {
        Guard::check($request, 'operate');

        $type = (string) $request->input('type', 'full');
        if (! in_array($type, ['full', 'database', 'rrd', 'configuration', 'system'], true)) {
            return ApiKernel::error('unsupported backup type', 422);
        }

        $busy = Job::whereIn('status', ['queued', 'running'])->first();
        if ($busy) {
            return ApiKernel::error('another job is already running', 409, [
                'job' => $busy->toPublic(),
            ]);
        }
        if ($blocking = Restore::whereIn('state', Restore::ACTIVE)->first()) {
            return ApiKernel::error('a restore is in progress; backups are suspended', 409,
                ['restore' => $blocking->toPublic()]);
        }

        $job = $this->enqueue('backup', $type);
        if ($job instanceof JsonResponse) {
            return $job;
        }

        ApiKernel::audit($request, 'backup.requested', ['job' => $job->job_id, 'type' => $type]);

        return ApiKernel::ok(['job' => $job->toPublic()], 202);
    }

    public function verifyBackup(Request $request, string $ref): JsonResponse
    {
        Guard::check($request, 'operate');
        $backup = Backup::where('backup_ref', $ref)->first();
        if (! $backup) {
            return ApiKernel::error('backup not found', 404);
        }
        if ($backup->status !== 'completed') {
            return ApiKernel::error('only completed backups can be verified', 422);
        }

        $job = $this->enqueue('verify', null, ['backup_ref' => $ref, 'level' => 'standard']);
        if ($job instanceof JsonResponse) {
            return $job;
        }
        ApiKernel::audit($request, 'backup.verify.requested', ['backup' => $ref]);

        return ApiKernel::ok(['job' => $job->toPublic()], 202);
    }

    public function deleteBackup(Request $request, string $ref): JsonResponse
    {
        Guard::check($request, 'operate');

        $backup = Backup::where('backup_ref', $ref)->first();
        if (! $backup) {
            return ApiKernel::error('backup not found', 404);
        }
        // Typed confirmation required for destructive actions.
        if (! $this->confirmed($request, $ref)) {
            return ApiKernel::error('confirmation phrase does not match', 422);
        }
        if (Job::whereIn('status', ['queued', 'running'])->exists()) {
            return ApiKernel::error('cannot delete while a job is running', 409);
        }
        if ($this->isProtected($ref)) {
            ApiKernel::audit($request, 'backup.delete.blocked', ['backup' => $ref], 'denied');

            return ApiKernel::error(
                'this archive is referenced by a recovery checkpoint and cannot be deleted', 409
            );
        }

        // Deletion of the file itself is queued so the worker (not PHP-FPM)
        // touches the filesystem.
        $job = $this->enqueue('retention', null, ['force_delete' => $ref]);
        if ($job instanceof JsonResponse) {
            return $job;
        }
        ApiKernel::audit($request, 'backup.delete.requested', ['backup' => $ref, 'job' => $job->job_id]);

        return ApiKernel::ok(['job' => $job->toPublic()], 202);
    }

    /**
     * Secure download: streams the archive through PHP after re-checking
     * authorisation + CSRF. The backup directory is never web-served.
     */
    public function downloadBackup(Request $request, string $ref)
    {
        Guard::check($request, 'operate');
        $backup = Backup::where('backup_ref', $ref)->first();
        if (! $backup || $backup->status !== 'completed') {
            abort(404, 'backup not found');
        }
        $path = $backup->path;
        if (! $path || ! is_file($path) || ! is_readable($path)) {
            ApiKernel::audit($request, 'backup.download.missing', ['backup' => $ref], 'error');

            abort(410, 'archive file is missing on disk');
        }
        $real = realpath($path);
        if ($real === false || Str::contains($real, '..') || ! Str::startsWith($real, '/')) {
            abort(400, 'invalid archive path');
        }

        ApiKernel::audit($request, 'backup.download', [
            'backup' => $ref,
            'size' => filesize($real),
            'sha256' => $backup->sha256,
        ]);

        return response()->streamDownload(function () use ($real): void {
            $fh = fopen($real, 'rb');
            if ($fh === false) {
                return;
            }
            while (! feof($fh)) {
                echo fread($fh, 1024 * 1024);
                flush();
            }
            fclose($fh);
        }, basename($real), [
            'Content-Type' => 'application/octet-stream',
            'X-Content-Type-Options' => 'nosniff',
            'Cache-Control' => 'no-store',
            'X-Backup-SHA256' => (string) $backup->sha256,
        ]);
    }

    // ------------------------------------------------------------------ //
    // Restore
    // ------------------------------------------------------------------ //

    public function restores(Request $request): JsonResponse
    {
        Guard::check($request, 'view');

        return ApiKernel::ok([
            'items' => Restore::orderByDesc('created_at')->limit(50)->get()->map->toPublic()->all(),
            'checkpoints' => Checkpoint::orderByDesc('created_at')->limit(20)->get()->map->toPublic()->all(),
            'active' => Restore::whereIn('state', Restore::ACTIVE)->first()?->toPublic(),
        ]);
    }

    public function restorePreflight(Request $request, string $ref): JsonResponse
    {
        Guard::check($request, 'restore');
        $backup = Backup::where('backup_ref', $ref)->first();
        if (! $backup) {
            return ApiKernel::error('backup not found', 404);
        }

        $checks = [];
        $checks[] = [
            'name' => 'archive_present',
            'ok' => (bool) ($backup->path && is_file($backup->path)),
            'detail' => $backup->path ? 'archive file located' : 'no archive path recorded',
        ];
        $checks[] = [
            'name' => 'checksum_verified',
            'ok' => $backup->verified_status === 'verified',
            'detail' => 'verification status: ' . $backup->verified_status,
        ];
        $checks[] = [
            'name' => 'encryption_key',
            'ok' => ! $backup->key_fingerprint || $backup->key_fingerprint === Settings::get('key_fingerprint'),
            'detail' => 'key fingerprint ' . ($backup->key_fingerprint ?: 'unknown'),
        ];
        $manifest = $this->manifestSidecar($backup);
        $checks[] = [
            'name' => 'manifest_valid',
            'ok' => $manifest !== null,
            'detail' => $manifest ? 'manifest schema ' . ($manifest['manifest_schema'] ?? '?') : 'manifest sidecar missing',
        ];
        $checks[] = [
            'name' => 'free_space',
            'ok' => $this->storageSnapshot()['free_bytes'] > ($backup->size_bytes * 2),
            'detail' => 'requires ~2x archive size for checkpoint + extraction',
        ];
        $checks[] = [
            'name' => 'no_active_restore',
            'ok' => ! Restore::whereIn('state', Restore::ACTIVE)->exists(),
            'detail' => 'concurrent restores are refused',
        ];
        $checks[] = [
            'name' => 'no_active_job',
            'ok' => ! Job::whereIn('status', ['queued', 'running'])->exists(),
            'detail' => 'in-flight backups must finish first',
        ];

        return ApiKernel::ok([
            'backup' => $backup->toPublic(),
            'checks' => $checks,
            'ready' => ! in_array(false, array_column($checks, 'ok'), true),
            'confirm_phrase' => (string) Settings::get('confirm_phrase', 'RESTORE'),
        ]);
    }

    public function restore(Request $request): JsonResponse
    {
        Guard::check($request, 'restore');

        $ref = (string) $request->input('backup_ref', '');
        $phrase = (string) Settings::get('confirm_phrase', 'RESTORE');
        if (strtoupper(trim((string) $request->input('confirm', ''))) !== $phrase) {
            ApiKernel::audit($request, 'restore.denied', ['backup' => $ref], 'denied');

            return ApiKernel::error('typed confirmation did not match', 422);
        }

        $backup = Backup::where('backup_ref', $ref)->first();
        if (! $backup) {
            return ApiKernel::error('backup not found', 404);
        }
        if (! $backup->isRestorable()) {
            return ApiKernel::error(
                'backup must be completed and verified before it can be restored', 422
            );
        }
        if (Restore::whereIn('state', Restore::ACTIVE)->exists()) {
            return ApiKernel::error('another restore is already running', 409);
        }
        if (Job::whereIn('status', ['queued', 'running'])->exists()) {
            return ApiKernel::error('a backup job is still running', 409);
        }

        $restoreId = (string) Str::uuid();
        Restore::create([
            'restore_id' => $restoreId,
            'backup_ref' => $ref,
            'state' => 'QUEUED',
            'status' => 'running',
            'actor' => $request->user()->username,
            'create_checkpoint' => true,
            'options' => json_encode(['confirm' => $phrase, 'requested_at' => now()->toIso8601String()]),
            'created_at' => now(),
        ]);

        $job = $this->enqueue('restore', null, ['restore_id' => $restoreId, 'backup_ref' => $ref]);
        if ($job instanceof JsonResponse) {
            Restore::where('restore_id', $restoreId)->update(['status' => 'failed', 'state' => 'FAILED']);

            return $job;
        }

        ApiKernel::audit($request, 'restore.requested', [
            'backup' => $ref, 'restore' => $restoreId, 'job' => $job->job_id,
        ]);

        return ApiKernel::ok([
            'restore' => Restore::where('restore_id', $restoreId)->first()->toPublic(),
            'job' => $job->toPublic(),
        ], 202);
    }

    public function restoreCheckpoint(Request $request): JsonResponse
    {
        Guard::check($request, 'restore');
        $checkpointId = (string) $request->input('checkpoint', '');
        $checkpoint = Checkpoint::where('checkpoint_id', $checkpointId)->first();
        if (! $checkpoint) {
            return ApiKernel::error('checkpoint not found', 404);
        }
        $phrase = (string) Settings::get('confirm_phrase', 'RESTORE');
        if (strtoupper(trim((string) $request->input('confirm', ''))) !== $phrase) {
            return ApiKernel::error('typed confirmation did not match', 422);
        }

        // A checkpoint is itself a backup row; find its ref by path basename.
        $ref = basename((string) $checkpoint->path, '.brarchive');
        $backup = Backup::where('backup_ref', $ref)->first();
        if (! $backup || ! is_file((string) $checkpoint->path)) {
            return ApiKernel::error(
                'checkpoint archive is not present as a restorable backup record', 422
            );
        }

        $restoreId = (string) Str::uuid();
        Restore::create([
            'restore_id' => $restoreId,
            'backup_ref' => $ref,
            'state' => 'QUEUED',
            'status' => 'running',
            'actor' => $request->user()->username,
            'create_checkpoint' => true,
            'options' => json_encode(['from_checkpoint' => $checkpointId]),
            'created_at' => now(),
        ]);
        $job = $this->enqueue('restore', null, ['restore_id' => $restoreId, 'backup_ref' => $ref]);
        if ($job instanceof JsonResponse) {
            return $job;
        }
        ApiKernel::audit($request, 'restore.from_checkpoint', [
            'checkpoint' => $checkpointId, 'restore' => $restoreId,
        ]);

        return ApiKernel::ok(['restore_id' => $restoreId, 'job' => $job->toPublic()], 202);
    }

    public function restoreDetail(Request $request, string $restoreId): JsonResponse
    {
        Guard::check($request, 'view');
        $restore = Restore::where('restore_id', $restoreId)->first();
        if (! $restore) {
            return ApiKernel::error('restore not found', 404);
        }

        return ApiKernel::ok(['restore' => $restore->toPublic()]);
    }

    // ------------------------------------------------------------------ //
    // Jobs
    // ------------------------------------------------------------------ //

    public function jobStatus(Request $request, string $jobId): JsonResponse
    {
        Guard::check($request, 'view');
        $job = Job::find($jobId);
        if (! $job) {
            return ApiKernel::error('job not found', 404);
        }
        $log = $this->tailJobLog($job);

        return ApiKernel::ok(['job' => $job->toPublic(), 'log' => $log]);
    }

    public function jobs(Request $request): JsonResponse
    {
        Guard::check($request, 'view');

        return ApiKernel::ok([
            'items' => Job::orderByDesc('created_at')->limit(50)->get()->map->toPublic()->all(),
        ]);
    }

    // ------------------------------------------------------------------ //
    // Schedules
    // ------------------------------------------------------------------ //

    public function schedules(Request $request): JsonResponse
    {
        Guard::check($request, 'view');

        return ApiKernel::ok([
            'items' => Schedule::orderBy('schedule_id')->get()->map->toPublic()->all(),
            'timer_state' => $this->timerState(),
        ]);
    }

    public function saveSchedule(Request $request): JsonResponse
    {
        Guard::check($request, 'configure');

        $data = $request->validate([
            'schedule_id' => 'nullable|integer|min:1',
            'name' => 'required|string|max:64',
            'frequency' => 'required|in:daily,weekly,monthly',
            'time' => ['required', 'regex:/^([01]\d|2[0-3]):[0-5]\d$/'],
            'weekday' => 'nullable|integer|min:0|max:6',
            'day_of_month' => 'nullable|integer|min:1|max:28',
            'enabled' => 'required|boolean',
            'backup_type' => 'required|in:full,database,rrd,configuration,system',
            'keep_daily' => 'required|integer|min:0|max:365',
            'keep_weekly' => 'required|integer|min:0|max:104',
            'keep_monthly' => 'required|integer|min:0|max:120',
        ]);

        $schedule = isset($data['schedule_id'])
            ? Schedule::findOrFail($data['schedule_id'])
            : new Schedule;

        $schedule->fill([
            'name' => $data['name'],
            'frequency' => $data['frequency'],
            'time_of_day' => $data['time'],
            'weekday' => $data['weekday'] ?? null,
            'day_of_month' => $data['day_of_month'] ?? null,
            'enabled' => (bool) $data['enabled'],
            'backup_type' => $data['backup_type'],
            'keep_daily' => $data['keep_daily'],
            'keep_weekly' => $data['keep_weekly'],
            'keep_monthly' => $data['keep_monthly'],
        ]);
        $schedule->created_at = $schedule->created_at ?? now();
        $schedule->updated_at = now();
        $schedule->save();

        ApiKernel::audit($request, 'schedule.saved', ['schedule' => $schedule->schedule_id]);

        return ApiKernel::ok(['schedule' => $schedule->fresh()->toPublic()]);
    }

    // ------------------------------------------------------------------ //
    // Settings
    // ------------------------------------------------------------------ //

    public function settings(Request $request): JsonResponse
    {
        Guard::check($request, 'view');

        return ApiKernel::ok([
            'plugin' => [
                'enabled' => (bool) Settings::get('enabled', true),
                'require_typed_confirmation' => (bool) Settings::get('require_typed_confirmation', true),
                'notify_on_failure' => (bool) Settings::get('notify_on_failure', true),
                'confirm_phrase' => (string) Settings::get('confirm_phrase', 'RESTORE'),
            ],
            'worker' => $this->workerHealthSnapshot(),
            'storage' => $this->storageSnapshot(),
            // Read-only reflection of root-owned br.conf; never editable here.
            'worker_config' => $this->workerConfigSummary(),
        ]);
    }

    public function saveSettings(Request $request): JsonResponse
    {
        Guard::check($request, 'configure');

        $data = $request->validate([
            'enabled' => 'required|boolean',
            'require_typed_confirmation' => 'required|boolean',
            'notify_on_failure' => 'required|boolean',
            'retention_keep_daily' => 'required|integer|min:0|max:365',
            'retention_keep_weekly' => 'required|integer|min:0|max:104',
            'retention_keep_monthly' => 'required|integer|min:0|max:120',
            'encryption_scheme' => 'required|in:age,aesgcm-pbkdf2,none',
        ]);

        Settings::set($data);
        ApiKernel::audit($request, 'settings.saved', ['keys' => array_keys($data)]);

        return ApiKernel::ok(['settings' => $data]);
    }

    // ------------------------------------------------------------------ //
    // Audit log
    // ------------------------------------------------------------------ //

    public function audit(Request $request): JsonResponse
    {
        Guard::check($request, 'view');

        $query = AuditEvent::query()->orderByDesc('event_id');
        if ($action = $request->query('action')) {
            $query->where('action', 'like', preg_replace('/[^a-z._\-]/', '', (string) $action) . '%');
        }
        if ($actor = $request->query('actor')) {
            $query->where('actor', (string) $actor);
        }

        return ApiKernel::ok([
            'items' => $query->limit(200)->get()->map->toPublic()->all(),
        ]);
    }

    // ------------------------------------------------------------------ //
    // Worker health
    // ------------------------------------------------------------------ //

    public function workerHealth(Request $request): JsonResponse
    {
        Guard::check($request, 'view');

        return ApiKernel::ok(['worker' => $this->workerHealthSnapshot()]);
    }

    // ------------------------------------------------------------------ //
    // internals
    // ------------------------------------------------------------------ //

    /**
     * Create a queue row for the worker.
     *
     * The web tier performs a single INSERT and nothing else: it has no
     * privileges, no sockets and no filesystem access to the backup tree.
     */
    private function enqueue(string $kind, ?string $backupType = null, array $options = [])
    {
        $job = Job::create([
            'job_id' => (string) Str::uuid(),
            'kind' => $kind,
            'backup_type' => $backupType,
            'status' => 'queued',
            'progress' => 0,
            'actor' => auth()->user()->username ?? 'system',
            'backup_id' => $options['backup_ref'] ?? null,
            'restore_id' => $options['restore_id'] ?? null,
            'created_at' => now(),
        ]);

        // NO privilege escalation from the web tier.
        //
        // The PHP-FPM process only writes a row. The root-owned systemd timer
        // `librenms-backup-dispatch.timer` runs `br-worker dispatch` as the
        // unprivileged `librenms` user every 15s; that process claims queued
        // rows and detaches one worker per job. See docs/SECURITY.md
        // "Privilege model".
        if (isset($options['force_delete'])) {
            DB::table('br_settings')->updateOrInsert(
                ['key' => 'force_delete_' . $job->job_id],
                ['value' => json_encode($options['force_delete']), 'updated_at' => now()]
            );
        }

        return $job->fresh();
    }

    /**
     * Read-only health snapshot produced by the worker.
     *
     * The web tier cannot query systemd (that would need root), so the worker
     * publishes a JSON status file. Absent/stale file => the worker is
     * reported as unavailable rather than guessing.
     */
    private function readHealth(): array
    {
        $path = (string) Settings::get('health_file', '/var/lib/librenms-backup/health.json');
        if (! is_readable($path)) {
            return ['available' => false, 'reason' => 'health file not readable: ' . $path];
        }
        $data = json_decode((string) @file_get_contents($path), true);

        return is_array($data)
            ? $data + ['available' => true]
            : ['available' => false, 'reason' => 'health file malformed'];
    }

    private function confirmed(Request $request, string $expected): bool
    {
        if (! Settings::get('require_typed_confirmation', true)) {
            return $request->boolean('confirm_ok', false);
        }

        return trim((string) $request->input('confirm', '')) === $expected;
    }

    /**
     * An archive is protected when any restore record refers to it, or when it
     * is stored in the recovery-checkpoint area. Deletion is queued, and the
     * worker re-checks this rule before unlinking anything.
     */
    private function isProtected(string $ref): bool
    {
        if (DB::table('br_restores')->where('backup_ref', $ref)->exists()) {
            return true;
        }
        $row = Backup::where('backup_ref', $ref)->first();
        if (! $row) {
            return true;
        }
        $checkpointRoot = (string) Settings::get('checkpoint_dir', '/var/backups/librenms/recovery-checkpoints');

        return $row->retention_class === 'checkpoint'
            || str_starts_with((string) $row->path, $checkpointRoot);
    }

    private function storageSnapshot(): array
    {
        $root = (string) Settings::get('backup_root', '/var/backups/librenms');
        $free = @disk_free_space($root);
        $total = @disk_total_space($root);
        $used = (int) Backup::where('status', 'completed')->sum('size_bytes');

        return [
            'root' => $root,
            'archive_bytes' => $used,
            'free_bytes' => (int) $free,
            'total_bytes' => (int) $total,
            'free_human' => $free !== false ? $this->human((float) $free) : 'unknown',
            'percent_used' => ($total && $total > 0) ? round((($total - $free) / $total) * 100, 1) : null,
        ];
    }

    private function human(float $bytes): string
    {
        $units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
        $i = 0;
        while ($bytes >= 1024 && $i < count($units) - 1) {
            $bytes /= 1024;
            $i++;
        }

        return number_format($bytes, 1) . ' ' . $units[$i];
    }

    private function nextScheduled(): ?array
    {
        $next = Schedule::where('enabled', true)->whereNotNull('next_run')
            ->orderBy('next_run')->first();
        if (! $next) {
            return null;
        }

        return [
            'name' => $next->name,
            'frequency' => $next->frequency,
            'at' => $next->next_run,
        ];
    }

    private function timerState(): array
    {
        $health = $this->readHealth();

        return [
            'unit' => 'librenms-backup-scheduler.timer',
            'active' => $health['timers']['librenms-backup-scheduler.timer'] ?? 'unknown',
            'dispatch' => $health['timers']['librenms-backup-dispatch.timer'] ?? 'unknown',
        ];
    }

    private function workerHealthSnapshot(): array
    {
        $health = $this->readHealth();
        $heartbeat = optional(Job::whereNotNull('heartbeat_at')
            ->orderByDesc('heartbeat_at')->first())->heartbeat_at;

        return [
            'helper' => $health['helper'] ?? ($health['available'] ? 'unknown' : 'unreachable'),
            'helper_detail' => $health['reason'] ?? null,
            'worker_version' => $health['version'] ?? null,
            'generated_at' => $health['at'] ?? null,
            'checks' => $health['checks'] ?? [],
            'stale' => isset($health['at']) ? (strtotime((string) $health['at']) < time() - 900) : true,
            'last_heartbeat' => $heartbeat instanceof \DateTimeInterface
                ? $heartbeat->format('c')
                : ($heartbeat ?: null),
        ];
    }

    private function workerConfigSummary(): array
    {
        $conf = Settings::confPath();
        if (! is_readable($conf)) {
            return ['readable' => false, 'path' => $conf];
        }
        $summary = ['readable' => true, 'path' => $conf];
        $interesting = ['compression', 'encryption', 'zstd_level', 'verify_after_backup'];
        foreach (file($conf, FILE_IGNORE_NEW_LINES) ?: [] as $line) {
            $line = trim($line);
            if ($line === '' || $line[0] === '#' || ! str_contains($line, '=')) {
                continue;
            }
            [$key, $value] = array_map('trim', explode('=', $line, 2));
            if (in_array($key, $interesting, true)) {
                $summary[$key] = $value;
            }
        }

        return $summary;
    }

    private function manifestSidecar(Backup $backup): ?array
    {
        $path = (string) $backup->path;
        if ($path === '') {
            return null;
        }
        $sidecar = $path . '.manifest.json';
        if (! is_readable($sidecar)) {
            return null;
        }
        $data = json_decode((string) file_get_contents($sidecar), true);

        return is_array($data) ? $data : null;
    }

    /**
     * Tail the worker log file for a job. Logs are keyed by job id so we do not
     * leak unrelated history to the browser.
     */
    private function tailJobLog(Job $job): array
    {
        $dir = (string) Settings::get('log_dir', '/var/log/librenms-backup');
        $file = match ($job->kind) {
            'backup' => 'backup.log',
            'verify' => 'verify.log',
            'restore' => 'restore.log',
            'retention' => 'retention.log',
            'checkpoint' => 'backup.log',
            default => 'backup.log',
        };
        $path = rtrim($dir, '/') . '/' . $file;
        if (! is_readable($path)) {
            return [];
        }
        $lines = [];
        $fh = new \SplFileObject($path, 'r');
        $fh->seek(max(0, $fh->getIterator()->count() - 400));
        foreach ($fh as $line) {
            $record = json_decode(trim((string) $line), true);
            if (! is_array($record)) {
                continue;
            }
            // Filter to this job where the worker recorded one.
            if (isset($record['fields']['job']) && $record['fields']['job'] !== $job->job_id) {
                continue;
            }
            $lines[] = [
                'ts' => $record['ts'] ?? null,
                'level' => $record['level'] ?? 'info',
                'message' => $record['message'] ?? '',
                'progress' => $record['fields']['progress'] ?? null,
                'stage' => $record['fields']['stage'] ?? null,
            ];
        }

        return array_slice($lines, -200);
    }
}
