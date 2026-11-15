<?php

/**
 * LibreNMS Backup & Restore - versioned schema installer.
 *
 * Needed because LibreNMS local plugins cannot register migrations.
 *
 * Usage (run as the LibreNMS application user):
 *   php scripts/br-schema.php install
 *   php scripts/br-schema.php status
 *   php scripts/br-schema.php uninstall --force
 *
 * The script boots the LibreNMS Laravel application so it uses the same
 * database connection, then executes plugin/Migrations/*.php in filename
 * order, recording each applied file in `br_settings` (schema_version).
 * It is idempotent: already-applied migrations are skipped.
 */

declare(strict_types=1);

const BR_PLUGIN_DIR = 'app/Plugins/BackupRestore';

$librenmsRoot = getenv('LIBRENMS_ROOT') ?: '/opt/librenms';
if (! is_dir($librenmsRoot)) {
    fwrite(STDERR, "LibreNMS root not found: {$librenmsRoot}\n");
    exit(2);
}

require $librenmsRoot . '/vendor/autoload.php';
$app = require $librenmsRoot . '/bootstrap/app.php';
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();

// Laravel's console exception handler renders a nice error block and then
// exits 0. Every caller here is a shell script that tests the exit status, so
// a fatal error used to look like success ("ok schema applied" printed over a
// broken migration). Force a non-zero status for anything uncaught.
set_exception_handler(function (Throwable $e): void {
    fwrite(STDERR, 'br-schema failed: ' . $e->getMessage() . "\n");
    fwrite(STDERR, '  at ' . basename($e->getFile()) . ':' . $e->getLine() . "\n");
    exit(1);
});

use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

$command = $argv[1] ?? 'status';
$migrationDir = $librenmsRoot . '/' . BR_PLUGIN_DIR . '/Migrations';

function br_setting_table_exists(): bool
{
    return Schema::hasTable('br_settings');
}

function br_get_version(): ?string
{
    if (! br_setting_table_exists()) {
        return null;
    }
    $row = DB::table('br_settings')->where('key', 'schema_version')->first();
    $raw = $row->value ?? null;
    if (! is_string($raw)) {
        return null;
    }
    // Unwrap a double-encoded value written by releases before 1.0.2: the
    // column held a JSON string whose contents were themselves JSON.
    $once = json_decode($raw, true);
    if (is_string($once)) {
        $twice = json_decode($once, true);
        if ($twice !== null) {
            return $once;
        }
    }

    return $raw;
}

/**
 * The applied-migration list, tolerant of every shape this key has held:
 * array, double-encoded JSON string, single name, or absent.
 */
function br_applied_migrations(): array
{
    $raw = br_get_version();
    if ($raw === null || $raw === '') {
        return [];
    }
    $decoded = json_decode($raw, true);
    if (is_array($decoded)) {
        return array_values(array_filter($decoded, 'is_string'));
    }
    if (is_string($decoded) && $decoded !== '') {
        // Still a string after two decodes: treat as a single migration name.
        return [$decoded];
    }

    // Not JSON at all: a bare version string from an early release.
    return [$raw];
}

function br_set_version(string $value): void
{
    DB::table('br_settings')->updateOrInsert(
        ['key' => 'schema_version'],
        ['value' => json_encode($value), 'updated_at' => now()]
    );
}

/**
 * Store the applied-migration list.
 *
 * The list is stored as a JSON array. Older releases accidentally stored it
 * double-encoded (a JSON string containing JSON), which made every second
 * run fatal. Readers therefore accept both shapes.
 */
function br_set_version_list(array $applied): void
{
    DB::table('br_settings')->updateOrInsert(
        ['key' => 'schema_version'],
        ['value' => json_encode(array_values($applied)), 'updated_at' => now()]
    );
}

function br_tables(): array
{
    $tables = [];
    foreach (['br_backups', 'br_jobs', 'br_restores', 'br_checkpoints',
        'br_schedules', 'br_settings', 'br_audit_events'] as $name) {
        $tables[$name] = Schema::hasTable($name);
    }

    return $tables;
}

