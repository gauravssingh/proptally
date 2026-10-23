# PropTally V2 Product & UI/UX Specification

**Document:** `07_PRODUCT_AND_UI_SPECIFICATION.md`  
**Author:** Product Architect & Senior Frontend Engineer  
**Status:** Approved Specification  
**Date:** October 2026  
**Context:** Screen-by-screen interface layouts, component hierarchy, interactive workflows, and visual design standards for the PropTally web dashboard.

---

## 1. Product Philosophy & Information Hierarchy

The PropTally user interface is engineered to provide immediate clarity on real estate performance, debt obligations, and tax readiness.

### Core Visual Principles
1. **Auditable Provenance on Demand:** Every reported number can be clicked or hovered over to display its origin (e.g., *"Derived from PNC Statement 03/25/2026, Page 1"*).
2. **Strict Epistemic Status Badges:** Visual labels clearly distinguish the origin and confidence of figures:
   * `[VERIFIED]`: Directly matched to official bank/mortgage statement with reconciled settlement.
   * `[CALCULATED]`: Deterministically computed from verified ledger lines (e.g. NOI, Cash Flow).
   * `[ESTIMATED]`: Based on manual user input or external valuation (e.g., Fair Market Value).
   * `[NEEDS REVIEW]`: Unmatched or ambiguous transaction awaiting user confirmation.
   * `[PARTIALLY ALLOCATED]`: Transaction with unallocated residual cents remaining.
   * `[CAPEX REVIEW REQUIRED]`: Single maintenance expenditure $\ge \$2,500$ flagged for CPA de minimis review.
   * `[SYSTEM RECONCILED]`: Mathematical balancing confirmed; **distinct from CPA tax approval**.
   * `[CPA APPROVED]`: Explicitly reviewed and approved by client's tax preparer.
3. **Primary vs. Rental Visual Segregation:** Rental investment properties (Alder, Maple) are displayed with green/blue financial indicators; primary residence (Cedar Grove) is visually partitioned with distinct neutral accents to reinforce tax isolation.

---

## 2. Navigation Architecture

