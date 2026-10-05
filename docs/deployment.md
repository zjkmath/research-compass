# Private deployment and backups

The README local launcher is the supported default. It binds only `127.0.0.1`; do not expose the development server or remove host/CSRF validation. Source publication does not deploy a public website.

Keep the application directory immutable and private state/config outside it. `pilot.py --config <PRIVATE_CONFIG> --state <PRIVATE_STATE> configure` creates a persistent owner-only configuration, without displaying its key. It defaults to SSH loopback transport. Initialize a separate instance using the local launcher first; `pilot.py` requires an existing validated database and never silently reseeds it. Stop the local instance before private pilot operation. Collect static assets during builds.

For private HTTPS behind a proxy, retain exact host/origin allowlists, secure sessions and a trusted loopback proxy. The optional Tailscale Serve code requires an exact operator-supplied FQDN and an **actually observed** `X-Forwarded-Proto: https` header. Do not enable that mode based on a guessed header or wildcard host. No tailnet, ACL, hostname, certificate or network service configuration is included. No Funnel/public exposure or OS scheduling is installed.

For SQLite use the application's consistent backup function, not a raw copy of a live WAL database. Preserve the original persistent key separately with restricted permissions. Restore to a new isolated state directory, compare expected records, then start the restored instance and verify HTTP read/write and restart. A file-integrity check or HTTP 200 alone is insufficient. Never overwrite the only database or backup. The included synthetic smoke script performs this bounded lifecycle exercise.

PostgreSQL is an optional separately provisioned deployment, requiring its driver and an isolated migration/restore test. The default lock and demo build use SQLite; no PostgreSQL production claim is made.
