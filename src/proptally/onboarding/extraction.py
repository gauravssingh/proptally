"""Text extraction and deterministic structured extraction.

Two separate jobs, kept separate on purpose:

1. **Text** -- PDFs through PyMuPDF, Office files through their XML, images and
   scanned pages through the configured OCR provider. If no text can be recovered
   the document fails; it is never reported as a successful empty extraction.
2. **Fields** -- rules over that text. Every value carries the page and the line it
   came from, and a field that is not present is omitted rather than guessed.

The rules are deliberately conservative: a label must be followed by a value on the
same line (or the next one), and money and dates are parsed into typed values only
when they are unambiguous. The optional provider in :mod:`providers` can replace the
rules, but it receives the same contract: no value without evidence.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from html import unescape
from pathlib import Path
from tempfile import NamedTemporaryFile

from proptally.onboarding.providers import ProviderError, ocr_provider

#: Below this many characters a PDF is treated as scanned rather than native.
SCANNED_TEXT_THRESHOLD = 40


class UnsupportedDocument(ValueError):
    """The file is retained but cannot be parsed by this extractor."""


class DocumentUnreadable(ValueError):
    """The file claims a supported format but its content cannot be read."""


@dataclass(frozen=True)
class ExtractedField:
    """One value, its location and the evidence that supports it."""

    name: str
    value: object
    page: int
    evidence: str
    method: str = "RULE"
    confidence: float = 0.7


@dataclass
class ExtractionResult:
    category: str
    fields: dict[str, ExtractedField] = field(default_factory=dict)
    pages: list[str] = field(default_factory=list)
    method: str = "rules"

    @property
    def has_fields(self) -> bool:
        return bool(self.fields)

    def values(self) -> dict:
        return {name: extracted.value for name, extracted in self.fields.items()}


@dataclass(frozen=True)
class TextDocument:
    pages: list[str]
    method: str

    @property
    def text(self) -> str:
        return "\n".join(self.pages)


# --- Text extraction -------------------------------------------------------


def _pdf_text(path: Path) -> TextDocument:
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - older wheels expose only ``fitz``
        try:
            import fitz
        except ImportError as exc:
            raise DocumentUnreadable("PDF support is not installed on this server.") from exc

    try:
        document = fitz.open(path)
    except Exception as exc:
        raise DocumentUnreadable("The PDF could not be opened; it may be corrupt.") from exc

    if document.needs_pass:
        document.close()
        raise DocumentUnreadable("The PDF is password-protected; remove the password and re-upload.")

    try:
        pages = [page.get_text("text") or "" for page in document]
        if sum(len(page.strip()) for page in pages) >= SCANNED_TEXT_THRESHOLD:
            return TextDocument(pages=pages, method="pdf-text")

        provider = ocr_provider()
        if provider is None:
            raise DocumentUnreadable(
                "The PDF has no extractable text (likely a scan). Configure OCR or "
                "upload a text-based copy; it has not been treated as empty."
            )

        ocr_pages: list[str] = []
        for page in document:
            pixmap = page.get_pixmap(dpi=200)
            with NamedTemporaryFile(suffix=".png", delete=True) as image:
                image.write(pixmap.tobytes("png"))
                image.flush()
                ocr_pages.append(provider.extract(Path(image.name)))
        return TextDocument(pages=ocr_pages, method=f"ocr:{provider.name}")
    finally:
        document.close()


def _ocr_image(path: Path) -> TextDocument:
    provider = ocr_provider()
    if provider is None:
        raise DocumentUnreadable(
            "This is an image and OCR is not configured on this server. Upload a "
            "text-based PDF or enable the OCR provider."
        )
    try:
        text = provider.extract(path)
    except ProviderError as exc:
        raise DocumentUnreadable(str(exc)) from exc
    return TextDocument(pages=[text], method=f"ocr:{provider.name}")


def _docx_text(path: Path) -> TextDocument:
    try:
        with zipfile.ZipFile(path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise DocumentUnreadable("The Word document could not be read.") from exc
    text = re.sub(r"</w:p\s*>", "\n", xml)
    text = re.sub(r"<w:tab[^>]*/>", "\t", text)
    text = re.sub(r"<[^>]+>", "", text)
    return TextDocument(pages=[unescape(text)], method="docx-xml")


def _xlsx_text(path: Path) -> TextDocument:
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            shared = ""
            if "xl/sharedStrings.xml" in names:
                raw = archive.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
                shared = "\n".join(unescape(item) for item in re.findall(r"<t[^>]*>(.*?)</t>", raw, re.DOTALL))
            sheets = []
            for name in sorted(names):
                if not name.startswith("xl/worksheets/") or not name.endswith(".xml"):
                    continue
                raw = archive.read(name).decode("utf-8", errors="replace")
                rows = []
                for row in re.findall(r"<row[^>]*>(.*?)</row>", raw, re.DOTALL):
                    values = [
                        unescape(value)
                        for value in re.findall(r"<t[^>]*>(.*?)</t>", row, re.DOTALL)
                    ] or [
                        unescape(value)
                        for value in re.findall(r"<v[^>]*>(.*?)</v>", row, re.DOTALL)
                    ]
                    if values:
                        rows.append("\t".join(values))
                if rows:
                    sheets.append("\n".join(rows))
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocumentUnreadable("The spreadsheet could not be read.") from exc
    text = "\n\n".join(part for part in (shared, *sheets) if part.strip())
    if not text.strip():
        raise DocumentUnreadable("The spreadsheet contains no readable text.")
    return TextDocument(pages=[text], method="xlsx-xml")


def _text_file(path: Path) -> TextDocument:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return TextDocument(pages=[raw.decode(encoding)], method="text")
        except UnicodeDecodeError:
            continue
    raise DocumentUnreadable("The text file is not in a readable encoding.")


def extract_text(path: Path, file_format: str) -> TextDocument:
    """Text and page boundaries for a supported file."""
    if file_format in {"PDF", "NATIVE_PDF", "SCANNED_PDF"}:
        # Reprocessing a document that was already extracted sees the refined
        # format token, not the original "PDF".
        return _pdf_text(path)
    if file_format == "IMAGE":
        return _ocr_image(path)
    if file_format == "DOCX":
        return _docx_text(path)
    if file_format == "XLSX":
        return _xlsx_text(path)
    if file_format in {"CSV", "TEXT"}:
        return _text_file(path)
    raise UnsupportedDocument("This file type is retained but not read by the importer.")


# --- Value parsing ---------------------------------------------------------

_MONTHS = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)
_DATE_TOKEN = (
    r"(?:\d{4}-\d{2}-\d{2}"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|(?:" + "|".join(_MONTHS) + r")[a-z]*\.?\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}\s+(?:" + "|".join(_MONTHS) + r")[a-z]*\.?,?\s+\d{4})"
)
_MONEY_TOKEN = r"(?:₹|Rs\.?|INR|USD|US\$|\$|£|€)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(lakh|lac|crore|million|mn|k)?"


def parse_date(raw: str | None) -> date | None:
    """A date from common statement formats, or None.

    A two-part numeric date whose first part is greater than 12 is read as
    day/month; otherwise month/day, which is what the statements in this product
    use. Month names are unambiguous.
    """
    if not raw:
        return None
    text = " ".join(raw.strip().replace(",", " ").split())
    text = re.sub(r"(?i)\b(?:on|dated|date|of)\b", " ", text).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return date.fromisoformat(text)
        match = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", text)
        if match:
            first, second, year = (int(part) for part in match.groups())
            if year < 100:
                year += 2000 if year < 70 else 1900
            if first > 12:
                month, day = second, first
            else:
                month, day = first, second
            return date(year, month, day)
        match = re.search(
            r"(" + "|".join(_MONTHS) + r")[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})", text, re.IGNORECASE
        )
        if match:
            month = _MONTHS.index(match.group(1)[:3].lower()) + 1
            return date(int(match.group(3)), month, int(match.group(2)))
        match = re.search(
            r"(\d{1,2})\s+(" + "|".join(_MONTHS) + r")[a-z]*\.?,?\s+(\d{4})", text, re.IGNORECASE
        )
        if match:
            month = _MONTHS.index(match.group(2)[:3].lower()) + 1
            return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError:
        return None
    return None


def parse_money_cents(raw: str | None) -> int | None:
    """Money as integer cents, honouring lakh/crore/million suffixes.

    Commas are removed rather than interpreted as thousands separators, because
    Indian documents group them differently and the digits are the same either way.
    """
    if not raw:
        return None
    match = re.search(_MONEY_TOKEN, raw, re.IGNORECASE)
    if not match:
        return None
    number = match.group(1).replace(",", "")
    try:
        amount = Decimal(number)
    except InvalidOperation:
        return None
    unit = (match.group(2) or "").lower()
    multiplier = {
        "k": Decimal(1000),
        "lakh": Decimal(100000),
        "lac": Decimal(100000),
        "crore": Decimal(10000000),
        "million": Decimal(1000000),
        "mn": Decimal(1000000),
    }.get(unit, Decimal(1))
    return int((amount * multiplier * 100).quantize(Decimal(1)))


def parse_rate_bps(raw: str | None) -> int | None:
    """An interest rate, which must be written with a percent sign.

    Without that requirement, a label followed by a date ("Interest Rate (Until
    10/01/2032)") yields the year fragment "10" as a 10.000% rate.
    """
    if not raw:
        return None
    match = re.search(r"([0-9]{1,2}(?:\.[0-9]{1,4})?)\s*%", raw)
    if not match:
        return None
    return int((Decimal(match.group(1)) * 1000).quantize(Decimal(1)))


def parse_percentage(raw: str | None) -> int | None:
    if not raw:
        return None
    match = re.search(r"([0-9]{1,3}(?:\.[0-9]+)?)\s*%", raw)
    if not match:
        return None
    return int(Decimal(match.group(1)).quantize(Decimal(1)))


def parse_months(raw: str | None) -> int | None:
    if not raw:
        return None
    match = re.search(r"(\d{1,3})\s*(year|yr|month|mo)", raw, re.IGNORECASE)
    if not match:
        return None
    value = int(match.group(1))
    return value * 12 if match.group(2).lower().startswith(("year", "yr")) else value


def _digits(value: object) -> str:
    """Digits only; loan and account numbers are grouped differently by each servicer."""
    return re.sub(r"\D", "", "" if value is None else str(value))


def parse_loan_number(raw: str | None) -> str | None:
    """A loan number is one numeric token, not a date or a line of prose.

    Four digits are not enough: that is a masked suffix ("...0708"), and treating it
    as the full number would create a second loan. Six is the shortest full account
    number this importer will accept; a statement that only exposes a suffix is
    reconciled from the other documents for the same servicer instead.

    The token is taken whole, so ``10/14/2025`` is a date and never becomes the
    loan number ``10142025``.
    """
    if not raw:
        return None
    for match in re.finditer(r"\b\d[\d\-]{4,23}\b", raw):
        digits = _digits(match.group(0))
        if 6 <= len(digits) <= 24:
            return digits
    return None


#: Words that betray a sentence rather than an institution name. A labelled
#: "Lender" line on a statement is often prose ("your lender will make the
#: payments"), and that must never become the record's lender.
_SENTENCE_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "for", "from", "if",
    "in", "is", "it", "may", "not", "of", "on", "or", "our", "please", "send",
    "since", "that", "the", "this", "to", "transfers", "use", "was", "we",
    "will", "with", "you", "your", "paid", "expenses", "prepared", "date",
    "see", "ends", "securing", "statement", "mail", "cash", "payments",
})


def looks_like_institution(value: str | None) -> bool:
    """Whether a labelled value can plausibly be a lender or servicer name."""
    text = " ".join((value or "").strip(" .,;:").split())
    if not text or len(text) > 80 or len(text.split()) > 6:
        return False
    if any(char in text for char in "\x02\x03•"):
        return False
    if not re.search(r"[A-Za-z]", text):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z&.'-]*", text.casefold())
    if any(word in _SENTENCE_WORDS for word in words):
        return False
    return not text.endswith((",", ":", ";"))


def institution_from_filename(filename: str) -> str | None:
    """The servicer named by the archive folder, when the customer used one.

    A folder called ``PNC Mortgage`` or ``Rocket Mortgage`` is stronger evidence of
    the servicer than any sentence inside the PDF, and it is the one piece of
    metadata the customer controls. Generic folders (``Statements``, ``Documents``)
    are ignored.
    """
    parts = [part for part in re.split(r"[\\/]+", filename or "") if part]
    for part in parts[:-1]:
        stem = Path(part).stem
        cleaned = " ".join(re.sub(r"[_-]+", " ", stem).split())
        if not cleaned or len(cleaned.split()) > 6:
            continue
        if not re.search(r"(?i)\b(bank|mortgage|servicing|lending|finance|financial|credit\s+union|nbfc)\b", cleaned):
            continue
        if re.search(r"(?i)\b(statement|statements|escrow|tax|interest|documents?|uploads?|files?)\b", cleaned):
            continue
        if cleaned.casefold() == "mortgage":
            continue
        return cleaned
    return None


def institution_from_header(pages: list[str]) -> tuple[str, int, str] | None:
    """A company name in the document header, when the archive has no folder cue."""
    company = re.compile(
        r"^([A-Z][A-Za-z&.'-]+(?:\s+[A-Za-z&.'-]+){0,5}),?\s*"
        r"(?:LLC|Inc\.?|Bank|Mortgage|Lending|Servicing|Finance|Financial)?\s*[®™]?$"
    )
    noise = re.compile(
        r"(?i)\b(return service|please|po box|p\.o\.|property address|loan number|"
        r"account number|statement date|payment due|amount due|page\s*\d|"
        r"customer service|contact information|do not|make checks|detach)\b"
    )
    for page, lines in _lines(pages):
        for line in lines[:30]:
            cleaned = line.strip(" .,;:")
            if not cleaned or noise.search(cleaned):
                continue
            if not re.search(r"(?i)\b(bank|mortgage|servicing|lending|finance|financial|credit\s+union)\b", cleaned):
                continue
            match = company.match(cleaned)
            if not match:
                continue
            candidate = match.group(1).strip(" .,-")
            if looks_like_institution(candidate):
                return candidate, page, line
    return None


def plausible_address(line1: str | None, city: str | None = None) -> bool:
    """Reject label fragments such as "securing" captured from a 1098's prose."""
    text = " ".join((line1 or "").split())
    if not text or len(text) < 5 or len(text) > 120:
        return False
    if not re.search(r"\d", text):
        return False
    if not re.search(
        r"(?i)\b(st|street|ave|avenue|rd|road|dr|drive|ln|lane|ct|court|blvd|"
        r"boulevard|pl|place|way|ter|terrace|cir|circle|hwy|highway|pkwy|parkway)\b",
        text,
    ):
        return False
    return not re.search(
        r"(?i)\b(securing|instructions?|property|address|borrower|lender|mortgage)\b",
        text,
    )


