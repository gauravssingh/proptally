"""Mortgages and Escrow web router with strict posted-balance integrity."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from proptally.db.models import (
    EscrowAccount,
    EscrowMovement,
    FinancialAccount,
    FinancialInstitution,
    JournalEntry,
    JournalEntryLine,
    LoanAccount,
    MortgagePeriod,
    Property,
    TransactionProcessingState,
)


def get_mortgages_router(templates, get_session_dep):
    router = APIRouter()

    @router.get("/mortgages", response_class=HTMLResponse)
    def view_mortgages(request: Request, session: Session = Depends(get_session_dep)):
        loans = session.exec(select(LoanAccount).where(LoanAccount.is_active == True)).all()
        loans_data = []

        account_code_map = {
            "loan_rocket_1000000001": 2110,
            "loan_pnc_1000000002": 2120,
            "loan_ccm_1000000003": 2130,
        }

        clearing_code_map = {
            "loan_rocket_1000000001": 2115,
            "loan_pnc_1000000002": 2125,
            "loan_ccm_1000000003": 2135,
        }

        for loan in loans:
            prop = session.get(Property, loan.property_id)
            fin_acct = session.get(FinancialAccount, loan.financial_account_id)
            inst = session.get(FinancialInstitution, fin_acct.institution_id) if fin_acct else None

            # 1. Ledger-Calculated Principal from strictly POSTED journal entry lines
            code = account_code_map.get(loan.id)
            ledger_calculated_principal_cents = None
            if code:
                posted_lines = session.exec(
                    select(JournalEntryLine)
                    .join(JournalEntry, JournalEntryLine.journal_entry_id == JournalEntry.id)
                    .where(JournalEntry.status == "POSTED")
                    .where(JournalEntryLine.ledger_account_code == code)
                ).all()
                if posted_lines:
                    ledger_calculated_principal_cents = max(
                        0, sum(l.credit_cents for l in posted_lines) - sum(l.debit_cents for l in posted_lines)
                    )

            # 2. Servicer-Reported Principal from authoritative statement periods
            latest_period = session.exec(
                select(MortgagePeriod)
                .where(MortgagePeriod.loan_account_id == loan.id)
                .order_by(MortgagePeriod.statement_date.desc())
            ).first()
            servicer_reported_principal_cents = (
                latest_period.outstanding_principal_cents
                if latest_period and latest_period.outstanding_principal_cents is not None
                else None
            )

            # Never substitute the original loan amount for a current balance.
            if servicer_reported_principal_cents is not None:
                authoritative_principal_cents = servicer_reported_principal_cents
                principal_source = "SERVICER_REPORTED"
            elif ledger_calculated_principal_cents is not None:
                authoritative_principal_cents = ledger_calculated_principal_cents
                principal_source = "LEDGER_DERIVED"
            else:
                authoritative_principal_cents = None
                principal_source = "UNAVAILABLE"

            # 3. Outstanding Clearing Balance (In-flight bank settlement vs confirmed application)
            clr_code = clearing_code_map.get(loan.id)
            outstanding_clearing_balance_cents = 0
            if clr_code:
                clr_lines = session.exec(
                    select(JournalEntryLine)
                    .join(JournalEntry, JournalEntryLine.journal_entry_id == JournalEntry.id)
                    .where(JournalEntry.status == "POSTED")
                    .where(JournalEntryLine.ledger_account_code == clr_code)
                ).all()
                outstanding_clearing_balance_cents = sum(l.debit_cents for l in clr_lines) - sum(l.credit_cents for l in clr_lines)

            loans_data.append({
                "loan": loan,
                "property": prop,
                "institution_name": inst.name if inst else "Lender",
                "authoritative_principal_cents": authoritative_principal_cents,
                "principal_source": principal_source,
                "current_balance_cents": authoritative_principal_cents,
                "ledger_calculated_principal_cents": ledger_calculated_principal_cents,
                "servicer_reported_principal_cents": servicer_reported_principal_cents,
                "outstanding_clearing_balance_cents": outstanding_clearing_balance_cents,
            })

        escrows = session.exec(select(EscrowAccount)).all()
        escrow_data = []

        for e in escrows:
            loan = session.get(LoanAccount, e.loan_account_id)
            prop = session.get(Property, loan.property_id) if loan else None
            fin_acct = session.get(FinancialAccount, loan.financial_account_id) if loan else None
            inst = session.get(FinancialInstitution, fin_acct.institution_id) if fin_acct else None

            # 4. Running balance from confirmed escrow movements
            movements = session.exec(
                select(EscrowMovement)
                .where(EscrowMovement.escrow_account_id == e.id)
                .order_by(EscrowMovement.movement_date.desc())
            ).all()
            if movements:
                calculated_escrow_balance_cents = movements[0].running_calculated_balance_cents
            else:
                calculated_escrow_balance_cents = e.opening_balance_cents

            # 5. Authoritative statement reported escrow balance
            loan_period = session.exec(
                select(MortgagePeriod)
                .where(MortgagePeriod.loan_account_id == e.loan_account_id)
                .order_by(MortgagePeriod.statement_date.desc())
            ).first()
            reported_escrow_balance_cents = (
                loan_period.reported_escrow_balance_cents
                if loan_period and loan_period.reported_escrow_balance_cents is not None
                else None
            )

            # 6. Projected Escrow Balance & Annual Requirements
            projected_escrow_requirement_cents = e.projected_annual_tax_cents + e.projected_annual_insurance_cents

            escrow_data.append({
                "escrow": e,
                "servicer_name": inst.name if inst else "Servicer",
                "property_name": prop.name if prop else "Unknown Door",
                "current_balance_cents": calculated_escrow_balance_cents,
                "calculated_escrow_balance_cents": calculated_escrow_balance_cents,
                "reported_escrow_balance_cents": reported_escrow_balance_cents,
                "projected_escrow_requirement_cents": projected_escrow_requirement_cents,
                "projected_tax_cents": e.projected_annual_tax_cents,
                "projected_ins_cents": e.projected_annual_insurance_cents,
                "projected_shortage_cents": e.projected_shortage_cents,
            })

        unallocated_states = session.exec(
            select(TransactionProcessingState).where(
                TransactionProcessingState.allocation_status != "FULLY_ALLOCATED"
            )
        ).all()
        review_count = len(unallocated_states)

        return templates.TemplateResponse(
            request=request,
            name="mortgages.html",
            context={
                "active_page": "mortgages",
                "loans_data": loans_data,
                "escrow_data": escrow_data,
                "review_count": review_count,
            },
        )

    return router
