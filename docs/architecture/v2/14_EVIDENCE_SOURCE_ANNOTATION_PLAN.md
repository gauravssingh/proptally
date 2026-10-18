# Evidence-to-Source Annotation — Implementation Plan

Status: Phases 0–4 implemented. Phase 5 remains the hardening and acceptance gate.

Implemented in this pass:

- PDF.js 3.11.174 is vendored under `web/static/vendor/pdfjs/`; no CDN.
- PyMuPDF captures word geometry while the PDF is already open.
- `OnboardingFieldEvidence.source_location_json` stores canonical PDF-point boxes.
- `onboarding/source_location.py` resolves exact/normalized/line matches and reports
  `AMBIGUOUS` or `UNAVAILABLE` instead of guessing.
- The Document Inspector renders PDF pages with PDF.js, exposes evidence selection,
  and draws temporary highlight overlays.
- `scripts/backfill_evidence_locations.py` resolves coordinates for evidence rows
  created before the locator existed; unresolved rows remain valid and show the
  unavailable state.

## 1. Goal

On the Document Inspector, selecting an extracted evidence value should:

1. Open the source document on the evidence's page.
2. Scroll the page to the matching source text.
3. Draw a translucent yellow highlight over every matching text region.
4. Keep that highlight while the evidence item is selected.
5. Move the highlight when another evidence item is selected.
6. Leave the original document untouched.

This is evidence-to-source annotation, not a PDF editor. The system already
extracted the value; the missing link is from that value to the coordinates of
its source text.

## 2. Current implementation

### 2.1 Extraction

`src/proptally/onboarding/extraction.py` uses PyMuPDF (`pymupdf` / `fitz`):

- `_pdf_text()` calls `page.get_text("text")` and captures `page.get_text("words")`
  geometry in the same pass.
- `TextDocument.pages` remains a list of page strings; `TextDocument.geometry` holds
  page dimensions, rotation, lines and word boxes.
- `ExtractedField` stores `name`, `value`, `page`, `evidence`, `method`,
  `confidence` and `source_location`.
- The locator runs immediately after field extraction, before the PDF is closed.
- Image OCR returns text only; it has no word boxes to highlight.

### 2.2 Persistence

`OnboardingFieldEvidence` stores:

- `document_id`
- `page_number`
- `evidence_text`
- `value_json`
- `source_location_json` (canonical rectangles and matching metadata)
- `method`
- `confidence`

### 2.3 Viewer

`onboarding/document.html` previews a PDF in a native iframe:

```html
<iframe src=".../documents/{{ document.id }}/file#page={{ page }}">
```

The iframe cannot be asked to scroll to text or draw an overlay. A native PDF
viewer is therefore not sufficient for this capability. Images already render
through an `<img>` and can host an overlay.

### 2.4 Evidence panel

The Document Inspector shows evidence rows with field label, value, page and
supporting text. The rows do not carry an evidence id or source location, and
there is no shared selection state between the panel and the viewer.

## 3. Recommended architecture

### 3.1 Canonical source location

Persist one canonical location per evidence row. Store rectangles in **PDF points**
with an explicit coordinate system and page geometry:

```json
{
  "version": 1,
  "coordinate_system": "pdf_points_top_left",
  "page": 1,
  "page_width": 612.0,
  "page_height": 792.0,
  "rotation": 0,
  "matched_text": "$490,280.68",
  "match_type": "EXACT",
  "confidence": 1.0,
  "rects": [
    {"x0": 485.2, "y0": 548.4, "x1": 550.6, "y1": 561.8}
  ]
}
```

Rules:

- PDF coordinates are captured from the original PDF, never from a screenshot.
- Store multiple rectangles when the source spans lines or is split by the
  extractor.
- Store page width and height so the viewer can transform coordinates without
  reopening the PDF.
- Store rotation. Do not assume every page is unrotated.
- Keep `match_type` and `confidence`; never present fuzzy matching as exact.
- Store `matched_text` separately from the normalized field value.

Schema: `OnboardingFieldEvidence.source_location_json` is a single JSON column,
migrated with control migration 7. A single evidence row remains the source of truth.
Old rows default to `{}` and are reported as unavailable; a backfill can locate them
later without invalidating the extraction.

### 3.2 Locator service

Add `src/proptally/onboarding/source_location.py`:

```text
locate_source(pdf_path, evidence_text, *, page_hint=None) -> SourceLocation
```

Matching order in the shipped locator:

1. Exact text match.
2. Normalized match (whitespace, currency symbols, commas, case).
3. Line/context match around the value.
4. Multiple occurrences on the same page: highlight every occurrence, with
   `match_type: MULTIPLE` and a candidate count.
5. Multiple pages with no page hint: `AMBIGUOUS`, because the viewer can show only
   one page at a time.
6. No match: `UNAVAILABLE`, never a guessed location.

Fuzzy matching is deliberately deferred. Incorrect highlighting is worse than an
unavailable highlight.

Requirements:

- Prefer the evidence page when known.
- Search all pages when the page hint fails.
- Return every match rectangle, not only the first.
- If several occurrences remain on the selected page, return all rectangles and
  highlight them together. Do not choose one arbitrarily.
- If the same value only matches on several different pages without a page hint,
  report `AMBIGUOUS`.
- Do not run OCR coordinates in the MVP. OCR text can keep page numbers and text,
  but its evidence is marked `SOURCE_UNAVAILABLE` for highlighting.
- Re-open the PDF at proposal-build time, or capture word boxes in the extraction
  worker. Do not rescan on every evidence click.

### 3.3 Viewer decision

The native iframe must be replaced before overlays are possible. Two options:

**Recommended: a local PDF.js viewer.**

