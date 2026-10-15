# PropTally — Application-Wide UI/UX Audit

**Document:** `01_APPLICATION_AUDIT.md`
**Status:** Current state assessment (input to the design system)
**Date:** October 2026
**Scope:** Every rendered screen, template fragment, stylesheet rule, and client-side interaction in `src/proptally/web/`.

> This audit is based on the running application and its source, not on screenshots.
> Every numeric claim below is either quoted from a file or measured against the live
> DOM at `http://127.0.0.1:8099` (token/geometry/contrast probes executed from
> `getComputedStyle`). Measurements are reproducible with the method note in §1.3.

---

## 1. Scope and method

### 1.1 Surface inspected

| Artefact | Location | Size |
| :--- | :--- | :--- |
| Application shell | `web/templates/base.html` | 341 lines |
| Screens | `web/templates/{dashboard,property_detail,mortgages,transactions,reconciliation,tax_schedule_e}.html` | 2,100 lines |
| Stylesheet | `web/static/css/app.css` | 474 lines, 252 selectors |
| Routes | `web/routes/*.py` | 6 routers |
| Formatting contract | `web/helpers.py` | 4 Jinja filters |
| Client script | inline `<script>` in `base.html`, `transactions.html`, `reconciliation.html`, `property_detail.html` | 4 blocks, 33 inline event handlers |

Two documentation artefacts describe the interface and were treated as inputs:
`docs/architecture/v2/07_PRODUCT_AND_UI_SPECIFICATION.md` (approved V2 spec, largely
aspirational — it specifies screens that do not exist yet) and `README.md`.

### 1.2 Inventory — screens and their primary function

| # | Route | Template | Primary function | Distinct header pattern |
| :-- | :--- | :--- | :--- | :--- |
| 1 | `/` | `dashboard.html` | Portfolio KPIs, door cards, cash-flow waterfall | Title + evidence tag + primary CTA |
| 2 | `/properties/:id` | `property_detail.html` | Property hero, basis/valuation bar, narrative + gallery, P&L, mortgage terms, lease | Photographic hero with overlaid action pills |
| 3 | `/mortgages` | `mortgages.html` | Consolidated loan ledger, escrow reserve health | Title + 2 static evidence chips |
| 4 | `/transactions` | `transactions.html` | Register, manual entry, split/allocate | Title + primary CTA |
| 5 | `/reconciliation` | `reconciliation.html` | Review queue, filters, vendor rules, batch post | Title + count pill + secondary CTA |
| 6 | `/tax/schedule-e` | `tax_schedule_e.html` | Schedule E workpaper per property, CSV export | Title + segmented year control + CTA |
| 7 | ~~`/report`, `/report/email`~~ | *removed* | Client audit report — **removed from the project**: the generated HTML carried a real client portfolio, so the artefacts, their generators and the routes were deleted | n/a |

Displayed modals/dialogs (8): manual transaction entry, transaction splitter, vendor
rules manager, property narrative editor, photo upload, lightbox, mobile drawer,
delete confirmations (native `confirm()`).

### 1.3 Measurement method

Probes were run against the live DOM:

```js
getComputedStyle(el).borderRadius / .boxShadow / .outlineStyle / .fontFamily
[...document.querySelectorAll('label')].filter(l => !l.getAttribute('for'))
```

Contrast ratios use WCAG 2.1 relative luminance. Class-coverage was computed by
extracting every token from `class="…"` (after stripping Jinja `{% %}`/`{{ }}`)
and subtracting the selector set parsed from `app.css`, including escaped
variants (`.\:`, `.\/`).

---

## 2. Findings — critical usability issues

### C1. 235 of 470 referenced style classes are inert (517 occurrences)

The stylesheet is a hand-maintained subset of Tailwind utilities. Template usage has
drifted past it, so a large share of the markup's visual intent silently does nothing.

