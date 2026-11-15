"""MariaDB discovery, consistent dump and restore."""

from __future__ import annotations

import gzip
import os
import re
import shutil
import subprocess
import typing as t

from .common import (
    BRException,
    Logger,
    PreflightError,
    ValidationError,
    free_bytes,
    human,
    run,
    which,
)

IDENT_RE = re.compile(r"^[A-Za-z0-9_$]{1,64}$")

#: Statements that must be present in a dump we consider "complete".
REQUIRED_TABLES = ("devices", "users", "config")


class DbCredentials(t.NamedTuple):
    host: str
    port: int
    name: str
    user: str
    password: str
    # Unix socket path. Preferred when set: the helper runs with
    # RestrictAddressFamilies=AF_UNIX, so it cannot open a TCP connection, and
    # a local socket never crosses the network anyway.
    socket: str | None = None

    def argv_safe(self) -> dict[str, str]:
        return {"host": self.host, "port": str(self.port), "user": self.user}


def _parse_dotenv(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    if not os.path.exists(path):
        return values
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key.strip()] = value
    return values


CONFIG_PHP_RE = {
    "host": re.compile(r"\$config\[\s*['\"]db_host['\"]\s*\]\s*=\s*['\"]([^'\"]+)['\"]"),
    "port": re.compile(r"\$config\[\s*['\"]db_port['\"]\s*\]\s*=\s*['\"]?(\d+)"),
    "name": re.compile(r"\$config\[\s*['\"]db_name['\"]\s*\]\s*=\s*['\"]([^'\"]+)['\"]"),
    "user": re.compile(r"\$config\[\s*['\"]db_user['\"]\s*\]\s*=\s*['\"]([^'\"]+)['\"]"),
    "pass": re.compile(r"\$config\[\s*['\"]db_pass['\"]\s*\]\s*=\s*['\"]([^'\"]*)['\"]"),
}


