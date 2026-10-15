# PropTally — Technical Exploration & PDF Ingestion Findings

**Project:** PropTally  
**Phase:** 0 — Exploration & PDF Feasibility  
**Date:** October 2026  
**Status:** In Progress (Exploration Phase)

---

## 1. Executive Summary & Vision

### Problem
Small/indie real estate investors (1–10 doors) are underserved:
* Enterprise software (Yardi, AppFolio, Buildium) is bloated, expensive, and geared toward large property management companies with 100+ doors.
* Generic personal finance tools (QuickBooks, Excel, Mint/Monarch) fail to handle the unique mechanics of real estate:
  * Mortgages are **not** simple single expenses: a $2,000 monthly payment splits into **Principal** (equity balance sheet), **Interest** (tax-deductible expense), and **Escrow** (pre-paid tax/insurance reserves).
  * Maintenance expenses are scattered across personal or business credit cards (Home Depot, Lowes, plumbers).
  * Rental income arrives via checking accounts (Zelle, direct deposit, ACH).

### The "PropTally" Magic Moment
An automated financial reconciliation engine for landlords:
```
[Mortgage Statements]  ──► Ingest: Principal (Equity) + Interest (Tax Ded.) + Escrow
[Credit Card PDFs]     ──► Ingest: Maintenance, Repairs, and Supplies
[Checking PDFs]        ──► Ingest: Gross Rental Income + Operating Debits
                                         │
                                         ▼
                     ┌───────────────────────────────────────┐
                     │          PropTally Engine             │
                     │  • Auto-matches to Property "Doors"   │
                     │  • Calculates True Net Cash Flow      │
                     │  • Tracks Equity Accumulation         │
                     │  • Produces IRS Schedule E Reports    │
                     └───────────────────────────────────────┘
```

---

## 2. Review of Real-World Documents (`client statements`)

A discrete review of the sample documents confirms a realistic, multi-property portfolio with 3 distinct asset loans and everyday operating accounts:

### Document Inventory
1. **Mortgage Servicers (3 distinct loan accounts):**
   * **Rocket Mortgage** (`700000001-*.pdf`): Monthly loan statements.
   * **PNC Mortgage** (`PNC *.pdf`): Monthly loan statements, annual escrow analysis statements, and Form 1098 Mortgage Interest statements.
   * **CrossCountry Mortgage** (`CCM *.pdf`): Monthly loan statements.
2. **Checking Accounts:**
   * **Bank of America Checking** (`eStmt_*.pdf`): Multi-page statements containing incoming rent deposits and ACH transfers.
3. **Credit Cards:**
   * **Bank of America Credit Card** (`eStmt_*.pdf`): Monthly transaction ledgers.
   * **Apple Card** (`Apple Card Statement - *.pdf`): Monthly transaction statements.

