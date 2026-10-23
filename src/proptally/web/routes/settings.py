"""Self-service portfolio setup: properties, accounts, loans, balances, rules.

Everything here writes to the signed-in portfolio's own database and nothing else.
The database session comes from the request's authenticated context, so no route
parameter can name another portfolio, and the shared authentication dependency has
already rejected an unauthenticated request, a missing CSRF token and a read-only
member before any handler runs.

Three rules are load-bearing:

* every entity the ledger must know about is allocated its own ledger account by
  ``ensure_entity_ledger_accounts`` immediately after it is added and flushed, so a
  journal line never has to guess a code;
* only the generic, portfolio-independent vendor rules are seeded from the engine
  (``DEFAULT_VENDOR_RULES``); a portfolio's own rules are rows it creates here, and
* opening balances go through the domain service, which is idempotent per date. A
  second submission for the same date is reported on the screen as a form error,
  because "already posted" describes the ledger rather than a failure.

Every handler parses and validates its whole form before it writes anything, so a
rejected submission leaves the portfolio exactly as it was.
"""

from typing import Optional
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from proptally.core.exceptions import PropTallyError
from proptally.db.models import (
    EscrowAccount,
    FinancialAccount,
    FinancialInstitution,
    JournalEntry,
    LedgerAccount,
    LoanAccount,
    Property,
    PropertyValuation,
    Statement,
    TransactionProcessingState,
    VendorRule,
)
from proptally.db.tenant_manager import slugify
from proptally.domain.accounting.coa import ensure_entity_ledger_accounts
from proptally.domain.accounting.opening_balances import (
    OpeningAccountBalance,
    record_opening_balances,
)
from proptally.domain.accounting.opening_balances import (
    post_loan_opening_balances as _post_loan_opening_balances,
)
from proptally.domain.transactions.suggest import DEFAULT_VENDOR_RULES
from proptally.web.auth import AuthorizationError, current_context
from proptally.web.form_parsing import (
    FormError as _FormError,
)
from proptally.web.form_parsing import (
    cents as _cents,
)
from proptally.web.form_parsing import (
    choice as _choice,
)
from proptally.web.form_parsing import (
    clean as _clean,
)
from proptally.web.form_parsing import (
    day as _day,
)
from proptally.web.form_parsing import (
    flag as _flag,
)
from proptally.web.form_parsing import (
    months as _months,
)
from proptally.web.form_parsing import (
    new_id as _new_id,
)
from proptally.web.form_parsing import (
    optional_choice as _optional_choice,
)
from proptally.web.form_parsing import (
    rate_bps as _rate_bps,
)
from proptally.web.form_parsing import (
    required as _required,
)
from proptally.web.property_forms import (
    PROPERTY_TYPE_OPTIONS,
    address_tuple,
    apply_property_location,
    property_from_form,
    record_property_valuation,
)

#: Account classes. A MORTGAGE account is the servicing front end of a loan: the
#: debt, clearing and escrow ledger accounts belong to the LoanAccount, so the
#: account row is deliberately left without a code of its own.
ACCOUNT_TYPE_OPTIONS = [
    {"value": "CHECKING", "label": "Checking (deposit account)"},
    {"value": "SAVINGS", "label": "Savings (deposit account)"},
    {"value": "CREDIT_CARD", "label": "Credit card (liability)"},
    {"value": "MORTGAGE", "label": "Mortgage servicing account (the loan carries the debt)"},
]
ACCOUNT_TYPES = tuple(option["value"] for option in ACCOUNT_TYPE_OPTIONS)
MORTGAGE_ACCOUNT_TYPE = "MORTGAGE"

#: Whether the owner should expect the rate to change. The rate itself is the
#: current_interest_rate_bps field on the loan.
LOAN_RATE_TYPE_OPTIONS = [
    {"value": "FIXED", "label": "Fixed rate"},
    {"value": "ARM", "label": "Adjustable rate (ARM)"},
]
LOAN_RATE_TYPES = tuple(option["value"] for option in LOAN_RATE_TYPE_OPTIONS)