# --- Line and label helpers -------------------------------------------------

_LABELS = {
    "purchase_price_cents": (
        r"purchase\s+price", r"sale\s+price", r"sale\s+consideration",
        r"consideration", r"amount\s+paid", r"total\s+consideration",
    ),
    "acquisition_date": (
        r"date\s+of\s+sale", r"sale\s+date", r"purchase\s+date",
        r"execution\s+date", r"registered\s+on", r"registration\s+date",
    ),
    "owner_names": (r"buyer", r"purchaser", r"grantee", r"transferee", r"owner"),
    "seller_names": (r"seller", r"vendor", r"grantor", r"transferor"),
    "registration_number": (
        r"registration\s+(?:number|no)", r"document\s+(?:number|no)", r"deed\s+(?:number|no)",
    ),
    "ownership_percentage": (r"ownership\s+(?:share|percentage|interest)", r"share\s+of\s+ownership"),
    "lender": (r"lender", r"servicer", r"mortgagee", r"financial\s+institution"),
    "loan_number": (
        r"loan\s+(?:number|no\.?|#)", r"loan\s+account\s+(?:number|no\.?|#)",
        r"account\s+(?:number|no\.?|#)", r"mortgage\s+(?:number|no\.?|#)",
    ),
    "original_principal_cents": (
        r"original\s+(?:loan\s+)?principal", r"sanctioned\s+amount",
        r"amount\s+sanctioned", r"original\s+loan\s+amount",
    ),
    "outstanding_principal_cents": (
        r"outstanding\s+principal", r"principal\s+balance", r"unpaid\s+principal",
        r"current\s+principal\s+balance", r"balance\s+principal",
    ),
    "interest_rate_bps": (r"interest\s+rate", r"rate\s+of\s+interest", r"annual\s+percentage\s+rate"),
    "term_months": (r"loan\s+term", r"term\s+of\s+(?:the\s+)?loan", r"tenure", r"amorti[sz]ation\s+term"),
    "emi_amount_cents": (
        r"monthly\s+payment", r"amount\s+due", r"total\s+due", r"emi",
        r"instal?lment", r"monthly\s+instal?lment",
    ),
    "origination_date": (r"origination\s+date", r"loan\s+date", r"loan\s+commencement", r"disbursement\s+date"),
    "maturity_date": (r"maturity\s+date", r"loan\s+maturity", r"final\s+payment\s+date"),
    "statement_date": (r"statement\s+date", r"billing\s+date", r"notice\s+date"),
    "escrow_balance_cents": (r"escrow\s+balance", r"escrow\s+account\s+balance", r"escrow\s+reserve"),
    "institution_name": (r"institution", r"bank", r"financial\s+institution", r"lender"),
    "account_number_masked": (
        r"account\s+(?:number|no\.?|#)", r"card\s+(?:number|no\.?|#)", r"account\s+ending\s+in",
    ),
    "account_type": (r"account\s+type", r"type\s+of\s+account"),
    "opening_balance_cents": (r"opening\s+balance", r"beginning\s+balance", r"balance\s+on\s+hand"),
    "closing_balance_cents": (r"closing\s+balance", r"ending\s+balance", r"balance\s+at\s+end"),
    "tenant_name": (r"tenant", r"lessee", r"renter"),
    "landlord": (r"landlord", r"lessor", r"owner"),
    "monthly_rent_cents": (r"monthly\s+rent", r"rent\s+amount", r"monthly\s+rental", r"rent\s+per\s+month"),
    "security_deposit_cents": (r"security\s+deposit", r"deposit\s+amount"),
    "lease_start": (r"lease\s+start", r"commencement\s+date", r"commencing\s+on", r"lease\s+from", r"term\s+starts"),
    "lease_end": (r"lease\s+end", r"expiration\s+date", r"expiry\s+date", r"lease\s+to", r"term\s+ends"),
    "tax_identifier": (r"parcel\s+(?:number|no)", r"tax\s+(?:id|identifier)", r"assessment\s+(?:number|no)"),
    "tax_period": (r"tax\s+period", r"assessment\s+year", r"tax\s+year"),
    "tax_amount_cents": (r"tax\s+amount", r"amount\s+due", r"total\s+tax"),
    "payment_date": (r"payment\s+date", r"paid\s+on", r"date\s+paid"),
    "insurance_provider": (r"insurer", r"insurance\s+(?:provider|company)", r"underwriter"),
    "policy_number": (r"policy\s+(?:number|no\.?|#)"),
    "coverage_amount_cents": (r"coverage\s+amount", r"sum\s+insured", r"dwelling\s+coverage"),
    "premium_cents": (r"premium", r"annual\s+premium"),
    "policy_start": (r"policy\s+(?:start|effective|from)", r"effective\s+date", r"period\s+from"),
    "policy_end": (r"policy\s+(?:end|expiration|to)", r"expiration\s+date", r"period\s+to"),
    "valuation_date": (r"valuation\s+date", r"date\s+of\s+valuation", r"appraisal\s+date", r"effective\s+date"),
    "estimated_value_cents": (
        r"estimated\s+value", r"market\s+value", r"appraised\s+value", r"valuation\s+amount",
    ),
    "valuation_provider": (r"valuer", r"appraiser", r"valuation\s+(?:firm|provider|company)"),
    "valuation_method": (r"valuation\s+method", r"method\s+of\s+valuation", r"approach"),
    "vendor": (r"vendor", r"service\s+provider", r"contractor", r"bill\s+from"),
    "invoice_date": (r"invoice\s+date", r"bill\s+date"),
    "service_period": (r"service\s+period", r"work\s+period"),
    "amount_cents": (r"invoice\s+total", r"amount\s+due", r"total\s+amount", r"amount"),
}

