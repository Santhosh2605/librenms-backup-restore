<?php

namespace App\Plugins\BackupRestore\Services;

/**
 * Plugin version metadata.
 *
 * NOTE: this class lives in Services/ on purpose. LibreNMS
 * (App\Providers\PluginProvider::loadLocalPlugins) globs
 * ``app/Plugins/<Plugin>/<Hook>.php`` and treats EVERY file it finds as a hook class;
 * a helper class at the top level raises PluginDoesNotImplementHookException
 * on every page load. Only real hook classes may sit in the plugin root.
 */
final class Version
{
    public const VERSION = '1.0.1';
    public const NAME = 'BackupRestore';
    public const TITLE = 'Backup & Restore';

    /** Database tables created by Migrations/Schema (prefix applied by installer). */
    public const TABLES = [
        'br_backups',
        'br_jobs',
        'br_schedules',
        'br_restores',
        'br_audit_events',
        'br_settings',
        'br_checkpoints',
    ];
}
