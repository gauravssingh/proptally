# PropTally

**Self-hosted, privacy-first property, mortgage, and real estate cash flow reconciliation engine for independent investors.**

---

## Overview

PropTally is a specialized financial platform built for residential real estate investors (1–10 doors). It replaces error-prone spreadsheets and generic budgeting tools with a mathematically sound, double-entry financial accounting ledger that reconciles rental income, multi-lender mortgages, escrow reserves, and property operating expenses.

### Key Architectural Pillars

* **Double-Entry General Ledger:** Implemented in integer cents with strict balance verification ($\sum \text{Debits} == \sum \text{Credits} > 0$) executed under atomic transactions (`BEGIN IMMEDIATE`).
* **Database-Enforced Immutability:** SQLite DDL triggers strictly prohibit any `UPDATE` or `DELETE` on posted journal entries and lines. Corrections require audited compensating reversals.
* **Two-Stage Mortgage Accounting:** Eliminates phantom equity by separating statement receipt (obligation recording) from bank cash settlement and confirmed servicer application.
* **Multi-Property Transaction Splitting:** Allocates single merchant outflows (e.g., Home Depot, Menards) across multiple properties and personal draws with exact absolute-value validation.
* **Independent Financial Metrics:** Mathematically isolates **Net Operating Income (NOI)**, **Operating Cash Flow (OCF)**, **Debt Service (P&I + Escrow)**, and **Cash Flow After Debt Service (CADS)**.
* **Self-Hosted & Zero Egress:** Runs entirely on local infrastructure with SQLite WAL mode. No financial data leaves the host machine.

---

## Demo Web Interface

PropTally includes a navigable, modern web interface:

