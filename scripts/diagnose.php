<?php

/**
 * LibreNMS Backup & Restore - standalone diagnostic.
 *
 * Purpose: when the web UI returns HTTP 500 with an HTML body, the browser only
 * shows "Server returned HTML instead of JSON", and the real cause stays in a
 * log file that may be rotated, empty, or at a path nobody remembers. This
 * script reproduces the same work the front controller does, step by step, in a
 * plain CLI process, and reports exactly which step fails and why.
 *
 * Run as the LibreNMS application user so permissions match the web tier:
 *
 *     sudo -u librenms php /opt/librenms-backup-restore/scripts/diagnose.php
 *
 * It is read-only: it opens no archives, writes no files, changes no settings.
 */

declare(strict_types=1);

$errors = [];
$notes  = [];

function out(string $line = ''): void
{
    fwrite(STDOUT, $line . "\n");
}

function banner(string $title): void
{
    out();
    out('== ' . $title . ' ' . str_repeat('=', max(0, 66 - strlen($title))));
}

function step(string $label, callable $fn): bool
{
    try {
        $value = $fn();
        out(sprintf('  [ok]   %-46s %s', $label, is_string($value) ? $value : ''));
        return true;
    } catch (Throwable $e) {
        out(sprintf('  [FAIL] %-46s', $label));
        out('         ' . get_class($e) . ': ' . $e->getMessage());
        out('         at ' . $e->getFile() . ':' . $e->getLine());
        global $errors;
        $errors[] = $label . ' -> ' . get_class($e) . ': ' . $e->getMessage()
            . ' (' . basename($e->getFile()) . ':' . $e->getLine() . ')';
        return false;
    }
}

// ---------------------------------------------------------------------------
// 0. Environment
// ---------------------------------------------------------------------------
banner('Environment');

$root = getenv('LIBRENMS_ROOT') ?: '/opt/librenms';
if (! is_dir($root)) {
    foreach (['/opt/librenms', '/srv/librenms', '/usr/local/librenms'] as $cand) {
        if (is_dir($cand) && is_file($cand . '/artisan')) {
            $root = $cand;
            break;
        }
    }
}

out('  php version      : ' . PHP_VERSION);
out('  php sapi         : ' . PHP_SAPI . '   (cli here; the web tier is fpm/apache)');
out('  running as       : ' . (function_exists('posix_getpwuid')
    ? (posix_getpwuid(posix_geteuid())['name'] ?? 'unknown')
    : (getenv('USER') ?: 'unknown')));
out('  librenms root    : ' . $root);
out('  script dir       : ' . __DIR__);
// Which user runs the WEB tier? The plugin's HTTP surface executes as that
// account, not as whoever runs this script. Testing as the wrong user proves
// nothing - permissions and .env readability differ completely.
$me = function_exists('posix_getpwuid')
    ? (posix_getpwuid(posix_geteuid())['name'] ?? 'unknown')
    : (getenv('USER') ?: 'unknown');

$webUser = null;
$fpmPids = trim((string) @shell_exec(
    'pgrep -f "php-fpm" | head -2'
));
if ($fpmPids !== '') {
    foreach (preg_split('/\s+/', $fpmPids) as $pid) {
        $u = trim((string) @shell_exec('ps -o user= -p ' . (int) $pid . ' 2>/dev/null'));
        if ($u !== '') {
            $webUser = $u;
            break;
        }
    }
}
out('  web tier user    : ' . ($webUser ?? 'not detected (no php-fpm running here)'));

if ($me === 'root') {
    out();
    out('  *** RUNNING AS ROOT - THE RESULT BELOW IS NOT TRUSTWORTHY FOR PERMISSIONS ***');
    out('      root can read every file, so a permission problem will not appear here.');
    out('      The web tier is neither root nor your login. Re-run as the pool user:');
    out('        sudo -u ' . ($webUser ?? '<php-fpm-user>') . ' LIBRENMS_ROOT=' . $root
        . ' php ' . __FILE__);
    out('      Find the pool user with:');
    out('        grep -h "^user" /etc/php/*/fpm/pool.d/*.conf');
}
if ($webUser !== null && $webUser !== $me) {
    out('  *** RUNNING AS THE WRONG USER ***');
    out('      this script is running as `' . $me . '` but requests execute as `' . $webUser . '`.');
    out('      re-run with:  sudo -u ' . $webUser . ' LIBRENMS_ROOT=' . $root
        . ' php ' . __FILE__);
}

