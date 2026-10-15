# PropTally V2.2 Cross-Document Consistency Matrix

**Document:** `00_CONSISTENCY_MATRIX.md`  
**Author:** Principal Solution Architect & Financial Systems Architect  
**Status:** Approved Reference (V2.2 Corrected & Validated)  
**Date:** October 2026  
**Context:** Authoritative index establishing the source of truth for every financial concept, formula, database implementation, implementation status, and UI presentation across the PropTally architecture suite.

---

## 1. Master Financial & Architectural Consistency Matrix

| Financial / System Concept | Authoritative Definition Document | Database Implementation (Table & Column) | Domain Service & Code Package | Status | User Interface View |
| :--- | :--- | :--- | :--- | :---: | :--- |
| **Gross Operating Income (GOI)** | `03_FINANCIAL_DOMAIN_MODEL.md` §1.1 | `journal_entry_lines` (Credit to Account `4010`/`4020`) | `domain.accounting.metrics.calculate_property_financials()` | **Implemented** (Phase 2.1) | Portfolio Overview & Property P&L Tab |
| **Operating Expenses (OpEx)** | `03_FINANCIAL_DOMAIN_MODEL.md` §1.1 | `journal_entry_lines` (Debits to `5020`-`5080`) | `domain.accounting.metrics.calculate_property_financials()` | **Implemented** (Phase 2.1) | Property P&L & Analytics Trends |
| **Net Operating Income (NOI)** | `03_FINANCIAL_DOMAIN_MODEL.md` §1.1, §2 | Derived: $\text{GOI} - \text{OpEx}$ (Accrual/Incurred) | `domain.accounting.metrics.calculate_property_financials()` | **Implemented** (Phase 2.1) | Portfolio KPI Cards & Property P&L |
| **Operating Cash Flow (OCF)** | `03_FINANCIAL_DOMAIN_MODEL.md` §1.1, §3 | Derived: $\text{Cash Rent Received in Checking} - \text{Direct Checking OpEx} - \text{Card Settlement Rental Share}$ | `domain.accounting.metrics.calculate_property_financials()` | **Implemented** (Phase 2.1) | Cash Flow Waterfall Table |
| **Cash Flow After Debt Service (CADS)** | `03_FINANCIAL_DOMAIN_MODEL.md` §1.1, §3 | Derived: $\text{OCF} - \text{Confirmed Bank Debt Service}$ (principal + interest, backed by an actual payment); escrow funding reported separately | `domain.accounting.metrics.calculate_property_financials()` | **Implemented** (Phase 2.1) | Portfolio Net Cash Flow KPI |
| **True Economic Net Income** | `03_FINANCIAL_DOMAIN_MODEL.md` §1.1, §2 | Derived: $\text{NOI} - \text{Interest} - \text{Depreciation}$ | `domain.accounting.metrics.calculate_property_financials()` | **Implemented** (Phase 2.1) | Property Deep Dive Financials |
| **Property Valuation & Equity** | `03_FINANCIAL_DOMAIN_MODEL.md` §1.1 | `property_valuations` $-$ principal attributed per door, with provenance (`SERVICER_REPORTED` / `LEDGER_DERIVED` / `UNAVAILABLE`) | `domain.accounting.metrics.calculate_portfolio_equity()` | **Implemented** (Phase 2.1) | Portfolio Total Equity & Debt KPIs |
| **Mortgage Principal Paydown** | `03_FINANCIAL_DOMAIN_MODEL.md` §4 | `journal_entry_lines` (Debits to `2110`/`2120`/`2130`) | `domain.accounting.ledger.post_journal_entry()` | **Implemented** (Phase 1) | Mortgage Ledger & Waterfall Table |
| **Mortgage Interest Expense** | `03_FINANCIAL_DOMAIN_MODEL.md` §4 | `journal_entry_lines` (Debits to `5010`) | `domain.accounting.ledger.post_journal_entry()` | **Implemented** (Phase 1) | Schedule E Line 12 & Property P&L |
| **Escrow Reserve Contribution** | `03_FINANCIAL_DOMAIN_MODEL.md` §4 | `escrow_movements` ($>0$) & Debit `1110`/`1120` | `domain.accounting.ledger.post_journal_entry()` | **Implemented** (Phase 1) | Mortgage Dashboard (Escrow Health) |
| **Escrow Disbursement (Taxes/Ins)** | `03_FINANCIAL_DOMAIN_MODEL.md` §2, §7 | `escrow_movements` ($<0$) & Debit `5020`/`5030`, Credit `1120` | `domain.accounting.ledger.post_journal_entry()` | **Implemented** (Phase 1) | Property P&L & Schedule E Line 16/9 |
| **Double-Entry Equilibrium & DDL Triggers** | `04_DATABASE_DESIGN.md` §3.4; `ADR-003` | `journal_entries`, `journal_entry_lines` + SQLite triggers | `domain.accounting.ledger.post_journal_entry()` | **Implemented** (Phase 1) | Verified Ledger Invariants |
| **Reversal Immutability (Compensating Entries)** | `03_FINANCIAL_DOMAIN_MODEL.md` §5 | `journal_entries.reversal_of_id` (Original Header Untouched) | `domain.accounting.ledger.reverse_journal_entry()` | **Implemented** (Phase 1 & 2.1) | Reclassification History & Audit Log |
| **Transaction Splitting & Validation** | `04_DATABASE_DESIGN.md` §3.3; `ADR-004` | `transaction_splits` ($\sum \text{splits} == |\text{transaction}|$) | `domain.transactions.splits.apply_transaction_splits()` | **Implemented** (Phase 1) | Split Transaction Interactive Modal |
| **Transaction-to-Ledger Posting Engine** | `03_FINANCIAL_DOMAIN_MODEL.md` §5 | `transaction_processing_states.posting_status` | `domain.transactions.splits.post_transaction_to_ledger()` | **Implemented** (Phase 2.1) | Transaction Register & Review Queue |
| **Client & CPA Governance Gates (CBD-1/CBD-2)**| `05_INGESTION_AND_RECONCILIATION.md` §2.2 | `transactions.requires_client_confirmation` / `requires_cpa_review`; `financial_accounts.mixed_use_status` | `domain.transactions.policy.evaluate_posting_policy()`, enforced by `domain.transactions.splits.post_transaction_to_ledger()` | **Implemented** (Phase 2.1) | Reconciliation Queue & Transaction Register |
| **Credit-Card Settlement (Transfer Posting)** | `03_FINANCIAL_DOMAIN_MODEL.md` §3 (card funding) | `transactions.counter_account_id`; entry debits card liability `2010`/`2020`, credits the deposit account | `domain.transactions.splits.post_transaction_to_ledger()` (`CARD_SETTLEMENT`) | **Implemented** (Phase 2.1) | Transaction Register (transfer target) & Cash Flow Waterfall |
| **Posting Authorization Evidence** | `05_INGESTION_AND_RECONCILIATION.md` §2.2 | `posting_authorizations` (per-gate required/supplied flags + timestamps, journal entry, allocation snapshot); append-only via DDL triggers | `domain.transactions.splits.post_transaction_to_ledger()` | **Implemented** (Phase 2.1) | Reconciliation Queue & Transaction Register |
| **Schema Versioning & Migration Parity** | `04_DATABASE_DESIGN.md` §3.4 | `schema_migrations`; column additions and CHECK-constraint table rebuilds | `db.schema.run_migrations()` | **Implemented** (Phase 2.1) | Verified Ledger Invariants |
| **Client Demo Web Application** | `07_PRODUCT_AND_UI_SPECIFICATION.md` | FastAPI + Jinja2 + Local Bundled CSS (Zero-Egress) | `proptally.web.*` | **Implemented** (Phase 2.1) | Navigable Web Dashboard |
| **Multi-Allocation Reconciliation Matcher**| `05_INGESTION_AND_RECONCILIATION.md` §2 | `reconciliation_allocations` ($\sum \text{alloc} \le |\text{source}|$) | `domain.reconciliation.engine.*` | **Planned** (Phase 3) | Multi-Allocation Center |
| **Document Vault & SHA-256 Deduplication**| `06_SECURITY_AND_DEPLOYMENT.md` §4; `ADR-007` | `documents.file_sha256`, storage vault | `domain.vault.storage.*` | **Planned** (Phase 3) | Ingestion Zone & Provenance Audit |
| **De Minimis Safe Harbor Gate ($2,500)**| `02_REQUIREMENTS_AND_AMBIGUITIES.md` §2.3 | `transaction_splits.tax_review_status` = `CAPEX_REVIEW` | `domain.tax.rules.evaluate_capex()` | **Planned** (Phase 3) | Tax Center Review Queue |
| **Deterministic Statement Parsers** | `05_INGESTION_AND_RECONCILIATION.md` §1 | Native parsers for Rocket, PNC, CCM, BoA, Apple Card | `domain.ingestion.parsers.*` | **Planned** (Phase 4) | Ingestion Pipeline |
| **Schedule E Workpaper & 1098 Reconciliation** | `03_FINANCIAL_DOMAIN_MODEL.md` §5 | `tax_mappings` (versioned by `tax_year`) | `domain.tax.schedule_e.*` | **Planned** (Phase 5) | Tax Center (IRS Schedule E Hub) |
| **Local AI Boundary & Tool Gate** | `06_SECURITY_AND_DEPLOYMENT.md` §5; `ADR-006` | Read-only tool execution; zero DB write | `domain.ai.tools.*` | **Planned** (Phase 6) | Local AI Copilot Chat Interface |

