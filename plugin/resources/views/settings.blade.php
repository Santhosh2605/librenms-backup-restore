<div style="margin: 15px;">
    <h4>Backup &amp; Restore &mdash; plugin settings</h4>

    @if(! $confReadable)
        <div class="alert alert-warning">
            Worker configuration <code>{{ $confPath }}</code> is not readable by the web server.
            Runtime settings (encryption, compression, paths) live there and are managed by
            the root-owned installer, not by the GUI.
        </div>
    @else
        <p class="text-muted">
            Operational settings are stored in <code>{{ $confPath }}</code> (root-owned).
            The form below only controls GUI-level behaviour.
        </p>
    @endif

    <form method="post">
        @csrf
        <table class="table table-condensed" style="max-width: 720px;">
            <tbody>
            <tr>
                <td>Plugin enabled</td>
                <td>
                    <select name="settings[enabled]" class="form-control input-sm">
                        <option value="1" @selected($effective['enabled'])>Yes</option>
                        <option value="0" @selected(! $effective['enabled'])>No</option>
                    </select>
                </td>
            </tr>
            <tr>
                <td>Require typed confirmation for destructive actions</td>
                <td>
                    <select name="settings[require_typed_confirmation]" class="form-control input-sm">
                        <option value="1" @selected($effective['require_typed_confirmation'])>Yes</option>
                        <option value="0" @selected(! $effective['require_typed_confirmation'])>No</option>
                    </select>
                </td>
            </tr>
            <tr>
                <td>Notify admins on backup failure</td>
                <td>
                    <select name="settings[notify_on_failure]" class="form-control input-sm">
                        <option value="1" @selected($effective['notify_on_failure'])>Yes</option>
                        <option value="0" @selected(! $effective['notify_on_failure'])>No</option>
                    </select>
                </td>
            </tr>
            <tr>
                <td>Retention: keep daily backups</td>
                <td>
                    <input type="number" min="0" max="365" class="form-control input-sm"
                           name="settings[retention_keep_daily]"
                           value="{{ (int) $effective['retention_keep_daily'] }}">
                </td>
            </tr>
            </tbody>
        </table>
        <button type="submit" class="btn btn-primary btn-sm">Save settings</button>
    </form>
</div>