| Class(es) | Occurrences | Intended effect | Actual effect |
| :--- | --: | :--- | :--- |
| `absolute`, `relative` | 18 | Overlay positioning of badges/menus | not positioned |
| `hover:text-white`, `hover:bg-slate-800/60`, `hover:bg-slate-800` | 47 | Nav and row hover feedback | **no hover feedback** |
| `truncate` | 4 | Clamp long merchant strings | text overflows (`text-overflow: clip`) |
| `rounded-2xl`, `shadow-2xl` | 8 | Modal elevation | **`border-radius: 0px`, `box-shadow: none`** |
| `animate-in`, `fade-in`, `zoom-in-95` | 12 | Modal entrance | no animation defined |
| `focus:ring-2`, `focus:outline-none`, `focus:ring-emerald-500` | 27 | Focus treatment | no-ops |
| `flex-shrink-0`, `cursor-pointer`, `leading-relaxed`, `items-start`, `w-3.5`, `h-3.5`, `text-indigo-700` | 40 | Layout/type intent | no-ops |
| `max-h-[90vh]`, `bg-[radial-gradient(...)]`, `py-0.2`, `text-[9px]` | 5 | Arbitrary values | never defined |

Measured on a door page (`/properties/<id>`), `#edit-details-modal > div`:
`borderRadius: "0px"`, `boxShadow: "none"`. The four modals render square and flat.

**Impact:** the interface is not what the templates claim it is. Every later fix is
guesswork until the style layer is authoritative.

### C2. No focus indicator on interactive controls

Keyboard focus lands on the first `<button>` with `outline: none`. Inputs and selects
get a 2px emerald `outline` (the only working focus rule, `app.css` input/select
`:focus` block), but **buttons, links, and table row actions have no visible focus
state**. The intended `focus:ring-*` classes are undefined (C1). WCAG 2.4.7 fails.

### C3. Secondary text fails WCAG AA throughout

`text-slate-400` (`#94a3b8`) is the standard metadata/sub-label colour and is used for
KPI sub-labels, table sub-rows, hero meta, and captions.

| Pair | Ratio | Requirement |
| :--- | --: | :--- |
| `text-slate-400` on white | **2.56** | 4.5 (text), 3.0 (large) |
| `text-slate-400` on `slate-50` | **2.45** | 4.5 |
| `text-slate-400` on `slate-100` | **2.34** | 4.5 |
| `emerald-600` money value on white | **3.77** | 4.5 at `text-xs` |
| `emerald-600` CADS KPI on `slate-50` | **3.60** | 4.5 |

Both `#94a3b8` and `#059669` must be replaced by AA-safe tokens; currency colour is
applied at `text-xs` in tables, where the 3.0 large-text exemption does not apply.

### C4. The global queue banner reports a filtered count as the queue depth

`base.html:260-266` renders:

> `CBD REVIEW` — **N items** pending classification in queue.

`N` is `review_count`, and `routes/reconciliation.py:217` sets `review_count =
filtered_count`. Verified live:

| URL | Banner text |
| :--- | :--- |
| `/reconciliation` | `7 items pending classification in queue` |
| `/reconciliation?q=APPLE` | `1 items pending classification in queue` |

The banner is rendered on every screen, but on the queue screen it silently reports the
current filter's match count while its copy claims to describe the whole queue. The
same variable also drives three badges (`base.html:127,215,300`), so the sidebar,
drawer, and bottom-nav counters change value when a filter is applied.

### C5. The closed mobile drawer is still in the accessibility tree

`#mobileDrawer` is hidden with `opacity: 0; pointer-events: none` only:

```json
{"drawerOpacity":"0","drawerPointerEvents":"none","drawerVisibility":"visible",
 "drawerDisplay":"block","ariaHidden":null,"linkVisible":"visible","linkFocusable":true}
```

It carries `role="dialog" aria-modal="true"` while closed, has no `aria-hidden`,
`inert`, or `hidden` attribute, and its navigation links remain tabbable. Keyboard and
screen-reader users traverse an invisible menu before reaching page content.