---

## 1.1 Platform & Multi-Tenancy Matrix

Authoritative specification for the platform layer introduced by the multi-tenant
work: `09_MULTI_TENANT_EXECUTION_PLAN.md`, `ADR-009` and `ADR-010`.

| Platform Concept | Authoritative Definition Document | Database Implementation (Table & Column) | Domain Service & Code Package | Status | User Interface View |
| :--- | :--- | :--- | :--- | :---: | :--- |
| **Portfolio Isolation (Database-per-Tenant)** | `ADR-009`; `09_MULTI_TENANT_EXECUTION_PLAN.md` §2 | Control plane `storage/system.db` (`users`, `tenants`, `tenant_memberships`, `user_sessions`); one ledger database at `storage/tenants/<tenant_id>/proptally.db` | `db.tenant_manager.TenantManager`, `web.auth.resolve_context()` | **Implemented** | One portfolio per session; workspace switcher on `/account` |
| **Per-Entity Ledger Accounts** | `09_MULTI_TENANT_EXECUTION_PLAN.md` §3 | `properties.asset_ledger_code`, `financial_accounts.ledger_account_code`, `loan_accounts.principal_ledger_code` / `clearing_ledger_code` / `escrow_ledger_code` | `domain.accounting.coa.ensure_entity_ledger_accounts()`, `domain.accounting.metrics.load_ledger_code_map()` | **Implemented** (M1) | Settings & property screens (M3) |
| **Local Authentication & Sessions** | `ADR-010`; `06_SECURITY_AND_DEPLOYMENT.md` §3 | `users.password_hash` (Argon2id), `user_sessions` (token digest, `csrf_token`, `revoked_at`, `expires_at`) | `web.auth` (`hash_password`, `verify_password`, `issue_session`, `resolve_context`) | **Implemented** (M2) | Sign-in, register, invite, reset, `/account` |
| **CSRF Double-Submit Protection** | `ADR-010` §3 | `user_sessions.csrf_token`; pre-session cookie for unauthenticated forms | `web.auth.AuthenticationDependency` (mounted on every protected router) | **Implemented** (M2) | Hidden `csrf_token` field on every state-changing form |
| **Role-Based Access Control** | `06_SECURITY_AND_DEPLOYMENT.md` §3.2 | `tenant_memberships.role` (`OWNER`, `CPA_REVIEWER`, `READ_ONLY`) | `web.auth.TenantContext.require_mutation()` / `require_admin()`; enforced in the shared dependency | **Implemented** (M2/M5) | `/account` portfolio & invitation management |
| **Verified Reviewer Identity (CBD-2)** | `05_INGESTION_AND_RECONCILIATION.md` §2.2 | `posting_authorizations.authorized_by_user_id` / `authorized_by_role` | `domain.transactions.splits.post_transaction_to_ledger()` | **Implemented** (M5) | Reconciliation queue attestation control |
| **Audited Actor Attribution** | `ADR-003` (append-only authorization evidence) | `audit_log.changed_by` | `domain.accounting.ledger.post_journal_entry(actor_id=...)`, `domain.transactions.splits.apply_transaction_splits(actor_id=...)` | **Implemented** (M2) | Audit history |
| **Browser Statement Ingestion** | `09_MULTI_TENANT_EXECUTION_PLAN.md` M4 | `documents`, `document_extractions`, per-portfolio `vault/` | `ingestion.service`, `web.routes.ingestion` | **Implemented** (M4) | `/statements` upload & extraction review |
| **Portfolio Onboarding** | `09_MULTI_TENANT_EXECUTION_PLAN.md` M3 | Standard accounts + generic vendor rules seeded per portfolio; owner-created entities | `web.routes.settings`, `domain.accounting.coa` | **Implemented** (M3) | `/settings`, dashboard empty state |
| **Single-Portfolio → Multi-Tenant Migration** | `09_MULTI_TENANT_EXECUTION_PLAN.md` M5 | Creates the first owner and tenant, relocates the database and vault | `scripts/migrate_to_multitenant.py` | **Implemented** (M5) | Operator CLI (dry-run by default) |
| **Multi-Portfolio Backup & Restore** | `ADR-008` (consistent backup/DR) | `VACUUM INTO` per database + integrity check, all tenants or one | `scripts/backup.sh`, `scripts/restore.sh` | **Implemented** (M5) | Operator CLI |
| **Loopback-by-Default Binding** | `06_SECURITY_AND_DEPLOYMENT.md` §2 | — | `main.py` (`_resolve_host()`, `PROPTALLY_ALLOW_WIDE_BIND`) | **Implemented** (M2) | — |

