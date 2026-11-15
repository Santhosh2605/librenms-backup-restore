<a href="{{ url('plugin/BackupRestore') }}">
    <i class="fa fa-database fa-fw fa-lg" aria-hidden="true"></i>
    Backup &amp; Restore
    @if(($lastStatus ?? 'unknown') === 'failed')
        <span class="label label-danger" title="Last backup failed">!</span>
    @endif
</a>