### C6. Form controls are not programmatically labelled

| Screen | `<label>` elements | with `for` | Controls | Unlabelled controls |
| :--- | --: | --: | --: | --: |
| `/transactions` (2 modals) | 9 | 1 | 13 | **9** |
| `/reconciliation` (filter bar + 2 modals) | 15 | 3 | 31 | **22** |
| `/properties/:id` (2 modals) | 6 | 0 | 9 | 9 |

All 13 `<th>` elements on `/transactions` and `/mortgages` lack `scope`; no table has a
`<caption>`. Visible labels exist but are not associated with their control, so
clicking a label does nothing and assistive technology announces unlabelled comboboxes.

### C7. "Unavailable" and "zero" are rendered identically

`helpers.py:37-41` — `format_cents(None)` returns `"$0.00"`. Every template that prints
an optional figure through this filter therefore claims **zero** where the truth is
**no evidence**:

* `dashboard.html` card 2 prints `equity.total_debt_cents | format_cents`; when
  `equity.unavailable_property_ids` is non-empty the sub-label says
  *"N door(s) have no current balance evidence"* but the headline still reads `$0.00`.
* `mortgages.html` correctly uses `—` for missing statement values, so the same concept
  has two presentations in two screens.

For a financial application this is the highest-risk presentation defect: `$0.00` is a
material claim about the portfolio, and it is indistinguishable from an absent figure.

### C8. Sign is applied twice by hand

`dashboard.html` and `property_detail.html` render outflow lines as
`-{{ financials.repairs_cents | format_cents }}`. The formatter already emits a `-` for
negative input (`format_cents(-100)` → `"$-1.00"`), so a negative figure renders as
`-$-1.00`, and the two screens disagree with `format_cents_sign` (used only for CADS and
amounts). Magnitude leaks the accounting sign into presentation.

---

## 3. Findings — major visual inconsistencies

### M1. Three different navies in the shell

`app.css` declares the same selectors twice with different values; the last wins.

| Selector | First declaration | Second declaration (effective) |
| :--- | :--- | :--- |
| `.bg-navy-900` | `#0b1329` (line 204) | `#0f172a` (line 417) |
| `.bg-navy-950` | `#050b1a` (line 205) | `#020617` (line 418) |

Separately, `.mobile-drawer-panel` hardcodes `background-color: #0f172a`
(`app.css:447`), which is the *same* value as `.bg-slate-900`. Net result: the desktop
sidebar, the mobile top bar, the bottom nav, and the drawer panel are painted from three
unrelated constants, and two declared brand colours are dead code.

### M2. No design tokens exist

Zero CSS custom properties in 474 lines. The sheet hardcodes **57 hex values and
16 `rgba()` values**, including raw black shadows (`rgba(0, 0, 0, 0.05)`). There is no
theme layer, no semantic naming, and no single place to change a colour.

### M3. Navigation is duplicated and the portfolio is hardcoded in markup

The desktop sidebar and the mobile drawer contain byte-identical navigation blocks
(7 links + section labels + footer status). Property entries are hardcoded by id in
`base.html` at 6 locations and the portfolio size appears as literal copy in four
places: `All Doors (3)`, `Portfolio Doors (3 Properties)`, `3 ACTIVE LOANS`,
`Across 3 active mortgage loans`. `transactions.html` repeats the full property and
category option lists in two `<select>`s **and again inside a JS string** that builds
new split rows. Adding a fourth door or a new Schedule E category requires edits in
4–6 files with no test that would catch a miss.

### M4. Radii, shadows, and borders are per-screen

5 distinct radii (`0.25rem`, `0.375rem`, `0.5rem`, `0.75rem`, `9999px`) and 3 distinct
shadows, applied inconsistently: cards are `rounded-xl`, badges `rounded`, pills
`rounded-full`, filter controls `rounded-lg`.

