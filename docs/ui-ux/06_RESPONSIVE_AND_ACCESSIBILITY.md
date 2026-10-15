# PropTally — Responsive Design and Accessibility

**Document:** `06_RESPONSIVE_AND_ACCESSIBILITY.md`
**Status:** Normative
**Date:** October 2026

Desktop is the primary target: PropTally is a workpaper for extended sessions. The
application must nevertheless remain *usable*, not merely renderable, on a tablet and a
phone, because a landlord checks the queue and photographs a receipt from a device in hand.

---

## 1. Breakpoints

| Token | Width | Columns | Shell | Notes |
| :--- | --: | :--- | :--- | :--- |
| — | < 640px | 1 | Drawer + bottom nav | Phone. Tables scroll horizontally. |
| `--pt-bp-sm` | ≥ 640px | 2 | Drawer + bottom nav | KPI cards 2-up; 2-column form grids. |
| `--pt-bp-md` | ≥ 768px | 2 | Drawer + bottom nav | 2–3 column grids; side-by-side page header. |
| `--pt-bp-lg` | ≥ 1024px | 4 | **Fixed 256px sidebar** | Drawer and bottom nav are removed. |
| `--pt-bp-xl` | ≥ 1280px | 12 | Sidebar + 12-col content | Statement grids, multi-panel rows. |

CSS custom properties cannot be used in media queries, so `tokens.css` records these as
`--pt-bp-*` documentation values and component rules hard-code the four widths.

> **Change from the audit.** The sidebar previously appeared at 768px, where a 256px
> sidebar plus a 4-column KPI grid does not fit. Navigation now switches at 1024px, so
> tablets get the drawer and bottom navigation (audit `01` §4, responsive findings).

---

## 2. Behaviour per surface

### 2.1 Shell

* `≥1024px`: sidebar is `position: sticky; height: 100vh`; the environment strip sits above
  the content; the page scrolls as one document.
* `<1024px`: sticky 52px app bar (menu, brand, mode badge, door switcher, queue bell);
  fixed bottom navigation with `env(safe-area-inset-bottom)` padding; the drawer overrides
  both at `--pt-z-drawer`.
* Content reserves space for the bottom bar: `padding-bottom: var(--pt-bottomnav-h) +
  var(--pt-space-6)` below 1024px, reset above.
* Page padding: 16px mobile, 32px desktop; content capped at `--pt-content-max` (1280px).

### 2.2 Navigation

| Element | Behaviour |
| :--- | :--- |
| Sidebar | Full labels, section headings, door list with rental/primary dots, queue count. |
| Drawer | Identical navigation (one markup source), 288px wide, capped at 85vw. |
| Bottom nav | Five destinations: Overview, Doors, Mortgages, Review, Tax/Sched E. Label text is always visible — never icon-only. |
| Door switcher | Native `<select>` in the app bar; the full door list scrolls natively. |
| Table of contents | None: the shell is shallow by design. |

### 2.3 Tables

* Every table lives in `.pt-table-wrap > .pt-table-scroll` (`overflow-x: auto`), so a wide
  financial table scrolls horizontally rather than breaking the page.
* Columns never wrap financial values: numeric cells are `white-space: nowrap` with
  `font-variant-numeric: tabular-nums`.
* The first column stays meaningful at small widths (identifier), and secondary columns
  (provenance sub-labels, notes) are the ones that scroll away first.
* Rows are 40px (32px compact) so a 44px touch target is not required for read-only data,
  but every row action is at least 32px and reachable without hover.

### 2.4 KPI and metric layouts

| Width | Layout |
| :--- | :--- |
| < 640px | KPI cards stack; value stays 24px; label/value never truncated. |
| ≥ 640px | 2-up KPI grid. |
| ≥ 1024px | 4-up KPI grid (the single KPI row per screen). |
| Summary grids | `pt-summary-grid--5` collapses 5 → 2 columns below 768px. |

### 2.5 Forms

* Single column below 640px; `pt-form-grid--2/3/4` above their breakpoints.
* Labels stay above controls at every width (never inline-left), so the stacking order is
  identical to the visual order.
* Modal forms become full-height sheets below 640px (`--pt-modal-max-h: 90vh` plus
  `.pt-modal__body` scrolling); footers stay pinned so the primary action is always visible.
* Inputs are 36px (32px inline); checkboxes use `.pt-check--touch` (44px) in filter bars
  and gate blocks.

