# ADR-003: Lightweight Double-Entry Accounting Model

* **Status:** Accepted
* **Date:** October 2026
* **Deciders:** Financial Systems Architect, Senior Solution Architect

## Context
Commercial personal finance apps treat imported bank and card transactions as independent single-entry line items. This creates fatal accounting errors in real estate:
1. Mortgage payments: bank debit ($1,800) and mortgage statement breakdown ($1,000 principal + $300 interest + $500 escrow) are treated as two separate cash outflows, doubling the reported expense.
2. Credit card payments: card purchase ($500) and subsequent checking account settlement ($500) are both recorded as expenses, doubling reported operating costs.
3. Balance sheet changes: principal paydown (asset equity growth) is indistinguishable from interest (tax deductible expense).

## Decision
We implement a **Lightweight Double-Entry Accounting Ledger** with a standard Chart of Accounts (Assets, Liabilities, Equity, Income, Expenses). Every posted financial transaction creates a balanced `JournalEntry` consisting of multiple `JournalEntryLine` items where:
$$\sum \text{Debits} == \sum \text{Credits}$$

Single-entry imported bank transactions serve as evidence and clearing events that settle liabilities and balance sheet transfers, rather than being posted directly as independent expenses.

## Consequences
### Positive
* **Mathematical Invariant:** Zero phantom expenses or double-counting.
* **Separation of Balance Sheet vs. P&L:** Automatically distinguishes debt liability reduction (equity growth) from tax-deductible interest expenses.
* **Audit-Proof CPA Workpapers:** Financial reports conform to standard accounting principles, making CPA reviews seamless and defensible under IRS audit.

### Negative / Trade-Offs
* Requires more sophisticated domain logic when converting raw bank transactions into journal entries via automated rules.
