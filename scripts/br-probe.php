<?php

/**
 * LibreNMS Backup & Restore - web-tier probe.
 *
 * The CLI tells you nothing about the web tier: different user, different
 * memory_limit, different extensions, different php.ini. This file boots NO
 * framework at all - it is plain PHP - so it cannot produce a 500 of its own.
 * Whatever it prints is the environment the plugin actually runs in.
 *
 * INSTALL (temporary - remove it afterwards):
 *
 *   sudo install -o librenms -g librenms -m 0644 br-probe.php \
 *        /opt/librenms/html/br-probe.php
 *   curl -s http://127.0.0.1/br-probe.php
 *   sudo rm -f /opt/librenms/html/br-probe.php
 *
 * It prints no secrets: the database password is never shown, only whether the
 * connection succeeded.
 */

header('Content-Type: text/plain; charset=utf-8');

function line(string $label, string $value): void
{
    printf("%-28s %s\n", $label . ':', $value);
}

function whoamiName(): string
{
    if (function_exists('posix_geteuid') && function_exists('posix_getpwuid')) {
        $pw = posix_getpwuid(posix_geteuid());

        return $pw['name'] ?? (string) posix_geteuid();
    }

    return getenv('USER') ?: 'unknown';
}

echo "== PHP as the WEB tier sees it ==\n";
line('sapi', PHP_SAPI);
line('php version', PHP_VERSION);
line('running as', whoamiName());
line('process user id', (string) getmyuid());
line('memory_limit', ini_get('memory_limit') === false ? '(none)' : (string) ini_get('memory_limit'));
line('memory usage', round(memory_get_usage(true) / 1048576, 1) . ' MiB');
line('max_execution_time', (string) ini_get('max_execution_time'));
line('display_errors', (string) ini_get('display_errors'));
line('error_reporting', (string) ini_get('error_reporting'));

echo "\n== Extensions the plugin needs ==\n";
foreach (['pdo', 'pdo_mysql', 'mysqli', 'mbstring', 'json', 'openssl', 'ctype', 'tokenizer', 'xml', 'dom', 'curl', 'opcache', 'posix'] as $ext) {
    line($ext, extension_loaded($ext) ? 'loaded' : 'MISSING');
}

echo "\n== OPcache (per-SAPI; CLI values mean nothing here) ==\n";
line('opcache.enable', (string) ini_get('opcache.enable'));
line('validate_timestamps', (string) ini_get('opcache.validate_timestamps'));
line('revalidate_freq', (string) ini_get('opcache.revalidate_freq'));

echo "\n== Server identity ==\n";
foreach (['SERVER_SOFTWARE', 'SCRIPT_FILENAME', 'DOCUMENT_ROOT', 'REQUEST_URI', 'SERVER_NAME'] as $k) {
    line($k, (string) ($_SERVER[$k] ?? '-'));
}

echo "\n== The file we are executing ==\n";
line('this file', __FILE__);
line('owner', (function () {
    $st = @stat(__FILE__);
    if (! $st || ! function_exists('posix_getpwuid')) {
        return '-';
    }
    $pw = posix_getpwuid($st['uid']);

    return ($pw['name'] ?? $st['uid']) . ' mode ' . decoct($st['mode'] & 0777);
})());

$root = getenv('LIBRENMS_ROOT') ?: dirname(__DIR__);
echo "\n== LibreNMS tree (root: {$root}) ==\n";