_ADDRESS_LABELS = (
    r"property\s+address", r"address\s+of\s+(?:the\s+)?property",
    r"premises", r"situated\s+at", r"property\s+located\s+at",
)
_STREET_RE = re.compile(
    r"\d{2,6}\s+[A-Za-z0-9][A-Za-z0-9 .'\-]{2,45}\b"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Drive|Dr|Lane|Ln|Court|Ct|Boulevard|Blvd|"
    r"Place|Pl|Way|Terrace|Ter|Circle|Cir|Highway|Hwy)\b[^\n]{0,70}",
    re.IGNORECASE,
)


def _lines(pages: list[str]) -> list[tuple[int, list[str]]]:
    return [
        (index, [line.strip() for line in page.splitlines() if line.strip()])
        for index, page in enumerate(pages, start=1)
    ]


def _after(line: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, line, re.IGNORECASE)
        if match:
            remainder = line[match.end():]
            remainder = re.sub(r"^[\s:=\-#.]+", "", remainder)
            return remainder.strip()
    return ""


def _locate(
    pages: list[str],
    patterns: tuple[str, ...],
    lookahead: int = 1,
) -> tuple[str, int, str] | None:
    """The text after a label, its page, and the line it came from."""
    for page_number, lines in _lines(pages):
        for index, line in enumerate(lines):
            remainder = _after(line, patterns)
            if remainder:
                return remainder, page_number, line
            if re.search(r"(?:" + "|".join(patterns) + r")", line, re.IGNORECASE):
                for candidate in lines[index + 1:index + 1 + lookahead]:
                    if not candidate:
                        continue
                    # The next line may itself be labelled ("Institution: ..."),
                    # so strip its label before treating it as the value.
                    candidate_value = _after(candidate, patterns) or candidate
                    return candidate_value, page_number, candidate
    return None


