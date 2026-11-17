<?php

/**
 * LibreNMS Backup & Restore - HTTP front controller.
 *
 * INSTALLED AS: /opt/librenms/html/plugin-backuprestore.php
 *
 * ---------------------------------------------------------------------------
 * WHY THIS FILE EXISTS
 * ---------------------------------------------------------------------------
 * LibreNMS local (version 2) plugins cannot register routes, controllers or
 * migrations. Verified in the installed release:
 *
 *   app/Providers/PluginProvider::loadLocalPlugins()
 *       globs app/Plugins/<Plugin>/<Hook>.php and publishes hook classes only;
 *   routes/web.php
 *       exposes exactly one plugin route: GET plugin/{plugin_name}.
 *
 * There is no hook, service provider or config extension point that lets a
 * plugin add a route, a controller binding or a migration. Rather than patch
 * LibreNMS core (forbidden by the project rules) we ship this *additional*
 * front controller inside html/ - exactly the pattern LibreNMS core itself uses
 * for html/api_v0.php, html/ajax_form.php and html/graph.php. Core files are
 * never modified, and uninstalling is the removal of this single file.
 *
 * ---------------------------------------------------------------------------
 * SECURITY POSTURE
 * ---------------------------------------------------------------------------
 *  - boots the real LibreNMS Laravel application, so session handling, auth,
 *    CSRF, policies, error handling and logging are identical to core requests
 *  - every route runs in the stock `web` middleware group (session + CSRF)
 *  - every action re-checks the `admin` gate inside the controller (defence in
 *    depth: BackupRestoreController::Guard::check)
 *  - only GET and POST are accepted; unknown shapes are 404, never 500
 *  - no secret, credential or key path is ever serialised to the client
 *  - no shell execution: the web tier performs database writes only
 */

use Illuminate\Contracts\Http\Kernel;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;

define('LARAVEL_START', microtime(true));

$uri = (string) ($_SERVER['REQUEST_URI'] ?? '');
if (str_contains($uri, '..') || str_contains($uri, "\0")) {
    http_response_code(400);
    exit('bad request');
}

if (file_exists(__DIR__ . '/../storage/framework/maintenance.php')) {
    require __DIR__ . '/../storage/framework/maintenance.php';
}

// ---------------------------------------------------------------------------
// Everything from here until the exception handler is registered runs OUTSIDE
// Laravel. A failure in this window cannot be caught by the framework, is not
// written to the LibreNMS log, and reaches the browser as a bare HTML 500 -
// the "Server returned HTML instead of JSON" symptom with no cause anywhere.
//
// So: arm a shutdown handler BEFORE any framework code, and dump a real message
// if we die unexpectedly. It is disarmed once normal handling takes over.
// ---------------------------------------------------------------------------
$brBootStage = 'autoload';
$brBootDone = false;
$brRequest = null;

register_shutdown_function(function () use (&$brBootStage, &$brBootDone): void {
    if ($brBootDone) {
        return;
    }
    $err = error_get_last();
    $fatal = $err !== null
        && in_array($err['type'], [E_ERROR, E_PARSE, E_CORE_ERROR, E_COMPILE_ERROR, E_USER_ERROR], true);

    if (! headers_sent()) {
        http_response_code(500);
        header('Content-Type: application/json; charset=utf-8');
    }
    echo json_encode([
        'status' => 'error',
        'message' => $fatal
            ? 'fatal before the framework started: ' . $err['message']
            : 'the plugin entrypoint exited before the framework started',
        'stage' => $brBootStage,
        'at' => $fatal ? basename($err['file']) . ':' . $err['line'] : null,
        'hint' => 'this is a bootstrap failure, so it never reached the LibreNMS log; '
            . 'the "stage" above is the last step that was attempted',
        'memory_limit' => ini_get('memory_limit'),
        'user' => function_exists('posix_getpwuid')
            ? (posix_getpwuid(posix_geteuid())['name'] ?? '?')
            : '?',
    ], JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT);
}, 0);