---

## 2. Invariant Enforcement Cross-Reference

1. **Post-Settlement Mortgage Recognition:**
   * Authoritative Rule: `03_FINANCIAL_DOMAIN_MODEL.md` §4.
   * Status: **Implemented in Ledger Engine & Tests.** Principal debt reductions and Escrow asset debits occur strictly upon confirmed bank settlement and servicing application.
2. **Escrow Double-Counting Prevention:**
   * Authoritative Rule: `03_FINANCIAL_DOMAIN_MODEL.md` §2.
   * Status: **Implemented.** Escrow funding is a transfer into a servicer-held reserve: it is reported as its own cash movement and is excluded from debt service, so it can never reduce CADS. Escrow disbursements reduce operating net income (NOI) on disbursement date. Zero double-counting.
3. **Double-Entry Physical Immutability & Reversals:**
   * Authoritative Rule: `03_FINANCIAL_DOMAIN_MODEL.md` §5; `ADR-003`.
   * Status: **Implemented.** SQLite DDL triggers block any INSERT/UPDATE/DELETE on posted headers and lines. Reversal status is derived via `reversal_of_id` on compensating entries.
4. **Transaction Immutability & Exact Splitting:**
   * Authoritative Rule: `03_FINANCIAL_DOMAIN_MODEL.md` §4; `ADR-004`.
   * Status: **Implemented.** Parent transaction records are immutable; split amounts are strictly positive integer cents with exact absolute sum equality.