def _candidates(
    pages: list[str],
    patterns: tuple[str, ...],
    lookahead: int = 3,
) -> list[tuple[str, int, str]]:
    """Every plausible value position after a label, nearest first.

    A label's value is often on the next line, and on some forms the line after that
    is a bare ``$`` or a column heading. Returning candidates lets the caller pick the
    first one its parser actually understands, instead of accepting the first line.
    """
    found: list[tuple[str, int, str]] = []
    for page_number, lines in _lines(pages):
        for index, line in enumerate(lines):
            remainder = _after(line, patterns)
            if remainder:
                found.append((remainder, page_number, line))
            if re.search(r"(?:" + "|".join(patterns) + r")", line, re.IGNORECASE):
                for candidate in lines[index + 1:index + 1 + lookahead]:
                    if not candidate:
                        continue
                    found.append((_after(candidate, patterns) or candidate, page_number, candidate))
    return found


def _address_parts(raw: str) -> tuple[str, str, str, str]:
    """Street, city, state and ZIP from an address line, without inventing any."""
    text = re.sub(r"\s+", " ", raw).strip(" ,;.")
    match = re.search(
        r"^(?P<line1>.+?),\s*(?P<city>[^,]+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$",
        text,
    )
    if match:
        return match.group("line1"), match.group("city"), match.group("state"), match.group("zip")
    match = re.search(r"^(?P<line1>.+?),\s*(?P<city>[^,]+?)\s+(?P<zip>\d{5}(?:-\d{4})?)$", text)
    if match:
        return match.group("line1"), match.group("city"), "", match.group("zip")
    if text:
        return text, "", "", ""
    return "", "", "", ""


