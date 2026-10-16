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

PropTally features a clean, responsive left sidebar navigation layout:

```text
PropTally
├── 📊 Portfolio Overview
├── 🏘️ Properties (Doors)
├── 🏦 Mortgages & Debt
├── 💳 Transactions
├── ⚖️ Reconciliation Center   (Badge: Pending count)
├── 📁 Document Vault
├── 📑 Tax Center (Schedule E)
├── 📈 Analytics & Trends
├── 🤖 Local AI Copilot
└── ⚙️ Settings & Backups
```

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

---

### 3.6 Document Vault & In-App Uploader (`/documents`)
* **Upload Dropzone (Modal & Full Page):**
  * Supports drag-and-drop of single or batch PDF statements.
  * Real-time progress bar: Uploading $\rightarrow$ Fingerprinting $\rightarrow$ Extracting $\rightarrow$ Validating.
  * Ingestion Results Card: Displays detected institution, statement period, total extracted transactions, and status (`COMMITTED` or `NEEDS_REVIEW`).
* **Vault Document Table:**
  * List of all archived PDFs, organized by Institution, Account, and Year.
  * Download button and inline preview viewer.
  * Re-run extraction or view raw JSON extraction provenance.

---

### 3.7 Tax Center (IRS Schedule E Hub) (`/tax-center`)
* **Tax Year Selector:** Toggle between tax years (2025, 2026).
* **Schedule E Workpaper Grid:**
  * Columns: `Tax Line Number | Category Description | 12307 Larkspur St | 2718 Foxglove Ct | Total Rental Portfolio`
  * Direct line mapping: Line 3 (Rents), Line 12 (Mortgage Interest), Line 14 (Repairs), Line 16 (Taxes), Line 19 (Other).
  * **CPA Input Row:** Line 18 (Depreciation) input field for entering agreed CPA depreciation.
* **1-Click Export Actions:**
  * `[Export CPA Workbook (.xlsx)]`: Formatted multi-tab Excel workbook with Schedule E summary, monthly breakdown, and supporting transaction audit logs.
  * `[Export CSV]`: Plain CSV data for spreadsheet import.

---

### 3.8 Local AI Copilot (`/ai-assistant`)
* **Conversational Interface:** Chat panel powered by the local NVIDIA GPU server (Ollama / vLLM).
* **Security & Transparency Badges:**
  * Indicator confirming: *"Running locally on RTX GPU • Zero cloud egress • Deterministic tool access"*.
* **Pre-Built Insight Prompt Chips:**
  * *"Compare Q1 vs Q2 cash flow for Alder"*
  * *"List all repairs exceeding $1,000 across both rentals"*
  * *"Did PNC disburse property taxes in May?"*
* **Response Presentation:** Structured markdown responses with clickable links to underlying transaction records and source PDF pages.

---

*End of Product & UI/UX Specification.*