The clearest symptom is that **the same component renders two ways**. All five modal
cards are the same object — a white panel with a radius and a drop shadow — but the two
on `/transactions` are written with defined classes and the three elsewhere are written
with undefined ones:

| Modal | Classes for radius/shadow | Measured radius | Measured shadow |
| :--- | :--- | :--- | :--- |
| `#manual-modal` (`/transactions`) | `rounded-xl shadow-xl` | `12px` | `0 20px 25px -5px rgba(0,0,0,.1), …` |
| `#split-modal` (`/transactions`) | `rounded-xl shadow-xl` | `12px` | `0 20px 25px -5px rgba(0,0,0,.1), …` |
| `#vendor-rules-modal` (`/reconciliation`) | `rounded-2xl shadow-2xl` | **`0px`** | **`none`** |
| `#edit-details-modal` (door page) | `rounded-2xl shadow-2xl` | **`0px`** | **`none`** |
| `#upload-photo-modal` (door page) | `rounded-2xl shadow-2xl` | **`0px`** | **`none`** |

`max-h-[90vh]` is equally inert (`maxHeight: none`), so the vendor-rules table cannot
scroll on a short viewport even though it is wrapped in `overflow-y-auto`.

### M5. Typography has no scale

9 font-size values including raw pixels: `9px`, `10px`, `11px`, plus `text-xs/sm/base/lg/xl/2xl`.
Weights are chosen per element (`font-bold`, `font-semibold`, `font-medium`) with no rule:
KPI labels are `text-xs font-medium uppercase tracking-wider`, filter labels are
`text-xs font-semibold`, table headers are `text-xs uppercase font-semibold tracking-wider`
(mortgages) but `text-[10px] uppercase font-semibold` (tax) and `text-slate-600 uppercase text-[10px]`
(vendor rules). Line-heights are set only by the utility shorthand (`text-sm` → `1.25rem`).

Numeric type is `font-mono` (31 uses on a property page) mixed with `font-mono` on
status labels and ids — the mono/sans split does not consistently separate numbers from
prose.

### M6. Status and evidence badges are ad hoc

At least 24 distinct hand-written pills exist with no shared component, e.g.
`[POSTED ENTRIES ONLY]`, `[POSTED LEDGER BALANCES]`, `3 ACTIVE LOANS`, `[ESTIMATED]`,
`[CALCULATED]`, `[UNLEVERAGED PROPERTY EARNINGS]`, `[ACTUAL LIQUIDITY BEFORE DEBT]`,
`IN-FLIGHT (21x5)`, `POSTED LEDGER`, `NO POSTED LINES`, `STATEMENT`, `NO STATEMENT`,
`FROM MOVEMENTS`, `FROM STATEMENT`, `ASSET ACCOUNTS (1110 & 1120)`, `AUTH REQUIRED`,
`REVIEW IF RENTAL`, `FULLY ALLOCATED`, `PARTIAL (Resid: $x)`, `UNALLOCATED`, `○ UNPOSTED`,
`● POSTED LEDGER`, `CBD-1 CASH RENT`, `CBD-2 MIXED CARD`, `AWAITING POST`, `RENTAL DOOR`,
`PRIMARY RESIDENCE`, `DEMO`, `LIVE`, `Active Lease`, `Cover`, `Rule Match`, `Batch Action`.

Palette choice is per screen, not per meaning: emerald means "verified" on mortgages
(`POSTED LEDGER`) but "inflow" on transactions, while blue means "statement-derived"
on mortgages and "awaiting post" on reconciliation.

### M7. Two incompatible evidence grammars, and the spec's vocabulary is not implementable

* Screens use bracketed prose tags: `[POSTED ENTRIES ONLY]`, `[CALCULATED]`, `[ESTIMATED]`.
* The approved V2 spec mandates a closed set: `[VERIFIED] [CALCULATED] [ESTIMATED]
  [NEEDS REVIEW] [PARTIALLY ALLOCATED] [CAPEX REVIEW REQUIRED] [SYSTEM RECONCILED]
  [CPA APPROVED]`.