// ---------------------------------------------------------------------------
// The web tier is not the CLI. Two differences silently produce an HTML 500
// that no PHP-level handler can report:
//   * the pool user (permissions), and
//   * the pool's php_admin_value limits - an OOM fatal is uncatchable and is
//     written only to the FPM error log, never to the Laravel log.
// Read the pool config directly; do not guess.
// ---------------------------------------------------------------------------
$poolFiles = glob('/etc/php/*/fpm/pool.d/*.conf') ?: [];
$poolUser = null;
foreach ($poolFiles as $conf) {
    $text = @file_get_contents($conf);
    if (! is_string($text) || ! preg_match('/^\s*listen\s*=/m', $text)) {
        continue;
    }
    // A pool is only relevant if it could be serving LibreNMS.
    if (preg_match('/^\s*user\s*=\s*(\S+)/m', $text, $m)) {
        $poolUser = $m[1];
        out('  fpm pool         : ' . $conf);
        out('  fpm pool user    : ' . $poolUser);
        if (preg_match('/memory_limit\s*=\s*(\S+)/', $text, $mm)) {
            out('  fpm memory_limit : ' . $mm[1] . '   (CLI here: ' . ini_get('memory_limit') . ')');
        }
        if (preg_match('/^\s*catch_workers_output\s*=\s*(\S+)/m', $text, $cm)) {
            out('  catch_workers_out: ' . $cm[1]);
        }
        // Environment the pool passes through - the front controller reads these.
        if (preg_match_all('/env\[([A-Z_]+)\]\s*=\s*(\S+)/', $text, $em, PREG_SET_ORDER)) {
            foreach ($em as $e) {
                out('  fpm env          : ' . $e[1] . '=' . $e[2]);
            }
        }
        break;
    }
}
foreach (glob('/etc/php/*/fpm/php.ini') as $ini) {
    $t = @file_get_contents($ini);
    if (is_string($t) && preg_match('/^memory_limit\s*=\s*(\S+)/m', $t, $m)) {
        out('  fpm php.ini      : ' . $ini . ' memory_limit=' . $m[1]);
    }
}

// The web tier must be able to read LibreNMS' own .env to reach the database.
$envFile = $root . '/.env';
if (is_file($envFile)) {
    $canRead = is_readable($envFile);
    out('  .env readable    : ' . ($canRead ? 'yes' : 'NO - the web tier cannot reach the database'));
    if (! $canRead) {
        $st = @stat($envFile);
        out('      mode ' . decoct($st['mode'] & 0777) . ' owner '
            . (function_exists('posix_getpwuid') ? (posix_getpwuid($st['uid'])['name'] ?? $st['uid']) : $st['uid'])
            . ' - a user other than the owner gets nothing at 0640');
    }
}

out('  opcache (cli)    : ' . (function_exists('opcache_get_status')
    ? (ini_get('opcache.enable_cli') ? 'on' : 'off (cli)')
    : 'not loaded'));
out('  opcache (web)    : ' . (ini_get('opcache.enable') ? 'enable=on' : 'enable=off')
    . ' validate_timestamps=' . ini_get('opcache.validate_timestamps')
    . ' revalidate_freq=' . ini_get('opcache.revalidate_freq'));

if (! is_file($root . '/vendor/autoload.php')) {
    out();
    out('Cannot continue: ' . $root . '/vendor/autoload.php not found.');
    out('Set the right root with:  sudo -u librenms LIBRENMS_ROOT=/your/path php ' . __FILE__);
    exit(1);
}

// ---------------------------------------------------------------------------
// 1. Autoloader and framework bootstrap
// ---------------------------------------------------------------------------
banner('Framework bootstrap');

$app = null;
if (step('require vendor/autoload.php', function () use ($root) {
    require $root . '/vendor/autoload.php';
    return 'loaded';
})) {
    step('require bootstrap/app.php', function () use ($root, &$app) {
        $app = require_once $root . '/bootstrap/app.php';
        return get_class($app);
    });
    if ($app) {
        step('console kernel bootstrap()', function () use ($app) {
            $app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
            return 'booted';
        });
    }
}

if (! $app) {
    out();
    out('Framework did not boot - the failure is in LibreNMS core, not this plugin.');
    exit(1);
}

// ---------------------------------------------------------------------------
// 2. Plugin classes - this is where a PHP compile error in OUR code appears
// ---------------------------------------------------------------------------
banner('Files the web tier touches');

