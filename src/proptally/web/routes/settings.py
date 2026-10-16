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

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
    TransactionProcessingState,
    VendorRule,
)
from proptally.db.tenant_manager import slugify
from proptally.domain.accounting.coa import (
    OWNER_EQUITY_CODE,
    ensure_entity_ledger_accounts,
)
from proptally.domain.accounting.ledger import (
    add_journal_line,
    create_journal_entry,
    post_journal_entry,
)
from proptally.domain.accounting.opening_balances import (
    OpeningAccountBalance,
    record_opening_balances,
)
from proptally.domain.transactions.suggest import seed_vendor_rules
from proptally.web.auth import AuthorizationError, current_context

#: Property classes the owner may choose between. A rental door is a Schedule E
#: asset and a primary residence is not, and the reporting engine treats them
#: differently everywhere, so the choice is explicit rather than defaulted.
PROPERTY_TYPE_OPTIONS = [
    {"value": "RENTAL_INVESTMENT", "label": "Rental investment property (Schedule E)"},
    {"value": "PRIMARY_RESIDENCE", "label": "Primary residence (non-operating)"},
]
PROPERTY_TYPES = tuple(option["value"] for option in PROPERTY_TYPE_OPTIONS)

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
    {"value": "REPAIRS", "label": "Repairs & maintenance (line 14)"},
    {"value": "SUPPLIES", "label": "Supplies (line 17)"},
    {"value": "UTILITIES", "label": "Utilities (line 19)"},
    {"value": "MANAGEMENT", "label": "Management fees (line 11)"},
    {"value": "COMMISSIONS", "label": "Commissions (line 8)"},
    {"value": "HOA", "label": "HOA assessments (line 19)"},
    {"value": "TAXES", "label": "Property taxes (line 16)"},
]
CATEGORIES = tuple(option["value"] for option in CATEGORY_OPTIONS)


class _FormError(ValueError):
    """A rejected submission, reported on the setup screen instead of as an error page."""


# --- Parsing ---------------------------------------------------------------

def _clean(value) -> str:
    return (value or "").strip()


