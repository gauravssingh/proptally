# ADR-004: Immutable Transactions & Transaction Splitting

* **Status:** Accepted (Updated V2.2)
* **Date:** October 2026
* **Deciders:** Financial Systems Architect, Database Architect

## Context
In real estate operations, single financial transactions frequently span multiple properties, multiple tax categories, and personal vs. business allocations. For example, a $500.00 purchase at Home Depot on the Bank of America Visa may include:
- $250.00 in repairs for 12307 Larkspur St
- $150.00 in paint supplies for 2718 Foxglove Ct
- $100.00 in personal household garden supplies

If the system modifies or overwrites the imported bank transaction to fit these allocations, the audit trail is broken, and reconciliation against the original bank statement becomes impossible.

Furthermore, bank and card transactions are imported with signed amounts (negative for debit/outflows, positive for deposits/inflows). Earlier drafts stated an invariant where split amounts equaled the parent amount directly, which contradicted negative parent expense values.

## Decision
1. **Immutable Parent Transactions:**
   Imported transactions in the `transactions` table are **strictly immutable**. Raw dates, descriptions, reference numbers, and original imported amounts are never edited or overwritten.
   - Parent transactions store signed integer cents (`amount_cents`), where debits/outflows are negative and credits/inflows are positive.
   - An explicit direction enum is maintained: `direction` (`INFLOW` | `OUTFLOW`).
2. **Positive Split Allocations with Absolute Invariant:**
   We implement a **1-to-many Transaction Splitting Architecture**:
   - Each transaction can have one or more child records in the `transaction_splits` table.
   - Child split records store strictly **positive integer cents** (`amount_cents > 0`).
   - The child split inherits the transaction's `direction` from the parent.
   - **Enforced Invariant:**
     $$\sum \text{split.amount\_cents} == |\text{parent\_transaction.amount\_cents}|$$
   - A transaction cannot be marked `FULLY_ALLOCATED` or generate finalized property ledger entries while any unassigned residual exists ($|\text{parent}| - \sum \text{splits} > 0$).

## Consequences
### Positive
* **Mathematical Consistency:** Eliminates sign conflicts between negative banking expenses and positive allocation values.
* **Full Audit Integrity:** Original statement figures remain 100% auditable and cross-referenced to the PDF.
* **Granular Multi-Property Allocation:** Accurately attributes expenses to the exact property door where they were incurred.
* **Clean Personal Expense Isolation:** Personal draws are cleanly separated from business deductions, preventing IRS audit vulnerabilities.

### Negative / Trade-Offs
* Queries calculating property expenses must aggregate across `transaction_splits` rather than directly querying `transactions`. (Solved cleanly via SQL views and domain repository methods).