MIXED_USE_OPTIONS = [
    {"value": "UNCLASSIFIED", "label": "Unclassified — usage unknown (the safe default)"},
    {"value": "DEDICATED_RENTAL", "label": "Dedicated rental account"},
    {"value": "MIXED_USE", "label": "Mixed use — business and personal spend"},
]
MIXED_USE_STATUSES = tuple(option["value"] for option in MIXED_USE_OPTIONS)

#: Classification vocabulary for an owner's own vendor rules, matching the review
#: queue so a rule created here behaves identically to one created there.
CATEGORY_OPTIONS = [
    {"value": "PERSONAL", "label": "Personal draw (non-operating)"},
    {"value": "RENT_INCOME", "label": "Rent income (Schedule E line 3)"},
    {"value": "SECURITY_DEPOSIT", "label": "Tenant security deposit (liability)"},
    {"value": "REPAIRS", "label": "Repairs & maintenance (line 14)"},
    {"value": "SUPPLIES", "label": "Supplies (line 15)"},
    {"value": "UTILITIES", "label": "Utilities (line 17)"},
    {"value": "MANAGEMENT", "label": "Management fees (line 11)"},
    {"value": "COMMISSIONS", "label": "Commissions (line 8)"},
    {"value": "HOA", "label": "HOA assessments (line 19)"},
    {"value": "TAXES", "label": "Property taxes (line 16)"},
]
CATEGORIES = tuple(option["value"] for option in CATEGORY_OPTIONS)


# --- Application helpers ---------------------------------------------------

def _require_mutation() -> None:
    """Writes need a mutating role.

    The shared dependency already refuses a READ_ONLY member on an unsafe method;
    this states the same rule at the point of use so the guarantee does not depend on
    how the router happens to be mounted.
    """
    context = current_context()
    if context is None or not context.can_work:
        raise AuthorizationError("This role may read the portfolio but not change it.")


def _redirect(**params: str) -> RedirectResponse:
    """Back to the setup screen, carrying at most one message."""
    query = urlencode({key: value for key, value in params.items() if value})
    return RedirectResponse(url=f"/settings?{query}" if query else "/settings", status_code=303)


def _return_target(form) -> str:
    """Where a submitted setup form goes back to.

    The guided wizard sets ``return_to=/setup``; every other caller gets the
    management screen. Only paths under /setup are accepted, so a crafted form
    cannot turn this into an open redirect.
    """
    target = _clean(form.get("return_to"))
    if target == "/setup" or target.startswith("/setup/"):
        return target
    return "/settings"


def _redirect_for(form, **params: str) -> RedirectResponse:
    """Redirect to the form's return target, carrying at most one message."""
    target = _return_target(form)
    query = urlencode({key: value for key, value in params.items() if value})
    return RedirectResponse(url=f"{target}?{query}" if query else target, status_code=303)


def _find_or_404(session: Session, model, entity_id: str, label: str):
    record = session.get(model, entity_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"{label} {entity_id} not found")
    return record


def _institution_for(session: Session, name: str) -> FinancialInstitution:
    """The institution with this name, created on first use.

    Matching on name is case-insensitive so an owner who types "pnc bank" twice
    lands on the one institution rather than creating a second.
    """
    cleaned = _required(name, "Institution name")
    for institution in session.exec(select(FinancialInstitution)).all():
        if institution.name.strip().lower() == cleaned.lower():
            return institution

    institution_id = f"inst_{slugify(cleaned)}"
    if session.get(FinancialInstitution, institution_id) is not None:
        institution_id = _new_id("inst")
    institution = FinancialInstitution(id=institution_id, name=cleaned)
    session.add(institution)
    session.flush()
    return institution


