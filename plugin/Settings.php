<?php

/**
 * LibreNMS Backup & Restore - plugin settings hook.
 *
 * LibreNMS renders this at Administration > Plugins > BackupRestore > Settings
 * (PluginSettingsController). We keep it deliberately minimal: it only toggles
 * the plugin and mirrors the security-relevant operational settings from
 * /etc/librenms-backup/br.conf, which is root-owned and never written by PHP.
 */

namespace App\Plugins\BackupRestore;

use App\Plugins\Hooks\SettingsHook;

class Settings extends SettingsHook
{
    public function data(array $settings = []): array
    {
        return [
            'settings' => $settings,
            'effective' => [
                'enabled' => \App\Plugins\BackupRestore\Services\Settings::get('enabled', true),
                'require_typed_confirmation' => \App\Plugins\BackupRestore\Services\Settings::get('require_typed_confirmation', true),
                'retention_keep_daily' => \App\Plugins\BackupRestore\Services\Settings::get('retention_keep_daily', 7),
                'notify_on_failure' => \App\Plugins\BackupRestore\Services\Settings::get('notify_on_failure', true),
            ],
            'confPath' => \App\Plugins\BackupRestore\Services\Settings::confPath(),
            'confReadable' => is_readable(\App\Plugins\BackupRestore\Services\Settings::confPath()),
        ];
    }
}