$checks = [
    $root . '/.env'                                => 'database credentials',
    $root . '/vendor/autoload.php'                 => 'composer autoloader',
    $root . '/bootstrap/app.php'                   => 'framework bootstrap',
    $root . '/app/Plugins/BackupRestore/Menu.php'  => 'plugin Menu hook',
    $root . '/app/Plugins/BackupRestore/Controllers/BackupRestoreController.php' => 'plugin controller',
    $root . '/html/plugin-backuprestore.php'       => 'front controller',
    $root . '/logs'                                => 'exception log dir (needs WRITE)',
    $root . '/storage/framework/views'             => 'blade cache (needs WRITE)',
    '/etc/librenms-backup/br.conf'                 => 'plugin settings',
    '/var/lib/librenms-backup/health.json'         => 'worker health file',
    '/var/log/librenms-backup'                     => 'job log dir',
];
foreach ($checks as $path => $why) {
    $exists = file_exists($path);
    $read = $exists && is_readable($path);
    $write = $exists && is_writable($path);
    $needsWrite = str_contains($why, 'WRITE');
    $ok = $read && (! $needsWrite || $write);
    printf("%-28s %s  %s%s\n",
        '  ' . basename($path),
        $ok ? 'OK  ' : 'FAIL',
        $exists ? ($read ? 'readable' : 'NOT readable') . ($needsWrite ? ($write ? ', writable' : ', NOT writable') : '') . '  ' : 'MISSING  ',
        $why
    );
}

echo "\n== Database (credentials read directly, password never printed) ==\n";
$env = @file_get_contents($root . '/.env');
if ($env === false) {
    line('result', 'CANNOT READ .env - this alone causes a 500 on every page');
} else {
    $get = function (string $key) use ($env): string {
        if (preg_match('/^' . preg_quote($key, '/') . '\s*=\s*(.*)$/m', $env, $m)) {
            return trim($m[1], " \t\"'");
        }

        return '';
    };
    $host = $get('DB_HOST') ?: 'localhost';
    $port = $get('DB_PORT') ?: '3306';
    $name = $get('DB_DATABASE') ?: 'librenms';
    $user = $get('DB_USERNAME') ?: 'librenms';
    $pass = $get('DB_PASSWORD');

    line('host / database', $host . ':' . $port . ' / ' . $name);
    line('username', $user);
    line('password', $pass === '' ? '(empty)' : '(set, ' . strlen($pass) . ' chars)');

    if (! extension_loaded('pdo_mysql')) {
        line('result', 'pdo_mysql MISSING - cannot connect');
    } else {
        try {
            $dsn = $host === 'localhost' || str_starts_with($host, '/')
                ? 'mysql:unix_socket=' . ($host === 'localhost' ? '/run/mysqld/mysqld.sock' : $host) . ';dbname=' . $name
                : 'mysql:host=' . $host . ';port=' . $port . ';dbname=' . $name;
            $pdo = new PDO($dsn, $user, $pass, [PDO::ATTR_TIMEOUT => 5]);
            $count = $pdo->query('SELECT COUNT(*) FROM br_backups')->fetchColumn();
            line('result', 'CONNECTED - br_backups has ' . $count . ' row(s)');
        } catch (Throwable $e) {
            line('result', 'FAILED: ' . $e->getMessage());
        }
    }
}

echo "\n== Whether the framework can be booted here ==\n";
$autoload = $root . '/vendor/autoload.php';
if (! is_readable($autoload)) {
    line('result', 'autoload.php not readable - cannot test');
} else {
    // Capture any fatal so this page still returns 200 and tells us what broke.
    register_shutdown_function(function (): void {
        $err = error_get_last();
        if ($err !== null && in_array($err['type'], [E_ERROR, E_PARSE, E_CORE_ERROR, E_COMPILE_ERROR], true)) {
            echo "\n!! FATAL: " . $err['message'] . "\n   at " . $err['file'] . ':' . $err['line'] . "\n";
        }
    });
    try {
        require $autoload;
        $app = require $root . '/bootstrap/app.php';
        $app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
        $n = Illuminate\Support\Facades\DB::table('br_backups')->count();
        line('result', 'framework booted, br_backups visible: ' . $n . ' row(s)');
        $cls = 'App\Plugins\BackupRestore\Controllers\BackupRestoreController';
        line('plugin controller', class_exists($cls) ? 'autoloadable' : 'NOT autoloadable');
    } catch (Throwable $e) {
        line('result', 'EXCEPTION: ' . get_class($e) . ': ' . $e->getMessage());
        line('at', basename($e->getFile()) . ':' . $e->getLine());
    }
}

echo "\n== Done. Delete this file. ==\n";