Neither is implemented consistently, and **`[CPA APPROVED]` must not be adopted**: the
implementation deliberately records CBD-2 as a user self-attestation
(`USER_ATTESTED_CPA_REVIEW`) and the UI copy states it is "Self-attestation, not verified
CPA approval". The design system therefore defines a provenance badge vocabulary that
matches what the code can actually prove, and records the spec gap explicitly
(see `04_COMPONENT_LIBRARY.md` §G and `06_RESPONSIVE_AND_ACCESSIBILITY.md` §3).

### M8. Every screen composes its own page header

Four different header structures (§1.2) with different badge counts, spacing
(`pb-2` / `pb-3` / `pb-4`), and CTA placement. `/reconciliation` adds a `<div class="space-y-6">`
wrapper; no other screen does. Section headings are `text-lg font-bold` (dashboard card
grid), `text-base font-bold` (card titles), `text-sm font-bold` (hero panels).

### M9. `/properties/:id` uses a different visual language from the rest of the product

It is the only screen with a photographic hero, `bg-gradient-to-t`,
`bg-gradient-to-br`, a `radial-gradient` dot pattern, `⭐` emoji ratings, `hover:scale-105`
image zoom, `animate-pulse` dots, and drop-shadowed white text. The approved direction is
data-centric and understated, and this screen — the one users open most after the
dashboard — currently looks like a separate product.

### M10. Type below the readable floor

`text-[9px]` (photo captions, mobile badge) and `text-[10px]` (47 occurrences) are used
for status and provenance text. At 9–10px these are not reliably legible and are
unusable on high-DPI displays with scaling.

---

## 4. Findings — minor inconsistencies

| ID | Finding | Evidence |
| :-- | :--- | :--- |
| m1 | SVG attribute typo: `stroke-round="round"` instead of `stroke-linecap` | `reconciliation.html:341` |
| m2 | Version strings hardcoded and already stale (`v2.2 Self-Hosted`, `v2.2 Mobile`, `Zero-cloud offline vault`) | `base.html:24,168`; app version is `0.2.0` (`app.py:44`) |
| m3 | Modal behaviour differs per screen: Escape closes on `/properties` and `/reconciliation` only; **no** Escape handling on `/transactions` | 4 scripts |
| m4 | Backdrop click closes only the lightbox; the other 5 modals ignore it | `property_detail.html`, `transactions.html` |
| m5 | No modal has `role="dialog"`, `aria-modal`, focus trap, or initial focus | 6 modals |
| m6 | No loading state anywhere; the batch-post success alert has no `role="status"`/`aria-live` | `reconciliation.html:39` |
| m7 | Empty states differ per screen: full empty state (`/reconciliation`), long prose banners (`/`, `/properties`), bare centred text (`/mortgages`), **none at all** (`/transactions` renders an empty table body) | 4 templates |
| m8 | Table density inconsistent: `text-sm` + `py-3.5` (mortgages, transactions) vs `text-xs` + `py-2.5`/`py-2` (tax, vendor rules) | 4 tables |
| m9 | Row hover applied in 3 ways (`hover:bg-slate-50/60`, `/70`, none) and is inert where the class is undefined | C1 |
| m10 | 5 unrelated accent hues in navigation alone (emerald, blue, amber, rose, indigo) — hue carries no meaning | `base.html:95,115,119,124,134` |
| m11 | 33 inline `on*` handlers calling globals; split-row markup duplicated as a JS string | `transactions.html:317` |
| m12 | `/report` opened a page with none of the design language, without `rel="noopener noreferrer"` | `base.html:137,225` — **resolved by removal**: the report artefacts and their routes were deleted (see `07` §2) |
| m13 | Data table `<th>` never uses `scope`; no table has a `<caption>` | 4 tables |
| m14 | Static copy asserts facts the data may contradict (`Status: Active` on every door card, `Across 3 active mortgage loans`) | `dashboard.html:112,66` |