try {
    $brBootStage = 'vendor/autoload.php';
    require __DIR__ . '/../vendor/autoload.php';

    $brBootStage = 'bootstrap/app.php';
    $app = require_once __DIR__ . '/../bootstrap/app.php';

    // Routes can only be added to the route collection once the framework has
    // booted (the collection is created by the framework's route service
    // provider). bootstrap() followed by handle() below is the supported
    // sequence and is idempotent.
    $brBootStage = 'kernel bootstrap';
    $kernel = $app->make(Kernel::class);
    $kernel->bootstrap();

    // Bind the request IMMEDIATELY after bootstrap.
    //
    // Laravel binds 'request' inside Kernel::sendRequestThroughRouter(), which
    // only runs during handle(). Between bootstrap() and handle() the container
    // has no 'request' - and if anything throws in that window, Laravel's global
    // exception handler cannot render the error, because rendering needs the
    // request. The operator then sees a CRITICAL cascade about
    // 'Class "request" does not exist' that hides the real exception:
    //
    //   FatalError: Uncaught ReflectionException: Class "request" does not exist
    //   Next BindingResolutionException: Target class [request] does not exist.
    //     at HandleExceptions->renderHttpResponse()
    //
    // Binding it here lets that handler work, so the next failure names itself
    // instead of producing that cascade.
    $brBootStage = 'bind request';
    $brRequest = Request::capture();
    $app->instance('request', $brRequest);
    $app->instance('Illuminate\Http\Request', $brRequest);
} catch (\Throwable $e) {
    if (! headers_sent()) {
        http_response_code(500);
        header('Content-Type: application/json; charset=utf-8');
    }
    echo json_encode([
        'status' => 'error',
        'message' => 'bootstrap failed: ' . $e->getMessage(),
        'exception' => $e::class,
        'stage' => $brBootStage,
        'at' => basename($e->getFile()) . ':' . $e->getLine(),
        'memory_limit' => ini_get('memory_limit'),
        'user' => function_exists('posix_getpwuid')
            ? (posix_getpwuid(posix_geteuid())['name'] ?? '?')
            : '?',
    ], JSON_UNESCAPED_SLASHES | JSON_PRETTY_PRINT);
    $brBootDone = true;

    return;
}

$controller = 'App\\Plugins\\BackupRestore\\Controllers\\BackupRestoreController';
$base = 'plugin-backuprestore.php';
$ref = 'br-[0-9]{8}T[0-9]{6}Z-[a-z0-9]{3,12}';
$uuid = '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}';

// ---------------------------------------------------------------------------
// Route table. The API lives under "<script>/br/..." - deliberately NOT under
// /api, which is LibreNMS' own namespace (routes/api.php) and would shadow us.
// Two forms are registered:
//   * an explicit prefixed form, for servers that pass PATH_INFO through to an
//     existing .php file (Apache + mod_rewrite with AcceptPathInfo, i.e. the
//     documented production setup);
//   * a catch-all form, for servers that do not (PHP's built-in dev server).
// ---------------------------------------------------------------------------
// Laravel derives the routing path from REQUEST_URI *minus* SCRIPT_NAME, so for
// this file the path is `br/status` regardless of whether the server passed
// PATH_INFO (`/plugin-backuprestore.php/br/status`) or we rewrote `?r=br/status`.
// Registering against `br/...` therefore covers both transports with one table.
$brRoutes = [];
Route::middleware(['web'])->group(function () use ($controller, $ref, $uuid, &$brRoutes): void {

    // --- assets (this plugin's own files only) -----------------------------
    $brRoutes[] = Route::get('assets/{asset}', fn (string $asset) =>
        app()->call([app()->make($controller), 'asset'], ['asset' => $asset]))
        ->where('asset', '[A-Za-z0-9._-]{1,64}');

    // --- read-only ---------------------------------------------------------
    $brRoutes[] = Route::get('br', [$controller, 'apiIndex']);
    $brRoutes[] = Route::get('br/status', [$controller, 'status']);
    $brRoutes[] = Route::get('br/backups', [$controller, 'backups']);
    $brRoutes[] = Route::get('br/backups/{ref}', [$controller, 'backupDetail'])->where('ref', $ref);
    $brRoutes[] = Route::get('br/backups/{ref}/download', [$controller, 'downloadBackup'])->where('ref', $ref);
    $brRoutes[] = Route::get('br/jobs', [$controller, 'jobs']);
    $brRoutes[] = Route::get('br/jobs/{job}', [$controller, 'jobStatus'])->where('job', $uuid);
    $brRoutes[] = Route::get('br/restores', [$controller, 'restores']);
    $brRoutes[] = Route::get('br/restores/preflight/{ref}', [$controller, 'restorePreflight'])->where('ref', $ref);
    $brRoutes[] = Route::get('br/restores/{restore}', [$controller, 'restoreDetail'])->where('restore', $uuid);
    $brRoutes[] = Route::get('br/schedules', [$controller, 'schedules']);
    $brRoutes[] = Route::get('br/settings', [$controller, 'settings']);
    $brRoutes[] = Route::get('br/audit', [$controller, 'audit']);
    $brRoutes[] = Route::get('br/worker/health', [$controller, 'workerHealth']);

    // --- state-changing (CSRF enforced by the `web` group) -----------------
    $brRoutes[] = Route::post('br/backups', [$controller, 'createBackup']);
    $brRoutes[] = Route::post('br/backups/{ref}/verify', [$controller, 'verifyBackup'])->where('ref', $ref);
    $brRoutes[] = Route::post('br/backups/{ref}/delete', [$controller, 'deleteBackup'])->where('ref', $ref);
    $brRoutes[] = Route::post('br/restores', [$controller, 'restore']);
    $brRoutes[] = Route::post('br/restores/checkpoint', [$controller, 'restoreCheckpoint']);
    $brRoutes[] = Route::post('br/schedules', [$controller, 'saveSchedule']);
    $brRoutes[] = Route::post('br/settings', [$controller, 'saveSettings']);
});