### 2.6 Modals

| Size | Width | Mobile behaviour |
| :--- | --: | :--- |
| `sm` | 448px | Full width minus 16px gutter. |
| `md` | 576px | Full width minus 16px gutter. |
| `lg` | 768px | Full width minus 16px gutter. |
| `xl` | 1024px | Full width minus 16px gutter; internal scrolling for wide tables. |

Body scrolls; header and footer do not. `max-h-[90vh]` is now a real token
(`--pt-modal-max-h`), fixing the inert class that let the vendor-rules table overflow
without scrolling (audit M4).

### 2.7 Touch targets

| Control | Minimum |
| :--- | --: |
| Bottom navigation item | 44px |
| App bar buttons | 44px |
| Form control | 36px (44px for `.pt-check--touch`) |
| Table row action | 32px |
| Inline/chip control | 28px (chips in a filter row get ≥32px height) |

Targets are never adjacent without a gap: interactive rows and toolbars use
`--pt-space-2` minimum spacing.

---

## 3. Accessibility

### 3.1 Contrast (measured, WCAG 2.1)

The audit found `text-slate-400` at **2.45–2.56:1** on the label of nearly every KPI and
metadata line. The token layer replaces it and every documented pair was verified:

| Foreground | Background | Ratio | Requirement |
| :--- | :--- | --: | :--- |
| `--pt-text` `#0f172a` | surface / `--pt-bg` | 17.85 / 17.06 | 4.5 |
| `--pt-text-secondary` `#334155` | surface | 10.35 | 4.5 |
| `--pt-text-muted` `#52627a` | surface / `--pt-bg` / sunken | 6.20 / 5.93 / 5.66 | 4.5 |
| `--pt-text-on-dark` `#e2e8f0` | navy-900 | 14.48 | 4.5 |
| `--pt-text-on-dark-muted` `#a8b6c8` | navy-900 | 8.66 | 4.5 |
| `--pt-money-positive` `#047857` | surface / `--pt-bg` | 5.48 / 5.24 | 4.5 |
| `--pt-money-negative` `#be123c` | surface / `--pt-bg` | 6.29 / 6.01 | 4.5 |
| Badge pairs (success / warning / error / info / neutral / rule) | own tint | 6.49 / 6.37 / 6.68 / 7.15 / 9.45 / 8.06 | 4.5 |
| Primary button label (white on `--pt-accent`) | `#047857` | 5.48 | 4.5 |
| Destructive button label (white on `--pt-error-solid`) | `#be123c` | 6.29 | 4.5 |
| Focus ring (`--pt-accent`) | surface | 3.77 | 3.0 (non-text) |
| Negative amount on the navy total row | navy-900 | 9.44 | 4.5 |

The primary button fill moved from `emerald-600` (`#059669`, white label **3.77:1** —
below AA) to `emerald-700` (`#047857`, **5.48:1**).

### 3.2 Colour is never the only signal

| Meaning | Also conveyed by |
| :--- | :--- |
| Money in / out | A `+` or `-` sign character, and the column's context (income vs cost line). |
| Balance direction | The word (asset / liability / equity) and a labelled row. |
| Evidence | The badge text (`POSTED LEDGER`, `NO STATEMENT`, `—` for unavailable). |
| Allocation status | Badge text (`FULLY ALLOCATED`, `PARTIALLY ALLOCATED`, `UNALLOCATED`). |
| Governance gate | Distinct labels: CBD-1 *client confirmation* vs CBD-2 *external review attestation*. |
| Active navigation | Background, weight, `aria-current="page"`, accent icon, and (bottom nav) a top rule. |
| Rental vs primary residence | The `RENTAL DOOR` / `PRIMARY RESIDENCE` badge and the `(Primary)` qualifier, not just hue. |
| Statement hierarchy | Sunken subtotal rows, navy total row, and a 2px rule for carried subtotals — legible in greyscale. |

### 3.3 Focus visibility

`:focus-visible` draws a 2px `--pt-accent` outline with 2px offset on every interactive
element, with a `@supports not selector(:focus-visible)` fallback for older engines. The
audit's finding that focused buttons had `outline: none` (because the intended
`focus:ring-*` classes were never defined) is resolved by a rule on `:focus-visible`
itself rather than per-component classes.

### 3.4 Keyboard

* Full keyboard operability: every action is a real `<button>` or `<a>`; no click-only
  `<div>`s, no hover-only affordances (gallery actions show on `:hover` **and**
  `:focus-within`).