### Critical Technical Findings
* **100% Vector / Digital PDFs:** None of the files are phone snapshots or paper scans. All are native e-statements produced directly by bank systems (e.g., BoA's `StreamEDS`, Apple Card, Rocket Mortgage).
* **Zero OCR Dependency:** Text, lines, amounts, and coordinates can be extracted directly with **100% character fidelity**, eliminating OCR latency and OCR error margins.
* **Standardized Mortgage Formatting:** Under CFPB / RESPA regulations, all US mortgage statements require an explicit **"Explanation of Amounts Due"** table:
  * **Principal** (Equity paydown)
  * **Interest** (Tax deductible under IRS Schedule E)
  * **Escrow** (Property taxes + hazard insurance)
  * **Outstanding Principal Balance** (Current debt liability)
  * **Interest Rate**

---

## 3. PDF Parsing Tooling & Library Evaluation

### Prior Art: Lessons from `my-monee`
In the adjacent `my-monee` project (`../my-monee`), PDF extraction was solved using:
* **`PyMuPDF` (`pymupdf` / `fitz` v1.28+)**:
  * `page.get_text("blocks")` for spatial text blocks and coordinate bounding boxes.
  * `page.find_tables()` for tabular transaction grid detection.
* **`python-dateutil`** + **`re`** for normalized monetary parsing (`clean_amount`, handling commas, `$`, `Dr`/`Cr`).
* **`pydantic`** for domain models.

### Benchmark on `client statements` Statements
A quick run with PyMuPDF on the actual sample documents yielded immediate structure:

| Document Type | Sample Tested | Pages | Text Blocks (P1) | Native Tables Found (P1) |
| :--- | :--- | :--- | :--- | :--- |
| **Rocket Mortgage** | `700000001-493.pdf` | 4 | 41 | 2 tables |
| **PNC Mortgage** | `PNC Sept 2026.pdf` | 2 | 39 | 1 table |
| **CrossCountry Mortgage**| `CCM July 2026.pdf` | 6 | 48 | 2 tables |
| **BoA Checking** | `eStmt_2026-01-15.pdf`| 8 | 23 | Direct block extraction |
| **Apple Card** | `November 2025.pdf` | 7 | 20 | 1 table |
| **BoA Credit Card** | `eStmt_2026-10-08.pdf` | 4 | 32 | 2 tables |

### Library Comparison Matrix

| Library | Speed | Table Detection | Spatial Layout | Recommendation |
| :--- | :--- | :--- | :--- | :--- |
| **PyMuPDF (`pymupdf`)** | **Ultra fast (C-backed)** | **Built-in (`find_tables()`)** | **High (`get_text("blocks")`)** | **Top Choice** (matches `my-monee` stack) |
| **`pdfplumber`** | Slower | Excellent (visual lines) | Good | Viable fallback if a table has irregular borders |
| **`pypdf`** | Fast | None | Basic plain text | Good for page splitting/metadata, weak for tables |
| **macOS Native `PDFKit`** | Instant (native) | None | Plain string dump | Good for quick shell one-liners, lacking data science tooling |

---

## 4. Key Data Fields to Extract by Statement Type

### A. Mortgage Statements
* **Identity:** Loan Number (last 4), Servicer Name, Property Address.
* **Billing Details:** Statement Date, Payment Due Date, Total Monthly Payment Due.
* **Payment Split:**
  * Principal Amount $\rightarrow$ Balance Sheet reduction
  * Interest Amount $\rightarrow$ Expense (Tax deductible)
  * Escrow Amount $\rightarrow$ Pre-paid reserve (Taxes/Insurance)
  * Late fees / misc fees
* **Portfolio Valuation Metrics:**
  * Outstanding Principal Balance
  * Current Interest Rate
  * Escrow Account Balance

### B. Bank Checking Statements
* **Account Info:** Account Number (masked), Statement Period.
* **Deposits Table:**
  * Date, Description, Amount (identifying incoming tenant rent checks, Zelle deposits).
* **Withdrawals / Debits Table:**
  * Matching automated mortgage drafts to verify actual bank clearing.

### C. Credit Card Statements
* **Account Info:** Card ending digits, Statement Period.
* **Transaction Table:**
  * Post Date, Transaction Date, Merchant / Description, Amount.
  * Tagging rules (e.g., Home Depot, Lowes, Ferguson $\rightarrow$ Maintenance & Repairs for Property X).

---

## 5. Architectural Roadmap (Exploration Phase)

1. **Phase 0.1 — Environment Setup:**
   * Create Python 3.12+ virtualenv in `projects/proptally`.
   * Install core dependencies: `pymupdf`, `pydantic`, `python-dateutil`.
2. **Phase 0.2 — Parser Spikes (Read-Only Prototypes):**
   * `mortgage_parser.py`: Extract CFPB breakdown from Rocket, PNC, and CrossCountry into a unified `MortgageStatement` schema.
   * `card_parser.py`: Adapt `my-monee` transaction extractor for Apple Card and BoA credit card statements.
   * `bank_parser.py`: Extract deposits and debits from BoA checking.
3. **Phase 0.3 — Reconciliation & Property Mapping:**
   * Property configuration model (e.g., Property 1 = PNC Loan #1350 + Unit A rent; Property 2 = Rocket Loan #7624 + Unit B rent).
   * Match cash inflows/outflows to produce the property P&L.
