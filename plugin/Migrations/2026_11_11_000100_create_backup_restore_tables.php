<?php

/**
 * LibreNMS Backup & Restore - own-table migration.
 *
 * VERIFIED CONSTRAINT: LibreNMS local (v2) plugins cannot register migrations.
 * `PluginProvider` only loads hook classes. Therefore this file is *not*
 * auto-discovered; it is executed by the plugin's own versioned schema
 * installer:
 *
 *     php scripts/br-schema.php install      (runs as the librenms user)
 *
 * The installer wraps it in a transaction, records the applied version in
 * `br_settings` (key = schema_version) and is idempotent.
 *
 * NO existing LibreNMS table is altered. Only `br_*` tables are created.
 *
 * IDEMPOTENCY: MariaDB commits DDL implicitly, so a failure mid-migration
 * cannot be rolled back. Every CREATE is therefore guarded by hasTable(), and
 * the installer only records a migration as applied after it returns cleanly.
 * Re-running the installer is always safe.
 */

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\Schema;

return new class extends Migration
{
    public function up(): void
    {
        if (! Schema::hasTable('br_backups')) {
          Schema::create('br_backups', function (Blueprint $table): void {
            $table->bigIncrements('backup_id');
            $table->string('backup_ref', 64)->unique()->comment('br-YYYYMMDDTHHMMSSZ-xxxxxx');
            $table->string('type', 16)->default('full');
            $table->string('status', 16)->default('pending')->index();
            $table->string('verified_status', 16)->default('unverified');
            $table->string('path', 512)->nullable();
            $table->string('destination', 32)->default('local');
            $table->unsignedBigInteger('size_bytes')->default(0);
            $table->unsignedBigInteger('uncompressed_bytes')->default(0);
            $table->string('sha256', 64)->nullable();
            $table->boolean('encrypted')->default(true);
            $table->string('encryption_scheme', 32)->nullable();
            $table->string('key_fingerprint', 64)->nullable();
            $table->json('components')->nullable();
            $table->string('librenms_version', 64)->nullable();
            $table->string('php_version', 32)->nullable();
            $table->string('mariadb_version', 64)->nullable();
            $table->string('os_version', 128)->nullable();
            $table->string('worker_version', 32)->nullable();
            $table->string('actor', 64)->nullable();
            $table->string('source', 16)->default('manual');
            $table->text('error_message')->nullable();
            $table->dateTime('created_at')->index();
            $table->dateTime('verified_at')->nullable();
            $table->dateTime('deleted_at')->nullable();
            // 16, not 8: recovery checkpoints are tagged 'checkpoint' (10 chars).
            $table->string('retention_class', 16)->nullable()->comment('daily|weekly|monthly|checkpoint');
          });
        }

        if (! Schema::hasTable('br_jobs')) {
          Schema::create('br_jobs', function (Blueprint $table): void {
            $table->string('job_id', 36)->primary()->comment('uuid4');
            $table->string('kind', 16)->index();
            $table->string('backup_type', 16)->nullable();
            $table->string('status', 24)->default('queued')->index();
            $table->string('stage', 40)->nullable();
            $table->unsignedTinyInteger('progress')->default(0);
            $table->text('message')->nullable();
            $table->string('actor', 64)->nullable();
            $table->string('backup_id', 64)->nullable();
            $table->string('restore_id', 36)->nullable();
            $table->string('pid', 16)->nullable();
            $table->string('systemd_unit', 128)->nullable();
            $table->text('error_message')->nullable();
            $table->dateTime('created_at')->index();
            $table->dateTime('started_at')->nullable();
            $table->dateTime('finished_at')->nullable();
            $table->dateTime('heartbeat_at')->nullable();
          });
        }

        if (! Schema::hasTable('br_restores')) {
          Schema::create('br_restores', function (Blueprint $table): void {
            $table->string('restore_id', 36)->primary();
            $table->string('backup_ref', 64)->index();
            $table->string('state', 32)->default('QUEUED')->index();
            $table->string('status', 16)->default('running');
            $table->string('checkpoint_ref', 64)->nullable();
            $table->string('actor', 64)->nullable();
            $table->boolean('create_checkpoint')->default(true);
            $table->json('options')->nullable();
            $table->longText('log_tail')->nullable();
            $table->text('error_message')->nullable();
            $table->dateTime('created_at')->index();
            $table->dateTime('finished_at')->nullable();
          });
        }

        if (! Schema::hasTable('br_checkpoints')) {
          Schema::create('br_checkpoints', function (Blueprint $table): void {
            $table->string('checkpoint_id', 64)->primary();
            $table->string('label', 128)->nullable();
            $table->string('status', 16)->default('complete');
            $table->string('path', 512)->nullable();
            $table->unsignedBigInteger('size_bytes')->default(0);
            $table->string('sha256', 64)->nullable();
            $table->string('created_by', 16)->default('restore');
            $table->string('restore_id', 36)->nullable()->index();
            $table->dateTime('created_at')->index();
          });
        }

        if (! Schema::hasTable('br_schedules')) {
          Schema::create('br_schedules', function (Blueprint $table): void {
            $table->increments('schedule_id');
            $table->string('name', 64);
            $table->string('frequency', 16)->default('daily');
            $table->string('time_of_day', 5)->default('02:30');
            $table->unsignedTinyInteger('weekday')->nullable();
            $table->unsignedTinyInteger('day_of_month')->nullable();
            $table->boolean('enabled')->default(true);
            $table->string('backup_type', 16)->default('full');
            $table->unsignedSmallInteger('keep_daily')->default(7);
            $table->unsignedSmallInteger('keep_weekly')->default(4);
            $table->unsignedSmallInteger('keep_monthly')->default(6);
            $table->string('next_run', 32)->nullable();
            $table->dateTime('created_at');
            $table->dateTime('updated_at')->nullable();
          });
        }

        if (! Schema::hasTable('br_settings')) {
          Schema::create('br_settings', function (Blueprint $table): void {
            $table->string('key', 64)->primary();
            $table->text('value')->nullable();
            $table->dateTime('updated_at')->nullable();
          });
        }

        if (! Schema::hasTable('br_audit_events')) {
          Schema::create('br_audit_events', function (Blueprint $table): void {
            $table->bigIncrements('event_id');
            $table->string('action', 64)->index();
            $table->string('actor', 64)->index();
            $table->string('target', 128)->nullable();
            $table->text('detail')->nullable();
            $table->string('ip', 45)->nullable();
            $table->string('result', 16)->default('ok');
            $table->dateTime('created_at')->index();
          });
        }
    }

    public function down(): void
    {
        foreach (['br_audit_events', 'br_settings', 'br_schedules',
            'br_checkpoints', 'br_restores', 'br_jobs', 'br_backups'] as $table) {
            Schema::dropIfExists($table);
        }
    }
};
