# ADR-005: Escrow Asset Accounting & Disbursement Tracking

* **Status:** Accepted
* **Date:** October 2026
* **Deciders:** Financial Systems Architect, CPA Consultant

## Context
Standard US mortgage payments bundle Principal, Interest, and an Escrow contribution into a single monthly sum. In PropTally V1, the escrow contribution was classified directly as an immediate operating expense.

This is economically and legally incorrect:
1. When the client pays escrow to the lender (Rocket, PNC, CrossCountry), the lender deposits the money into an escrow reserve account held in trust for the borrower.
2. The borrower retains beneficial ownership of the balance. If the loan is refinanced or paid off, any remaining escrow balance is refunded directly to the borrower.
3. The economic expense and tax-deductible event occurs **only when the loan servicer disburses funds** to the County Treasurer for real estate taxes or to the insurance carrier for hazard insurance.

Treating monthly contributions as expenses creates artificial monthly expense volatility and produces incorrect annual tax deductions.

## Decision
1. **Escrow Contributions are Asset Transfers:**
   Monthly escrow payments increase an asset account (`Asset: Servicer Escrow Reserve`) and decrease checking cash:
   $$\text{Debit: Escrow Reserve Asset} \quad | \quad \text{Credit: Cash}$$
2. **Escrow Disbursements are Expense Events:**
   When the servicer pays property taxes or insurance, the expense is recognized, and the escrow reserve asset is reduced:
   $$\text{Debit: Property Tax / Insurance Expense} \quad | \quad \text{Credit: Escrow Reserve Asset}$$
3. **Escrow Reconciliation:**
   The escrow reserve balance is periodically reconciled against the annual lender Escrow Analysis statement (e.g. `PNC Escrow Oct 2025.pdf`), capturing escrow shortages, surpluses, and adjustment payments.

## Consequences
### Positive
* **True Net Operating Income (NOI):** Operating expenses reflect actual taxes and insurance incurred, not arbitrary monthly servicer withholding.
* **Precise Tax Deductions:** Annual tax workpapers match exact county property tax disbursements, ensuring IRS compliance.
* **Escrow Shortage Visibility:** Proactively tracks projected escrow shortages before the lender imposes monthly payment hikes.

### Negative / Trade-Offs
* Requires extracting or recording annual escrow analysis and 1098 disbursements to reconcile the escrow reserve account over time.