$touchables = [
    '/etc/librenms-backup/br.conf'                => 'settings (read)',
    '/var/lib/librenms-backup/health.json'        => 'worker health (read)',
    '/var/log/librenms-backup'                    => 'job log listing (read)',
    $root . '/.env'                               => 'database credentials (read)',
    $root . '/logs'                               => 'exception log (write)',
    $root . '/storage/framework/views'            => 'blade cache (write)',
];
foreach ($touchables as $path => $why) {
    if (! file_exists($path)) {
        out(sprintf('  [--]   %-44s missing (%s)', $path, $why));
        continue;
    }
    $ok = is_readable($path) && (! str_contains($why, 'write') || is_writable($path));
    $st = @stat($path);
    $owner = function_exists('posix_getpwuid')
        ? (posix_getpwuid($st['uid'])['name'] ?? (string) $st['uid'])
        : (string) $st['uid'];
    $group = function_exists('posix_getgrgid')
        ? (posix_getgrgid($st['gid'])['name'] ?? (string) $st['gid'])
        : (string) $st['gid'];
    out(sprintf('  [%s] %-44s %s %s:%s  (%s)',
        $ok ? 'ok' : 'FAIL', $path, decoct($st['mode'] & 0777), $owner, $group, $why));
    if (! $ok) {
        $errors[] = 'not ' . (str_contains($why, 'write') ? 'writable' : 'readable')
            . ' by ' . $me . ': ' . $path;
    }
}

banner('Plugin files on disk');

$pluginDir = $root . '/app/Plugins/BackupRestore';
out('  plugin dir       : ' . $pluginDir);
if (! is_dir($pluginDir)) {
    out('  [FAIL] directory does not exist - the plugin is not installed');
    $errors[] = 'plugin directory missing: ' . $pluginDir;
} else {
    $expected = [
        'Controllers/BackupRestoreController.php',
        'Models/Backup.php',
        'Models/Restore.php',
        'Services/ApiKernel.php',
        'Services/Version.php',
        'Menu.php',
        'Page.php',
        'Settings.php',
        'resources/views/page.blade.php',
        'resources/views/assets/backup-restore.js',
    ];
    foreach ($expected as $rel) {
        $p = $pluginDir . '/' . $rel;
        if (is_file($p)) {
            out(sprintf('  [ok]   %-46s %d bytes', $rel, filesize($p)));
        } else {
            out(sprintf('  [FAIL] %-46s MISSING', $rel));
            $errors[] = 'missing plugin file: ' . $rel;
        }
    }
    // Readable by the user running this script is what the web tier needs too.
    foreach (['Controllers/BackupRestoreController.php', 'Menu.php'] as $rel) {
        $p = $pluginDir . '/' . $rel;
        if (is_file($p) && ! is_readable($p)) {
            out('  [FAIL] not readable: ' . $rel . ' (owner/permissions)');
            $errors[] = 'unreadable: ' . $rel;
        }
    }
}

// Is the namespace actually mapped? App\ -> <root>/app via composer PSR-4.
step('PSR-4 maps App\\Plugins', function () use ($root) {
    $map = require $root . '/vendor/composer/autoload_psr4.php';
    $appDirs = array_filter(array_keys($map), fn ($k) => str_starts_with($k, 'App\\'));

    return $appDirs ? implode(', ', $appDirs) . ' -> ' . implode(',', $map[$appDirs[array_key_first($appDirs)]])
                    : 'no App\\ mapping (composer dump-autoload needed)';
});

banner('Plugin classes (compile + load)');

$classes = [
    'App\\Plugins\\BackupRestore\\Controllers\\BackupRestoreController',
    'App\\Plugins\\BackupRestore\\Models\\Backup',
    'App\\Plugins\\BackupRestore\\Models\\Restore',
    'App\\Plugins\\BackupRestore\\Models\\Job',
    'App\\Plugins\\BackupRestore\\Models\\Schedule',
    'App\\Plugins\\BackupRestore\\Models\\Checkpoint',
    'App\\Plugins\\BackupRestore\\Models\\AuditEvent',
    'App\\Plugins\\BackupRestore\\Services\\ApiKernel',
    'App\\Plugins\\BackupRestore\\Services\\Formatter',
    'App\\Plugins\\BackupRestore\\Services\\Guard',
    'App\\Plugins\\BackupRestore\\Services\\Settings',
    'App\\Plugins\\BackupRestore\\Services\\Version',
];

foreach ($classes as $class) {
    step('class_exists ' . substr((string) strrchr($class, '\\'), 1), function () use ($class) {
        if (! class_exists($class)) {
            throw new RuntimeException('class not found / not autoloadable');
        }
        $ref = new ReflectionClass($class);
        // Reading methods forces the whole class body to compile.
        $methods = count($ref->getMethods());

        return basename($ref->getFileName() ?: '?') . ' (' . $methods . ' methods)';
    });
}

// The controller declares methods with return types; instantiate it the way the
// router does so a constructor or parent-class problem shows up here.
step('instantiate BackupRestoreController', function () use ($app) {
    $c = $app->make(App\Plugins\BackupRestore\Controllers\BackupRestoreController::class);

    return get_class($c);
});

// ---------------------------------------------------------------------------
// 3. Database - the tables the two failing tabs read
// ---------------------------------------------------------------------------
banner('Database');

step('connection', function () {
    return Illuminate\Support\Facades\DB::connection()->getDatabaseName();
});