* Skip link (`Skip to content`) is the first focusable element.
* Overlays: the shell controller opens on a real button, focuses the first focusable
  element in the card, traps `Tab`/`Shift+Tab` within the card, closes on `Escape` or the
  named close control/scrim, and restores focus to the opener.
* Closed overlays use the `hidden` attribute, so they are out of the tab order **and** out
  of the accessibility tree — the audit found the closed drawer still tabbable and
  announced.
* Escape on `/transactions` now closes its modals (previously the page had no Escape
  handling at all).

### 3.5 Semantics

* One `<h1>` per screen (the page header title); sections are `<h2>`; modal titles are
  `<h3>`. Headings never skip a level (the audit found dashboards and property pages
  jumping `h1` → `h3`).
* Landmarks: `<aside>` (navigation), `<main id="main-content">`, `<header>` (app bar),
  `<nav aria-label>` for each navigation region, `<article>` per review item.
* Tables: `<th scope="col">` on every column header via `ui.th`, `<th scope="colgroup">`
  for group rows, `<caption>`/`aria-label` on data tables where a title is not adjacent.
* Status messages are announced: `role="alert"` on warning/error alerts and
  `role="status"` for success outcomes.
* Icons are `aria-hidden="true" focusable="false"` unless they are the only content of a
  control, in which case `ui.icon_btn(aria_label, …)` supplies the name.
* Decorative imagery (gallery photos) always carries `alt` — the caption when one exists,
  otherwise the property name.

### 3.6 Motion

Hover colour (120ms) and overlay entrance (180ms) are the only animations, plus the
status-dot pulse. All are disabled under `prefers-reduced-motion: reduce`. No entrance
animation is applied to content, so nothing is hidden behind an animation that may not
run.

### 3.7 Print

A print stylesheet hides the shell, drawer, bottom navigation, app bar and anything
marked `.pt-no-print`; forces a white background; and removes card shadows. The Schedule E
workpaper is the primary print target and its hierarchy (sunken subtotals, navy total row,
2px carried-subtotal rule) survives greyscale.

### 3.8 Zoom and text scaling

* Content reflows to 320px equivalent width without horizontal page scrolling (only data
  tables scroll, within their own wrapper).
* Body text is 14px minimum and no meaningful text is below 12px; the audit's 9px/10px
  status text is gone.
* `maximum-scale=5.0` (the viewport already allowed user zoom) — pinch-zoom is never blocked.

---

## 4. Verification performed

| Check | Method |
| :--- | :--- |
| Tokens resolve in the browser | `getComputedStyle` on `.pt-kpi__label` → `rgb(82, 98, 122)` = `--pt-text-muted` |
| Card surface, radius, shadow from tokens | `borderRadius: 8px`, `boxShadow: rgba(15,23,42,0.05) 0 1px 2px` |
| Total row uses chrome surface | `backgroundColor: rgb(15, 23, 42)` = `--pt-navy-900` |
| Sheet load order | `tokens.css` → `base.css` → `components.css`, asserted by `scripts/check_design_system.py` |
| Frames do not scroll horizontally | `.pt-table-wrap` gives every wide table its own scroll container |
| No inert classes remain | `scripts/check_design_system.py` (0 undefined classes) |
| Contrast pairs | Computed WCAG 2.1 ratios, tabulated in §3.1 and `03_DESIGN_TOKENS.md` §2 |

**Not yet automated, stated honestly:** there is no axe/pa11y run and no screen-reader
regression test in the suite. The checks above are computed-style and static-analysis
based. Adding an automated accessibility pass is item P1-4 in
`07_IMPLEMENTATION_ROADMAP.md` and would require a new dev dependency, which this exercise
deliberately avoided.

---

## 5. Responsive and accessibility checklist for a new screen

1. Composed from `components/ui.html`; no bespoke card, badge, button or table markup.
2. One `h1`, `h2` sections, `h3` modal titles.
3. Every control labelled; every control keyboard reachable; visible focus.
4. Every state present: populated, empty, unavailable/partial, error.
5. Every figure via `ui.amount` (sign + tone + unavailable) and every derived figure with
   an evidence badge.
6. Verified at 375px, 768px, 1280px: no horizontal page scroll, no clipped values, bottom
   navigation not overlapping content.
7. `python scripts/check_design_system.py` passes.