// ---------------------------------------------------------------------------
// PRIORITY
// ---------------------------------------------------------------------------
// LibreNMS ends routes/web.php with a catch-all:
//     Route::any('{path?}', [InstallationController::class, 'invalid'])->where('path', '.*');
// Symfony's matcher returns the FIRST match, so a catch-all registered before
// our routes wins and every plugin URL 404s. We therefore rebuild the route
// collection with our routes first. Core routes keep their relative order, and
// because our URIs are fixed literals (`br/...`, `assets/...`) they cannot
// shadow a real core route - except that catch-all, which is exactly the point.
// ---------------------------------------------------------------------------
$router = app('router');
$collection = new Illuminate\Routing\RouteCollection;
foreach ($brRoutes as $brRoute) {
    $brRoute->setRouter($router);
    $collection->add($brRoute);
}
foreach ($router->getRoutes() as $coreRoute) {
    $collection->add($coreRoute);
}
$collection->refreshNameLookups();
$collection->refreshActionLookups();
$router->setRoutes($collection);

// ---------------------------------------------------------------------------
// Canonical transport: ?r=<resource>
//
// Not every server hands PATH_INFO to an existing .php file:
//   * Apache + AcceptPathInfo (LibreNMS ships it in html/.htaccess) -> PATH_INFO
//     works, and the routes above match directly;
//   * PHP's built-in dev server refuses to execute a .php file whose URL has
//     extra path segments - the request falls through to index.php and 404s.
//
// To be transport-agnostic the GUI uses ?r=br/status style URLs. When that
// parameter is present we rewrite the request onto the equivalent routed URL so
// the identical middleware stack (session, CSRF, auth) and route table apply.
// ---------------------------------------------------------------------------
$request = $brRequest ?? Request::capture();
$resource = (string) ($_GET['r'] ?? '');
if ($resource !== '') {
    // Backward compatibility for clients that incorrectly place a query string
    // inside the `r` parameter, e.g. ?r=backups%3Flimit%3D100.
    // Split it before validating the resource, then add those parameters to the
    // Laravel request query bag so controllers can read $request->query('limit').
    $embeddedQuery = '';
    $queryPosition = strpos($resource, '?');
    if ($queryPosition !== false) {
        $embeddedQuery = substr($resource, $queryPosition + 1);
        $resource = substr($resource, 0, $queryPosition);

        if ($embeddedQuery !== '') {
            $embeddedParameters = [];
            parse_str($embeddedQuery, $embeddedParameters);

            // Keep explicitly supplied URL query parameters authoritative if a
            // key appears in both places; merge only the embedded parameters.
            $existingParameters = $request->query->all();
            $request->query->replace(array_replace($embeddedParameters, $existingParameters));
        }
    }

    // Validate only the resource path. Query parameters are separate data and
    // must not weaken the resource-path allowlist or traversal protection.
    if (preg_match('#[^A-Za-z0-9._/-]#', $resource) || str_contains($resource, '..')) {
        abort(400, 'invalid resource');
    }
    // Only the API namespace gets the `br/` prefix; `assets/...` is registered
    // at the top level so it is addressed directly.
    $resource = ltrim($resource, '/');
    $target = '/' . $base . '/' . (str_starts_with($resource, 'assets/') ? $resource : 'br/' . $resource);
    $request->initialize(
        $request->query->all(),
        $request->request->all(),
        $request->attributes->all(),
        $request->cookies->all(),
        $request->files->all(),
        array_merge($request->server->all(), [
            'REQUEST_URI' => $target,
            'SCRIPT_NAME' => '',
            'PHP_SELF' => '',
        ]),
        $request->getContent()
    );
}