def _cents(value, label: str) -> Optional[int]:
    """Form money ("1,234.56", "$1234.56") as integer cents; blank means "not set"."""
    text = _clean(value).replace("$", "").replace(",", "")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise _FormError(f"{label}: {value!r} is not a money amount.")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _day(value, label: str) -> Optional[date]:
    """Form date as ``date``; blank means "not set"."""
    text = _clean(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise _FormError(f"{label}: {value!r} is not a date.")


def _months(value, label: str) -> int:
    text = _clean(value)
    if not text:
        return 360
    try:
        months = int(text)
    except ValueError:
        raise _FormError(f"{label}: {value!r} is not a whole number of months.")
    if months <= 0:
        raise _FormError(f"{label} must be a positive number of months.")
    return months


def _rate_bps(value, label: str) -> Optional[int]:
    """A percentage as typed ("3.125") into the stored thousandths of a percent."""
    text = _clean(value).replace("%", "")
    if not text:
        return None
    try:
        rate = Decimal(text)
    except InvalidOperation:
        raise _FormError(f"{label}: {value!r} is not an interest rate.")
    if rate < 0:
        raise _FormError(f"{label} cannot be negative.")
    return int((rate * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _required(value, label: str) -> str:
    text = _clean(value)
    if not text:
        raise _FormError(f"{label} is required.")
    return text


def _choice(value, allowed: tuple, label: str) -> str:
    text = _clean(value)
    if text not in allowed:
        raise _FormError(f"{label} must be one of: {', '.join(allowed)}.")
    return text


def _optional_choice(value, allowed: tuple, label: str) -> Optional[str]:
    text = _clean(value)
    if not text:
        return None
    return _choice(text, allowed, label)


def _flag(value) -> bool:
    return _clean(value).lower() in {"true", "1", "yes", "on"}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:12]}"


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


def _post_loan_opening_balances(
    session: Session,
    loan: LoanAccount,
    principal_cents: Optional[int],
    escrow_cents: Optional[int],
) -> None:
    """Records a loan's opening principal and escrow reserve as one opening-balance entry.

    A loan declared after the portfolio's dated baseline cannot share that entry's date
    key, so it gets a key of its own, and the difference between the liability assumed
    and the escrow asset acquired goes to owner equity exactly as
    ``record_opening_balances`` plugs the difference for a whole portfolio.

    Both of the loan's opening figures belong here rather than in the dated baseline.
    Offering the same balance in two forms is how it gets counted twice: an owner who
    filled in their mortgage on this screen *and* listed the principal in the baseline
    would report twice the debt they owe.
    """
    principal_cents = principal_cents or 0
    escrow_cents = escrow_cents or 0
    if principal_cents <= 0 and escrow_cents <= 0:
        return

    entry = create_journal_entry(
        session,
        entry_date=loan.origination_date or date.today(),
        entry_type="OPENING_BALANCE",
        description=f"Opening balances declared for loan {loan.loan_number}",
        idempotency_key=f"MORTGAGE_OPENING:{loan.id}",
    )
    if principal_cents:
        add_journal_line(
            session,
            entry.id,
            loan.principal_ledger_code,
            credit_cents=principal_cents,
            property_id=loan.property_id,
        )
    if escrow_cents:
        add_journal_line(
            session,
            entry.id,
            loan.escrow_ledger_code,
            debit_cents=escrow_cents,
            property_id=loan.property_id,
        )

    difference = principal_cents - escrow_cents
    if difference > 0:
        add_journal_line(
            session, entry.id, OWNER_EQUITY_CODE,
            debit_cents=difference, description="Opening mortgage liability assumed",
        )
    elif difference < 0:
        add_journal_line(
            session, entry.id, OWNER_EQUITY_CODE,
            credit_cents=-difference, description="Opening escrow reserve acquired",
        )
    post_journal_entry(session, entry.id)


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
                "opening_targets": _opening_balance_targets(session),
                "opening_entry": opening_entry,
                "property_type_options": PROPERTY_TYPE_OPTIONS,
                "account_type_options": ACCOUNT_TYPE_OPTIONS,
                "mixed_use_options": MIXED_USE_OPTIONS,
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
            prop = _property_from_form(session, form, Property(
                id=_new_id("prop"), name="", address_line1="", city="", zip_code="",
            ))
            session.add(prop)
            session.flush()
            # The door's fixed-asset ledger account, allocated before any journal
            # line can reference it.
            ensure_entity_ledger_accounts(session)
            _record_valuation(session, prop, form)
            session.commit()
        except _FormError as exc:
            session.rollback()
            return _redirect(error=str(exc))
        return _redirect(message=f"Property {prop.name} created.")

    @router.post("/properties/{property_id}/edit", dependencies=mounted)
    async def edit_property(
        property_id: str, request: Request, session: Session = Depends(get_session_dep)
    ):
        _require_mutation()
        prop = _find_or_404(session, Property, property_id, "Property")
        form = await request.form()
        try:
            _property_from_form(session, form, prop)
            session.add(prop)
            session.flush()
            # A door created before this screen existed still needs its ledger account.
            ensure_entity_ledger_accounts(session)
            _record_valuation(session, prop, form)
            session.commit()
        except _FormError as exc:
            session.rollback()
            return _redirect(error=str(exc))
        return _redirect(message=f"Property {prop.name} updated.")

    def _property_from_form(session: Session, form, prop: Property) -> Property:
        """Applies the shared property fields. Raises before anything is written."""
        name = _required(form.get("name"), "Property name")
        address_line1 = _required(form.get("address_line1"), "Street address")
        city = _required(form.get("city"), "City")
        property_type = _choice(form.get("property_type"), PROPERTY_TYPES, "Property type")
        acquisition_date = _day(form.get("acquisition_date"), "Acquisition date")
        purchase_price_cents = _cents(form.get("purchase_price"), "Purchase price")
        land_basis_cents = _cents(form.get("land_basis"), "Land basis")
        building_basis_cents = _cents(form.get("building_basis"), "Building basis")
        # Valuation is parsed here too, so a bad value cannot be discovered after
        # the property has already been committed by the allocator.
        _cents(form.get("current_valuation"), "Current valuation")
        _day(form.get("valuation_date"), "Valuation date")

        prop.name = name
        prop.address_line1 = address_line1
        prop.address_line2 = _clean(form.get("address_line2")) or None
        prop.city = city
        prop.state = _clean(form.get("state")) or "IL"
        prop.zip_code = _clean(form.get("zip_code"))
        prop.property_type = property_type
        prop.acquisition_date = acquisition_date
        prop.purchase_price_cents = purchase_price_cents
        prop.land_basis_cents = land_basis_cents
        prop.building_basis_cents = building_basis_cents
        prop.short_label = _clean(form.get("short_label")) or None
        return prop

    def _record_valuation(session: Session, prop: Property, form) -> None:
        """Records the owner's stated current value as a manual valuation."""
        value_cents = _cents(form.get("current_valuation"), "Current valuation")
        if value_cents is None:
            return
        valuation_day = _day(form.get("valuation_date"), "Valuation date") or date.today()
        latest = session.exec(
            select(PropertyValuation)
            .where(PropertyValuation.property_id == prop.id)
            .order_by(PropertyValuation.valuation_date.desc())
        ).first()
        if latest is not None and latest.valuation_date == valuation_day:
            latest.estimated_value_cents = value_cents
            latest.valuation_source = "CLIENT_MANUAL"
            session.add(latest)
            return
        session.add(PropertyValuation(
            id=_new_id("val"),
            property_id=prop.id,
            valuation_date=valuation_day,
            estimated_value_cents=value_cents,
            valuation_source="CLIENT_MANUAL",
            notes="Owner-declared value from the portfolio setup screen",
        ))

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
            return _redirect(error=str(exc))
        return _redirect(message=f"Account {account.account_name} created.")

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
            return _redirect(error=str(exc))
        except PropTallyError as exc:
            session.rollback()
            return _redirect(error=str(exc))
        return _redirect(message=f"Mortgage loan {loan.loan_number} created.")

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
            return _redirect(error=str(exc))
        except PropTallyError as exc:
            session.rollback()
            return _redirect(error=str(exc))
        return _redirect(message=f"Opening balances recorded as at {as_of.isoformat()}.")

    # --- Vendor rules -----------------------------------------------------

    @router.post("/vendor-rules/create", dependencies=mounted)
    async def create_vendor_rule(request: Request, session: Session = Depends(get_session_dep)):
        """Adds one classification rule owned by this portfolio."""
        _require_mutation()
        form = await request.form()
        try:
            pattern = _required(form.get("pattern"), "Match pattern")
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
