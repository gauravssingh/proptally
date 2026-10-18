# Document-Based Portfolio Onboarding

**Status:** implemented (phases 1–6 of the master prompt)
**Scope:** an alternative to manual portfolio setup. A new customer uploads one ZIP
archive, PropTally extracts and reconciles what the documents say, the customer
reviews every proposed record against its source, and only approved records are
created — through the same domain services the setup wizard uses.

> **Principle:** AI and rules propose. PropTally validates. The customer decides.
> Existing domain services create the records.

## 1. Architecture assessment

### What existed and is reused

| Concern | Existing mechanism | Reused as |
|---|---|---|
| Identity and tenancy | `users`, `tenants`, `tenant_memberships`, `user_sessions` | Staging is keyed by `user_id` + `tenant_id` and every route checks both |
| Portfolio provisioning | `TenantManager.provision_tenant_storage`, `provision_tenant` | The import never creates a portfolio; it writes into the portfolio the account already owns |
| Domain records | `Property`, `FinancialAccount`, `LoanAccount`, `Lease`, `Document` | The only models written at import time |
| Ledger allocation | `ensure_entity_ledger_accounts` | Every imported entity gets the same codes the wizard would allocate |
| Setup state | `PortfolioSetup`, `domain.setup_progress` | Imported data marks steps done; the wizard resumes at what is missing |
| Document vault | `documents` + `TenantPaths.vault` | Approved source files are copied into the portfolio's own vault |
| UI | Jinja templates, `components/ui.html`, design tokens | No new frontend framework; new screens compose the same macros |
| Native document parsing | PyMuPDF, deterministic parsers | Text extraction reuses the installed dependency |

### What was missing and was added

- Control-plane staging tables (sessions, uploads, documents, extractions,
  proposals, field evidence, review decisions, jobs, provisioning manifest).
- A bounded, hostile-input-safe ZIP inventory and extraction store.
- A durable per-document job table and a worker thread.
- Provider boundaries for OCR and structured extraction.
- Cross-document reconciliation and conflict detection.
- Review, approval and idempotent provisioning services and screens.

### Deliberate non-changes

- No table was added to a portfolio database.
- No React/frontend framework was introduced.
- No accounting or tax behaviour was changed.
- No transaction is posted automatically. Bank statement transactions are not
  imported in this release; the document is retained and linked.
- No ownership model was invented. Owner names are preserved as field evidence
  until the domain has an ownership entity.

## 2. Technical specification

### 2.1 Staging model (control plane)

`onboarding_sessions` → `onboarding_uploads` → `onboarding_documents` →
`onboarding_extractions` → `onboarding_proposals` → `onboarding_field_evidence` →
`onboarding_review_decisions`; `onboarding_jobs`; `onboarding_provision_entries`.

Key properties:

- **Isolation:** every query is scoped by `user_id` and `tenant_id`, not just by
  session id. A session id is never authority.
- **Provenance:** each proposed field has one or more evidence rows with document
  id, page, supporting line, method and confidence.
- **Immutability:** `original_payload_json` is never rewritten. Customer edits live
  in `payload_json`; decisions are append-only.
- **Retention:** `expires_at` is 30 days. The worker expires abandoned sessions and
  deletes their staged bytes. Completed sessions are retained because their
  documents now live in the portfolio vault.

### 2.2 State machines

Session:
`CREATED → UPLOADING → PROCESSING → READY_FOR_REVIEW | PARTIALLY_PROCESSED |
PROCESSING_FAILED → REVIEWING → APPROVED → PROVISIONING → COMPLETED`, with
`CANCELLED`, `EXPIRED` and `PROVISIONING_FAILED` as recoverable exits.

Document:
`DISCOVERED → EXTRACTING → READY | NEEDS_REVIEW | UNSUPPORTED | EXTRACTION_FAILED`,
plus `DUPLICATE` decided at inventory time.

Proposal:
`NEEDS_REVIEW → ACCEPTED | REJECTED | DEFERRED → IMPORTED` (only accepted records
are importable). Acceptance is refused while a field conflict remains.

### 2.3 API surface

