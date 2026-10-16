"""Automated transaction classification and vendor service suggestion engine."""

from dataclasses import dataclass
from typing import List, Optional
from sqlmodel import Session, select

from proptally.db.models import VendorRule


@dataclass
class TransactionSuggestion:
    property_id: Optional[str]
    category: str
    confidence: float
    rule_name: str
    requires_client_confirmation: bool = False
    requires_cpa_review: bool = False
    vendor_name: Optional[str] = None
    service_type: Optional[str] = None
    rule_id: Optional[str] = None


DEFAULT_VENDOR_RULES = [
    {
        "id": "vrule_jnl_climate",
        "pattern": "JNL CLIMATE",
        "vendor_name": "JNL Climate Control",
        "service_type": "HVAC Servicing, Heating & AC System Maintenance",
        "default_category": "REPAIRS",
        "default_property_id": None,
        "confidence": 0.92,
        "requires_client_confirmation": False,
        "requires_cpa_review": True,
    },
    {
            "id": "vrule_hertz",
            "pattern": "HERTZ",
            "vendor_name": "Hertz Rent-A-Car",
            "service_type": "Vehicle Rental / Travel & Transportation",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.95,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_parkwhiz",
            "pattern": "PARKWHIZ",
            "vendor_name": "ParkWhiz Parking",
            "service_type": "Parking Facility Reservations & Transit",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.95,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_uber",
            "pattern": "UBER",
            "vendor_name": "Uber Technologies",
            "service_type": "Rideshare Ground Transportation",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.95,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_american_taxi",
            "pattern": "AMERICAN TAXI",
            "vendor_name": "American Taxi Dispatch",
            "service_type": "Local Taxi & Airport Transit",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.95,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_apple_bill",
            "pattern": "APPLE.COM",
            "vendor_name": "Apple Services & App Store",
            "service_type": "Digital Subscriptions & Cloud Storage (iCloud/Apple One)",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.98,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_applecard",
            "pattern": "APPLECARD",
            "vendor_name": "Apple Card (Goldman Sachs)",
            "service_type": "Credit Card Statement Settlement Payment",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.98,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_amazon",
            "pattern": "AMAZON",
            "vendor_name": "Amazon Marketplace & Retail",
            "service_type": "E-Commerce Orders & Household Supplies",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.85,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_google_workspace",
            "pattern": "GOOGLE",
            "vendor_name": "Google Cloud & Workspace",
            "service_type": "Cloud Productivity & Business Software",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.90,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_anthropic",
            "pattern": "ANTHROPIC",
            "vendor_name": "Anthropic AI",
            "service_type": "AI Compute & Software Subscription",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.90,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_netflix",
            "pattern": "NETFLIX",
            "vendor_name": "Netflix",
            "service_type": "Entertainment Video Streaming Subscription",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.98,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_zoom",
            "pattern": "ZOOM.COM",
            "vendor_name": "Zoom Video Communications",
            "service_type": "Video Conferencing Software",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.95,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_menards",
            "pattern": "MENARDS",
            "vendor_name": "Menards Home Improvement",
            "service_type": "Building Materials, Hardware & Repair Supplies",
            "default_category": "REPAIRS",
            "default_property_id": None,
            "confidence": 0.85,
            "requires_client_confirmation": False,
            "requires_cpa_review": True,
        },
    {
            "id": "vrule_home_depot",
            "pattern": "HOME DEPOT",
            "vendor_name": "The Home Depot",
            "service_type": "Hardware, Plumbing & Electrical Maintenance Supplies",
            "default_category": "REPAIRS",
            "default_property_id": None,
            "confidence": 0.85,
            "requires_client_confirmation": False,
            "requires_cpa_review": True,
        },
    {
            "id": "vrule_sherwin_williams",
            "pattern": "SHERWIN-WILLIAMS",
            "vendor_name": "Sherwin-Williams Paint",
            "service_type": "Paint & Wall Coating Materials",
            "default_category": "REPAIRS",
            "default_property_id": None,
            "confidence": 0.90,
            "requires_client_confirmation": False,
            "requires_cpa_review": True,
        },
    {
            "id": "vrule_nicor",
            "pattern": "NICOR",
            "vendor_name": "Nicor Gas Utility",
            "service_type": "Natural Gas Heating Utility (Schedule E Line 17)",
            "default_category": "UTILITIES",
            "default_property_id": None,
            "confidence": 0.90,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_comed",
            "pattern": "COMED",
            "vendor_name": "ComEd Electric Utility",
            "service_type": "Electric Power Utility (Schedule E Line 17)",
            "default_category": "UTILITIES",
            "default_property_id": None,
            "confidence": 0.90,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_regency_hoa",
            "pattern": "REGENCY SQUARE",
            "vendor_name": "Regency Square HOA",
            "service_type": "Homeowners Association Assessments & Common Dues",
            "default_category": "HOA",
            "default_property_id": None,
            "confidence": 0.95,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_prop_mgmt",
            "pattern": "PROPERTY MANAGEMENT",
            "vendor_name": "Property Management Firm",
            "service_type": "Property Management Monthly Retainer (Schedule E Line 11)",
            "default_category": "MANAGEMENT",
            "default_property_id": None,
            "confidence": 0.90,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_realtor_fee",
            "pattern": "REALTOR",
            "vendor_name": "Realtor Brokerage & Advisory",
            "service_type": "Realtor Management & Advisory Fees (Schedule E Line 11)",
            "default_category": "MANAGEMENT",
            "default_property_id": None,
            "confidence": 0.90,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_commission",
            "pattern": "COMMISSION",
            "vendor_name": "Leasing Brokerage",
            "service_type": "Tenant Placement Commission (Schedule E Line 8)",
            "default_category": "COMMISSIONS",
            "default_property_id": None,
            "confidence": 0.90,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_wells_fargo_auto",
            "pattern": "WELLS FARGO AUTO",
            "vendor_name": "Wells Fargo Auto Finance",
            "service_type": "Personal Vehicle Financing & Installment Payment",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.98,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_ford_credit",
            "pattern": "FORD MOTOR CR",
            "vendor_name": "Ford Motor Credit",
            "service_type": "Personal Vehicle Financing & Installment Payment",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.98,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_boost_mobile",
            "pattern": "BOOST MOBILE",
            "vendor_name": "Boost Mobile",
            "service_type": "Cellular Wireless Phone Service",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.95,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_hughes_payroll",
            "pattern": "HUGHES NETWORK",
            "vendor_name": "Hughes Network Systems",
            "service_type": "Payroll Direct Deposit (Personal W-2 Earnings)",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.99,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_bofa_payment",
            "pattern": "PAYMENT - THANK YOU",
            "vendor_name": "Bank of America Credit Card Settlement",
            "service_type": "Credit Card Statement Settlement Payment",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.95,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_bofa_card_fees",
            "pattern": "BOFA CREDIT CARD FEES",
            "vendor_name": "Bank of America Credit Card Fees",
            "service_type": "Credit Card Financing & Annual Membership Fees",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.95,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_paypal",
            "pattern": "PAYPAL",
            "vendor_name": "PayPal Inc.",
            "service_type": "Online Merchant Payment & Processing",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.85,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_costco",
            "pattern": "COSTCO",
            "vendor_name": "Costco Wholesale",
            "service_type": "Groceries & General Household Retail",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.92,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_bombay_mart",
            "pattern": "BOMBAY MART",
            "vendor_name": "Bombay Mart",
            "service_type": "Specialty Grocery Retail",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.95,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_tous_les_jours",
            "pattern": "TOUS LES JOURS",
            "vendor_name": "Tous Les Jours Bakery",
            "service_type": "Bakery & Dining Retail",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.95,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
    {
            "id": "vrule_deli_4_you",
            "pattern": "DELI 4 YOU",
            "vendor_name": "Deli 4 You",
            "service_type": "European Deli & Food Market",
            "default_category": "PERSONAL",
            "default_property_id": None,
            "confidence": 0.95,
            "requires_client_confirmation": False,
            "requires_cpa_review": False,
        },
]


def seed_vendor_rules(session: Session, rules: Optional[List[dict]] = None) -> int:
    """Idempotently seeds vendor classification rules.

    Defaults to the portfolio-independent merchant rules. A caller seeding a
    particular portfolio (a test fixture, or a future portfolio template) passes
    its own list, so no owner's tenant names or door attributions live in the engine.
    """
    rules = DEFAULT_VENDOR_RULES if rules is None else rules
    existing_ids = {r.id for r in session.exec(select(VendorRule)).all()}
    added = 0
    for r in rules:
        if r["id"] not in existing_ids:
            rule_record = VendorRule(
                id=r["id"],
                pattern=r["pattern"],
                vendor_name=r["vendor_name"],
                service_type=r["service_type"],
                default_category=r["default_category"],
                default_property_id=r["default_property_id"],
                confidence=r.get("confidence", 0.90),
                requires_client_confirmation=r.get("requires_client_confirmation", False),
                requires_cpa_review=r.get("requires_cpa_review", False),
                is_active=True,
            )
            session.add(rule_record)
            added += 1
    if added > 0:
        session.commit()
    return added


def suggest_split_for_transaction(
    raw_description: str,
    amount_cents: int,
    session: Optional[Session] = None,
    rules_cache: Optional[List[VendorRule]] = None,
) -> Optional[TransactionSuggestion]:
    """Suggest property allocation and category based on vendor rules and description."""
    desc = raw_description.upper()

    # 2. Check Database / Cached Vendor Rules
    rules_to_check = rules_cache
    if rules_to_check is None and session is not None:
        try:
            rules_to_check = session.exec(select(VendorRule).where(VendorRule.is_active == True)).all()
        except Exception:
            rules_to_check = None

    if rules_to_check:
        for r in rules_to_check:
            if r.pattern.upper() in desc:
                return TransactionSuggestion(
                    property_id=r.default_property_id,
                    category=r.default_category,
                    confidence=r.confidence,
                    rule_name=f"{r.vendor_name}: {r.service_type}",
                    requires_client_confirmation=r.requires_client_confirmation,
                    requires_cpa_review=r.requires_cpa_review,
                    vendor_name=r.vendor_name,
                    service_type=r.service_type,
                    rule_id=r.id,
                )

    # 2. Fallback: in-memory default vendor rules
    for r in DEFAULT_VENDOR_RULES:
        if r["pattern"].upper() in desc:
            return TransactionSuggestion(
                property_id=r["default_property_id"],
                category=r["default_category"],
                confidence=r.get("confidence", 0.90),
                rule_name=f"{r['vendor_name']}: {r['service_type']}",
                requires_client_confirmation=r.get("requires_client_confirmation", False),
                requires_cpa_review=r.get("requires_cpa_review", False),
                vendor_name=r["vendor_name"],
                service_type=r["service_type"],
                rule_id=r["id"],
            )

    # 3. Fallback keyword heuristics
    if any(k in desc for k in ["PROPERTY MANAGEMENT", "REALTY", "REALTOR", "MGMT FEE", "MANAGEMENT FEE"]):
        return TransactionSuggestion(
            property_id=None,
            category="MANAGEMENT",
            confidence=0.88,
            rule_name="Property Management / Realtor Fee (Schedule E Line 11)",
            vendor_name="Property Manager",
            service_type="Management Retainer",
        )
    if any(k in desc for k in ["COMMISSION", "PLACEMENT FEE", "TENANT PLACEMENT"]):
        return TransactionSuggestion(
            property_id=None,
            category="COMMISSIONS",
            confidence=0.90,
            rule_name="Tenant Placement Commission (Schedule E Line 8)",
            vendor_name="Leasing Brokerage",
            service_type="Tenant Placement Commission",
        )
    if any(k in desc for k in ["RESTAURANT", "CUISINE", "DELI 4 YOU", "BOMBAY MART", "BUTERA", "WALMART", "COSTCO"]):
        return TransactionSuggestion(
            property_id=None,
            category="PERSONAL",
            confidence=0.90,
            rule_name="Groceries / Dining / Retail -> Personal Expense",
            vendor_name="Retail / Dining",
            service_type="Personal Expense",
        )

    return None
