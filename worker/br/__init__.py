"""LibreNMS Backup & Restore engine.

Modules
-------
common        shared helpers: hashing, safe paths, argv-only process execution
config        typed br.conf loader with validation
crypto        age / AES-256-CTR+HMAC container writer & reader
checksums     SHA-256 member manifests
archive       streaming, path-traversal-safe tar writer/reader
collect       filesystem collection (application, RRD, system, integrations)
db            MariaDB discovery, dump, validation, restore
writer        tar -> compressor -> encryption pipeline
verify        shallow / standard / deep archive verification
manifest      manifest schema, build and validation
retention     GFS retention planner (pure functions, unit tested)
state         MariaDB access to the plugin's own br_* tables
helper        root daemon: closed-set privileged operations
helper_client unprivileged client for the helper socket
worker        job orchestration, dispatcher, scheduler
restore       checkpointed restore state machine
"""

from .common import VERSION

__all__ = ["VERSION"]
__version__ = VERSION