| Method | Route | Purpose |
|---|---|---|
| GET | `/onboarding` | Choose manual or document path; resume an import |
| GET | `/onboarding/import` | Upload screen and limits |
| POST | `/onboarding/import/upload` | Validate, inventory and enqueue a ZIP |
| GET | `/onboarding/import/{id}` | Progress and per-document status |
| POST | `/onboarding/import/{id}/retry` | Queue failed documents again |
| POST | `/onboarding/import/{id}/documents/{doc}/retry` | Retry one document |
| GET | `/onboarding/import/{id}/review` | Proposed portfolio summary |
| GET | `/onboarding/import/{id}/records/{proposal}` | Field review + source viewer |
| POST | `/onboarding/import/{id}/records/{proposal}/decision` | Accept, reject, defer |
| POST | `/onboarding/import/{id}/records/{proposal}/field` | Edit, resolve, mark unknown |
| GET | `/onboarding/import/{id}/documents/{doc}` | Source inspector |
| GET | `/onboarding/import/{id}/documents/{doc}/file` | Authorized inline file |
| GET/POST | `/onboarding/import/{id}/approve` | Final summary and explicit approval |
| POST | `/onboarding/import/{id}/provision/retry` | Resume a partial import |
| POST | `/onboarding/import/{id}/cancel` | Cancel and delete staged bytes |

All routes run behind the shared portfolio authentication dependency.

### 2.4 Pipeline

1. **Upload:** size cap, ZIP signature, streamed hash.
2. **Inventory:** safe names, no symlinks/encryption, declared-size and ratio checks,
   per-file and total caps, magic-byte format verification, generated filenames.
3. **Per-document jobs:** classification → text extraction → structured rules →
   schema-validated payload → evidence rows.
4. **Finalize:** merge by natural key, detect conflicts, attach mortgages/leases to
   properties, compute the review status.
5. **Review:** decisions at record and field level, with original values preserved.
6. **Provision:** manifest entries + natural-key lookups; then copy source documents
   into the portfolio vault and link them.

### 2.5 Extraction contracts

Categories: property deed, mortgage statement, loan agreement, bank statement,
rental agreement, property tax, insurance, valuation, maintenance, unknown.
Schemas are versioned (`onboarding.extraction/1.0`). Each field records value, page,
evidence line, method and confidence.

Recognised field types: money (integer cents, lakh/crore/million suffixes), dates
(ISO, numeric and month names), rates (basis points), terms (months), lists and
enumerations. Missing values are omitted; they are never inferred from unrelated
text. A default required for the domain (for example property type) is stored with a
`DEFAULT` evidence row at confidence 0, so it is visibly a decision, not a fact.

### 2.6 Reconciliation rules

- Property key: normalised street + city + ZIP. No street address means no merge.
- Mortgage key: loan number, else lender + masked number.
- Account key: institution + masked number.
- Lease key: property group + tenant.
- Outstanding balances and statement periods are historical facts: the latest dated
  statement wins and every observation is kept in evidence/history. They are not
  reported as conflicts.
- Same-semantic scalar fields (purchase price, acquisition date, original
  principal, rate, term, rent) with different values become conflicts, each with its
  own source. The customer chooses or edits; nothing is auto-selected.

### 2.7 Provisioning and idempotency

- Only `ACCEPTED` proposals are read. Approval additionally requires at least one
  accepted property, resolved conflicts and complete required fields.
- Each entity is committed before its manifest row. A retry skips `DONE` entries and
  looks up natural keys, so a crash between the two cannot duplicate a record.
- Partial failure sets `PROVISIONING_FAILED`; the retry route resumes.
- Imported properties/accounts/loans/leases are created with the same writers and
  ledger allocation as the setup wizard. Opening balances remain the customer's step.

### 2.8 Provider and privacy boundary

`providers.ocr_provider` uses a local Tesseract binary when installed and is
disabled with `PROPTALLY_OCR_PROVIDER=none`. `providers.structured_extractor`
defaults to deterministic rules. `PROPTALLY_EXTRACTION_URL` opts into an
OpenAI-compatible endpoint only when it resolves to a loopback or private address;
a public endpoint also requires `PROPTALLY_ALLOW_REMOTE_EXTRACTION=true`. No
document is sent anywhere by default.

### 2.9 Error handling

- A malformed ZIP rejects the archive as a whole with an actionable message.
- A corrupt, encrypted, unsupported or text-less document fails its own row; the
  rest of the archive continues.
