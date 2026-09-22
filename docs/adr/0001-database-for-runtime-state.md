---
status: accepted
date: 2026-09-22
---

# Runtime state lives in a relational database, files stay on disk

Session vaults, the audit trail and the admin rules overlay used to be files under
`webui/runs/` and `config/`. That tied the service to one process on one machine: the audit
index was re-read on every listing and rewritten on every expiry, rules had no history, and a
second worker or instance could not see the first one's sessions. We moved those three into a
database behind SQLAlchemy Core (`maskroom/store/`), with SQLite as the zero-config default
and Postgres (as a container beside the app) for production. Uploaded and masked files, audit
file copies and the packaged extension stay on disk under `MASKROOM_DATA_DIR`.

## Considered options

- **Keep files, mount a shared volume.** Rejected: the JSONL audit index still rewrites
  wholesale, cross-host appends interleave, and rules stay a single unversioned file.
- **An ORM.** Rejected: three small stores with hand-shaped JSON contracts gain nothing from
  mapped classes, and Core keeps the SQL visible for review.
- **Files in the database too (bytea).** Rejected: uploads up to 25 MB would bloat backups
  and WAL for no query benefit.

## Consequences

- The database is never touched inside the masking loop. Token lookups stay in the in-memory
  vault; the store flushes only the tokens added during a request, in one batched insert.
  Vault entries are append-only and tokens are deterministic per session, so concurrent
  workers writing the same session converge on the same rows.
- Each process caches sessions and the overlay; it re-syncs a session from its entry count
  and polls the rules revision id every few seconds. Rule changes therefore take effect on
  other processes within that window, not instantly.
- Schema changes are applied by hand for now (`schema_meta.version`); Alembic is deferred
  until the first migration is actually needed.
- Encryption of vault values and audit originals at rest is still open (ROADMAP item 6,
  TECHNICAL_DESIGN 5.3); the database gives that work a single place to land.
- YAML remains the interchange format for rules (`maskroom-admin policy export|import`);
  `MASKROOM_POLICY_FILE` is gone.