| View | Endpoint | Description |
| :--- | :--- | :--- |
| **Portfolio Overview** | [`/`](http://127.0.0.1:8080/) | Top KPI cards (Equity, Debt, NOI, CADS), Door performance cards with primary residence segregation, and cash flow waterfall ledger. |
| **Property Deep Dives** | [`/properties/:id`](http://127.0.0.1:8080/properties/prop_tinsley_huntley) | Monthly Operating P&L, 30-year mortgage debt terms, and tenant lease agreements. |
| **Mortgages & Escrow** | [`/mortgages`](http://127.0.0.1:8080/mortgages) | Consolidated loan ledger across servicers (Rocket, PNC, CCM) and servicer escrow reserve asset accounts. |
| **Transactions Register** | [`/transactions`](http://127.0.0.1:8080/transactions) | Color-coded inflow/outflow register, manual transaction entry modal, and dynamic multi-property splitting modal. |
| **Review & Reconciliation Queue** | [`/reconciliation`](http://127.0.0.1:8080/reconciliation) | Dedicated review queue for unallocated cash deposits and rental deductions on **mixed-use** cards awaiting client confirmation or a user attestation of external review. |

Two things the interface is deliberately explicit about:

* **Credit-card settlement is a transfer, not an expense.** Recording a payment from a deposit account to a card (choose the card as the transfer target in the manual-entry modal) debits the card liability and credits the bank. The original purchase already carried the expense, so nothing is expensed twice, and only the payment's allocated rental share reduces rental cash flow.
* **The CBD-2 checkbox is a user self-attestation**, recorded as `USER_ATTESTED_CPA_REVIEW`. It states that external review has occurred; it is **not** verified CPA identity or authorization, and the application does not yet have the CPA role, approval record or audit trail that real CPA sign-off would require. Whether a card is reviewable at all follows the account's `mixed_use_status` (`DEDICATED_RENTAL`, `MIXED_USE`, or the safe default `UNCLASSIFIED`), not the fact that it is a credit card.
* **The two governance gates are not interchangeable.** CBD-1 is a client-authorization gate satisfied by explicit client confirmation. CBD-2 is an external-review gate over a Schedule E deduction, so the client's sign-off does **not** satisfy it — the attestation is required. A transaction that needs both must supply both.

---

## Quickstart

### Prerequisites
* Python 3.11+
* Git

### Installation

1. Clone the repository:
   ```bash
   git clone https://github.com/gauravssingh/proptally.git
   cd proptally
   ```

2. Create and activate a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. Install dependencies:
   ```bash
   pip install -e .
   ```

### Running the Test Suite
The test suite validates double-entry invariants, DDL triggers, direct SQL bypass blocks, compensating reversals, and web endpoints:

```bash
pytest -v
```

### Launching the Web Dashboard
```bash
python main.py
```
Open **`http://127.0.0.1:8080`** in your browser.

---

## Multi-Portfolio Deployment

PropTally serves several independent owners from one self-hosted instance. Each
owner has their own login and their own portfolio, and no portfolio's data is
reachable from another's.

### First run
A new account starts with an empty portfolio: the standard chart of accounts and the
generic vendor rules, and nothing else. There is no sample data to clear out — doors,
accounts, mortgages and opening balances are entered on the setup screen.

The server refuses to bind every interface by default. It listens on
`127.0.0.1:8080`; to serve a LAN or Tailscale address, set one explicitly:

```bash
PROPTALLY_HOST=100.64.0.5 python main.py     # a specific address
# or, deliberately:
PROPTALLY_HOST=0.0.0.0 PROPTALLY_ALLOW_WIDE_BIND=true python main.py
```

Open the app and create the first account at `/auth/register`. While the instance
has no accounts, registration is open and the first account becomes the instance
administrator; afterwards, registration closes unless you set
`PROPTALLY_ALLOW_REGISTRATION=true`. Other people join an existing portfolio only
through an invitation created on the **Account** screen.

For installations shared by several people, set `PROPTALLY_INSTANCE_NAME` to a name they
recognize; it appears on the sign-in screen. Password recovery does not send email: a local
operator issues a single-use link on the host with
`python scripts/issue_password_reset.py --email user@example.com` and gives it to the
verified account holder over a trusted channel. The public reset page never reveals or
issues a token.

### Where data lives

```text
storage/
├── system.db                        # accounts, portfolios, memberships, sessions
└── tenants/
    └── <tenant_id>/
        ├── proptally.db             # this portfolio's ledger (WAL mode)
        ├── vault/                   # statement PDFs, addressed by SHA-256
        ├── photos/                  # property photos
        ├── inbox/                   # optional hot folder for unattended intake
        ├── processed/
        └── quarantine/
```

Isolation is physical: a request resolves its session to exactly one of those
databases, and the session handed to a route can only reach that file. See
[ADR-009](docs/architecture/v2/adr/ADR-009-database-per-portfolio-isolation.md).

### Operating notes
* **Serve it over TLS.** Behind a reverse proxy (Caddy, Nginx) that sets
  `X-Forwarded-Proto`, session cookies are marked `Secure` automatically. Over plain
  HTTP they are not, because a `Secure` cookie that never comes back would lock you
  out. `PROPTALLY_SECURE_COOKIES=true|false` overrides the detection.
* **No mail transport.** Password reset creates a single-use token and shows it to
  the operator, who hands it to the account holder. See
  [ADR-010](docs/architecture/v2/adr/ADR-010-local-zero-egress-authentication.md).
* **Backups cover every portfolio.** `scripts/backup.sh` snapshots `system.db` and
  each tenant database with `VACUUM INTO`, verifies each with
  `PRAGMA integrity_check`, and archives the vaults; `--tenant <id>` exports one.
* **Existing single-portfolio data** moves with
  `python scripts/migrate_to_multitenant.py --db storage/client-portfolio.db` (dry
  run by default; add `--apply` to write).

### Roles
| Role | Can |
| :--- | :--- |
| `OWNER` | Everything in the portfolio: setup, import, post, invite, export, remove. |
| `CPA_REVIEWER` | Read, and record the external review a Schedule E deduction requires. |
| `READ_ONLY` | Read only. |

### Running more than one worker

One process is the recommended deployment: `python main.py` starts a single uvicorn
process, and for a household or an investor with 1–10 doors that is ample. Running
several processes is **supported** when you need it — typically to keep statement
parsing, which is CPU-bound, from delaying other requests.

What is safe to share, and what is not:

| | Behaviour with N processes |
| :--- | :--- |
| Accounts, portfolios, memberships, invitations, **sessions** | Shared. They live in `storage/system.db`, so a session issued by one worker is honoured by every other, and signing out or changing a password applies everywhere. |
| Portfolio databases | Shared safely. Each is SQLite in WAL mode, which supports concurrent processes; connections use a 10-second `busy_timeout`, so a writer waits rather than failing. |
| **Login throttling** | **Per process.** The 5-attempts-per-15-minutes limit is an in-memory counter, so N workers effectively allow N× the attempts. It is a speed bump, not an authorization control; if you need a hard limit under multiple workers, that counter has to move into `system.db` first. |
| Per-process caches | Multiplied. Each worker keeps its own database handles and caches, so file handles and memory grow with the worker count. |
| Write throughput | Unchanged. SQLite allows one writer at a time, so extra workers add concurrency for reads and for requests that do not write, not write capacity. |

A multi-worker command, if you need one:

```bash
PYTHONPATH=src PROPTALLY_HOST=127.0.0.1 gunicorn \
  -w 4 -k uvicorn.workers.UvicornWorker proptally.web.app:app
```

Every worker must share the same `PROPTALLY_STORAGE_ROOT`. Do not point workers at
different roots while fronting them with one address: they would appear to be different
instances with different accounts.

Most of these properties are asserted by `tests/test_multitenant_deployment.py`, so the
table above cannot drift away from the code.


---

## Project Structure

```text
proptally/
├── src/proptally/
│   ├── core/                  # Domain exceptions, configuration, SQLite retry handler
│   ├── db/                    # Ledger models, WAL pragmas, DDL triggers, schema migrations
│   │   ├── system_models.py   # Control plane: users, tenants, memberships, sessions
│   │   └── tenant_manager.py  # Per-portfolio engine, paths and provisioning
│   ├── domain/
│   │   ├── accounting/        # Chart of accounts + allocation, ledger, opening balances, metrics
│   │   ├── tax/               # Schedule E workpapers and depreciation
│   │   └── transactions/      # Splitting, validation and posting-authorization policy
│   ├── ingestion/             # Statement parsers, ingestion service, hot-folder watcher, CLI
│   └── web/                   # FastAPI factory, sessions/CSRF, Jinja2 templates, routers
├── scripts/                   # Backup, restore and single→multi-tenant migration
├── tests/                     # Unit & integration suite on live SQLite
├── docs/                      # Architectural specifications & decision records (V2 Suite)
└── main.py                    # Server launcher (loopback by default)
```

---

## Technical Documentation

* **[Architecture V2 Specification Suite](docs/architecture/v2/01_ARCHITECTURE_REVIEW.md)**
  * [00. Cross-Document Consistency Matrix](docs/architecture/v2/00_CONSISTENCY_MATRIX.md)
  * [01. Architecture Review & Gap Analysis](docs/architecture/v2/01_ARCHITECTURE_REVIEW.md)
  * [02. Requirements & Ambiguity Register](docs/architecture/v2/02_REQUIREMENTS_AND_AMBIGUITIES.md)
  * [03. Financial Domain & Accounting Spec](docs/architecture/v2/03_FINANCIAL_DOMAIN_MODEL.md)
  * [04. Database & Ledger Design](docs/architecture/v2/04_DATABASE_DESIGN.md)
  * [05. Ingestion Pipeline & Reconciliation Engine](docs/architecture/v2/05_INGESTION_AND_RECONCILIATION.md)
  * [06. Security, Privacy & Deployment](docs/architecture/v2/06_SECURITY_AND_DEPLOYMENT.md)
  * [07. Product & UI/UX Specification](docs/architecture/v2/07_PRODUCT_AND_UI_SPECIFICATION.md)
  * [08. Phased Implementation Roadmap](docs/architecture/v2/08_IMPLEMENTATION_ROADMAP.md)
  * [09. Multi-Tenant Self-Hosted Execution Plan](docs/architecture/v2/09_MULTI_TENANT_EXECUTION_PLAN.md)
  * [Architecture Decision Records (ADR 001–010)](docs/architecture/v2/adr/)
    * [ADR-009: Database-per-Portfolio Isolation](docs/architecture/v2/adr/ADR-009-database-per-portfolio-isolation.md)
    * [ADR-010: Local, Zero-Egress Authentication](docs/architecture/v2/adr/ADR-010-local-zero-egress-authentication.md)

## Local Document Ingestion

The first local ingestion release extracts monthly mortgage terms from Rocket
Mortgage, PNC, and CrossCountry PDFs. Other PDFs are copied into the local
document vault on commit and reported as unsupported; bank and credit-card
transaction parsing is out of scope. Parser validation does not constitute
approval: recognized documents are retained with their extraction data and a
`PENDING` approval state. A mortgage period and current interest rate are
updated only after explicit approval and a separate import operation. No
journal entry is created from a mortgage statement.

Run a no-write dry-run first:

```bash
PYTHONPATH=src python -m proptally.ingestion.cli "$HOME/Documents/client-statements" \
  --db storage/client-portfolio.db
```

Persist documents and extraction results; this does not import mortgage periods:

```bash
PYTHONPATH=src python -m proptally.ingestion.cli "$HOME/Documents/client-statements" \
  --db storage/client-portfolio.db --commit
```

The command output includes extracted values and an `extraction_id`. Review the
values before explicitly approving and importing a validated extraction:

```bash
PYTHONPATH=src python -m proptally.ingestion.cli \
  --db storage/client-portfolio.db --approve-extraction <extraction_id>
PYTHONPATH=src python -m proptally.ingestion.cli \
  --db storage/client-portfolio.db --import-approved-extraction <extraction_id>
```

If an extraction requires review or operator adjustments, use `--update-extraction`
with `--set` to correct fields and re-evaluate validation status:

```bash
PYTHONPATH=src python -m proptally.ingestion.cli \
  --db storage/client-portfolio.db --update-extraction <extraction_id> \
  --set principal_cents=52179 payment_due_date=2026-04-01
```

Once corrected values satisfy balance invariants (`principal + interest + escrow + fees == total_due`),
the extraction is automatically promoted to `VALIDATED` and can be approved and imported.

There is no browser review queue in this release. Incomplete recognized PDFs,
including documents with no extracted statement date, are retained in
`document_extractions` with all extracted fields and review reasons so a
future review interface can process them. Parser re-extraction is planned and is not
currently supported. A future implementation must attach another extraction
record/version to the existing SHA-256-deduplicated `Document`, not create a
duplicate document.
Approval is currently a mutable local status/timestamp on the extraction, not
an append-only audit record or identity-verified sign-off.

### Local ingestion run

The client statement set contains 48 documents across Rocket Mortgage, PNC Bank,
CrossCountry Mortgage, Apple Card, and Bank of America accounts.

With section-scoped Respa/CFPB statement parsing:
- **Scanned:** 48 documents (0 errors, 0 duplicates).
- **Supported / VALIDATED:** 29 monthly mortgage statements (100% of monthly statements).
- **NEEDS_REVIEW:** 3 auxiliary tax/escrow disclosure documents (PNC 2025 Form 1098 and 2 annual escrow account disclosure statements, retained for review and excluded from monthly activity as designed).
- **UNSUPPORTED:** 16 credit card and checking statements (retained in document vault, out of scope for mortgage parser).
- **Validation Balance:** All 29 monthly statements exhibit exact arithmetic balance: `principal + interest + escrow + fees == total_due`.

For each mortgage PDF, verify the statement date and payment due date, the
monthly principal, interest, escrow, fees and total due, the outstanding
principal balance, and the reported interest rate. Distinguish monthly payment
components from balance, year-to-date, or historical amounts. In this run,
extracted rate values included 4.990% and 2.500%; these are parser outputs, not
verified loan terms. Do not approve an extraction solely because its amounts
appear arithmetically consistent.

That database predates multi-tenancy. Bring it into the multi-portfolio layout rather
than pointing the app at it:

```bash
PYTHONPATH=src python scripts/migrate_to_multitenant.py \
  --source storage/client-portfolio.db --email you@example.com --keep-source
# add --apply once the dry run looks right
```

The dry-run parses against an in-memory database and does not write to the
source folder, selected database, or vault. Duplicate detection runs at commit
time. Source PDFs are copied byte-for-byte and never modified. Validated data
remains non-authoritative until approved and separately imported; uncertain or
incomplete data stays recoverable for review.