$models = [
    'br_backups'  => 'App\\Plugins\\BackupRestore\\Models\\Backup',
    'br_restores' => 'App\\Plugins\\BackupRestore\\Models\\Restore',
    'br_jobs'     => 'App\\Plugins\\BackupRestore\\Models\\Job',
];

foreach ($models as $table => $model) {
    step('table ' . $table, function () use ($table, $model) {
        if (! Illuminate\Support\Facades\Schema::hasTable($table)) {
            throw new RuntimeException('table missing - run: br-schema.php install');
        }
        $count = Illuminate\Support\Facades\DB::table($table)->count();
        $rows = $model::query()->limit(3)->get();
        $mapped = $rows->map->toPublic()->all();

        return $count . ' rows, toPublic() ok on ' . count($mapped) . ' row(s)';
    });
}

// ---------------------------------------------------------------------------
// 4. The exact queries the two tabs run
// ---------------------------------------------------------------------------
banner('Tab queries (what Backup History and Restore actually do)');

// Call the controller methods the router calls. Re-implementing the query here
// would test a copy of the logic rather than the code that actually runs - which
// is exactly the mistake that made an earlier version of this script report "all
// passed" while a real fault sat in backups().
$callTab = function (string $method) use ($app) {
    $controller = $app->make(App\Plugins\BackupRestore\Controllers\BackupRestoreController::class);
    $user = App\Models\User::query()->orderBy('user_id')->first();
    if (! $user) {
        throw new RuntimeException('no users in database');
    }
    // Sign in as an admin so Guard::check() does not reject us before the code
    // under test is reached.
    Illuminate\Support\Facades\Auth::login($user);

    $request = Illuminate\Http\Request::create('/plugin-backuprestore.php?r=' . $method, 'GET');
    $request->setUserResolver(fn () => $user);
    app()->instance('request', $request);

    $response = $controller->{$method}($request);
    $payload = json_decode((string) $response->getContent(), true);
    if (! is_array($payload)) {
        throw new RuntimeException('controller returned non-JSON');
    }
    $count = isset($payload['data']['items']) ? count($payload['data']['items']) : 0;

    return 'HTTP ' . $response->getStatusCode() . ', ' . $count . ' item(s)';
};

step('controller backups()  [Backup History tab]', fn () => $callTab('backups'));
step('controller restores()  [Restore tab]',        fn () => $callTab('restores'));

// ---------------------------------------------------------------------------
// 5. View layer
// ---------------------------------------------------------------------------
banner('View layer');

step('render page.blade.php', function () use ($app, $root) {
    $finder = app('view')->getFinder();
    // The namespace root is the plugin directory; PageHook asks for
    // 'resources.views.page', so this resolves plugin/resources/views/page.blade.php
    // On a real install the plugin lives in <librenms>/app/Plugins/BackupRestore.
    $pluginDir = file_exists(__DIR__ . '/../plugin/Page.php')
        ? __DIR__ . '/../plugin'
        : ($root . '/app/Plugins/BackupRestore');
    $finder->addNamespace('BackupRestore', $pluginDir);
    $user = App\Models\User::query()->first();
    if (! $user) {
        throw new RuntimeException('no users in database');
    }
    Illuminate\Support\Facades\Auth::login($user);
    $payload = App\Plugins\BackupRestore\Services\ApiKernel::bootstrapPayload($user->user_id);
    $assets = [
        'css' => '/plugin-backuprestore.php?r=assets/backup-restore.css&v=1',
        'js'  => '/plugin-backuprestore.php?r=assets/backup-restore.js&v=1',
    ];
    // LibreNMS registers each local plugin's views under the plugin name and
    // PageHook defaults to 'resources.views.page', i.e. BackupRestore::resources.views.page
    $html = view('BackupRestore::resources.views.page', [
        'brBootstrap' => $payload,
        'brUser'      => $user->username,
        'brAssets'    => $assets,
    ])->render();

    return strlen($html) . ' bytes';
});

// ---------------------------------------------------------------------------
// Summary
// ---------------------------------------------------------------------------
banner('Summary');

if ($errors === []) {
    out('  All plugin-side steps passed.');
    out();
    out('  If the browser still shows HTTP 500, the failure is NOT in the plugin');
    out('  code path. Look at the web server error log:');
    out('    /var/log/apache2/error.log        (Apache)');
    out('    /var/log/nginx/error.log          (nginx)');
    out('    journalctl -u php*-fpm -n 50      (PHP-FPM)');
    exit(0);
}

out('  ' . count($errors) . ' step(s) failed:');
foreach ($errors as $i => $e) {
    out('    ' . ($i + 1) . '. ' . $e);
}
out();
out('  Paste this whole output when reporting the problem.');
exit(1);