def _combine_address(lines: list[str], index: int, first: str | None = None) -> str:
    """A street line plus the city/state/ZIP lines that follow it.

    Statements print the address over three lines:

        1017 DR JOHN BURKEY DR
        ALGONQUIN, IL 60102

    Reading only the first line loses the city and ZIP, which then produces a second
    property record for the same door during reconciliation.
    """
    if index < 0 or index >= len(lines):
        return (first or "").strip()
    parts = [(first or lines[index]).strip()]
    for candidate in lines[index + 1:index + 4]:
        text = candidate.strip()
        if not text:
            break
        if re.match(r"^[A-Za-z .'-]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?$", text):
            parts.append(text)
            break
        if re.match(r"^[A-Za-z .'-]+,\s*[A-Za-z .'-]+$", text) and len(parts) == 1:
            parts.append(text)
            continue
        if re.match(r"^\d{5}(?:-\d{4})?$", text):
            parts.append(text)
            break
        break
    return ", ".join(part for part in parts if part)


def _address_candidates(pages: list[str]) -> list[tuple[str, int, str]]:
    found: list[tuple[str, int, str]] = []
    for page_number, lines in _lines(pages):
        for index, line in enumerate(lines):
            remainder = _after(line, _ADDRESS_LABELS)
            if remainder:
                found.append((_combine_address(lines, index, remainder), page_number, line))
            elif (
                re.search(r"(?:" + "|".join(_ADDRESS_LABELS) + r")", line, re.IGNORECASE)
                and index + 1 < len(lines)
            ):
                found.append((_combine_address(lines, index + 1), page_number, lines[index + 1]))
    return found


def _add(
    result: ExtractionResult,
    name: str,
    value: object,
    page: int,
    evidence: str,
    confidence: float = 0.8,
) -> None:
    if value is None or value == "" or value == []:
        return
    if name in result.fields:
        return
    result.fields[name] = ExtractedField(
        name=name,
        value=value,
        page=page,
        evidence=evidence[:500],
        confidence=confidence,
    )


