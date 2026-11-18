# Security model

The threat this design takes seriously is **the web tier being compromised**.
LibreNMS is a public-facing PHP application with a large attack surface; a
backup plugin is a natural privilege-escalation target because it legitimately
needs root to stop services and read the database. Most such plugins hand
PHP-FPM a sudo rule. This one does not.

## Privilege separation

```
PHP-FPM  ──(DB rows only)──>  dispatcher ──(helper socket)──>  br-helper (root)
```

### The web tier has no privileged filesystem access

`plugin/` and `front-controller/` never call `exec`, never open a privileged
file, and are **absent from `/etc/sudoers.d/librenms-backup`**. There is
deliberately no entry for `www-data` or `librenms` that grants anything beyond a
single root binary.

PHP-FPM can write only to `storage/app/br-uploads`, a dedicated intake folder
with ACL access for the configured PHP user. It cannot read or write the backup
archive tree. The worker treats every upload as untrusted, validates its
manifest, and verifies it before registration.

Consequence: PHP RCE can submit malicious or oversized uploads and queue jobs,
but it cannot modify registered archives or directly invoke the root helper.
Host upload limits and free disk space still constrain intake capacity.

### Exactly one escalation path

```sudoers
librenms ALL=(root) NOPASSWD: /usr/local/libexec/librenms-backup/br-helper
```

One command, no arguments, no wildcards. `NOEXEC` is *not* used because the
helper itself must exec `mariadb-dump` and `systemctl`; instead the helper
validates argv itself and never invokes a shell.

### The helper protocol

* Unix socket `/run/librenms-backup/br-helper.sock`, mode `0660`, group `librenms`.
* Every message is HMAC-SHA256 signed with `/run/librenms-backup/br-helper.hmac`
  (root-created, `0640`). The worker can read this shared key, so the MAC
  detects modification in transit; it does not distinguish a compromised
  worker from an intended caller. The helper therefore enforces its own
  operation and path policy for every request.
* Closed operation set — anything else is refused before dispatch:

| Operation | Constraint |
|---|---|
| `fs.stat` / `fs.list` / `fs.hash` | Read-only, path must be under an allowlisted root |
| `fs.mkdir` / `fs.unlink` | Backup roots only |
| `fs.install_tree` / `fs.install_file` | Source must be readable from approved roots; target must be inside the LibreNMS root, configured integration roots, or approved certificate/key paths; ownership and modes are fixed |
| `service.start` / `service.stop` | Unit must be in `ALLOWED_UNITS` |
| `service.validate` | Fixed validator per unit — never a caller-supplied command |
| `db.restore` | Socket path allowlisted; database/user names match `^[A-Za-z_][A-Za-z0-9_]{0,63}$`; file must end `.sql.gz` |
| `worker.spawn` | `job_id` must match a UUID regex; the unit name is never built from caller input |
| `maintenance.on` / `maintenance.off` | LibreNMS root only |

* Requests are size-bounded and parsed as JSON; no `eval`, no templates, no
  path concatenation from unvalidated input.
* Responses are bounded (64 MiB cap) so a hostile client cannot exhaust memory.

### systemd hardening

`br-helper.service`:

```ini
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=read-only
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
ProtectSystem=full
RestrictAddressFamilies=AF_UNIX
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
SystemCallArchitectures=native
```

Two of these have operational consequences worth stating plainly:

* **`RestrictAddressFamilies=AF_UNIX`** blocks TCP. The helper therefore restores
  MySQL over `/run/mysqld/mysqld.sock`, not `127.0.0.1:3306`. The socket path is
  validated against an allowlist rather than passed through.
* **`ProtectSystem=full`** keeps `/usr` and `/boot` read-only. `/etc` and
  `/opt/librenms` must stay writable during a restore, so they are not mounted
  read-only — access is constrained by the path allowlist instead.

The worker unit adds `ProtectSystem=full`, `PrivateTmp=yes` and an explicit
`ReadWritePaths=` allowlist, so a job cannot write outside the backup tree.

## Application-layer access control

Every request passes four independent checks (`Services/Guard.php`):

1. Authenticated session (`web` guard).
2. `admin` gate — LibreNMS's own `hasRole('admin')`.
3. CSRF token on every non-GET verb (both Laravel's middleware and an explicit
   `hash_equals` re-check inside `Guard`).
4. A named ability: `view`, `operate`, `restore`, `configure`.

A refusal writes an `access.denied` audit row *before* aborting, so brute-force
probing is visible.

## Restore confirmation

A restore requires the operator to type a phrase (default `RESTORE`, configurable
to any 4–32 upper-case letters). This is enforced server-side, before the backup
reference is even looked up. The GUI cannot bypass it because the phrase is not
checked client-side at all.

Additional interlocks:

* the target backup must be `completed` **and** `verified`;
* a second restore is refused with `409` while one is active;
* restores are refused while a backup job is queued or running;
* a recovery checkpoint is taken before anything is overwritten.

## Secrets

| Secret | Location | Never |
|---|---|---|
| DB credentials | LibreNMS `.env` / `config.php` | Written to `br.conf`, the archive, or the API payload |
| age private key (legacy archives) | `/etc/librenms-backup/keys/identity.txt`, `0640` | Inside the archive |
| Container passphrase | `/etc/librenms-backup/keys/passphrase`, `0640` | In `br.conf` or argv |
| LibreNMS `APP_KEY` | LibreNMS `.env` | In API responses |

Passwords never appear in argv: the DB client is invoked with
`--defaults-extra-file=` pointing at a `0600` file in `/dev/shm`, removed in a
`finally` block.

### Scrubbing on the way in

Application config is scrubbed before it is archived. `collect.SECRET_LINE_RE`
recognises `KEY=value`, `KEY = value`, `KEY: value` and `"KEY": "value"` forms of
anything ending in `_PASSWORD`, `_SECRET`, `_TOKEN`, `_PASSWD`, `api_key`,
`api_secret`, `APP_KEY` or `password`, and replaces the value with
`__REDACTED__`. The count of redactions is recorded in the manifest so an
operator can see that scrubbing happened.

### Transport

The API returns no secrets at all: job ids, statuses and byte counts only.
New archives use AES-256-CTR authenticated with HMAC-SHA256. Independent
encryption and MAC keys are derived from the offline passphrase with
PBKDF2-HMAC-SHA256 (600,000 iterations and a random salt). Existing `age`
archives remain readable when their original identity key is retained.
Automatic restore skips general `/etc` service configuration because root
services may load executable configuration from those paths. Only certificate
and key trees can be installed from the system component.

## What is deliberately *not* protected

* **Local root on the host.** Anyone with root can read the keys and the
  archives. This tool protects against compromise of the *web tier* and against
  operator error, not against a hostile administrator.
* **Filesystem-level tampering with an archive plus its sidecar.** Member hashes
  live inside the archive; the container hash lives in a sidecar. An attacker who
  can rewrite both is assumed to have the host already. Offsite/immutable copies
  are the answer at that threat level.
* **Machine-specific network config.** `/etc/network`, `/etc/netplan`,
  `/etc/hostname`, `/etc/fstab`, `machine-id` and SSH host keys are captured for
  reference but never auto-restored.

## Reporting

Security issues should be reported privately to the maintainer before any public
disclosure. Include the version (`apiIndex` reports it), the affected operation
and a reproduction.