PropTally
├── Workspaces
│   └── All Portfolios (membership-scoped workspace list)
└── Current portfolio · <active portfolio name, shown in the shell>
    ├── Portfolio Overview
    ├── Administration (People, Portfolio status, Backup, Sign-in)
    ├── Portfolio Setup
    ├── Portfolio doors (the active portfolio's properties)
    └── Financial Core
        ├── Mortgages & Escrow
        ├── Transactions Register
        ├── Review Queue   (Badge: Pending count)
        └── Tax & Schedule E
    └── Documents
        ├── Document Vault          (folders, search, upload, download)
        └── Statements & Ingestion  (extraction review, approval and import)

The sidebar and mobile drawer share one navigation source. When the account belongs to
multiple portfolios, the shell's workspace selector changes only that browser session; a
new sign-in presents a portfolio chooser rather than silently opening the oldest membership.

---

## 3. Screen-by-Screen Interface Specifications

### 3.1 Portfolio Overview (The Command Center)
* **Header KPI Metric Bar:**
  * **Total Portfolio Equity:** `$XXX,XXX` `[ESTIMATED]` (Total Valuations less Total Mortgage Debt).
  * **Total Debt Outstanding:** `$647,214` `[VERIFIED]` across the 3 loans.
  * **Net Cash Flow (MTD / YTD):** Real cash pocketed after all mortgage payments and expenses.
  * **Portfolio NOI (Rental Only):** Unleveraged operating performance across Alder and Maple.
  * **Action Center Banner:** Alerts for unreviewed transactions or incoming statement uploads.
* **Property Door Performance Cards:**
  * **Card 1: 12307 Larkspur St (Rental):**
    * Inflow: Riley Alvarez rent ($2,600/mo) | Debt: Rocket Loan (`$1,618.12/mo` @ 3.125%)
    * Monthly Net Cash Flow: `+$981.88` | Equity Paydown: `+$620.00/mo`
  * **Card 2: 2718 Foxglove Ct (Rental):**
    * Inflow: Monthly Rent | Debt: PNC Loan (`$1,801.48/mo` @ 2.500%)
    * Monthly Net Cash Flow: Calculated | Escrow Reserve Balance: `$2,523.96`
  * **Card 3: 3345 Elderberry Dr (Primary Residence):**
    * Status: Primary Home | Debt: CrossCountry (`$3,857.96/mo`) | Principal Balance: Tracked
* **Cash Flow Waterfall Chart (Recharts):**
  * Visualizes cash progression: Gross Rent Collected $\rightarrow$ Operating Expenses $\rightarrow$ Mortgage Interest $\rightarrow$ Escrow Reserve $\rightarrow$ Principal Equity Paydown $\rightarrow$ Net In-Pocket Cash.

---

### 3.2 Property Deep Dive (`/properties/:id`)
* **Property Header:** Address, acquisition date, cost basis, current valuation, and occupancy status.
* **Three Functional Tabs:**
  1. **Tab 1: Monthly Operating P&L:** Month-by-month table of Gross Rent, Property Taxes, Insurance, Repairs, Supplies, Snow/Lawn, and Net Operating Income.
  2. **Tab 2: Mortgage & Debt Amortization:** Interactive loan paydown chart showing remaining principal curve over time, current interest rate, and total equity accumulated to date.
  3. **Tab 3: Tenant & Lease Ledger:** Lease terms, security deposit held in trust, monthly payment history, and Zelle confirmation audit trail.

---

### 3.3 Mortgages & Debt Management (`/mortgages`)
* **Consolidated Loan Ledger:**
  * Loan Servicer, Account Number (masked), Securing Property Address, Interest Rate (BPS), Monthly Payment, Principal / Interest / Escrow breakdown, and Current Principal Balance.
* **Escrow Reserve Health Panel:**
  * Tracks servicer escrow balance over time.
  * Compares projected county tax disbursements against current escrow balance, flagging projected shortages before the servicer levies shortage fees.
* **Form 1098 Reconciliation Table:**
  * Side-by-side comparison between 12-month statement interest totals and annual Form 1098 Box 1 amounts.

---

### 3.4 Transactions & Transaction Splitting (`/transactions`)
* **Filtering & Search Bar:** Filter by Account (Checking, Visa, Apple Card), Property Door, Date Range, Category, and Reconciliation Status.
* **Transaction Table Columns:**
  `Date | Account | Description | Clean Merchant | Amount | Property | Category | Split? | Status | Source Doc`
* **Interactive Transaction Splitting Modal:**
  * Opened by clicking "Split Transaction" on any row (e.g. $500 Home Depot purchase).
  * Interface allows adding multiple split allocations:
    * Allocation 1: `$250.00` $\rightarrow$ Property: `12307 Larkspur` $\rightarrow$ Category: `Repairs (Line 14)`
    * Allocation 2: `$150.00` $\rightarrow$ Property: `2718 Foxglove` $\rightarrow$ Category: `Supplies (Line 19)`
    * Allocation 3: `$100.00` $\rightarrow$ Property: `Unassigned (Personal)` $\rightarrow$ Category: `Personal Draw`
  * **Dynamic Validation:** Modal disables the "Save Split" button until:
    $$\sum \text{Split Amounts} == \text{Total Parent Amount}$$

---

### 3.5 Reconciliation Center (`/reconciliation`)
* **Pending Matches Queue:**
  * Highlighting pending reconciliation events (e.g., BoA Checking -$1,801.48 debit matched to PNC Mortgage May 2026 billing).
* **Split-Screen Verification Workspace:**
  * **Left Pane:** Structured match details (checking debit vs mortgage breakdown, variance delta `$0.00`).
  * **Right Pane:** Embedded native PDF viewer displaying the actual statement page with extracted coordinates highlighted in yellow.
  * **Action Controls:** `[Confirm Match]`, `[Reject / Unlink]`, `[Edit Allocation]`.

#### 3.5.1 Controls & Reconciliation report (`/reconciliation/report`)
* **Purpose:** a read-only detective control over the posted ledger, reachable from
  *Settings → Controls & Reconciliation*. It never writes; it names drift instead.
* **Header:** portfolio totals (`Assets`, `Liabilities`, `Equity`) from posted journal
  lines, plus the number of transactions still unposted.
* **Integrity controls table:** every check states severity (`BLOCKING`, `WARNING`,
  `INFO`), `PASS`/`FAIL`, a count and a sample of the offending rows.
  * `BLOCKING`: ledger debits ≠ credits; statement arithmetic (checking uses
    `opening + net = closing`, cards use `opening − net = closing` because card
    amounts are holder-signed); split allocation ≠ transaction amount.
  * `WARNING`: property cost basis missing; account ledger ≠ latest statement
    closing; unposted transactions; statements that start after their opening
    entry; mortgage periods without a `MORTGAGE_BILLING` entry; unapproved
    statements.
  * `INFO`: duplicate extraction rows for the same parser version; statement
    documents with no extraction.
* **Account reconciliation table:** per account, posted ledger, latest statement
  balance normalised to ledger sign, the difference, and the unposted count.
* **Property basis table:** acquisition date, purchase price, basis and the
  property's ledger asset balance.

* **Publish gate:** `POST /setup/publish` evaluates the same checks. Any
  `BLOCKING` failure refuses the publish and redirects back to setup with the
  failing labels. Any `WARNING` diverts to `/setup/publish/review`, which lists
  the findings and requires an explicit *Publish with these findings*
  acknowledgement (`acknowledge_gaps=1`) before the portfolio goes live.

---

### 3.6 Document Vault (`/documents`)
One surface divided the way a file manager divides it: what you navigate by on the left, what
the folder holds in the middle, and the details of the document you chose on the right. Every
piece of that state is in the URL and rendered on the server — `property`, `type`, `kind`,
`sort`, `doc`, `tab`, `q`, `per_page` — so a view survives a refresh, can be shared, and
works with scripting switched off entirely.

* **Folders are opened, not filtered.** The rail is the model: the vault, then one folder per
  property (with `Unfiled` for documents not yet assigned to one), then one folder per
  document type inside it (with `Unclassified` when the type is unknown). Every node shows
  how many documents it holds. Opening a folder fills the middle with that folder's
  subfolders *and* its documents, so nothing is hidden a level deeper than the level you are
  reading; the open branch is expanded, its row marked `aria-current="page"`, and its icon
  switches from closed to open. The tree is plain `<details>`/`<summary>` with a rotating
  chevron, so it opens and closes with no script.
* **The path is the way back up.** A breadcrumb states the open folder and links each parent;
  the last crumb is the folder you are standing in and is not a link.
* **The middle names its subject.** The folder's glyph, name and counts (`N folders ·
  M documents · size`) sit in a title bar with the one action that belongs to the folder —
  *Upload* — and below them the chips (All / Folders / and one per file kind actually
  present, each with its count) and the sort (Uploaded / Name / Size) as URL-backed chips.
  A chip is only offered when something is behind it.
* **The right pane is the document you chose.** Selecting a row (`doc`) opens the inspector
  beside the list — its glyph, filename, a close link, tabs (*Preview*, *Details*, and
  *Extraction* when the parser staged one), a framed inline preview of the document's own
  serving endpoint, the actions that exist for it (open, download, statements), and the tags
  it belongs to (property, type). Selecting again is navigation, so it is a link shaped like
  a checkbox, not a checkbox: no selection model that lives only inside the browser.
* **Search looks across every folder**, deliberately: finding a file should not depend on
  guessing which folder somebody filed it in. Opening a folder leaves the search behind and
  searching leaves the folder behind, so neither silently overrides the other. The tree's
  counts follow the search — the root row included — which makes the tree itself the answer
  to "where is it?".
* **Upload is a dialog, not furniture.** The screen is a file browser, so nothing is spent on
  a permanent upload panel: *Upload* in the folder's title bar opens the uploader, and
  dropping a file anywhere on the page opens it with those files already staged. Files are
  filed by the folder you are in — the Property and Document-type selects arrive pre-filled
  from it — and several files can be sent at once (PDF, PNG, JPG, WEBP; 50 MB each). Content
  already stored is recognised by its SHA-256 and is never written twice; re-uploading known
  content still files it against a property or gives it a type, and never overwrites a
  classification that already exists. The verdict is per file — stored, already in the vault,
  or rejected with the reason — because those are different outcomes and one summary sentence
  would hide which happened.
* **Each file row** states what the file is (name, its folder, its own type — PDF, PNG — and
  size, upload date, extraction state) and hides its destinations behind a `<details>` menu:
  open (inline), download (with the original filename), and the extraction on the statements
  screen when one was staged. Files sit in the portfolio's own vault, content-addressed as
  `<vault>/<sha[:2]>/<sha><ext>`, and each row's media type is the type the file actually is.
* **Paging** is per folder, 25/50/100/all, and the folder's count and size are stated above
  the list so the size of what you are looking at is never a guess.
* A statement the parser recognises is staged for review exactly as if it had been uploaded
  on the statements screen, so there is one ingestion path rather than two.

---

### 3.7 Statements & Ingestion (`/statements`)
A statement arrives as a document, and it has to be matched, checked and made
authoritative — in that order, and never by accident.

The screen is one surface in the same language as the vault (`/documents` §3.6): a rail of
queue facets — All, Needs review, Ready to approve, Approved, Imported, each with its count,
the current one marked `aria-current="page"` — and a footer stating the queue in words
(`N awaiting review · M ready to import`); a middle holding the statements with a title bar,
the sort chips (Received / Filename / Type) and the table; and, when a statement is selected,
an inspector beside it with tabs (*Preview*, *Figures*, *Statement*), the actions that exist
for that statement, and — for anything awaiting review — the reasons it is, stated in full.
Those reasons are the case for the verdict, so they belong where there is room for a
sentence: the row shows the verdict, the inspector shows why.

View state is in the URL (`status`, `sort`, `doc`, `tab`) and rendered on the server, and the
uploader is a dialog like the vault's rather than a permanent form.

* **Upload** takes a PDF, stores it in the portfolio's vault, and matches it against the
  accounts this portfolio has registered. A file already in the vault is reported rather
  than stored twice, and an unrecognised format is kept with its reason rather than guessed
  at.
* **One row per document.** Each row states what was read (type, servicer or institution,
  loan or account ending), the extraction's verdict — `VALIDATED`, `NEEDS_REVIEW`, or
  `UNSUPPORTED` for a document that is not a statement — with its reasons listed when it
  has any, its approval state, the account or loan it matched, and anything already
  imported from it.
* **One next step per row.** The row offers a single primary action: *Review & correct*
  while anything is unresolved, *Approve* once validated, *Import* once approved, and
  nothing at all once imported — with *Review* beside Approve and Import so the figures stay
  reachable. Every action opens a dialog on that row; no correction field is stacked inline
  beneath it (`05_INTERACTION_STANDARDS.md` §6, rule 6).
* **The Review dialog** carries the fields a person may need to correct — institution or
  servicer, the account or loan identity, the balances or mortgage amounts, the dates and
  the reported interest rate — and, when the statement names an institution and last four
  digits this portfolio has not registered, the offer to register that account from the
  statement itself. Saving re-validates and reports the new verdict.
* **The Approve dialog** restates the document, its type and date, what it matched and the
  figure in question, and says plainly that approving writes nothing to the ledger.
* **The Import dialog** says what importing means: the statement becomes this portfolio's
  authoritative record for that account and period; importing the same statement twice is
  refused; correcting it afterwards is a separate, audited step.
* **The header badges** count what is ready to import, awaiting review and imported, so the
  state of the backlog is legible before any row is read.

---

### 3.8 Tax Center (IRS Schedule E Hub) (`/tax-center`)
* **Tax Year Selector:** Toggle between tax years (2025, 2026).
* **Schedule E Workpaper Grid:**
  * Columns: `Tax Line Number | Category Description | 12307 Larkspur St | 2718 Foxglove Ct | Total Rental Portfolio`
  * Direct line mapping: Line 3 (Rents), Line 12 (Mortgage Interest), Line 14 (Repairs), Line 16 (Taxes), Line 19 (Other).
  * **CPA Input Row:** Line 18 (Depreciation) input field for entering agreed CPA depreciation.
* **1-Click Export Actions:**
  * `[Export CPA Workbook (.xlsx)]`: Formatted multi-tab Excel workbook with Schedule E summary, monthly breakdown, and supporting transaction audit logs.
  * `[Export CSV]`: Plain CSV data for spreadsheet import.

---

### 3.9 Local AI Copilot (`/ai-assistant`)
* **Conversational Interface:** Chat panel powered by the local NVIDIA GPU server (Ollama / vLLM).
* **Security & Transparency Badges:**
  * Indicator confirming: *"Running locally on RTX GPU • Zero cloud egress • Deterministic tool access"*.
* **Pre-Built Insight Prompt Chips:**
  * *"Compare Q1 vs Q2 cash flow for Alder"*
  * *"List all repairs exceeding $1,000 across both rentals"*
  * *"Did PNC disburse property taxes in May?"*
* **Response Presentation:** Structured markdown responses with clickable links to underlying transaction records and source PDF pages.

---

### 3.10 Portfolio administration (`/account`)

The administration screen covers the open portfolio's people, status and backup. It is a
portfolio-level screen, not an instance-level one, and is reachable from the shell's
*Administration* navigation. The shell keeps the active portfolio name visible and provides
a membership-scoped switcher; the identity row links to personal sign-in settings, not back
to the tenant administrator's People section.

**Sections, not one page.** Four sections, each its own URL (`/account?section=…`), so a
section can be bookmarked, reloaded, and reached with the browser's back button, and the
section navigation is ordinary links (`05_INTERACTION_STANDARDS.md` §5). Exactly one section
renders at a time. The section navigation is a `.pt-segmented` control in the page header,
with the active section carrying `is-active` and `aria-current="page"`; the shell's
navigation marks the screen itself the same way, so both levels of the hierarchy are
represented. A query parameter for an unknown section falls back to *People*.

* **Header:** the title, active portfolio name, role-accurate capability label (`MANAGER —
  Bookkeeping`, not `read only`), and the section navigation. The primary action appears
  only where it belongs: *Invite somebody* in *People*, and only for an owner.
* **People** (default): *People with access to `<portfolio>`* — one row per member (name,
  `You` marker, email, role, the date access was granted). An owner gets a role select and a
  confirmed **Remove** per row; the last owner cannot be demoted or removed, and the row
  says `the portfolio's only owner` instead of showing a control that would fail. Then
  *What each role can do*: a capabilities matrix (capability rows × the four role columns,
  with the reader's own column marked) rather than prose, because four sentences cannot be
  compared and the matrix is what an administrator actually needs when choosing a role.
  Each row's role list is the very tuple its gate uses (`WORK_ROLES`, `ADMIN_ROLES`,
  `CLIENT_CONFIRMATION_ROLES`, `CPA_REVIEW_ROLES`, `EXTERNAL_REVIEW_ROLES`, and the export
  and removal sets the portfolio routes enforce), so the screen cannot describe authority the
  application does not carry out. Then *Invitations*: outstanding
  invitations with the role offered, the expiry date, a confirmed **Withdraw**, and an
  explicit statement that an invited address holds no access until it is accepted — or an
  empty state when none are outstanding. *Invite somebody* is a modal (two fields), opened
  from the header; the generated link is shown once on the screen afterwards, because a
  self-hosted deployment sends no email.
* **Portfolio status:** what the portfolio holds, counted from its own database (doors,
  loans, documents, transactions, journal entries, files on disk), whether the database
  reads, and when it was created; then its storage sizes and the export size. Absolute host
  paths are shown only to the installation operator, not to portfolio members; then the
  portfolios this account reaches, with a row action to open one; then the removal route,
  offered only to a role the removal endpoint accepts. An unreadable database is reported,
  with advice to take a copy first.
* **Backup:** what an export of this portfolio contains (a `VACUUM INTO` database snapshot,
  the vault, the photos, a manifest), the owner-only download, its estimated size, and the
  statement that the app keeps no copy and records no export history. Whole-installation
  backup instructions and the control-plane path are shown only to the installation
  operator; portfolio members are told that the operator manages shared-host backups.
  The operator scripts (`scripts/backup.sh`, `scripts/restore.sh`) cover the control plane
  and every portfolio, verify each database with `PRAGMA integrity_check`, and produce a
  SHA-256 sidecar. Nothing in the app schedules a backup.
* **Sign-in:** who you are (name, email, role in this portfolio, whether the account is an
  installation operator) and the password form, which every role may use — it is about the
  person's own credentials, not about the portfolio. The public reset page does not issue a
  token; the local operator uses `scripts/issue_password_reset.py` and hands the single-use,
  two-hour link to the verified account holder out of band.

**Roles.** `OWNER` — portfolio administration, bookkeeping, client confirmation, export and
removal. `MANAGER` — bookkeeping and setup, but not people or the client's own confirmation.
`CPA_REVIEWER` — bookkeeping plus a verified external review, but not people or client
confirmation. `READ_ONLY` — view only. The header describes the role's actual capability;
it does not call a manager read-only.

**Deliberately absent.** No activity log, because the `access_events` table is never
written; no sessions list, because there is no read helper for `user_sessions`; no
"integrity check now" button, because no importable checker exists (the scripts do it at
backup time); and no backup history or last-backup timestamp, because nothing records one.
The installation-operator flag gates only operator backup details on this screen; it does
not grant portfolio membership or global financial-data access. There is no global user or
portfolio administration console yet.

**Design-system sources.** Assembled from the component library
(`docs/ui-ux/04_COMPONENT_LIBRARY.md`): `ui.page_header`, `ui.section_open/close`,
`ui.table_open/th/table_close`, `ui.empty_state`, `ui.text_field`, `ui.select_field`,
`ui.btn`, `ui.badge`, `ui.alert` and `ui.modal_open/close`, plus `.pt-segmented` for the
section navigation and `.pt-cell-stack`/`.pt-table__primary`/`.pt-table__sub` inside cells.
It declares no inline styles and no class outside the three design-system stylesheets, which
`scripts/check_design_system.py` enforces.

---

*End of Product & UI/UX Specification.*