---

## 5. Opportunities

| ID | Opportunity | Rationale |
| :-- | :--- | :--- |
| O1 | **One financial period control.** Today: a static period label chip (dashboard, property), a hardcoded 2-year toggle (tax), and no control on mortgages/transactions. | A single `FinancialPeriodSelector` makes "which window am I looking at" answerable the same way everywhere. |
| O2 | **One review-item card.** `/reconciliation` currently offers three parallel posting paths (rule-suggestion banner, inline assign form, batch banner). | Same domain action, three layouts. |
| O3 | **One property summary panel**, reused by dashboard door cards and `/properties` header. | Same figures (value, basis, debt, equity, rent) are composed twice with different labels. |
| O4 | **Consolidate the metric card.** 4 KPI cards (dashboard), 4 stat cards (reconciliation), 5 stat cells (property), 4 loan/escrow cards (mortgages). | One `MetricCard` with `size ∈ {kpi, stat, inline}` removes four bespoke implementations. |
| O5 | **Print stylesheet for the tax workpaper.** | Accountants print Schedule E workpapers; today it prints the sidebar and dark banner. |
| O6 | **Dark theme is nearly free** once tokens exist (the shell is already dark). | Optional, low cost after Phase 3. |
| O7 | **Move export affordances into the page header component.** | CSV export exists only on tax; it is a header-level action. |
| O8 | **Standardise empty/loading/error states** from one macro set. | Removes 4 bespoke treatments and adds the missing `/transactions` empty state. |

---

## 6. Domain-specific assessment

**Properties / portfolio.** Door cards show basis, value, servicer, and status; the
property screen repeats the same figures in a 5-cell bar, then a hero, then three panels.
Figures that decide whether a door is performing (rent, debt service, cash flow) are only
on the dashboard, so neither screen is self-contained. `Status: Active` is hardcoded.

**Financial KPIs.** The four headline metrics mix provenance in the same row:
`[ESTIMATED]` equity, `[VERIFIED]`/`[CALCULATED]` debt, `[CALCULATED]` NOI,
`[CALCULATED]` CADS — presented with four different chip styles. CADS is the only
signed, colour-coded value, and it also carries a manual `+`/`-` prefix.

**Mortgages and escrow.** Best-structured screen: ledger-vs-servicer principal and
escrow movement-vs-statement are separated explicitly with sub-labels
(`POSTED LEDGER` / `STATEMENT` / `NO POSTED LINES` / `NO STATEMENT`). This is the
pattern the rest of the product should follow, and it is currently implemented with
inline spans rather than a component. `IN-FLIGHT (21x5)` is unexplained terminology
presented as a footnote badge.

**Transactions and splitting.** The splitter validates the residual client-side and
disables save on over-allocation — good behaviour. But client-facing meaning
(`AUTH REQUIRED`, `REVIEW IF RENTAL`) is conveyed by pill colour plus a `title`
attribute, and the option lists are triplicated (§M3). The register has no empty state,
no pagination, and its filter strip displays two **static, non-interactive** chips
labelled `All Accounts` / `All Doors` under the heading `Filter By:` — controls that look
interactive and are not.

**Reconciliation and classification.** The strongest workflow in the app: rule
suggestion → accept, or inline assign → post, with governance gates surfaced as required
checkboxes. It leaks scope (C4) and its three posting paths need unifying (O2). The
CBD-1/CBD-2 distinctness is communicated correctly in copy — the design system must
preserve that and must not compress the two gates into one "approval" badge.

**Tax / Schedule E.** Dense, correct, and the only screen with a print-worthy artefact,
but the smallest type (`text-[10px]` headers) and no print stylesheet. Portfolio total
column recomputes with `selectattr('is_rental') | sum` inline in the template while the
row values come from the workpaper object — two sources for one row.

