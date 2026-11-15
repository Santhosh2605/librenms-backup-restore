<?php

namespace App\Plugins\BackupRestore\Services;

/**
 * Thin accessor for this plugin's LibreNMS settings.
 *
 * The `plugins` table stores settings as JSON (App\Models\Plugin casts
 * `settings` to array). We cache per request to avoid repeated queries during
 * menu composition, which runs on every page render.
 */
class Settings
{
    private const CONF_PATH = '/etc/librenms-backup/br.conf';

    private static ?array $cache = null;

    public static function all(): array
    {
        if (self::$cache === null) {
            try {
                $row = \App\Models\Plugin::where('plugin_name', 'BackupRestore')->first();
                self::$cache = is_array($row?->settings) ? $row->settings : [];
            } catch (\Throwable $e) {
                // DB not migrated / offline: fail closed to defaults.
                self::$cache = [];
            }
        }

        return self::$cache;
    }

    public static function get(string $key, mixed $default = null): mixed
    {
        return self::all()[$key] ?? $default;
    }

    public static function set(array $values): void
    {
        $plugin = \App\Models\Plugin::firstOrCreate(
            ['plugin_name' => 'BackupRestore', 'version' => 2],
            ['plugin_active' => 1]
        );
        $plugin->settings = array_merge(self::all(), $values);
        $plugin->save();
        self::$cache = $plugin->settings;
    }

    public static function confPath(): string
    {
        return getenv('BR_CONF') ?: self::CONF_PATH;
    }
}