def _opening_balance_targets(session: Session) -> list[dict]:
    """Every ledger account an opening balance may be recorded against.

    A property, a deposit account and a card each carry their ledger code on their own
    row; a loan's principal and escrow codes live on the loan, because the debt and
    the servicer's reserve are the loan's accounts rather than the servicing account's.
    The side is read from the ledger account's own normal balance, so the form never
    has to ask the owner which way a balance points.
    """
    normal = {a.code: a.normal_balance for a in session.exec(select(LedgerAccount)).all()}
    targets: list[dict] = []

    # A door's own cost basis belongs in the baseline too: without it the portfolio's
    # largest asset would be absent from the opening balance entry.
    for prop in session.exec(select(Property).order_by(Property.created_at)).all():
        if prop.asset_ledger_code is None:
            continue
        targets.append({
            "field": f"asset_{prop.id}",
            "label": f"Property — {prop.name} (cost basis)",
            "side": "DEBIT",
            "code": prop.asset_ledger_code,
            "property_id": prop.id,
        })

    for account in session.exec(select(FinancialAccount).order_by(FinancialAccount.created_at)).all():
        if account.ledger_account_code is None:
            # Mortgage servicing accounts hold no code of their own; the loan does.
            continue
        targets.append({
            "field": f"amount_{account.id}",
            "label": f"{account.account_name} ({account.account_number_masked})",
            "side": "DEBIT" if normal.get(account.ledger_account_code) == "DEBIT" else "CREDIT",
            "code": account.ledger_account_code,
            "property_id": None,
        })

    # A loan's principal and escrow reserve are deliberately absent from this dated
    # baseline: they are stated on the loan itself (see _post_loan_opening_balances),
    # and a figure with two homes is a figure that can be counted twice.
    return targets


def opening_balance_suggestions(session: Session, targets: list[dict]) -> dict[str, dict]:
    """Balances already stated by this portfolio's imported statements.

    The opening entry is one dated snapshot, so nothing here is applied
    automatically: each suggestion carries the date its figure belongs to, and the
    form offers it per account. A property's cost basis is not on a statement, so
    only account targets produce suggestions.
    """
    suggestions: dict[str, dict] = {}
    for target in targets:
        field = target.get("field", "")
        if not field.startswith("amount_"):
            continue
        account_id = field.removeprefix("amount_")
        statements = session.exec(
            select(Statement)
            .where(
                Statement.financial_account_id == account_id,
                Statement.ingestion_status == "COMMITTED",
            )
            .order_by(Statement.statement_date)
        ).all()
        if not statements:
            continue
        first = statements[0]
        if first.opening_balance_cents is None:
            continue
        latest = statements[-1]
        suggestions[field] = {
            "field": field,
            "label": target["label"],
            "amount": f"{first.opening_balance_cents // 100}.{first.opening_balance_cents % 100:02d}",
            "as_of": first.period_start_date or first.statement_date,
            "latest_closing_cents": latest.closing_balance_cents,
            "latest_closing_date": latest.statement_date,
            "statement_count": len(statements),
        }
    return suggestions


# --- Router ----------------------------------------------------------------

