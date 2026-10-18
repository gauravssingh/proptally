"""Resolve extracted evidence to coordinates in the original PDF.

The extraction worker captures page lines and word boxes while PyMuPDF has the PDF
open. This module turns those boxes into a canonical source location. It deliberately
prefers no highlight over a wrong highlight: ambiguous matches are reported as such.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from proptally.onboarding.fields import display_value

SOURCE_VERSION = 1
COORDINATE_SYSTEM = "pdf_points_top_left"

#: Labels that make one occurrence of a repeated value the right one. A mortgage
#: statement can print the same amount as amount due, monthly payment, and history;
#: the label around the value is the cheapest reliable discriminator.
CONTEXT_LABELS: dict[str, tuple[str, ...]] = {
    "address_line1": ("property address", "property", "address"),
    "address_line2": ("property address", "property", "address"),
    "city": ("property address", "property", "address"),
    "state": ("property address", "property", "address"),
    "zip_code": ("property address", "property", "address"),
    "owner_names": ("borrower", "owner", "mortgagor"),
    "seller_names": ("seller", "grantor"),
    "lender": ("lender", "servicer"),
    "loan_number": ("loan number", "loan no", "account number"),
    "account_number_masked": ("account number", "loan number", "loan no"),
    "purchase_price_cents": ("purchase price", "sale price", "consideration"),
    "acquisition_date": ("sale date", "purchase date", "acquisition date"),
    "outstanding_principal_cents": ("outstanding principal", "principal balance", "unpaid principal"),
    "original_principal_cents": ("original principal", "loan amount", "sanctioned amount"),
    "escrow_balance_cents": ("escrow balance", "escrow"),
    "emi_amount_cents": ("monthly payment", "amount due", "payment amount"),
    "interest_rate_bps": ("interest rate", "note rate", "rate"),
    "statement_date": ("statement date", "period ending", "statement period"),
    "monthly_rent_cents": ("monthly rent", "rent"),
    "security_deposit_cents": ("security deposit", "deposit"),
}


@dataclass(frozen=True)
class Line:
    text: str
    words: tuple[dict[str, Any], ...]

    @property
    def rect(self) -> dict[str, float]:
        return _union(self.words)


def _normalise(value: str) -> str:
    return re.sub(r"[$₹,\s]+", "", value or "").casefold()


def _union(words) -> dict[str, float]:
    return {
        "x0": min(word["x0"] for word in words),
        "y0": min(word["y0"] for word in words),
        "x1": max(word["x1"] for word in words),
        "y1": max(word["y1"] for word in words),
    }


def _lines(page: dict) -> list[Line]:
    lines = []
    for raw in page.get("lines", []):
        words = tuple(raw.get("words", ()))
        if not words:
            continue
        lines.append(Line(
            text=" ".join(word["text"] for word in words),
            words=words,
        ))
    return lines


def _match_rects(lines: list[Line], start: int, end: int) -> list[dict[str, float]]:
    return [line.rect for line in lines[start:end]]


def _phrase_match(line: Line, search: str, wanted: int) -> tuple[list[dict], str] | None:
    words = line.words
    if wanted > len(words):
        return None
    for start in range(len(words) - wanted + 1):
        phrase = " ".join(word["text"] for word in words[start:start + wanted])
        if _normalise(phrase) == _normalise(search):
            match_type = "EXACT" if phrase == search else "NORMALIZED"
            return [_union(words[start:start + wanted])], match_type
    return None


def _line_matches(page: dict, search: str, *, whole_lines: bool = False) -> list[dict]:
    lines = _lines(page)
    wanted = max(1, len(search.split()))
    matches: list[dict] = []
    seen: set[tuple] = set()

    def add(rects, match_type, matched_text, line_text, start, window):
        key = tuple(
            (round(rect["x0"], 2), round(rect["y0"], 2), round(rect["x1"], 2), round(rect["y1"], 2))
            for rect in rects
        )
        if key in seen:
            return
        seen.add(key)
        context = " ".join(
            line.text for line in lines[max(0, start - 2):start + window]
        )
        matches.append({
            "rects": rects,
            "match_type": match_type,
            "matched_text": matched_text,
            "line_text": line_text,
            "context": context,
        })

    # Multi-line windows handle addresses and evidence text that wraps.
    for window in range(1, 4):
        for start in range(len(lines) - window + 1):
            chosen = lines[start:start + window]
            text = " ".join(line.text for line in chosen)
            if _normalise(text) == _normalise(search):
                add(_match_rects(lines, start, start + window),
                    "EXACT" if text == search else "NORMALIZED", text, text, start, window)

    if matches:
        return matches

    if not whole_lines:
        for index, line in enumerate(lines):
            found = _phrase_match(line, search, wanted)
            if found is not None:
                add(found[0], found[1], search, line.text, index, 1)

    return matches


def _available(page: dict, page_number: int, matches: list[dict]) -> dict:
    rects = []
    seen = set()
    for match in matches:
        for rect in match["rects"]:
            key = (round(rect["x0"], 2), round(rect["y0"], 2),
                   round(rect["x1"], 2), round(rect["y1"], 2))
            if key in seen:
                continue
            seen.add(key)
            rects.append(rect)
    single = matches[0]
    match_type = single["match_type"] if len(matches) == 1 else "MULTIPLE"
    return {
        "version": SOURCE_VERSION,
        "status": "AVAILABLE",
        "coordinate_system": COORDINATE_SYSTEM,
        "page": page_number,
        "page_width": float(page.get("width") or 0),
        "page_height": float(page.get("height") or 0),
        "rotation": int(page.get("rotation") or 0),
        "matched_text": single["matched_text"],
        "match_type": match_type,
        "candidate_count": len(matches),
        "confidence": 0.99 if match_type in {"EXACT", "NORMALIZED"} else 0.9,
        "rects": rects,
    }


def _unavailable(page: int, reason: str) -> dict:
    return {
        "version": SOURCE_VERSION,
        "status": "UNAVAILABLE",
        "coordinate_system": COORDINATE_SYSTEM,
        "page": page,
        "reason": reason,
        "rects": [],
    }


def _ambiguous(page: int, count: int) -> dict:
    return {
        "version": SOURCE_VERSION,
        "status": "AMBIGUOUS",
        "coordinate_system": COORDINATE_SYSTEM,
        "page": page,
        "candidate_count": count,
        "rects": [],
    }


def _choose_value_matches(
    matches: list[dict], evidence: str, field_name: str = ""
) -> list[dict]:
    """Prefer a value occurrence whose surrounding labels name this field."""
    if len(matches) <= 1:
        return matches

    labels = CONTEXT_LABELS.get(field_name, ())
    if labels:
        labelled = [
            match for match in matches
            if any(
                _normalise(label) in _normalise(match.get("context") or "")
                for label in labels
            )
        ]
        if labelled:
            return labelled

    if evidence:
        evidence_norm = _normalise(evidence)
        contextual = [
            match for match in matches
            if evidence_norm and evidence_norm in _normalise(match.get("line_text") or "")
        ]
        if contextual:
            return contextual
    return matches


def attach_source_locations(result, geometry: list[dict]) -> None:
    """Attach one canonical location to every extracted field, in place."""
    if not geometry:
        return
    for name, extracted in list(result.fields.items()):
        location = locate_source(
            field_name=name,
            value=extracted.value,
            evidence=extracted.evidence,
            page=extracted.page,
            geometry=geometry,
        )
        result.fields[name] = replace(extracted, source_location=location)


def locate_source(
    *,
    field_name: str,
    value: Any,
    evidence: str,
    page: int,
    geometry: list[dict],
) -> dict:
    """Return a canonical source location, or a typed unavailable/ambiguous state."""
    if not geometry:
        return _unavailable(page, "NO_GEOMETRY")
    page = max(1, int(page or 1))
    if page > len(geometry):
        return _unavailable(page, "PAGE_OUT_OF_RANGE")

    value_text = display_value(field_name, value) if value not in (None, "", []) else ""
    page_data = geometry[page - 1]
    page_number = page

    if value_text and value_text != "—":
        matches = _choose_value_matches(
            _line_matches(page_data, value_text), evidence, field_name
        )
        if matches:
            return _available(page_data, page_number, matches)

    if evidence:
        matches = _line_matches(page_data, evidence)
        if matches:
            return _available(page_data, page_number, matches)

    # The page hint was wrong; search all pages, but still never choose arbitrarily.
    if value_text and value_text != "—":
        found = []
        for index, candidate in enumerate(geometry, start=1):
            matches = _choose_value_matches(
                _line_matches(candidate, value_text), evidence, field_name
            )
            found.extend((index, candidate, match) for match in matches)
        if found:
            pages = {index for index, _candidate, _match in found}
            if len(pages) == 1:
                index, candidate, _match = found[0]
                return _available(
                    candidate, index,
                    [match for _index, _candidate, match in found],
                )
            return _ambiguous(page_number, len(found))

    return _unavailable(page_number, "NOT_FOUND")
