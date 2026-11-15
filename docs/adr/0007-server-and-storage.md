# ADR-0007: Server-first deployment; SQLite stays, the bucket holds backups, PostgreSQL waits

- Status: accepted
- Date: 2026-11-15

## Context

The owner now has a VPS, the OpenAI-compatible gateway, an S3 bucket (mountable as a folder
through SFTPGo) and PostgreSQL. The project moves from "laptop first" to "server first":
push to GitHub, CI tests, automatic deploy. Nothing was ever run on the laptop.

## Decision

1. **One process**: `roj serve` (scheduler, Telegram, panel, daily backup) in a rootless Podman
   container under its own Linux user, capped at 1 CPU / 2 GB. Restarts are safe: the durable
   kernel resumes parked work.
2. **CI/CD as in apigate**: tests and the supply-chain gate, then an image pushed to GitHub
   Packages; the server pulls it itself every 2 minutes (`podman auto-update`) with health-gated
   rollback. No SSH keys or deploy secrets in GitHub. (A first draft used SSH deploys; replaced
   at the owner's request the same day.)
3. **SQLite remains the store**, on local disk. It is transactional, needs no service, has
   FTS5 for BM25, and the kernel has exactly one writer process. Live database files are
   never placed on the bucket mount (FUSE and network filesystems break SQLite locking).
4. **The bucket is for backups**: one consistent snapshot per day and before every deploy,
   made with SQLite's online backup API.
5. **PostgreSQL is not a dependency yet.** It is the right move when one of these is true:
   several worker nodes must share one task queue; the knowledge base outgrows in-process
   vector search (roughly > 100k chunks) and pgvector pays off; other services need to query
   roj's state. Until then it would add a driver dependency (the core is capped at 5), a
   second SQL dialect and a service whose outage stops roj, for no measurable gain.

## Consequences

- The laptop runbook (docs/FIRST_RUN.md) still works for local experiments; the server
  runbook is docs/SERVER.md.
- The Postgres backend is an item in docs/BACKLOG.md with the trigger conditions above, so
  the self-improvement loop or the owner can pick it up when a condition is met.