def discover_credentials(cfg) -> DbCredentials:
    """Resolve DB credentials from br.conf, then .env, then config.php.

    The password is never logged and never crosses the HTTP boundary.
    """
    root = cfg.librenms_root
    env = _parse_dotenv(os.path.join(root, ".env"))
    php: dict[str, str] = {}
    config_php = os.path.join(root, "config.php")
    if os.path.exists(config_php):
        with open(config_php, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for key, rx in CONFIG_PHP_RE.items():
            match = rx.search(text)
            if match:
                php[key] = match.group(1)

    host = cfg.get("database", "db_host", "") or env.get("DB_HOST") or php.get("host") or "127.0.0.1"
    port = (
        cfg.get("database", "db_port", "")
        or env.get("DB_PORT")
        or php.get("port")
        or "3306"
    )
    name = cfg.get("database", "db_name", "") or env.get("DB_DATABASE") or php.get("name") or "librenms"
    user = cfg.get("database", "db_user", "") or env.get("DB_USERNAME") or php.get("user") or "librenms"
    password = (
        cfg.get("database", "db_password", "")
        or env.get("DB_PASSWORD")
        or php.get("pass")
        or ""
    )

    db_socket = (cfg.get("database", "db_socket", "") or env.get("DB_SOCKET") or "").strip()

    host = host.split(":")[0]
    if host in ("localhost", ""):
        host = "127.0.0.1"
    # `localhost` in MySQL means "use the local socket"; keep that semantic and
    # fall back to the well-known socket path so the privileged helper never
    # needs TCP at all.
    if not db_socket and host == "127.0.0.1":
        default_sock = "/run/mysqld/mysqld.sock"
        if os.path.exists(default_sock):
            db_socket = default_sock
    if not IDENT_RE.match(name):
        raise PreflightError(f"invalid database name in configuration: {name!r}")
    if not IDENT_RE.match(user):
        raise PreflightError(f"invalid database user in configuration: {user!r}")
    try:
        port_i = int(port)
    except ValueError as exc:
        raise PreflightError(f"invalid database port: {port!r}") from exc
    return DbCredentials(host, port_i, name, user, password, db_socket or None)


def dump_tool(cfg) -> str:
    want = str(cfg.get("database", "dump_tool", "auto")).lower()
    if want == "auto":
        for candidate in ("/usr/bin/mariadb-dump", "/usr/bin/mysqldump"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        raise PreflightError("neither mariadb-dump nor mysqldump is installed")
    path = which(want)
    return path


def _mysql_client() -> str:
    for candidate in ("/usr/bin/mariadb", "/usr/bin/mysql"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    raise PreflightError("mariadb/mysql client not installed")


def _cnf_file(cred: DbCredentials, path: str, *, group: str = "client") -> str:
    """Write a 0600 option file so the password never appears in argv."""
    if cred.socket:
        # `socket` in an option file implies a local connection; passing host
        # as well would make the client prefer TCP.
        lines = [f"[{group}]", f"socket={cred.socket}", f"user={cred.user}"]
    else:
        lines = [f"[{group}]", f"host={cred.host}", f"port={cred.port}", f"user={cred.user}"]
    if cred.password:
        # Escape per MySQL option-file rules.
        pw = cred.password.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'password="{pw}"')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def version(cred: DbCredentials) -> str:
    cnf = _cnf_file(cred, f"/dev/shm/br-dbver-{os.getpid()}.cnf")
    try:
        proc = run(
            [_mysql_client(), f"--defaults-extra-file={cnf}", "--batch", "--skip-column-names",
             "-e", "SELECT VERSION()"],
        )
        return (proc.stdout or b"").decode().strip().splitlines()[0]
    finally:
        _unlink(cnf)


def server_reachable(cred: DbCredentials) -> bool:
    try:
        version(cred)
        return True
    except BRException:
        return False


def list_tables(cred: DbCredentials) -> list[str]:
    cnf = _cnf_file(cred, f"/dev/shm/br-tables-{os.getpid()}.cnf")
    try:
        proc = run([
            _mysql_client(), f"--defaults-extra-file={cnf}", "--batch", "--skip-column-names",
            "-e", f"SHOW FULL TABLES FROM `{cred.name}` WHERE Table_type = 'BASE TABLE'",
        ])
        return [ln.split("\t")[0] for ln in (proc.stdout or b"").decode().splitlines() if ln]
    finally:
        _unlink(cnf)


def dump(cred: DbCredentials, cfg, dest_gz: str, logger: Logger) -> dict:
    """Create a logical dump of the whole database, gzip-compressed.

    Uses ``--single-transaction`` for a consistent InnoDB snapshot without
    taking write locks, which keeps polling running during the backup.
    The dump is validated before this function returns.
    """
    tool = dump_tool(cfg)
    os.makedirs(os.path.dirname(dest_gz), exist_ok=True)
    cnf = _cnf_file(cred, f"/dev/shm/br-dump-{os.getpid()}.cnf")

    argv = [
        tool,
        f"--defaults-extra-file={cnf}",
        "--databases", cred.name,
        "--add-drop-database",
        "--skip-add-locks",
        "--no-tablespaces",
        "--hex-blob",
        "--default-character-set=utf8mb4",
        "--skip-comments",
    ]
    if cfg.get_bool("database", "single_transaction", True):
        argv.append("--single-transaction")
    if cfg.get_bool("database", "routines", True):
        argv += ["--routines", "--triggers", "--events"]
    excludes = [t for t in cfg.get_list("database", "exclude_tables") if IDENT_RE.match(t)]
    for table in excludes:
        argv.append(f"--ignore-table={cred.name}.{table}")
    argv += cfg.get_list("database", "extra_args")

    logger.info("database dump starting", tool=os.path.basename(tool), database=cred.name)
    tmp_raw = dest_gz + ".raw"
    started = _now()

    # No shell and no pipe symbol: mariadb-dump writes to a file descriptor we
    # own, then we gzip it ourselves. This keeps every argument an argv item.
    import subprocess

    with open(tmp_raw, "wb") as raw:
        proc = subprocess.run(argv, stdout=raw, stderr=subprocess.PIPE, check=False)
    _unlink(cnf)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[-2000:]
        raise BRException(f"database dump failed (exit {proc.returncode}): {err}")

    raw_size = os.path.getsize(tmp_raw)
    if raw_size < 1024:
        raise BRException("database dump is suspiciously small - aborting")
    validate_raw_dump(tmp_raw, cred, logger)

    with open(tmp_raw, "rb") as src, gzip.open(dest_gz, "wb", compresslevel=6) as dst:
        shutil.copyfileobj(src, dst, 1024 * 1024)
    _unlink(tmp_raw)

    size = os.path.getsize(dest_gz)
    logger.info(
        "database dump complete",
        raw=human(raw_size), compressed=human(size), seconds=_elapsed(started),
    )
    return {"raw_bytes": raw_size, "size_bytes": size, "tables": len(list_tables(cred))}


def validate_raw_dump(path: str, cred: DbCredentials, logger: Logger) -> None:
    """Sanity-check the SQL text: header, terminator and key tables present."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        head = fh.read(8192)
    if "CREATE DATABASE" not in head and f"`{cred.name}`" not in head:
        raise BRException("dump validation failed: no CREATE DATABASE statement")
    if "Dump completed" in head:
        pass
    # tail check
    with open(path, "rb") as fh:
        fh.seek(max(0, os.path.getsize(path) - 4096))
        tail = fh.read().decode("utf-8", "replace")
    if not tail.rstrip().endswith(";") and "Dump completed" not in tail:
        raise BRException("dump validation failed: statement terminator missing (truncated dump?)")
    missing = [t for t in REQUIRED_TABLES if f"CREATE TABLE `{t}`" not in head and f"`{t}`" not in head]
    if missing:
        # Only warn: table names appear throughout, not necessarily in the first 8 KiB.
        logger.warn("dump validation: core tables not seen in header", tables=",".join(missing))


def restore(cred: DbCredentials, sql_gz: str, logger: Logger, *, create: bool = True,
            verify_name: str | None = None) -> dict:
    """Restore a gzipped dump. Runs as root through the helper.

    Implemented here for the helper process only.

    Crash safety
    ------------
    The live database is never dropped before the import is known to work. The
    dump is first imported into a scratch schema (``<name>__brrestore``); only
    when that succeeds are the two schemas swapped. A failure at any point
    leaves the original database untouched.
    """
    client = _mysql_client()
    cnf = _cnf_file(cred, f"/dev/shm/br-restore-{os.getpid()}.cnf")
    staging_db = verify_name or f"{cred.name}__brrestore"
    if not IDENT_RE.match(staging_db):
        raise PreflightError(f"invalid staging database name: {staging_db!r}")

    try:
        # 1. Fresh scratch schema.
        try:
            for stmt in (
                f"DROP DATABASE IF EXISTS `{staging_db}`",
                f"CREATE DATABASE `{staging_db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
            ):
                _run_sql(client, cnf, stmt)
        except BRException as exc:
            message = str(exc)
            if "Access denied" in message or "1044" in message:
                raise BRException(
                    f"the restore account cannot create the staging schema "
                    f"`{staging_db}`. Grant it explicitly, e.g.:\n"
                    f"    GRANT ALL PRIVILEGES ON `{cred.name}\\_\\%`.* TO "
                    f"'{cred.user}'@'localhost';\n"
                    f"LibreNMS itself only grants the application database, so a "
                    f"stock install needs this one extra grant. The live database "
                    f"was not modified."
                ) from exc
            raise

        # 2. Import into the scratch schema, rewriting the dump's `USE` so
        #    nothing is ever written to the live database at this stage.
        tables = _import_dump(client, cnf, sql_gz, staging_db, logger)
        if not tables:
            raise BRException("dump produced no tables in the staging schema")
        logger.info("dump imported into staging schema", database=staging_db, tables=len(tables))

        # 3. Swap: drop live, promote staging.
        if create:
            _run_sql(client, cnf, f"DROP DATABASE IF EXISTS `{cred.name}`")
            _run_sql(client, cnf,
                     f"CREATE DATABASE `{cred.name}` CHARACTER SET utf8mb4 "
                     f"COLLATE utf8mb4_unicode_ci")
            logger.info("database dropped and recreated", database=cred.name)

        for table in tables:
            _run_sql(client, cnf,
                     f"RENAME TABLE `{staging_db}`.`{table}` TO `{cred.name}`.`{table}`")
        _run_sql(client, cnf, f"DROP DATABASE IF EXISTS `{staging_db}`")

        live = list_tables(cred)
        logger.info("database restored", tables=len(live))
        return {"tables": len(live)}
    finally:
        _unlink(cnf)


def _run_sql(client: str, cnf: str, statement: str) -> None:
    run([client, f"--defaults-extra-file={cnf}", "--binary-mode", "-e", statement])


def sanitised_dump_lines(sql_gz: str):
    """Yield a dump's lines with its database-retargeting statements removed.

    A `mariadb-dump --databases` dump begins with

        DROP DATABASE IF EXISTS `librenms`;
        CREATE DATABASE `librenms`;
        USE `librenms`;

    Piping that to `mariadb <scratch_db>` does NOT import into the scratch
    schema: `USE` wins, so the rows land in the LIVE database, and the leading
    DROP deletes it. That defeats the entire crash-safe design silently - the
    import "succeeds" against the wrong schema.

    Kept at module level (rather than as a closure) so the retarget-stripping
    can be regression-tested directly.
    """
    # Both forms occur in the wild: mariadb-dump wraps the drop in a version
    # comment, but a hand-written or older dump does not.
    drop_re = re.compile(
        rb"^(?:/\*!\d+\s+)?DROP\s+DATABASE"
        rb"(?:\s+IF\s+EXISTS)?\s+`[^`]+`\s*(?:\*/)?\s*;?\s*$",
        re.IGNORECASE,
    )
    create_re = re.compile(rb"^(?:/\*!\d+\s+)?CREATE\s+DATABASE\b.*;\s*$", re.IGNORECASE)
    use_re = re.compile(rb"^USE\s+`[^`]+`\s*;\s*$", re.IGNORECASE)
    with gzip.open(sql_gz, "rb") as src:
        for line in src:
            stripped = line.rstrip(b"\r\n")
            # Leading whitespace is legal before a statement; match on the
            # trimmed form so indented retargeting cannot slip through.
            trimmed = stripped.lstrip()
            if drop_re.match(trimmed) or create_re.match(trimmed) or use_re.match(trimmed):
                continue
            yield line


def _import_dump(client: str, cnf: str, sql_gz: str, target_db: str,
                 logger: Logger) -> list[str]:
    """Stream the decompressed dump into the client, retargeted at ``target_db``.

    Two details matter here:

    * ``gzip.open()`` returns a *file object*; handing it to
      ``subprocess(stdin=...)`` would pass its raw descriptor (the compressed
      bytes) straight to the client. The filter runs as a thread that feeds
      ``communicate()``, so the client really receives SQL.
    * ``--binary-mode`` is required: dumps legitimately contain NUL bytes.
    """
    import subprocess
    import threading

    argv = [client, f"--defaults-extra-file={cnf}", "--binary-mode",
            "--default-character-set=utf8mb4", target_db]

    errors: list[BaseException] = []

    def feed(stdin) -> None:
        try:
            for block in sanitised_dump_lines(sql_gz):
                stdin.write(block)
        except BrokenPipeError:
            # The client exited early (it will report why on stderr); this is
            # not a failure to read the dump, so do not mask the real error.
            pass
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
        finally:
            try:
                stdin.close()
            except OSError:
                pass

    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    feeder = threading.Thread(target=feed, args=(proc.stdin,), daemon=True)
    feeder.start()
    # Do NOT use communicate(): it closes stdin as soon as it is called, which
    # pulls the pipe out from under the feeder ("write to closed file"). Drain
    # stderr here instead, then wait for the process.
    stderr_chunks: list[bytes] = []
    while True:
        block = proc.stderr.read(65536)
        if not block:
            break
        stderr_chunks.append(block)
    err = b"".join(stderr_chunks)
    proc.stdout = None
    feeder.join(timeout=120)
    proc.wait()
    if errors:
        raise BRException(f"could not read dump {sql_gz}: {errors[0]}")
    if proc.returncode != 0:
        message = (err or b"").decode("utf-8", "replace").strip()[-3000:]
        raise BRException(f"database restore failed (exit {proc.returncode}): {message}")
    return list_tables_named(client, cnf, target_db)


def list_tables_named(client: str, cnf: str, database: str) -> list[str]:
    proc = run([
        client, f"--defaults-extra-file={cnf}", "--batch", "--skip-column-names",
        "-e", f"SHOW FULL TABLES FROM `{database}` WHERE Table_type = 'BASE TABLE'",
    ])
    return [ln.split("\t")[0] for ln in (proc.stdout or b"").decode().splitlines() if ln]


# -- helpers ---------------------------------------------------------------

def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _now() -> float:
    import time

    return time.time()


def _elapsed(started: float) -> int:
    import time

    return int(time.time() - started)


# ---------------------------------------------------------------------- #
# Plugin-table preservation across a restore
# ---------------------------------------------------------------------- #
#: Tables owned by this plugin. A restore replaces the whole schema with the
#: dump, which was taken from the *user's* database and may not contain these
#: tables at all (they are created by br-schema.php, not by LibreNMS).
PLUGIN_TABLES = (
    "br_jobs",
    "br_backups",
    "br_restores",
    "br_checkpoints",
    "br_settings",
    "br_schedules",
    "br_audit",
)


def _table_exists(cred: DbCredentials, table: str) -> bool:
    if not IDENT_RE.match(table):
        raise ValidationError(f"invalid table name: {table!r}")
    cnf = _cnf_file(cred, f"/dev/shm/br-tx-{os.getpid()}.cnf")
    try:
        proc = run([
            _mysql_client(), f"--defaults-extra-file={cnf}", "--batch", "--skip-column-names",
            "--database", cred.name,
            "-e", ("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA="
                   f"DATABASE() AND TABLE_NAME='{table}'"),
        ])
        return (proc.stdout or b"").decode().strip() not in ("", "0")
    finally:
        _unlink(cnf)


def _columns(cred: DbCredentials, table: str) -> list[str]:
    if not IDENT_RE.match(table):
        raise ValidationError(f"invalid table name: {table!r}")
    cnf = _cnf_file(cred, f"/dev/shm/br-col-{os.getpid()}.cnf")
    try:
        proc = run([
            _mysql_client(), f"--defaults-extra-file={cnf}", "--batch", "--skip-column-names",
            "--database", cred.name,
            "-e", ("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA="
                   f"DATABASE() AND TABLE_NAME='{table}' ORDER BY ORDINAL_POSITION"),
        ])
        return [ln.strip() for ln in (proc.stdout or b"").decode().splitlines() if ln.strip()]
    finally:
        _unlink(cnf)


def _execute(cred: DbCredentials, statement: str) -> None:
    if not IDENT_RE.match(cred.name):
        raise ValidationError(f"invalid database name: {cred.name!r}")
    cnf = _cnf_file(cred, f"/dev/shm/br-sql-{os.getpid()}.cnf")
    try:
        # The option file carries no database, so name it explicitly.
        run([_mysql_client(), f"--defaults-extra-file={cnf}", "--binary-mode",
             "--database", cred.name, "-e", statement])
    finally:
        _unlink(cnf)


def snapshot_tables(cred: DbCredentials, tables: t.Sequence[str] | None = None) -> dict:
    """Capture the rows of ``tables`` as plain text cells.

    Values are kept as the client rendered them (tab-separated batch output)
    and written back as quoted literals, which round-trips NULL, numbers and
    strings faithfully for the types this plugin uses.
    """
    wanted = list(tables or PLUGIN_TABLES)
    out: dict[str, dict] = {}
    cnf = _cnf_file(cred, f"/dev/shm/br-snap-{os.getpid()}.cnf")
    try:
        for table in wanted:
            if not IDENT_RE.match(table):
                continue
            if not _table_exists(cred, table):
                out[table] = {"exists": False, "columns": [], "rows": []}
                continue
            columns = _columns(cred, table)
            proc = subprocess.run(
                [_mysql_client(), f"--defaults-extra-file={cnf}", "--batch",
                 "--skip-column-names", "--binary-mode", "--database", cred.name,
                 "-e", f"SELECT * FROM `{table}`"],
                stdin=subprocess.DEVNULL, capture_output=True, check=False, timeout=600,
            )
            if proc.returncode != 0:
                raise BRException(
                    f"could not snapshot {table}: "
                    + proc.stderr.decode("utf-8", "replace")[:400])
            rows = [ln.split("\t") for ln in proc.stdout.decode("utf-8", "replace").splitlines()]
            out[table] = {"exists": True, "columns": columns, "rows": rows}
    finally:
        _unlink(cnf)
    return out


def restore_snapshot(cred: DbCredentials, snapshot: t.Mapping[str, dict]) -> dict:
    """Write a snapshot back with REPLACE INTO, ignoring vanished columns."""
    written: dict[str, int] = {}
    for table, data in (snapshot or {}).items():
        if not IDENT_RE.match(str(table)) or not isinstance(data, dict):
            continue
        if not data.get("exists") or not data.get("rows"):
            written[table] = 0
            continue
        if not _table_exists(cred, str(table)):
            # Plugin table did not survive the import and cannot be created
            # here; the plugin's own migration recreates it on next load.
            written[table] = 0
            continue
        columns = [str(c) for c in data.get("columns") or [] if IDENT_RE.match(str(c))]
        live = set(_columns(cred, str(table)))
        keep = [c for c in columns if c in live]
        if not keep:
            written[table] = 0
            continue
        idx = [columns.index(c) for c in keep]
        col_sql = ", ".join(f"`{c}`" for c in keep)
        values: list[str] = []
        for row in data["rows"]:
            if len(row) < len(columns):
                continue
            cells = []
            for i in idx:
                cell = row[i]
                if cell == "NULL":
                    cells.append("NULL")
                else:
                    escaped = cell.replace("\\", "\\\\").replace("'", "''")
                    cells.append("'" + escaped + "'")
            values.append("(" + ", ".join(cells) + ")")
        total = 0
        for start in range(0, len(values), 500):
            chunk = values[start:start + 500]
            _execute(cred, f"REPLACE INTO `{table}` ({col_sql}) VALUES " + ", ".join(chunk) + ";")
            total += len(chunk)
        written[table] = total
    return written
