<?php
/**
 * Backup & Restore shell.
 *
 * Rendered by App\Plugins\BackupRestore\Page (SinglePageHook) inside
 * resources/views/plugins/settings.blade.php -> layouts.librenmsv1.
 *
 * Only NEW assets are loaded (plugin-private CSS/JS served from the plugin
 * directory via the same entrypoint), so no LibreNMS core file is modified.
 *
 * @var array $brBootstrap
 * @var array $brAssets
 * @var string $brUser
 */
$id = 'br' . substr(md5((string) microtime(true)), 0, 6);
?>
<div id="<?= $id ?>" class="br-app" data-bootstrap='<?= json_encode($brBootstrap, JSON_HEX_APOS | JSON_HEX_QUOT) ?>'>
    <div class="br-loading">
        <i class="fa fa-circle-o-notch fa-spin fa-2x"></i>
        <p>Loading Backup &amp; Restore&hellip;</p>
    </div>
</div>

<link rel="stylesheet" href="<?= htmlspecialchars($brAssets['css']) ?>">
<script>
window.BR_BOOTSTRAP = <?= json_encode($brBootstrap, JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT) ?>;
</script>
<script src="<?= htmlspecialchars($brAssets['js']) ?>" defer></script>