- Scanned documents without OCR fail explicitly rather than appearing as empty
  successful extractions.
- Every failed job retains its technical error; the customer sees a plain-language
  reason and a retry control.
- On startup, `RUNNING` jobs are returned to `PENDING`, and `EXTRACTING` documents
  to `DISCOVERED`, so a restart is recoverable.

## 3. UX specification

| Screen | Route | What it establishes |
|---|---|---|
| Choose a path | `/onboarding` | Manual vs documents, privacy, resume |
| Upload | `/onboarding/import` | Drag/drop, limits, accepted formats, what happens next |
| Processing | `/onboarding/import/{id}` | Archive progress, per-file status, retries, cancel |
| Portfolio review | `.../review` | Counts, conflicts, grouped records, attention documents |
| Record review | `.../records/{proposal}` | Editable fields + evidence + source preview, accept/reject/defer |
| Source inspector | `.../documents/{doc}` | Full document, page links, citing fields |
| Final approval | `.../approve` | Accepted vs rejected vs deferred vs undecided, explicit checkbox |
| Created | `/onboarding/import/{id}` (completed) | Counts created, next setup step, vault link |

The visual language is the existing one: `ui` macros, `tokens.css` variables, the
application shell, and responsive two-column review that stacks below 1024px.
Conflicts are never hidden behind a single accept button; the evidence pane is
sticky beside the field list.

## 4. Verification

`tests/test_document_onboarding.py` covers:

- end-to-end ZIP → proposals → approval → tenant records → vault documents;
- setup-wizard resume after imported data;
- conflict refusal, resolution and original-value preservation;
- field edits and mark-unknown decisions;
- path traversal, file-count limits, non-ZIP uploads;
- one unreadable and one unsupported document not failing the archive;
- independent retry;
- cross-customer 404s for session, review and file;
- provisioning reusing an existing property at the same address.

Full suite: **411 passed, 4 skipped** (including 11 onboarding tests).

### Tuning against a real mortgage archive

The first live archive (127 files across CC Mortgage, PNC and Rocket folders)
exposed extraction failures that synthetic fixtures did not. The rules were tuned
against the staged text, not by sending documents to an external model:

- **Servicer identity** comes from the archive folder first, then a header company
  name. A labelled "Lender" line is accepted only if it passes a prose rejection
  test; "LENDER PAID EXPENSES", "Do not send cash by mail" and "your lender will
  make the payments" are no longer identities.
- **Loan numbers** are one numeric token of six or more digits. Dates such as
  `10/14/2025` and masked suffixes such as `0708` are rejected.
- **Servicer defaults**: when every document for one servicer agrees on one full
  loan number or one high-confidence property address, that value fills documents
  that omit it. The value is marked `DERIVED` in evidence; two loans or two
  properties for one servicer get no default.
- **Multi-line addresses** are combined (`street`, `city, ST ZIP`) so a statement
  and a 1098 for the same door do not produce two property proposals.
- **Tax forms and escrow analyses** report no current principal, rate or payment.
  A 1098's `$1` box rows are not loan facts.
- **Historical values** are reconciled per field by statement date: the newest
  outstanding balance, payment and rate win; every observation stays in evidence.
  This removed six false conflicts from year-over-year statements.
- **Properties are proposed from mortgages** when the archive has no deed, so a
  mortgage-only upload can still produce a complete portfolio.

Result on the same archive: **3 properties, 3 mortgages, 0 conflicts**, 124 ready
documents and 3 duplicates. The live session was reprocessed with a control-plane
backup taken first.

## 5. Known limitations / later phases

- Transaction extraction and posting are not implemented; bank statements are
  retained and linked, never posted to the ledger.
- Rental, tax, insurance, valuation and maintenance documents are classified and
  retained; only leases become a domain record. The others cite property evidence.
- Ownership names have no domain entity to write to; they remain field evidence.
- OCR depends on a host-installed Tesseract; PDFs that need it fail with an
  actionable message when it is absent.
- The worker is an in-process thread, appropriate for this single self-hosted
  service. The durable job table means a restart loses no queued work, but a
  multi-process deployment would need a shared claim loop (the atomic claim already
  supports it).
- Google Places confirmation remains a property-screen action; the import never
  treats a document address or a geocode as proof of ownership.
