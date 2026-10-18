"""Document classification from the filename first, then the text.

Classification is a *proposal*, not a fact. A file whose type cannot be established
is kept and labelled ``UNKNOWN`` so the customer can see it and decide, rather than
being silently dropped. The category decides which extraction schema is applied;
the schema never rewrites the category.
"""

from __future__ import annotations

import re

#: Category -> (strong filename cues, weaker body cues). The first category whose
#: cue matches wins, so more specific documents are listed before generic ones.
_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "RENTAL_AGREEMENT",
        (r"rental", r"lease", r"tenancy", r"tenant"),
        (r"rental agreement", r"lease agreement", r"lessor", r"lessee", r"security deposit"),
    ),
    (
        "MORTGAGE_STATEMENT",
        (r"mortgage", r"loan\s*statement", r"emi", r"amorti[sz]ation"),
        (r"mortgage statement", r"amount due", r"principal balance", r"escrow", r"escrow account"),
    ),
    (
        "LOAN_AGREEMENT",
        (r"sanction", r"loan\s*agreement", r"loan\s*document", r"promissory"),
        (r"sanction letter", r"loan agreement", r"promissory note", r"rate of interest"),
    ),
    (
        "PROPERTY_DEED",
        (r"deed", r"title", r"conveyance", r"sale\s*agreement", r"purchase\s*agreement", r"registration"),
        (r"sale deed", r"title deed", r"grantor", r"grantee", r"purchase price", r"consideration"),
    ),
    (
        "PROPERTY_TAX",
        (r"property\s*tax", r"tax\s*bill", r"tax\s*receipt", r"municipal"),
        (r"property tax", r"tax parcel", r"assessment year", r"tax period"),
    ),
    (
        "INSURANCE",
        (r"insurance", r"policy", r"premium"),
        (r"policy number", r"coverage", r"premium", r"insured", r"insurer"),
    ),
    (
        "VALUATION",
        (r"valuation", r"appraisal", r"appraised", r"market\s*value"),
        (r"appraised value", r"market value", r"valuation date", r"valuer", r"appraiser"),
    ),
    (
        "MAINTENANCE",
        (r"invoice", r"maintenance", r"repair", r"utility", r"vendor", r"receipt"),
        (r"invoice number", r"service period", r"work order", r"amount due", r"bill to"),
    ),
    (
        "BANK_STATEMENT",
        (r"statement", r"bank", r"checking", r"chequing", r"savings", r"credit\s*card"),
        (r"account statement", r"beginning balance", r"closing balance", r"account number"),
    ),
)


def classify(filename: str, text: str) -> str:
    """The most likely category for a file, or ``UNKNOWN``.

    The filename is evidence of what the customer called the document; the body is
    evidence of what it contains. A filename match is deliberately stronger because
    legal text often contains words from several categories at once.
    """
    name = filename.casefold()
    body = " ".join(text.casefold().split())[:20000]

    for category, name_patterns, body_patterns in _RULES:
        if any(re.search(pattern, name) for pattern in name_patterns):
            return category

    for category, _name_patterns, body_patterns in _RULES:
        if any(re.search(pattern, body) for pattern in body_patterns):
            return category

    return "UNKNOWN"


#: Versioned schemas, so a payload can be read years later.
SCHEMA_VERSION = "onboarding.extraction/1.0"

CATEGORY_LABELS = {
    "PROPERTY_DEED": "Property ownership document",
    "MORTGAGE_STATEMENT": "Mortgage statement",
    "LOAN_AGREEMENT": "Loan agreement",
    "BANK_STATEMENT": "Bank statement",
    "RENTAL_AGREEMENT": "Rental agreement",
    "PROPERTY_TAX": "Property tax",
    "INSURANCE": "Insurance policy",
    "VALUATION": "Valuation report",
    "MAINTENANCE": "Maintenance or utility",
    "UNKNOWN": "Unclassified document",
}


def category_label(category: str) -> str:
    return CATEGORY_LABELS.get(category, "Document")