5. **Zero Network Egress Boundary:**
   * Authoritative Rule: `06_SECURITY_AND_DEPLOYMENT.md` §5.
   * Status: **Implemented.** Zero external CDN or Google Fonts runtime calls; all assets and fonts bundled locally; bound to `127.0.0.1`.
6. **Posting Authorization vs. Matching Confidence:**
   * Authoritative Rule: `05_INGESTION_AND_RECONCILIATION.md` §2.2.
   * Status: **Implemented.** Authorization is evaluated by a single domain policy from durable transaction flags and account identity (a card's `mixed_use_status`; description text is only a legacy fallback), and is enforced inside the posting service — so the register, the reconciliation queue, reclassification and credit-card settlements all obey the same rules and no route can manufacture an approval.
   * **CPA vocabulary:** the CBD-2 checkbox is a **user self-attestation** that external review occurred (`USER_ATTESTED_CPA_REVIEW`). It is not verified CPA identity or authorization. Real CPA authorization is not implemented and would require authenticated identity, a CPA role, an explicit approval record with timestamp and approved item/version, and immutable audit history.
   * **The gates are not interchangeable:** CBD-1 is satisfied by explicit client confirmation; CBD-2 requires the external-review attestation and is *not* satisfied by client confirmation, because the deduction is claimed on Schedule E by the same party. A transaction needing both must supply both.

7. **Portfolio Isolation (Physical):**
   * Authoritative Rule: `ADR-009`; `09_MULTI_TENANT_EXECUTION_PLAN.md` §2.
   * Status: **Implemented.** Each portfolio has its own SQLite database, vault and
     photo store; a request resolves its session cookie to exactly one portfolio and the
     database session handed to a route can only reach that file. The control plane holds
     no ledger data and is created with an explicit table list. Covered by
     `tests/test_multitenant_isolation.py`, which asserts isolation at the raw-SQL level
     (the other portfolio's database contains zero rows) rather than only through routes.
   * **Limits:** at-rest separation between portfolios on one host is not provided;
     sessions, throttling state and the engine cache are per-process.

8. **Authorization Is Not Inferred:**
   * Authoritative Rule: `ADR-010` §5; `05_INGESTION_AND_RECONCILIATION.md` §2.2.
   * Status: **Implemented.** CSRF and the mutating-role check are enforced in the single
     shared dependency mounted on every protected router, so a route added later cannot
     omit either. The acting user is recorded from the authenticated session; no route
     can set the audit actor or the reviewer identity by writing a field.

---

*End of Cross-Document Consistency Matrix V2.2 (Multi-Tenant Work Registered).*