def _add_address(result: ExtractionResult, pages: list[str]) -> None:
    for raw, page, line in _address_candidates(pages):
        if len(raw) < 6:
            continue
        line1, city, state, zip_code = _address_parts(raw)
        if not plausible_address(line1, city):
            # A 1098's "Address or description of property securing mortgage" label
            # can yield the word "securing"; it is not an address.
            continue
        _add(result, "address_line1", line1, page, line, 0.85)
        _add(result, "city", city, page, line, 0.75)
        _add(result, "state", state, page, line, 0.75)
        _add(result, "zip_code", zip_code, page, line, 0.8)
        return
    for page, lines in _lines(pages):
        for index, line in enumerate(lines):
            match = _STREET_RE.search(line)
            if match:
                # The match may be only the street line; append the city/ZIP lines.
                combined = _combine_address(lines, index, match.group(0))
                line1, city, state, zip_code = _address_parts(combined)
                if not plausible_address(line1, city):
                    continue
                _add(result, "address_line1", line1, page, line, 0.55)
                _add(result, "city", city, page, line, 0.5)
                _add(result, "state", state, page, line, 0.5)
                _add(result, "zip_code", zip_code, page, line, 0.55)
                return


def _add_labeled(
    result: ExtractionResult,
    pages: list[str],
    field_name: str,
    parser,
    confidence: float = 0.8,
    lookahead: int = 3,
) -> None:
    for raw, page, line in _candidates(pages, _LABELS[field_name], lookahead=lookahead):
        value = parser(raw)
        if value is not None:
            _add(result, field_name, value, page, line, confidence)
            return


def _names(raw: str) -> list[str]:
    cleaned = re.sub(r"(?i)\b(?:and|&)\b", ",", raw)
    cleaned = re.sub(r"\b(?:s/o|w/o|d/o|c/o)\b.*$", "", cleaned, flags=re.IGNORECASE)
    names = [
        " ".join(part.strip(" .;:").split())
        for part in cleaned.split(",")
        if part.strip(" .;:")
    ]
    return [name for name in names if 2 <= len(name) <= 80]


def _known_institution(pages: list[str]) -> tuple[str, int, str] | None:
    """A lender or bank named in the first lines, when no label exists."""
    pattern = re.compile(
        r"\b([A-Z][A-Za-z&.'-]+(?:\s+[A-Z][A-Za-z&.'-]+){0,3}\s+"
        r"(?:Bank|Mortgage|Lending|Finance|Financial|Housing|Credit\s+Union|NBFC))\b"
    )
    for page, lines in _lines(pages):
        for line in lines[:30]:
            match = pattern.search(line)
            if match:
                return match.group(1).strip(" .,-"), page, line
    return None


def _extract_property(result: ExtractionResult, pages: list[str]) -> None:
    _add_address(result, pages)
    _add_labeled(result, pages, "purchase_price_cents", parse_money_cents)
    _add_labeled(result, pages, "acquisition_date", parse_date)
    located = _locate(pages, _LABELS["owner_names"])
    if located:
        raw, page, line = located
        _add(result, "owner_names", _names(raw), page, line, 0.7)
    located = _locate(pages, _LABELS["seller_names"])
    if located:
        raw, page, line = located
        _add(result, "seller_names", _names(raw), page, line, 0.65)
    _add_labeled(result, pages, "registration_number", lambda raw: raw.strip(" .,;:") or None)
    _add_labeled(result, pages, "ownership_percentage", parse_percentage)
    if re.search(r"(?i)\b(primary residence|owner[- ]occupied)\b", "\n".join(pages)):
        _add(result, "property_type", "PRIMARY_RESIDENCE", 1, "primary residence", 0.6)
    elif re.search(r"(?i)\b(rental|investment property|let out)\b", "\n".join(pages)):
        _add(result, "property_type", "RENTAL_INVESTMENT", 1, "rental/investment", 0.6)


def _add_mortgage_payment(result: ExtractionResult, pages: list[str]) -> None:
    """The regular monthly payment, in priority order.

    "Explanation of Amount Due" is a heading whose next line is "Principal", so a
    generic amount-due search returns the principal component. The explicit labels
    are tried first, and only then the generic ones.
    """
    for pattern in (
        r"regular\s+monthly\s+payment",
        r"monthly\s+payment",
        r"total\s+amount\s+due",
        r"amount\s+due",
        r"\bemi\b",
        r"instal?lment",
    ):
        for raw, page, line in _candidates(pages, (pattern,), lookahead=1):
            value = parse_money_cents(raw)
            if value is not None:
                _add(result, "emi_amount_cents", value, page, line, 0.8)
                return


