"""The evidence locator must be conservative: ambiguous means no highlight."""

from proptally.onboarding.source_location import locate_source


def _word(text, x, line=0, block=0, word=0):
    return {
        "text": text, "x0": x, "y0": 100 + line * 20,
        "x1": x + 40, "y1": 112 + line * 20,
        "block": block, "line": line, "word": word,
    }


def _page(*lines):
    return {
        "width": 612.0,
        "height": 792.0,
        "rotation": 0,
        "lines": [
            {
                "text": " ".join(word["text"] for word in words),
                "words": list(words),
            }
            for words in lines
        ],
    }


def test_exact_value_highlight_returns_its_page_and_rects():
    page = _page((
        _word("Outstanding", 72, 1, 0, 0),
        _word("principal:", 150, 1, 0, 1),
        _word("$490,280.68", 250, 1, 0, 2),
    ))
    location = locate_source(
        field_name="outstanding_principal_cents",
        value=49028068,
        evidence="Outstanding principal: $490,280.68",
        page=1,
        geometry=[page],
    )
    assert location["status"] == "AVAILABLE"
    assert location["page"] == 1
    assert location["match_type"] == "EXACT"
    assert location["rects"]


def test_repeated_value_uses_evidence_context():
    page = _page(
        (
            _word("Regular", 72, 1, 0, 0),
            _word("payment", 130, 1, 0, 1),
            _word("$3,857.96", 250, 1, 0, 2),
        ),
        (
            _word("Amount", 72, 3, 0, 0),
            _word("due", 130, 3, 0, 1),
            _word("$3,857.96", 250, 3, 0, 2),
        ),
    )
    location = locate_source(
        field_name="emi_amount_cents",
        value=385796,
        evidence="Amount due $3,857.96",
        page=1,
        geometry=[page],
    )
    assert location["status"] == "AVAILABLE"
    assert location["rects"][0]["y0"] == 160.0


def test_repeated_value_without_context_is_ambiguous():
    page = _page(
        (_word("$3,857.96", 250, 1, 0, 0),),
        (_word("$3,857.96", 250, 3, 0, 0),),
    )
    location = locate_source(
        field_name="emi_amount_cents",
        value=385796,
        evidence="",
        page=1,
        geometry=[page],
    )
    assert location["status"] == "AMBIGUOUS"
    assert location["rects"] == []


def test_missing_geometry_is_unavailable_not_an_exception():
    location = locate_source(
        field_name="emi_amount_cents",
        value=385796,
        evidence="Amount due $3,857.96",
        page=1,
        geometry=[],
    )
    assert location["status"] == "UNAVAILABLE"
    assert location["reason"] == "NO_GEOMETRY"
