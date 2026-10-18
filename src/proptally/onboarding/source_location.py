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

    def add(rects, match_type, matched_text, line_text):
        key = tuple(
            (round(rect["x0"], 2), round(rect["y0"], 2), round(rect["x1"], 2), round(rect["y1"], 2))
            for rect in rects
        )
        if key in seen:
            return
        seen.add(key)
        matches.append({
            "rects": rects,
            "match_type": match_type,
            "matched_text": matched_text,
            "line_text": line_text,
        })

    # Multi-line windows handle addresses and evidence text that wraps.
    for window in range(1, 4):
        for start in range(len(lines) - window + 1):
            chosen = lines[start:start + window]
            text = " ".join(line.text for line in chosen)
            if _normalise(text) == _normalise(search):
                add(_match_rects(lines, start, start + window),
                    "EXACT" if text == search else "NORMALIZED", text, text)

    if matches:
        return matches

    if not whole_lines:
        for line in lines:
            found = _phrase_match(line, search, wanted)
            if found is not None:
                add(found[0], found[1], search, line.text)
            elif _normalise(search) and _normalise(search) in _normalise(line.text):
                add([line.rect], "LINE", search, line.text)

    return matches


def _available(page: dict, page_number: int, match: dict) -> dict:
    return {
        "version": SOURCE_VERSION,
        "status": "AVAILABLE",
        "coordinate_system": COORDINATE_SYSTEM,
        "page": page_number,
        "page_width": float(page.get("width") or 0),
        "page_height": float(page.get("height") or 0),
        "rotation": int(page.get("rotation") or 0),
        "matched_text": match["matched_text"],
        "match_type": match["match_type"],
        "confidence": 0.99 if match["match_type"] in {"EXACT", "NORMALIZED"} else 0.8,
        "rects": match["rects"],
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


def _choose_value_matches(matches: list[dict], evidence: str) -> list[dict]:
    """Prefer a value occurrence whose line carries the evidence context."""
    if len(matches) <= 1 or not evidence:
        return matches
    evidence_norm = _normalise(evidence)
    contextual = [
        match for match in matches
        if evidence_norm and evidence_norm in _normalise(match.get("line_text") or "")
    ]
    return contextual or matches


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
        matches = _choose_value_matches(_line_matches(page_data, value_text), evidence)
        if len(matches) == 1:
            return _available(page_data, page_number, matches[0])
        if len(matches) > 1:
            return _ambiguous(page_number, len(matches))

    if evidence:
        matches = _line_matches(page_data, evidence)
        if len(matches) == 1:
            return _available(page_data, page_number, matches[0])
        if len(matches) > 1:
            return _ambiguous(page_number, len(matches))

    # The page hint was wrong; search all pages, but still never choose arbitrarily.
    if value_text and value_text != "—":
        found = []
        for index, candidate in enumerate(geometry, start=1):
            matches = _choose_value_matches(_line_matches(candidate, value_text), evidence)
            found.extend((index, candidate, match) for match in matches)
        if len(found) == 1:
            index, candidate, match = found[0]
            return _available(candidate, index, match)
        if len(found) > 1:
            return _ambiguous(page_number, len(found))

    return _unavailable(page_number, "NOT_FOUND")