def get_settings_router(templates, get_session_dep, auth_dep=None):
    """Builds the portfolio-setup router.

    ``auth_dep`` is optional: the app mounts this router behind the shared
    authentication dependency. Passing the dependency here as well makes the router
    safe to mount on its own, because every route then carries the same checks.
    """
    router = APIRouter()
    mounted = [Depends(auth_dep)] if auth_dep is not None else []

    @router.get("/settings", response_class=HTMLResponse, dependencies=mounted)
    def view_settings(request: Request, session: Session = Depends(get_session_dep)):
        context = current_context()
        properties = session.exec(select(Property).order_by(Property.created_at)).all()
        institutions = {i.id: i.name for i in session.exec(select(FinancialInstitution)).all()}

        accounts = [
            {
                "account": account,
                "institution_name": institutions.get(account.institution_id, account.institution_id),
            }
            for account in session.exec(
                select(FinancialAccount).order_by(FinancialAccount.created_at)
            ).all()
        ]

        escrows = {e.loan_account_id: e for e in session.exec(select(EscrowAccount)).all()}
        loans = []
        for loan in session.exec(select(LoanAccount).order_by(LoanAccount.created_at)).all():
            loan_account = session.get(FinancialAccount, loan.financial_account_id)
            loans.append({
                "loan": loan,
                "account": loan_account,
                "institution_name": (
                    institutions.get(loan_account.institution_id, "") if loan_account else ""
                ),
                "property": session.get(Property, loan.property_id),
                "escrow": escrows.get(loan.id),
            })

        vendor_rules = session.exec(select(VendorRule).order_by(VendorRule.created_at)).all()

        # Latest valuation per door, for prefilling the edit form. Walking the rows in
        # date order leaves the newest one in the map.
        property_valuations: dict[str, PropertyValuation] = {}
        for valuation in session.exec(
            select(PropertyValuation).order_by(PropertyValuation.valuation_date)
        ).all():
            property_valuations[valuation.property_id] = valuation

        opening_entry = session.exec(
            select(JournalEntry)
            .where(JournalEntry.entry_type == "OPENING_BALANCE")
            .order_by(JournalEntry.entry_date.desc())
        ).first()

        review_count = len(session.exec(
            select(TransactionProcessingState).where(
                TransactionProcessingState.allocation_status != "FULLY_ALLOCATED"
            )
        ).all())

        opening_targets = _opening_balance_targets(session)

        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                "active_page": "settings",
                "can_work": bool(context and context.can_work),
                "properties": properties,
                "property_valuations": property_valuations,
                "accounts": accounts,
                "loans": loans,
                "vendor_rules": vendor_rules,
                "institution_names": sorted(set(institutions.values())),
                "opening_targets": opening_targets,
                "opening_suggestions": opening_balance_suggestions(session, opening_targets),
                "opening_entry": opening_entry,
                "property_type_options": PROPERTY_TYPE_OPTIONS,
                "account_type_options": ACCOUNT_TYPE_OPTIONS,
                "mixed_use_options": MIXED_USE_OPTIONS,
                "loan_rate_type_options": LOAN_RATE_TYPE_OPTIONS,
                "category_options": CATEGORY_OPTIONS,
                "review_count": review_count,
                "message": request.query_params.get("message"),
                "error": request.query_params.get("error"),
            },
        )

    # --- Properties -------------------------------------------------------

    @router.post("/properties/create", dependencies=mounted)
    async def create_property(request: Request, session: Session = Depends(get_session_dep)):
        _require_mutation()
        form = await request.form()
        try:
            prop = Property(
                id=_new_id("prop"), name="", address_line1="", city="", zip_code="",
            )
            property_from_form(session, form, prop)
            apply_property_location(prop, form)
            session.add(prop)
            session.flush()
            # The door's fixed-asset ledger account, allocated before any journal
            # line can reference it.
            ensure_entity_ledger_accounts(session)
            record_property_valuation(session, prop, form)
            session.commit()
        except _FormError as exc:
            session.rollback()
            return _redirect_for(form, error=str(exc))
        return _redirect_for(form, message=f"Property {prop.name} created.")

    @router.post("/properties/{property_id}/edit", dependencies=mounted)
    async def edit_property(
        property_id: str, request: Request, session: Session = Depends(get_session_dep)
    ):
        _require_mutation()
        prop = _find_or_404(session, Property, property_id, "Property")
        previous_address = address_tuple(prop)
        previous_place_id = prop.google_place_id
        form = await request.form()
        try:
            property_from_form(session, form, prop)
            apply_property_location(
                prop,
                form,
                previous_address=previous_address,
                previous_place_id=previous_place_id,
            )
            session.add(prop)
            session.flush()
            # A door created before this screen existed still needs its ledger account.
            ensure_entity_ledger_accounts(session)
            record_property_valuation(session, prop, form)
            session.commit()
        except _FormError as exc:
            session.rollback()
            return _redirect(error=str(exc))
        return _redirect(message=f"Property {prop.name} updated.")

    # --- Accounts ---------------------------------------------------------

    @router.post("/accounts/create", dependencies=mounted)
    async def create_account(request: Request, session: Session = Depends(get_session_dep)):
        _require_mutation()
        form = await request.form()
        try:
            account = _account_from_form(session, form, FinancialAccount(
                id=_new_id("acct"),
                institution_id="",
                account_name="",
                account_type="CHECKING",
                account_number_masked="",
            ))
            session.add(account)
            session.flush()
            ensure_entity_ledger_accounts(session)
            session.commit()
        except _FormError as exc:
            session.rollback()
            return _redirect_for(form, error=str(exc))
        return _redirect_for(form, message=f"Account {account.account_name} created.")

    @router.post("/accounts/{account_id}/edit", dependencies=mounted)
    async def edit_account(
        account_id: str, request: Request, session: Session = Depends(get_session_dep)
    ):
        _require_mutation()
        account = _find_or_404(session, FinancialAccount, account_id, "Account")
        form = await request.form()
        try:
            _account_from_form(session, form, account)
            session.add(account)
            session.flush()
            # Allocates the account's own code, and reallocates in the right class if
            # its type moved between a deposit account and a card.
            ensure_entity_ledger_accounts(session)
            session.commit()
        except _FormError as exc:
            session.rollback()
            return _redirect(error=str(exc))
        return _redirect(message=f"Account {account.account_name} updated.")

    def _account_from_form(session: Session, form, account: FinancialAccount) -> FinancialAccount:
        """Applies the shared account fields. Raises before anything is committed."""
        account_name = _required(form.get("account_name"), "Account name")
        account_type = _choice(form.get("account_type"), ACCOUNT_TYPES, "Account type")
        masked = _clean(form.get("account_number_masked"))
        mixed_use_status = _optional_choice(
            form.get("mixed_use_status"), MIXED_USE_STATUSES, "Mixed-use status"
        ) or "UNCLASSIFIED"
        institution = _institution_for(session, form.get("institution_name"))

        # Two rows for the same account double-count cash or debt. A masked number
        # is the strongest key; when neither row has one, the account name is the
        # only available key, and a duplicate name at the same institution and type
        # is refused rather than silently creating a second "Checking".
        for other in session.exec(
            select(FinancialAccount).where(
                FinancialAccount.id != account.id,
                FinancialAccount.institution_id == institution.id,
                FinancialAccount.account_type == account_type,
            )
        ).all():
            other_masked = _clean(other.account_number_masked)
            if masked and other_masked:
                if masked.casefold() == other_masked.casefold():
                    raise _FormError(
                        f"An account with this number already exists at {institution.name}."
                    )
            elif not masked and not other_masked:
                if account_name.casefold() == other.account_name.casefold():
                    raise _FormError(
                        f"An account named {account_name} already exists at "
                        f"{institution.name}. Add the masked number to distinguish them."
                    )

        account.institution_id = institution.id
        account.account_name = account_name
        if account_type != account.account_type:
            # The current code belongs to the previous class (a deposit account's
            # code is an asset, a card's is a liability), so it is dropped and
            # reallocated in the right class.
            account.ledger_account_code = None
        if account_type == MORTGAGE_ACCOUNT_TYPE:
            # The loan carries the debt, clearing and escrow accounts; the servicing
            # account row never holds one of its own.
            account.ledger_account_code = None
        account.account_type = account_type
        account.account_number_masked = masked
        account.mixed_use_status = mixed_use_status
        return account

    # --- Mortgage loans ---------------------------------------------------

    def _loan_from_form(
        session: Session,
        form,
        loan: LoanAccount,
        account: FinancialAccount,
        escrow_cents: Optional[int],
    ) -> None:
        """Applies the loan's fields and, when declared, its escrow reserve."""
        prop = _find_or_404(
            session, Property, _required(form.get("property_id"), "Property"), "Property"
        )
        institution = _institution_for(session, form.get("institution_name"))
        account_name = _required(form.get("account_name"), "Account name")
        loan_number = _required(form.get("loan_number"), "Loan number")
        clash = session.exec(
            select(LoanAccount).where(
                LoanAccount.loan_number == loan_number, LoanAccount.id != loan.id
            )
        ).first()
        if clash is not None:
            raise _FormError(f"Loan number {loan_number} is already registered in this portfolio.")
        origination_date = _day(form.get("origination_date"), "Origination date")
        original_principal_cents = _cents(form.get("original_principal"), "Original principal")
        term_months = _months(form.get("term_months"), "Loan term")
        rate_type = _choice(form.get("rate_type") or "FIXED", LOAN_RATE_TYPES, "Rate type")
        rate_bps = _rate_bps(form.get("interest_rate"), "Interest rate")

        account.institution_id = institution.id
        account.account_name = account_name
        account.account_number_masked = _clean(form.get("account_number_masked")) or (
            f"...{loan_number[-4:]}" if len(loan_number) >= 4 else loan_number
        )
        loan.property_id = prop.id
        loan.loan_number = loan_number
        loan.origination_date = origination_date
        loan.original_principal_cents = original_principal_cents
        loan.term_months = term_months
        loan.rate_type = rate_type
        loan.current_interest_rate_bps = rate_bps
        session.add(account)
        session.add(loan)

        if escrow_cents is not None:
            escrow = session.exec(
                select(EscrowAccount).where(EscrowAccount.loan_account_id == loan.id)
            ).first()
            if escrow is None:
                escrow = EscrowAccount(
                    id=_new_id("escrow"), loan_account_id=loan.id, opening_balance_cents=escrow_cents
                )
            else:
                escrow.opening_balance_cents = escrow_cents
            session.add(escrow)

    @router.post("/mortgages/create", dependencies=mounted)
    async def create_mortgage(request: Request, session: Session = Depends(get_session_dep)):
        _require_mutation()
        form = await request.form()
        try:
            escrow_cents = _cents(form.get("escrow_opening_balance"), "Escrow opening balance")
            opening_principal_cents = _cents(form.get("opening_principal"), "Opening principal")
            loan = LoanAccount(
                id=_new_id("loan"),
                financial_account_id=_new_id("acct"),
                property_id="",
                loan_number="",
            )
            servicing_account = FinancialAccount(
                id=loan.financial_account_id,
                institution_id="",
                account_name="",
                account_type=MORTGAGE_ACCOUNT_TYPE,
                account_number_masked="",
                mixed_use_status="UNCLASSIFIED",
            )
            # Both rows are written by _loan_from_form, which resolves the institution
            # before either is added to the session: a pending account still carrying
            # an empty institution would be autoflushed by that lookup and rejected.
            _loan_from_form(session, form, loan, servicing_account, escrow_cents)
            session.flush()
            # Principal, clearing and escrow codes for this loan, allocated as a set.
            ensure_entity_ledger_accounts(session)
            _post_loan_opening_balances(session, loan, opening_principal_cents, escrow_cents)
            session.commit()
        except _FormError as exc:
            session.rollback()
            return _redirect_for(form, error=str(exc))
        except PropTallyError as exc:
            session.rollback()
            return _redirect_for(form, error=str(exc))
        return _redirect_for(form, message=f"Mortgage loan {loan.loan_number} created.")

    @router.post("/mortgages/{loan_id}/edit", dependencies=mounted)
    async def edit_mortgage(
        loan_id: str, request: Request, session: Session = Depends(get_session_dep)
    ):
        _require_mutation()
        loan = _find_or_404(session, LoanAccount, loan_id, "Loan")
        account = _find_or_404(
            session, FinancialAccount, loan.financial_account_id, "Account"
        )
        form = await request.form()
        try:
            escrow_cents = _cents(form.get("escrow_opening_balance"), "Escrow opening balance")
            _loan_from_form(session, form, loan, account, escrow_cents)
            session.flush()
            ensure_entity_ledger_accounts(session)
            session.commit()
        except _FormError as exc:
            session.rollback()
            return _redirect(error=str(exc))
        except PropTallyError as exc:
            session.rollback()
            return _redirect(error=str(exc))
        return _redirect(message=f"Mortgage loan {loan.loan_number} updated.")

    # --- Opening balances -------------------------------------------------

    @router.post("/opening-balances", dependencies=mounted)
    async def record_opening_balances_route(
        request: Request, session: Session = Depends(get_session_dep)
    ):
        """Records one dated opening-balance entry for the whole portfolio.

        ``record_opening_balances`` is idempotent by date, so a second submission for
        a date already posted comes back as a message on the setup screen rather than
        a failed request.
        """
        _require_mutation()
        form = await request.form()
        try:
            as_of = _day(form.get("as_of"), "Opening balance date")
            if as_of is None:
                raise _FormError("An opening balance date is required.")

            balances: list[OpeningAccountBalance] = []
            for target in _opening_balance_targets(session):
                amount_cents = _cents(form.get(target["field"]), target["label"])
                if amount_cents is None:
                    continue
                if amount_cents <= 0:
                    raise _FormError(
                        f"{target['label']}: enter a positive amount. Which side of the ledger "
                        "the balance sits on is decided by the account type, not by the sign."
                    )
                balances.append(OpeningAccountBalance(
                    account_code=target["code"],
                    amount_cents=amount_cents,
                    side=target["side"],
                    property_id=target.get("property_id"),
                    description=f"Opening {target['label']}",
                ))
            if not balances:
                raise _FormError("Enter at least one account balance to record.")

            record_opening_balances(
                session,
                as_of,
                balances,
                description=f"Portfolio opening balances recorded {as_of.isoformat()}",
            )
        except _FormError as exc:
            session.rollback()
            return _redirect_for(form, error=str(exc))
        except PropTallyError as exc:
            session.rollback()
            return _redirect_for(form, error=str(exc))
        return _redirect_for(form, message=f"Opening balances recorded as at {as_of.isoformat()}.")

    # --- Vendor rules -----------------------------------------------------

    @router.post("/vendor-rules/create", dependencies=mounted)
    async def create_vendor_rule(request: Request, session: Session = Depends(get_session_dep)):
        """Adds one classification rule owned by this portfolio."""
        _require_mutation()
        form = await request.form()
        try:
            pattern = _required(form.get("pattern"), "Match pattern")
            # A seeded default may be overridden once by the owner's own rule; a
            # second owner-defined rule with the same pattern is a duplicate.
            default_ids = {rule["id"] for rule in DEFAULT_VENDOR_RULES}
            for existing in session.exec(select(VendorRule)).all():
                if existing.id in default_ids:
                    continue
                if existing.pattern.strip().casefold() == pattern.casefold():
                    raise _FormError(
                        f"A rule matching {pattern!r} already exists in this portfolio."
                    )
            property_id = _clean(form.get("default_property_id")) or None
            if property_id is not None and session.get(Property, property_id) is None:
                raise _FormError("The rule's target property is not in this portfolio.")
            category = _choice(form.get("default_category"), CATEGORIES, "Default category")
            rule = VendorRule(
                id=f"vrule_{slugify(pattern)[:20]}_{uuid4().hex[:6]}",
                pattern=pattern,
                vendor_name=_clean(form.get("vendor_name")) or pattern,
                service_type=_clean(form.get("service_type")) or "Owner-defined rule",
                default_category=category,
                default_property_id=property_id,
                requires_client_confirmation=_flag(form.get("requires_client_confirmation")),
                requires_cpa_review=_flag(form.get("requires_cpa_review")),
                is_active=True,
            )
            session.add(rule)
            session.commit()
        except _FormError as exc:
            session.rollback()
            return _redirect(error=str(exc))
        return _redirect(message=f"Vendor rule {rule.pattern} created.")

    @router.post("/vendor-rules/{rule_id}/toggle", dependencies=mounted)
    def toggle_vendor_rule(rule_id: str, session: Session = Depends(get_session_dep)):
        _require_mutation()
        rule = _find_or_404(session, VendorRule, rule_id, "Vendor rule")
        rule.is_active = not rule.is_active
        session.add(rule)
        session.commit()
        state = "active" if rule.is_active else "paused"
        return _redirect(message=f"Vendor rule {rule.pattern} is now {state}.")

    return router
