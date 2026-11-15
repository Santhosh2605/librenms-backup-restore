<?php

/**
 * LibreNMS Backup & Restore - plugin menu entry.
 *
 * Verified against LibreNMS master (2026-11-11, commit dd4cebd7):
 *   app/Plugins/Hooks/MenuEntryHook.php  -> entry in the "Plugins" menu
 *   app/Http/ViewComposers/MenuComposer  -> $menu_hooks rendered in layouts/menu
 *
 * LibreNMS has no hook that injects items into the *Administration* menu, so
 * the entry lives under "Plugins" and the page itself is titled
 * "Administration > Backup & Restore".
 */

namespace App\Plugins\BackupRestore;

use App\Plugins\Hooks\MenuEntryHook;
use Illuminate\Contracts\Auth\Authenticatable;

class Menu extends MenuEntryHook
{
    /**
     * Show to admins and global-read users; the page itself requires 'admin'.
     */
    public function authorize(Authenticatable $user, array $settings = []): bool
    {
        return $user->can('global-read');
    }

    public function data(array $settings = []): array
    {
        return [
            'enabled' => (bool) \App\Plugins\BackupRestore\Services\Settings::get('enabled', true),
            'lastStatus' => \App\Plugins\BackupRestore\Services\Settings::get('last_backup_status', 'unknown'),
        ];
    }
}