switch ($command) {
    case 'install':
        echo "LibreNMS Backup & Restore schema installer\n";
        echo 'Database: ' . DB::connection()->getDatabaseName() . "\n";

        if (! is_dir($migrationDir)) {
            fwrite(STDERR, "Migration directory missing: {$migrationDir}\n");
            exit(2);
        }

        $files = glob($migrationDir . '/*.php');
        sort($files);
        $applied = br_applied_migrations();
        $didWork = false;

        foreach ($files as $file) {
            $name = basename($file, '.php');
            if (in_array($name, $applied, true)) {
                echo "  [skip] {$name}\n";
                continue;
            }
            echo "  [apply] {$name}\n";
            $migration = require $file;
            if (! is_object($migration) || ! method_exists($migration, 'up')) {
                fwrite(STDERR, "  migration did not return an object with up(): {$file}\n");
                exit(3);
            }
            // MariaDB performs an implicit COMMIT on DDL, so wrapping schema
            // changes in a transaction aborts with "no active transaction".
            // Migrations are written to be idempotent and are recorded only
            // after they succeed, which gives us the same safety property.
            $migration->up();
            $applied[] = $name;
            // br_set_version() json_encodes its argument, so pass the array.
            // Passing json_encode($applied) stored a JSON *string*, and the next
            // run's json_decode() returned a string, making in_array() fatal:
            //   in_array(): Argument #2 ($haystack) must be of type array
            br_set_version_list($applied);
            $didWork = true;
        }

        // Seed defaults once.
        $defaults = [
            'enabled' => true,
            'require_typed_confirmation' => true,
            'notify_on_failure' => true,
            'confirm_phrase' => 'RESTORE',
            'backup_root' => '/var/backups/librenms',
            'log_dir' => '/var/log/librenms-backup',
            'retention_keep_daily' => 7,
            'retention_keep_weekly' => 4,
            'retention_keep_monthly' => 6,
        ];
        foreach ($defaults as $key => $value) {
            if (! DB::table('br_settings')->where('key', $key)->exists()) {
                DB::table('br_settings')->insert([
                    'key' => $key,
                    'value' => json_encode($value),
                    'updated_at' => now(),
                ]);
            }
        }

        // Activate the plugin record so LibreNMS renders the menu entry.
        DB::table('plugins')->updateOrInsert(
            ['plugin_name' => 'BackupRestore', 'version' => 2],
            ['plugin_active' => 1]
        );

        echo "Schema version: " . ($applied ? implode(', ', $applied) : 'none') . "\n";
        foreach (br_tables() as $table => $exists) {
            printf("  %-16s %s\n", $table, $exists ? 'OK' : 'MISSING');
        }
        // Exit status must reflect the END STATE, not whether work was done.
        // A table that is still missing is a real failure even if this run
        // "did work"; and if every table is present there is nothing to fix.
        $allPresent = ! in_array(false, array_values(br_tables()), true);
        if (! $allPresent) {
            fwrite(STDERR, "One or more br_* tables are missing - install incomplete.\n");
        }
        exit($allPresent ? 0 : 1);

    case 'status':
        echo "Database: " . DB::connection()->getDatabaseName() . "\n";
        $appliedNames = br_applied_migrations();
        echo "Schema version: " . ($appliedNames ? implode(', ', $appliedNames) : 'not installed') . "\n";
        $ok = true;
        foreach (br_tables() as $table => $exists) {
            printf("  %-16s %s\n", $table, $exists ? 'OK' : 'MISSING');
            $ok = $ok && $exists;
        }
        echo "Plugin row: " . (DB::table('plugins')->where('plugin_name', 'BackupRestore')->exists()
            ? 'present' : 'absent') . "\n";
        exit($ok ? 0 : 1);

    case 'uninstall':
        $force = in_array('--force', $argv, true);
        echo "This drops ONLY the plugin's own br_* tables.\n";
        echo "LibreNMS core tables (devices, ports, users, ...) are never touched.\n";
        if (! $force) {
            echo "Re-run with --force to proceed.\n";
            exit(1);
        }
        foreach (['br_audit_events', 'br_settings', 'br_schedules', 'br_checkpoints',
            'br_restores', 'br_jobs', 'br_backups'] as $table) {
            Schema::dropIfExists($table);
            echo "  dropped {$table}\n";
        }
        DB::table('plugins')->where('plugin_name', 'BackupRestore')->delete();
        echo "  removed plugin row\n";
        exit(0);

    default:
        fwrite(STDERR, "Usage: br-schema.php install|status|uninstall [--force]\n");
        exit(2);
}
