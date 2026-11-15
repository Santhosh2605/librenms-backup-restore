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

require __DIR__ . '/../vendor/autoload.php';

$app = require_once __DIR__ . '/../bootstrap/app.php';

// Routes can only be added to the route collection once the framework has
// booted (the collection is created by the framework's route service provider).
// bootstrap() followed by handle() below is the supported sequence and is
// idempotent.
$kernel = $app->make(Kernel::class);
$kernel->bootstrap();

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
$request = Request::capture();
$resource = (string) ($_GET['r'] ?? '');
if ($resource !== '') {
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

$response = tap($kernel->handle($request))->send();

$kernel->terminate($request, $response);