def _extract_mortgage(
    result: ExtractionResult,
    pages: list[str],
    *,
    agreement: bool,
    filename: str,
) -> None:
    _add_address(result, pages)
    text = "\n".join(pages).casefold()
    is_tax_form = (
        "form 1098" in text
        or "substitute form 1098" in text
        or "mortgage interest statement" in text
    )
    is_escrow_analysis = (
        "escrow analysis" in text
        or "escrow account disclosure" in text
    )

    # The archive's folder name is the strongest servicer signal; a labelled
    # "Lender" line is usually the weakest because it often contains prose.
    path_institution = institution_from_filename(filename)
    header_institution = institution_from_header(pages)
    if path_institution:
        _add(result, "lender", path_institution, 1, f"Archive folder: {path_institution}", 0.8)
    elif header_institution:
        _add(
            result, "lender", header_institution[0],
            header_institution[1], header_institution[2], 0.75,
        )
    else:
        for raw, page, line in _candidates(pages, _LABELS["lender"], lookahead=1):
            if looks_like_institution(raw):
                _add(result, "lender", raw.strip(" .,;:"), page, line, 0.65)
                break
        else:
            known = _known_institution(pages)
            if known and looks_like_institution(known[0]):
                _add(result, "lender", known[0], known[1], known[2], 0.55)

    # A 1098's "Account number (see instructions)" is followed by prose; the
    # immediate next line is the only safe place to look for the number.
    for raw, page, line in _candidates(pages, _LABELS["loan_number"], lookahead=1):
        value = parse_loan_number(raw)
        if value:
            _add(result, "loan_number", value, page, line, 0.85)
            break

    _add_labeled(result, pages, "origination_date", parse_date)
    _add_labeled(result, pages, "maturity_date", parse_date)

    if not is_tax_form:
        # A 1098 reports tax-year figures (and its boxes are full of $1 rows), and
        # an escrow analysis reports projections rather than the current balance.
        # Neither is evidence of the loan's present principal or rate.
        _add_labeled(result, pages, "statement_date", parse_date, lookahead=2)
        if not is_escrow_analysis:
            _add_labeled(result, pages, "original_principal_cents", parse_money_cents)
            _add_labeled(result, pages, "outstanding_principal_cents", parse_money_cents)
            _add_labeled(result, pages, "interest_rate_bps", parse_rate_bps)
            _add_labeled(result, pages, "term_months", parse_months)
            _add_mortgage_payment(result, pages)
            _add_labeled(result, pages, "escrow_balance_cents", parse_money_cents)

    # Plausibility guards: a money amount taken from prose ("$5,000 Mortgage
    # Closing Guarantee") or a date fragment is not a loan fact.
    limits = {
        "original_principal_cents": (1_000_000, 10_000_000_000),
        "outstanding_principal_cents": (10_000, 10_000_000_000),
        "emi_amount_cents": (1_000, 10_000_000),
        "interest_rate_bps": (1, 30_000),
        "term_months": (1, 600),
        "escrow_balance_cents": (0, 10_000_000),
    }
    for name, (low, high) in limits.items():
        item = result.fields.get(name)
        if item is not None and (
            not isinstance(item.value, (int, float)) or not low <= item.value <= high
        ):
            del result.fields[name]

    # A masked number is a display convenience, not a second identity.
    loan_number = result.fields.get("loan_number")
    if loan_number is not None:
        digits = _digits(loan_number.value)
        if len(digits) >= 4:
            _add(
                result, "account_number_masked", "..." + digits[-4:],
                loan_number.page, loan_number.evidence, 0.8,
            )
    for raw, page, line in _candidates(pages, _LABELS["account_number_masked"], lookahead=2):
        digits = _digits(raw)
        if 4 <= len(digits) <= 24:
            _add(result, "account_number_masked", "..." + digits[-4:], page, line, 0.7)
            break


def _extract_bank(result: ExtractionResult, pages: list[str], *, filename: str) -> None:
    path_institution = institution_from_filename(filename)
    header_institution = institution_from_header(pages)
    if path_institution:
        _add(result, "institution_name", path_institution, 1, f"Archive folder: {path_institution}", 0.8)
    elif header_institution:
        _add(
            result, "institution_name", header_institution[0],
            header_institution[1], header_institution[2], 0.75,
        )
    else:
        for raw, page, line in _candidates(pages, _LABELS["institution_name"], lookahead=1):
            if looks_like_institution(raw):
                _add(result, "institution_name", raw.strip(" .,;:"), page, line, 0.65)
                break
        else:
            known = _known_institution(pages)
            if known and looks_like_institution(known[0]):
                _add(result, "institution_name", known[0], known[1], known[2], 0.55)

    located = _locate(pages, _LABELS["account_number_masked"])
    if located:
        raw, page, line = located
        digits = re.sub(r"\D", "", raw)
        if len(digits) >= 4:
            _add(result, "account_number_masked", "..." + digits[-4:], page, line, 0.8)

    located = _locate(pages, _LABELS["account_type"])
    if located:
        raw, page, line = located
        lowered = raw.casefold()
        value = (
            "CREDIT_CARD" if "credit" in lowered
            else "SAVINGS" if "saving" in lowered
            else "CHECKING" if "check" in lowered or "current" in lowered
            else None
        )
        _add(result, "account_type", value, page, line, 0.7)
    else:
        text = "\n".join(pages).casefold()
        if "credit card" in text:
            _add(result, "account_type", "CREDIT_CARD", 1, "credit card", 0.5)
        elif "savings account" in text:
            _add(result, "account_type", "SAVINGS", 1, "savings account", 0.5)
        elif "checking" in text or "current account" in text:
            _add(result, "account_type", "CHECKING", 1, "checking/current account", 0.5)

    _add_labeled(result, pages, "opening_balance_cents", parse_money_cents)
    _add_labeled(result, pages, "closing_balance_cents", parse_money_cents)

    for page, lines in _lines(pages):
        for line in lines:
            match = re.search(
                r"(?i)(?:statement|billing)\s+period[^0-9]{0,10}(" + _DATE_TOKEN + r")"
                r"\s*(?:to|through|-|–)\s*(" + _DATE_TOKEN + r")",
                line,
            )
            if match:
                _add(result, "statement_period_start", parse_date(match.group(1)), page, line, 0.8)
                _add(result, "statement_period_end", parse_date(match.group(2)), page, line, 0.8)
                break