**Documents / ingestion.** No UI exists (spec-only). The ingestion pipeline
(`src/proptally/ingestion/`) has no presentation layer, and the dashboard's
"awaiting ingestion" banner is the only trace of it.

**Client audit / reporting.** The client report screens and their generators have been
**removed**: the generated HTML embedded a real client portfolio, so it does not belong in
version control. Any replacement must be client-agnostic — a generator that reads the
ledger and renders from the design system, with the client's data supplied at runtime
rather than committed. Tracked as P1-1 in `07_IMPLEMENTATION_ROADMAP.md`.

---

## 7. Contracts that must not change

Any redesign must preserve these, because tests and inbound links depend on them:

1. **Structural ids:** `mobileMenuBtn`, `mobileDrawer`, `mobileDrawerClose`, `mobileDrawerOverlay`.
2. **Meta/PWA:** `viewport-fit=cover`, `theme-color #020617`, `apple-mobile-web-app-*`,
   `/static/manifest.json`, `apple-mobile-web-app-title PropTally`.
3. **Pinned visible strings:** `All Doors (3)`; the three seeded door names/addresses;
   bottom-nav labels `Overview`, `Doors`, `Mortgages`, `Review`, `Tax / Sched E`;
   `Portfolio Financial Overview`, `Portfolio Equity`, `Total Debt Outstanding`,
   `Rental Portfolio NOI`, `Net Cash Flow (CADS)`; `PRIMARY RESIDENCE`, `RENTAL DOOR`;
   `CBD REVIEW`; `Consolidated Mortgage Ledger`, `Servicer Escrow Reserve Accounts`,
   `Ledger Principal`, `Servicer Reported`, `Clearing Balance`, `Calculated Balance`,
   `Reported Balance`, servicer names; `Transaction Register`;
   `Reconciliation & Review Queue`, `CBD-1 CASH RENT`, `CBD-2 MIXED CARD`,
   `Rule Suggestion`, `FULLY ALLOCATED`, `Net Operating Income (NOI)`.
   *(Verified against `tests/test_mobile_ui.py`, `tests/test_client_demo_web.py`,
   `tests/test_review_queue_ui.py`.)*
4. **Routes, form actions, field names, and JS function names** used by inline handlers
   (`openSplitModal`, `addSplitRow`, `updateSplitMath`, `openVendorRulesModal`,
   `openNewRuleModal`, `openEditDetailsModal`, `openUploadPhotoModal`, `openLightbox`).
5. **Formatting filters** (`format_cents`, `format_cents_sign`, `format_bps`,
   `format_date`) and every accounting calculation. No business logic changes.

---

## 8. Priority summary

| Priority | Findings | Why first |
| :--- | :--- | :--- |
| **P0** | C1 (inert classes), C2 (focus), M1–M2 (duplicate tokens, none exist) | The style layer must become authoritative before any visual change can be trusted. |
| **P0** | C4 (queue count), C7 (missing vs zero), C8 (double sign) | Presenting a wrong financial number is worse than presenting an ugly one. |
| **P1** | C3 (contrast), C5 (drawer), C6 (labels), M13 (`th scope`) | Accessibility debt with the widest blast radius. |
| **P1** | M3 (duplicated nav / hardcoded portfolio), M6–M8 (badges, headers) | Removes the structural cause of per-screen drift. |
| **P2** | M4–M5 (radii, type scale), M9 (property screen), M10 (tiny type), §4 minors | Visual coherence; mostly falls out of the component library. |
| **P3** | §5 opportunities | Polish and scope expansion. |

Continue to `02_VISUAL_DESIGN_SYSTEM.md` for the proposed direction.

> **Disposition of every finding in this audit** is tracked in
> `07_IMPLEMENTATION_ROADMAP.md` §9 (finding → fix trace), together with the verified
> evidence for each fix. Findings marked P1/P2 in that document are consciously deferred.
