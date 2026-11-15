<?php

/**
 * LibreNMS Backup & Restore - single page hook.
 *
 * Verified against LibreNMS master (2026-11-11):
 *   app/Plugins/Hooks/PageHook.php            -> SinglePageHook
 *   app/Http/Controllers/PluginPageController  -> GET plugin/{plugin_name}
 *
 * IMPORTANT ARCHITECTURAL NOTE
 * ---------------------------
 * LibreNMS local (version 2) plugins cannot register routes, controllers or
 * migrations. `PluginProvider::loadLocalPlugins()` only globs
 * `app/Plugins/* /*.php` for hook classes, and the only HTTP surface is the
 * generic `plugin/{plugin_name}` route. Therefore:
 *
 *   * This hook renders the read-only dashboard shell (server-rendered PHP).
 *   * Every state-changing or data-bearing operation is performed by
 *     Controllers\BackupRestoreController, reached through the dedicated,
 *     explicitly registered entrypoint `html/plugin-backuprestore.php`
 *     (installed by scripts/install.sh). That entrypoint boots the LibreNMS
 *     Laravel application and attaches our route table - the same pattern
 *     LibreNMS core uses for html/api_v0.php - so no core file is modified.
 */

namespace App\Plugins\BackupRestore;

use App\Plugins\Hooks\PageHook;
use Illuminate\Contracts\Auth\Authenticatable;

class Page extends PageHook
{
    public function authorize(Authenticatable $user): bool
    {
        // Only LibreNMS administrators may see backup/restore data.
        return $user->can('admin');
    }

    public function data(): array
    {
        $userId = auth()->id();

        return [
            'brBootstrap' => \App\Plugins\BackupRestore\Services\ApiKernel::bootstrapPayload($userId),
            'brUser' => auth()->user()->username,
            'brAssets' => [
                'css' => url('plugin-backuprestore.php') . '?r=assets/backup-restore.css',
                'js' => url('plugin-backuprestore.php') . '?r=assets/backup-restore.js',
            ],
        ];
    }
}
