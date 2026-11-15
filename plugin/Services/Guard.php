<?php

namespace App\Plugins\BackupRestore\Services;

use App\Plugins\BackupRestore\Models\AuditEvent;
use Illuminate\Http\Request;

/**
 * Central authorisation + audit gate for every plugin request.
 *
 * Rules (defence in depth - each one independently sufficient):
 *   1. Authenticated session (web guard).
 *   2. `admin` gate  (AppServiceProvider:315 -> hasRole('admin')).
 *   3. CSRF token on all non-GET verbs (Laravel VerifyCsrfToken).
 *   4. Named ability for the specific action (view|operate|restore|configure).
 */
class Guard
{
    public const ABILITIES = ['view', 'operate', 'restore', 'configure'];

    public static function check(Request $request, string $ability): void
    {
        if (! in_array($ability, self::ABILITIES, true)) {
            abort(500, 'unknown ability');
        }

        $user = $request->user();
        if (! $user || ! auth()->check()) {
            abort(401, 'authentication required');
        }

        if (! $user->can('admin')) {
            AuditEvent::record('access.denied', [
                'actor' => $user->username ?? 'unknown',
                'detail' => "ability=$ability path=" . $request->path(),
                'ip' => $request->ip(),
                'result' => 'denied',
            ]);
            abort(403, 'administrator role required');
        }

        if ($request->method() !== 'GET' && ! $request->isMethodSafe()) {
            $token = $request->input('_token') ?: $request->header('X-CSRF-TOKEN');
            if (! is_string($token) || ! hash_equals(csrf_token(), $token)) {
                abort(419, 'CSRF token mismatch');
            }
        }
    }

    public static function assertAdmin(): void
    {
        if (! auth()->check() || ! auth()->user()->can('admin')) {
            abort(403, 'administrator role required');
        }
    }
}