def _extract_rental(result: ExtractionResult, pages: list[str]) -> None:
    _add_address(result, pages)
    for field_name in ("tenant_name", "landlord"):
        located = _locate(pages, _LABELS[field_name])
        if located:
            raw, page, line = located
            names = _names(raw)
            _add(result, field_name, names[0] if names else raw.strip(" .,;:"), page, line, 0.7)
    _add_labeled(result, pages, "monthly_rent_cents", parse_money_cents)
    _add_labeled(result, pages, "security_deposit_cents", parse_money_cents)
    _add_labeled(result, pages, "lease_start", parse_date)
    _add_labeled(result, pages, "lease_end", parse_date)
    text = "\n".join(pages).casefold()
    for frequency in ("monthly", "quarterly", "annual", "weekly"):
        if frequency in text:
            _add(result, "payment_frequency", frequency.upper(), 1, frequency, 0.5)
            break


def _extract_tax(result: ExtractionResult, pages: list[str]) -> None:
    _add_address(result, pages)
    _add_labeled(result, pages, "tax_identifier", lambda raw: raw.strip(" .,;:") or None)
    _add_labeled(result, pages, "tax_period", lambda raw: raw.strip(" .,;:") or None)
    _add_labeled(result, pages, "tax_amount_cents", parse_money_cents)
    _add_labeled(result, pages, "payment_date", parse_date)


def _extract_insurance(result: ExtractionResult, pages: list[str]) -> None:
    _add_address(result, pages)
    _add_labeled(result, pages, "insurance_provider", lambda raw: raw.strip(" .,;:") or None)
    _add_labeled(result, pages, "policy_number", lambda raw: raw.strip(" .,;:") or None)
    _add_labeled(result, pages, "coverage_amount_cents", parse_money_cents)
    _add_labeled(result, pages, "premium_cents", parse_money_cents)
    _add_labeled(result, pages, "policy_start", parse_date)
    _add_labeled(result, pages, "policy_end", parse_date)


def _extract_valuation(result: ExtractionResult, pages: list[str]) -> None:
    _add_address(result, pages)
    _add_labeled(result, pages, "valuation_date", parse_date)
    _add_labeled(result, pages, "estimated_value_cents", parse_money_cents)
    _add_labeled(result, pages, "valuation_provider", lambda raw: raw.strip(" .,;:") or None)
    _add_labeled(result, pages, "valuation_method", lambda raw: raw.strip(" .,;:") or None)


def _extract_maintenance(result: ExtractionResult, pages: list[str]) -> None:
    _add_address(result, pages)
    _add_labeled(result, pages, "vendor", lambda raw: raw.strip(" .,;:") or None)
    _add_labeled(result, pages, "invoice_date", parse_date)
    _add_labeled(result, pages, "service_period", lambda raw: raw.strip(" .,;:") or None)
    _add_labeled(result, pages, "amount_cents", parse_money_cents)
    _add(result, "currency", "USD", 1, "default currency", 0.3)


class RuleBasedExtractor:
    """Deterministic field extraction for the categories this release supports."""

    name = "rules"

    def extract(self, *, category: str, filename: str, pages: list[str]) -> ExtractionResult:
        result = ExtractionResult(category=category, pages=pages, method="rules")
        if category == "PROPERTY_DEED":
            _extract_property(result, pages)
        elif category == "MORTGAGE_STATEMENT":
            _extract_mortgage(result, pages, agreement=False, filename=filename)
        elif category == "LOAN_AGREEMENT":
            _extract_mortgage(result, pages, agreement=True, filename=filename)
        elif category == "BANK_STATEMENT":
            _extract_bank(result, pages, filename=filename)
        elif category == "RENTAL_AGREEMENT":
            _extract_rental(result, pages)
        elif category == "PROPERTY_TAX":
            _extract_tax(result, pages)
        elif category == "INSURANCE":
            _extract_insurance(result, pages)
        elif category == "VALUATION":
            _extract_valuation(result, pages)
        elif category == "MAINTENANCE":
            _extract_maintenance(result, pages)
        return result


def _fields_from_json(payload: dict) -> dict[str, object]:
    """Normalise a provider's field dictionary to canonical names.

    Unknown keys are kept: an extractor may know a fact these rules do not, and the
    review screen shows it with the evidence that exists. Values are not coerced
    beyond the obvious money/date parse.
    """
    if not isinstance(payload, dict):
        return {}
    result: dict[str, object] = {}
    for name, value in payload.items():
        if value in (None, "", []):
            continue
        key = str(name).strip()
        if key in {"purchase_price_cents", "original_principal_cents", "outstanding_principal_cents",
                   "emi_amount_cents", "escrow_balance_cents", "opening_balance_cents",
                   "closing_balance_cents", "monthly_rent_cents", "security_deposit_cents",
                   "tax_amount_cents", "coverage_amount_cents", "premium_cents",
                   "estimated_value_cents", "amount_cents"}:
            result[key] = parse_money_cents(str(value)) if not isinstance(value, int) else value
        elif key in {"acquisition_date", "origination_date", "maturity_date", "statement_date",
                     "lease_start", "lease_end", "payment_date", "policy_start", "policy_end",
                     "valuation_date", "invoice_date"}:
            result[key] = parse_date(str(value)) or value
        elif key in {"interest_rate_bps"}:
            result[key] = parse_rate_bps(str(value)) if not isinstance(value, int) else value
        elif key == "term_months":
            result[key] = parse_months(str(value)) or value
        else:
            result[key] = value
    return result