- PDF.js 3.11.174 is vendored locally; no CDN and no external egress.
- The viewport transforms stored PDF rectangles into viewport pixels.
- Supports page navigation and zoom now; rotation and a text layer remain hardening.
- The highlight layer is separate from the canvas and contains only temporary
  rectangles.

**Fallback: server-rendered page images.**

- Render each page through PyMuPDF and overlay absolute-positioned rectangles.
- Simpler and uses an existing dependency.
- Loses text selection and smooth zoom; rotation must be handled server-side.
- Acceptable only if vendoring PDF.js is rejected.

Do not build the annotation layer around the browser's native PDF viewer.

### 3.4 Shared selection contract

The evidence panel and viewer should share one small state:

```json
{
  "evidence_id": "ev_...",
  "document_id": "doc_...",
  "page": 1,
  "rects": [[485.2, 548.4, 550.6, 561.8]],
  "match_type": "EXACT"
}
```

Implemented API:

```text
GET /onboarding/import/{onboarding_id}/documents/{document_id}/evidence/{evidence_id}/location
```

It returns the canonical location or:

```json
{"available": false, "reason": "SOURCE_UNAVAILABLE"}
```

The evidence row gets `data-evidence-id`; the viewer is driven only by this state.
The panel never imports viewer internals.

## 4. Interaction behaviour

| Action | Expected behaviour |
| --- | --- |
| Select evidence | Mark the evidence row selected |
| Same page already displayed | Highlight immediately |
| Different page | Navigate to the page, then scroll and highlight |
| Text spans multiple lines | Highlight all matching rectangles |
| Same value occurs several times on the page | Highlight every exact occurrence |
| Same value occurs on several pages with no page hint | Report ambiguous |
| Select another evidence | Replace the previous active highlight |
| Select the same evidence again | Keep the selection |
| Source cannot be located | Show “Source location unavailable” |
| Source is OCR-only | Show “Source coordinates unavailable” |
| Approximate match | Label the match as approximate; do not imply exact evidence |

Visual states:

- Selected evidence row: stronger border/background, not a second highlight colour.
- Active PDF highlight: translucent yellow fill plus a subtle outline.
- No highlight for unselected evidence.

The viewer must keep the highlight aligned at every zoom level and page rotation.

## 5. Phases

### Phase 0 — Viewer feasibility

- Vendor PDF.js locally, or confirm the server-rendered page-image fallback.
- Prove page navigation, viewport transformation and overlay alignment at two zoom
  levels.
- Confirm no external network requests and CSP compatibility.

Outcome: a viewer component with a stable coordinate contract.

### Phase 1 — Capture coordinates

- Extend the extraction/locator path to produce canonical source locations for
  native PDFs.
- Add `source_location_json` to evidence rows.
- Backfill existing onboarding evidence where the staged document still exists.
- Evidence that cannot be located stays valid; it is flagged, not discarded.

Outcome: every locatable native-PDF evidence row has coordinates.

### Phase 2 — Evidence location API

- Add the evidence id and location endpoint.
- Keep authorization identical to the inspector: signed-in user, active portfolio,
  owned session and owned document.
- Return a typed unavailable state, never a 500.

Outcome: the panel can resolve one evidence location without exposing another
customer's document.

### Phase 3 — Highlight layer

- Replace the PDF iframe with the selected viewer.
- Draw stored rectangles in the overlay layer.
- Implement page jump, scroll-to-region and selected highlight.
- Handle multi-line rectangles and page rotation.

Outcome: a location can be drawn and cleared.

### Phase 4 — Evidence integration

- Add `data-evidence-id` to evidence rows.
- Clicking a row selects it, fetches the location, and drives the viewer.
- Clicking another moves the highlight.
- Show unavailable/approximate states in the panel.

Outcome: click-to-highlight works end to end.

### Phase 5 — Edge cases and hardening

- Duplicate values on one page.
- Normalized currency/date values.
- Multi-line addresses and names.
- Rotated pages and different page sizes.
- OCR-only documents.
- Large PDFs and repeated evidence clicks.
- Keyboard selection and focus visibility.

## 6. Testing

Unit tests for the locator:

- exact match
- normalized match
- multi-line match
- duplicate candidates
- no match
- rotated page
- scanned/OCR document

Integration tests:

- evidence selection returns the right page and rectangles
- another portfolio's document is 404
- missing document or session is 404
- unavailable location returns a clear state, not a 500
- original PDF bytes are unchanged after highlighting
- no network egress is added

Browser smoke:

- select evidence, observe page jump and highlight
- change zoom, observe alignment
- select another evidence, observe replacement
- select evidence with no location, observe unavailable state

## 7. Security and privacy

- The source endpoint resolves membership and ownership before reading any file.
- Another customer's document id returns 404, never a location.
- The viewer uses the same local file endpoint and authorization as today.
- Coordinates are metadata; they do not expose text the account cannot already
  read.
- No external PDF service, OCR service or CDN is introduced by the viewer.
- The original PDF is never modified.

## 8. Answers to the design questions

1. **Current extraction:** PyMuPDF. It persists extracted page text and page
   numbers, not bounding boxes. OCR is text-only.
2. **Highlight persistence:** agree with the recommendation. Persist source
   coordinates; do not persist PDF annotations. Highlights are viewer overlays.
3. **Future scope:** build the viewer interface so a text layer could support
   manual selection later, but ship only click-to-highlight in the first release.

## 9. Non-goals

- Editing or rewriting the source PDF.
- Persisting highlights into the PDF file.
- Manual freehand annotations.
- OCR bounding boxes in the MVP.
- Cross-document evidence highlighting.
- Replacing the reviewer's decision flow; highlighting is evidence navigation, not
  approval.