// ---------------------------------------------------------------------------
// Where does this install actually write its log?
//
// It is NOT storage/logs/laravel.log: LibreNMS uses its own channel whose path
// is `env('APP_LOG', base_path('logs/librenms.log'))` (see config/logging.php).
// Telling an operator to grep a path that does not exist wastes the one thing
// they need in an outage, so resolve it from the app's own config and fall back
// through the paths LibreNMS has used over time.
// ---------------------------------------------------------------------------
$logPath = (function () use ($app): string {
    try {
        $path = $app->make('config')->get('logging.channels.log_file.path');
        if (is_string($path) && $path !== '') {
            return $path;
        }
    } catch (\Throwable) {
        // fall through
    }
    foreach ([
        base_path('logs/librenms.log'),
        storage_path('logs/librenms.log'),
        storage_path('logs/laravel.log'),
    ] as $candidate) {
        if (is_file($candidate)) {
            return $candidate;
        }
    }

    return base_path('logs/librenms.log');
})();

// ---------------------------------------------------------------------------
// Failure reporting.
//
// Any uncaught Throwable used to surface as LibreNMS' HTML error page with a
// bare HTTP 500, so the GUI could only report "Unexpected server response
// (500)" and the browser console showed nothing useful. The one line that
// matters was buried in logs/librenms.log.
//
// Laravel's exception handler swallows the throwable and returns a rendered
// response, so a try/catch around handle() never runs. The supported hook is
// `renderable`: a callback that decides how a given throwable becomes a
// response. For our API requests we return JSON carrying the exception class,
// message and the file:line inside OUR code. The trace goes to the log only.
// ---------------------------------------------------------------------------
$wantsJson = ($resource !== '')
    || str_contains($request->getRequestUri(), '/br/');

// The version of THIS file's code, read from the installed plugin package.
// PHP-FPM keeps compiled bytecode in opcache, so after an upgrade the web tier
// can still be running the previous release while `php -l` on disk looks
// perfect. Reporting the version in every API response makes "is the new code
// actually running?" a fact instead of an inference.
$brBuild = (function (): string {
    try {
        $ver = @file_get_contents(__DIR__ . '/../app/Plugins/BackupRestore/Services/Version.php');
        if (is_string($ver) && preg_match("/const VERSION = '([^']+)'/", $ver, $m)) {
            return $m[1];
        }
    } catch (\Throwable) {
        // ignore
    }

    return 'unknown';
})();

if ($wantsJson) {
    try {
        $handler = $app->make(\Illuminate\Contracts\Debug\ExceptionHandler::class);
        if (method_exists($handler, 'renderable')) {
            $handler->renderable(function (\Throwable $e, $req) use ($logPath, $brBuild) {
                // Only JSON for our own namespace; core LibreNMS errors keep
                // their normal rendering.
                $uri = $req->getRequestUri();
                if (! isset($_GET['r']) && ! str_contains($uri, '/br/')) {
                    return null;
                }
                // A 404 is a client mistake, not a server fault. Reporting it
                // as 500 made a mistyped asset name look like a broken install.
                $notFound = $e instanceof \Symfony\Component\HttpKernel\Exception\NotFoundHttpException;
                $status = $notFound ? 404 : 500;
                if (! headers_sent()) {
                    http_response_code($status);
                    header('Content-Type: application/json; charset=utf-8');
                }
                // Only expose the location when it is inside our own plugin;
                // a framework path is noise (and version-revealing).
                $file = $e->getFile();
                $ours = str_contains($file, 'Plugins/BackupRestore');

                return response()->json([
                    'status' => 'error',
                    'message' => $notFound
                        ? 'not found'
                        : 'internal error: ' . $e->getMessage(),
                    'exception' => $e::class,
                    'at' => $ours ? basename($file) . ':' . $e->getLine() : null,
                    'hint' => $notFound ? null : 'full trace in ' . ($logPath ?? 'the LibreNMS log'),
                    'log' => $notFound ? null : ($logPath ?? null),
                    'build' => $brBuild ?? null,
                ], $status, [], JSON_UNESCAPED_SLASHES);
            });
        }
    } catch (\Throwable) {
        // Never let diagnostics machinery break the request.
    }
}

$brBootDone = true;

try {
    $response = tap($kernel->handle($request))->send();
    $kernel->terminate($request, $response);
} catch (\Throwable $e) {
    report($e);
    throw $e; // nothing more we can safely do
}