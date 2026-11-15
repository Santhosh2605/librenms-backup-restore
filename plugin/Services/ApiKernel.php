<?php

namespace App\Plugins\BackupRestore\Services;

use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use App\Plugins\BackupRestore\Models\AuditEvent;

/**
 * Builds the payload the Blade shell receives.
 *
 * SECURITY: this payload contains NO secrets. Credentials never leave the
 * server: the browser only ever sees job ids, statuses and byte counts.
 */
class ApiKernel
{
    public static function bootstrapPayload(?int $userId): array
    {
        $role = 'guest';
        if ($userId) {
            $user = \App\Models\User::find($userId);
            $role = $user?->can('admin') ? 'admin' : 'read';
        }

        return [
            'version' => (string) \App\Plugins\BackupRestore\Services\Version::VERSION,
            'user' => auth()->user()->username ?? null,
            'role' => $role,
            'csrf' => csrf_token(),
            // Absolute URL of the front controller's API namespace. Derived
            // from the request so it is correct behind a reverse proxy or in a
            // sub-directory install; falls back to the canonical path.
            // Transport: ?r=<resource>. Works behind Apache, nginx+FastCGI and
            // PHP's built-in server alike (see front-controller notes).
            'endpoint' => url((request()->getBasePath() ?: '') . '/plugin-backuprestore.php'),
            'routes' => [
                'dashboard' => 'status',
                'backups' => 'backups',
                'restores' => 'restores',
                'schedules' => 'schedules',
                'settings' => 'settings',
                'audit' => 'audit',
                'worker' => 'worker/health',
            ],
            'pollIntervalMs' => 3000,
        ];
    }

    /**
     * Uniform JSON envelope so the JS client has one error path.
     */
    public static function ok(array $data = [], int $status = 200): JsonResponse
    {
        return response()->json(['status' => 'ok', 'data' => $data], $status);
    }

    public static function error(string $message, int $status = 400, array $extra = []): JsonResponse
    {
        return response()->json(['status' => 'error', 'message' => $message] + $extra, $status);
    }

    public static function audit(Request $request, string $action, array $detail = [], string $result = 'ok'): void
    {
        AuditEvent::record($action, [
            'actor' => $request->user()->username ?? 'system',
            'detail' => json_encode($detail, JSON_UNESCAPED_SLASHES),
            'ip' => $request->ip(),
            'result' => $result,
        ]);
    }
}
