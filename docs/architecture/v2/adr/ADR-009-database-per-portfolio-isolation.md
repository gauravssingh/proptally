# ADR-009: Database-per-Portfolio Isolation with a Control-Plane Database

* **Status:** Accepted
* **Date:** October 2026
* **Deciders:** Principal Systems Architect, Financial Systems Architect
* **Supersedes:** nothing. Extends ADR-002 (SQLite with WAL), which is silent on multiplicity.

## Context
PropTally was built as a single-portfolio deployment: one SQLite database, one vault,
one photo store, and ledger account codes hardcoded to that owner's properties, loans
and cards. It must now serve several independent individuals on one self-hosted host,
each with their own account, login and portfolio, without sending any data off the host.

Two isolation models were viable:

1. **Database per portfolio** — one SQLite file, vault and photo store per portfolio,
   with a small control-plane database for accounts, memberships and sessions.
2. **Shared database with `tenant_id`** — one database, a tenant column on every table,
   and a predicate on every query.

## Decision
We select **database-per-portfolio isolation** (option 1).

1. Each portfolio's ledger lives at `storage/tenants/<tenant_id>/proptally.db`, with its
   own `vault/`, `photos/`, `inbox/`, `processed/` and `quarantine/`.
2. The **control plane** at `storage/system.db` holds only users, tenants, memberships,
   sessions, invitations and password-reset tokens. It is created with an explicit table
   list so a ledger table can never appear in it.
3. A request resolves its session cookie to exactly one portfolio, and the database
   session handed to a route can only reach that portfolio's file. No request path opens
   a portfolio chosen by the client.
4. Each portfolio's database receives the full DDL trigger set, so ledger immutability is
   enforced identically in every portfolio.

## Consequences
### Positive
* **Isolation is physical, not conditional.** A forgotten `WHERE` clause cannot leak
  another owner's balances, because the query is not running against their file.
* **Per-portfolio SQLite write locks.** One owner importing a year of statements does not
  block another owner's write; under option 2 every tenant would contend on one lock.
* **Constraints stay simple.** `documents.file_sha256`, `loan_accounts.loan_number` and
  `journal_entries.idempotency_key` (e.g. `OPENING_BALANCE:2026-01-01`) stay unique per
  portfolio instead of needing composite keys, and two owners can legitimately record
  opening balances for the same date.
* **Backup, export and deletion are per file.** An owner's portfolio is a directory that
  can be archived, handed to them, or removed whole, without filtered dumps.

### Negative / Trade-Offs
* **More files: one database per portfolio**, plus WAL sidecars. A host serving hundreds
  of portfolios carries hundreds of small databases and a bounded engine cache.
* **Portfolio-wide reporting is not a single query.** Cross-portfolio totals (an instance
  administrator's view, or one CPA across many owners) require iterating portfolios.
* **Schema changes must reach every database.** `init_schema` is idempotent and runs per
  portfolio on access, so an upgrade applies on next use rather than in one transaction.
* **At-rest separation is not provided between portfolios.** All portfolios share the
  host, the OS user and the volume-level LUKS encryption described in
  `06_SECURITY_AND_DEPLOYMENT.md §4.1`. Per-portfolio encryption at rest is not
  implemented; treat host access as access to every portfolio on it.

## Alternatives Considered
* **Shared database with `tenant_id`** — rejected: it converts every present and future
  query into an isolation liability, makes ledger immutability triggers tenant-aware, and
  serialises all writers behind one SQLite file lock.
* **Database per portfolio on separate hosts** — rejected: disproportionate operational
  cost for the target scale (individual investors with 1–10 doors), and it would not
  remove the need for the control plane.
