# PropTally — Component Library

**Document:** `04_COMPONENT_LIBRARY.md`
**Status:** Normative. Implemented in `web/templates/components/ui.html` (components),
`web/templates/components/shell.html` (application shell), and
`web/static/css/components.css` (styling).
**Date:** October 2026

---

## 1. How to use the library

```jinja
{% import "components/ui.html" as ui with context %}
```

* Screens pass **data and slots**. They never restate component styling and never
  introduce a new class; a genuinely new pattern is added here first (`02` §5.6).
* Component structure is emitted by macros (`<form>` wrappers, ARIA roles, scrims,
  `scope="col"`, `hidden` handling). Leaf content is written with the documented
  classes (`pt-field`, `pt-kpi`, …).
* Multi-element components that cannot take two `caller()` slots are split into
  `*_open()` / `*_close()` pairs.
* `docs/ui-ux/07_IMPLEMENTATION_ROADMAP.md` §5 defines the automated check that fails
  the build when a class is used that the design system does not define.

Optional accessibility gaps are stated per component; a component's documented
behaviour is a requirement, not a suggestion.

---

## A. Application layout

| Component | Macro / class | Purpose |
| :--- | :--- | :--- |
| Application shell | `base.html` + `.pt-shell` | Chrome beside canvas; one layout for every screen. |
| Sidebar | `shell.sidebar(doors, active_page, active_property, review_count)` | Fixed 256px navigation at `≥1024px`, grouped into Workspaces and Current portfolio; the shell names the active portfolio and shows a membership-scoped switcher when there is more than one. |
| Top environment strip | `shell.topbar()` | Deployment posture and locality statement. |
| Workspace selector | `shell.workspace_switcher(id)` | Membership-scoped native select in the sidebar and drawer. It switches the active portfolio for this session only; the current portfolio's name is also visible in the brand. |
| Mobile app bar | `shell.appbar(doors, active_property, review_count)` | Menu, brand with active portfolio name, mode badge, current-portfolio door switcher, queue bell. |
| Drawer | `shell.drawer(...)` | Same navigation and workspace switcher as the sidebar, one markup source. |
| Bottom navigation | `shell.bottomnav(active_page, active_property, review_count)` | Five highest-frequency destinations. |
| Page header | `ui.page_header(title, subtitle, meta)` | Title + optional meta badges; caller supplies actions. |
| Section card | `ui.section_open/close` | Card with title, subtitle, actions, flush body. |
| Content container | `.pt-content` | 1280px cap, tokenised padding, 20px rhythm. |
| Centred single-column layout (card) | `.pt-auth` + `.pt-auth__card` | Standalone shell for the screens shown before a portfolio context exists: `register.html`, `reset.html`, `reset_password.html`. `auth_base.html` provides it; the screens inside it are ordinary cards, fields and buttons from this library. |
| Scene panel layout (artwork) | `.auth-root`, `.auth-scene`, `.auth-panel`, `.auth-form` (`auth.css` §4) | The same shell with the neighbourhood scene behind it and the form frosted over the scene's own contrast halo: `login.html` and `invite.html`, the two screens a person meets first. Field surfaces come from the utility set on the input (`bg-white/95 backdrop-blur-xl border-2 border-white rounded-xl shadow-lg`), scoped in `auth.css` §3; icons, reveal toggle, CTA and steps are in §5–§8. |
| Account row | `shell.account_controls()` (`.pt-account`, `.pt-account__identity`, `.pt-account__name`, `.pt-account__meta`) | At the foot of the sidebar and drawer: monogram, name, muted `ROLE · email`, and a separate sign-out icon control. The identity links to personal sign-in settings (`/account?section=signin`); tenant Administration is a separate navigation destination. |
| Avatar | `.pt-avatar` (+ `--pt-avatar-size`) | Monogram circle for a person: initials, on the on-dark ramp so it reads on the chrome. A self-hosted instance has no photos to fall back on, so the initials are the image. |

**Required data.** `doors` is a `Property` sequence (from `navigation.get_nav_doors`);
the shell derives labels, order and the `(Primary)` qualifier. A door absent from
`DOOR_SHORT_LABELS` gets a derived short label, so a new property needs no template edit.

**Behaviour.** The drawer and every modal are hidden with the `hidden` attribute, so a
closed overlay is out of the accessibility tree and out of the tab order (resolves audit
C5/m5). Escape closes the topmost overlay and returns focus to the opener; the drawer's
scrim and the modal scrim are real `<button>`s, so they are reachable and announce a name.

**Responsive.** Sidebar and top strip are `≥1024px`; app bar, drawer and bottom nav are
`<1024px`. Bottom-nav items are ≥44px tall with safe-area padding (audit `06` §2).

---

## B. Navigation and actions

### B1–B5. Button — `ui.btn(label, variant, size, href, type, icon_name, icon_after, block, disabled, attrs, title, id)`

| Variant | Fill | Use |
| :--- | :--- | :--- |
| `primary` | `--pt-accent` (`#047857`, white label 5.48:1) | The one committed action on a screen: *Post*, *Upload*, *Export*. |
| `neutral` | `--pt-navy-900` | High-emphasis, non-branded creation actions (*New transaction*, *Manual entry*). |
| `secondary` | surface + strong border | Alternative/adjacent action: *Cancel*, *Reset filters*, *Previous*. |
| `ghost` | transparent | Toolbar and header affordances, close controls. |
| `destructive` | `--pt-error-solid` | Delete/void. Always paired with a confirmation. |
| `subtle` | `--pt-neutral-bg` | Low-emphasis inline affordance (*Split / Assign*). |
| `chrome` | transparent + on-dark text | Actions sitting on the dark chrome (sidebar and drawer footers). Never use `ghost` or `secondary` there: their canvas colours are unreadable or a solid white pill against `--pt-surface-chrome`. |

Sizes: `md` 36px, `sm` 32px (table rows, headers), `xs` 28px (inline/chips level).
`block=true` for full width. Disabled buttons set both `disabled` and
`aria-disabled`, keeping the tokenised disabled colours of `base.css`.

**Accessibility.** Renders `<button>` unless `href` is given, in which case `<a>`. Every
button must have a visible text label; icon-only controls must use `ui.icon_btn(aria_label, …)`.

### B6. Icon-only control — `ui.icon_btn(aria_label, icon_name, variant, size, href, …)`

`aria_label` is a required positional argument — an unlabelled icon control cannot be
built with this library. Minimum 32px target at `md`.

### B7. Button group — `.pt-btn-group`

A flex row with `--pt-space-2` gap. Use for a primary plus one secondary; three or more
actions belong in a `ui.section_open(actions=…)` header or a modal footer.

### B8. Segmented navigation — `.pt-segmented`

For mutually exclusive short options (tax year, period granularity). Items are links with
`is-active`; the active item carries `aria-current="page"`. Height 28px, radius `md`.

---

## C. Information presentation

### C1. Card — `ui.card_open(pad, cls, interactive)` / `ui.card_close()`

Surface, 1px border, radius `lg` (8px), `--pt-shadow-xs`, 20px padding (`lg` = 24px).
`interactive=true` adds a hover border/shadow lift (used by dashboard door cards).
`pt-card--sunken` for inset panels; `pt-card--selected` for the active tile (used by
property selection).

### C2. Section — `ui.section_open(title, subtitle, actions, flush, cls)` / `ui.section_close()`

Card + header + body. `flush=true` removes body padding so tables sit edge-to-edge.
`actions` is a pre-rendered HTML string:

```jinja
{% set actions %}{{ ui.btn("Export CPA CSV", href=..., icon_name="download", size="sm") }}{% endset %}
{{ ui.section_open("Schedule E workpaper", "Per-property mapping", actions=actions, flush=true) }}
...table...
{{ ui.section_close() }}
```

### C3. Page header — `ui.page_header(title, subtitle, meta)`

One header for every screen (resolves audit M8). `meta` takes badges/status chips;
the caller block supplies actions, so the action row is always right-aligned and
bottom-aligned with the title stack.

### C4. KPI card — `ui.kpi(label, value, sub, evidence_kind, tone)`

Value is pre-rendered HTML — pass `ui.amount(...)` so sign and colour are consistent.
`tone ∈ {neutral, positive, negative, unavailable}`. One KPI row per screen, maximum
four cards; the evidence badge sits in the header row.

### C5. Stat cell — `ui.stat(label, value, sub, boxed, tone)`

Compact metric for dense rows: portfolio basis bars, escrow balances, loan terms.
`boxed=true` adds the sunken inset treatment; `on_surface=true` renders the inset on a
white tile inside a sunken panel (escrow "Calculated / Reported" pair).

### C6. Statement row — `ui.statement_row(label, value, line, note, kind, indent)`

Financial statement line. `kind ∈ {"", subtotal, carry, total, graded}`:

* `subtotal` — sunken row for NOI/OCF.
* `carry` — `subtotal` plus a 2px top rule, to mark a carried-forward subtotal.
* `total` — navy chrome row (dashboard CADS, tax Line 22).
* `graded` — sunken row with a top border, between subtotal levels.

**Rule (audit C8).** Outflow magnitudes are rendered with `ui.amount(cents, mode=…)`; a
manual `-` is never typed in front of a formatted value, because the formatter already
emits a sign for negative input.

### C7. Badge — `ui.badge(label, variant, mono, upper, pill, dot, size, title)`

Variants: `neutral`, `success`, `warning`, `error`, `info`, `rule`, `accent`, `chrome`,
`outline`. Radius `sm` (4px); `pill=true` for full radius. `mono=true` for machine
tokens (`POSTED LEDGER`, `CBD-1`), `upper=true` for status text.
A badge never carries meaning by colour alone: the label states it.

### C8. Amount — `ui.amount(cents, mode, size, strong, cls, unavailable_title)`

The **only** renderer for financial values.

| `mode` | Output | Tone | Used for |
| :--- | :--- | :--- | :--- |
| `signed` (default) | `+$1,234.56` / `-$1,234.56` / `$0.00` | emerald / rose / neutral | Transaction amounts, CADS, KPIs |
| `income` | `+$1,234.56` | emerald | Rent and deposit lines |
| `expense` | `-$1,234.56` | rose | Cost lines in a statement |
| `movement` | `-$1,234.56` | neutral | Principal paydown and escrow funding: real cash movement, not an expense |
| `plain` | `$1,234.56` | neutral | Basis, valuation, balances |

`cents is none` renders `—` with a title, never `$0.00` (resolves audit C7). `0` renders
`$0.00` in neutral tone, so zero and absent are distinguishable at a glance.

### C9. Alert — `ui.alert(variant, title, text, action_label, action_href, icon_name, strip)`

`variant ∈ {info, warning, error, success, neutral}`. `strip=true` for the full-bleed
shell banner. `warning`/`error` carry `role="alert"` so the message is announced.
Sentence-case title, one sentence of body, at most one action link.

### C10. Key/value and metadata rows — `.pt-kv`, `ui.meta_row(items)`

Right-aligned value column with a divider between pairs. Metadata rows are caption-size,
muted, and wrap at `--pt-space-2`.

### C11. Activity item — `.pt-activity`

Marker + body + meta. Used for escrow movements and posting history.

---

## D. Data presentation

### D1/D2. Table — `ui.table_open(compact, label, cls)` / `ui.th(label, num, center)` / `ui.table_close()`

`table_open` emits `.pt-table-wrap` (border, radius, surface, `--pt-shadow-xs`) and the
horizontal scroll container; `th()` always emits `scope="col"` (resolves audit C6/m13).
Column header: 12px/600 uppercase `--pt-tracking-wide` on `--pt-surface-sunken`.
Row height 40px (32px compact); numeric cells use `.pt-table__num` (right-aligned,
mono, tabular). Two-line cells use `.pt-cell-stack` with `.pt-provenance`.
`ui.th_row(label, colspan)` emits a `<th scope="colgroup">` group divider.

**Subtext discipline.** `.pt-provenance` (or `ui.provenance`) is *evidence about the
figure* — `POSTED LEDGER`, `FROM STATEMENT`, `NO STATEMENT`, `IN-FLIGHT`. Anything else
that simply needs a second muted line (city, loan number, term, id) uses
`.pt-table__sub`. Both render identically; the distinction keeps "provenance" meaningful
when it is used to decide whether a number can be trusted.

### D3. Pagination — `ui.pagination(page, total_pages, start_item, end_item, total_items, prev_url, next_url)`

Shows `Showing a–b of n` plus Previous/Next and the page position. Disabled states are
non-interactive `<span>`s carrying `aria-disabled`.

### D4. Filter bar — `.pt-filterbar` + `ui.text_field`/`ui.select_field`/`ui.search_field`

5-column grid at `≥1024px`, 2 at `≥768px`, 1 stacked. Actions row separated by a divider.

### D5. Filter chips — `ui.chip(label, href, active, count)`

Repeat-vendor fast filters and saved views. `count` renders in a `.pt-chip__count`.
The active chip is accent-filled **and** carries more weight (not colour alone).

### D6. Empty table state — `ui.empty_state(...)`

See F9. A table that can return zero rows must render an empty state instead of a bare
`<tbody>` (audit: `/transactions` currently renders nothing at all).

### D7. Row actions — `.pt-row-action`, `ui.btn(variant="subtle", size="sm")`

One primary row action plus optional secondary actions; full row menu only when three or
more actions exist. `size="sm"` (32px) is the row-action size because the touch-target
table (`06_RESPONSIVE_AND_ACCESSIBILITY.md` §2.7) requires a table row action to be at
least 32px; `size="xs"` (28px) is for inline and chip controls only. A destructive row
action is `variant="destructive"` and confirmed (interaction standard §3).

---

## E. Forms and inputs

Every field macro emits `<label for>` + control with a matching `id`, plus `aria-describedby`
for help/error text and `aria-invalid` on error — resolving the 31 unlabelled controls the
audit found.

| Component | Macro |
| :--- | :--- |
| Text input | `ui.text_field(name, label, value, type, placeholder, required, help, error, id, hidden_label)` |
| Number / currency | `ui.number_field(name, label, value, step, …)` — mono, tabular |
| Date | `ui.date_field(name, label, value, …)` — mono |
| Select | `ui.select_field(name, label, options, selected, placeholder, …)` |
| Text area | `ui.textarea_field(name, label, value, rows, …)` |
| Checkbox | `ui.checkbox_field(name, label, value, checked, help, touch)` |
| Search | `ui.search_field(name, label, value, placeholder, hidden_label)` — leading icon |
| Governance gate | `ui.cbd_gate(gate, name, label, id, checked, required, hint)` |
| Form grid | `.pt-form-grid` (+ `--2`, `--3`, `--4`) |
| Fieldset / section | `.pt-fieldset`, `.pt-form-section` |
| Actions | `.pt-form-actions` |

**CBD gates (G9).** `cbd_gate("cbd1", …)` renders amber filled (client authorisation);
`cbd_gate("cbd2", …)` renders indigo (external review). The hint text is mandatory for
CBD-2: it states that the attestation is not verified CPA approval. Client confirmation
never satisfies CBD-2, so the two are never merged into one "approve" control.

**Sizes.** `36px` default, `--sm` 32px for filter/inline rows. Touch targets reach 44px
via `.pt-check--touch`.

---

## F. Interaction components

| Component | Macro / class |
| :--- | :--- |
| Modal | `ui.modal_open(id, title, subtitle, size)` / `ui.modal_close()` |
| Confirmation dialog | `ui.modal_open(id, …, size="sm")` + destructive footer button |
| Drawer | `shell.drawer(...)` |
| Toast / success | `.pt-toast` (available; current screens report outcomes with an inline `ui.alert`) |
| Row action menu | `.pt-table__actions` with `ui.btn(variant="subtle", size="xs")`; use a menu only above three actions |
| Accordion | `.pt-accordion__summary` / `.pt-accordion__body` inside `<details>` |
| Loading | `.pt-spinner`, `.pt-progress` |
| Empty state | `ui.empty_state(title, message, action_label, action_href, icon_name)` |
| Error state | `ui.alert(variant="error")` inside the section that failed |
| Data visualization | `.pt-chart-bar`, `.pt-chart-bar__segment--[1-7]`, `.pt-chart-legend`, `.pt-chart-series--[1-7]` |

**Modal behaviour.** `modal_open` emits the scrim, `role="dialog"`, `aria-modal`,
`aria-labelledby`, a named close button and the `hidden` attribute. The controller in
`shell.html`: opens by `data-modal-open="<id>"`, closes on `[data-modal-close]`, Escape,
or scrim click; traps Tab inside the card; restores focus to the opener on close. Body
scrolls (`max-height: 90vh` via `--pt-modal-max-h`) while header and footer stay fixed.

**Modal widths.** `sm` 448px (confirm), `md` 576px (forms), `lg` 768px (split allocator),
`xl` 1024px (rules manager / tables).

**Toast vs alert.** Use an inline alert for state that belongs to the page (e.g. "batch
posted N transactions") and a toast only for background outcomes that have no host region.

**Data visualization.** Stacked distribution bars (`.pt-chart-bar`) and chart legends (`.pt-chart-legend`)
consume the dedicated 7-series color tokens (`--pt-chart-1` through `--pt-chart-7`). Series colors
are assigned consistently to financial dimensions (e.g., rental income, operating expenses, valuation,
interest, principal, reserves, deficit) and never rely on color alone to communicate financial figures.

---

## G. Domain components

### G1. Property identity — `.pt-property-identity`

Replaces the photographic hero (audit M9). Left accent bar (emerald for a rental door,
indigo for a primary residence) plus badges, name, address, actions and a media strip.
No gradients, no emoji, no image zoom. `--primary` modifier switches the accent and the
badge to the residence variant.

### G2. Financial summary grid — `.pt-summary-grid` (+ `--5`/`--4`/`--3`)

Uniform 2/3/4/5-column metric bars built from `ui.stat`, used by the portfolio basis bar,
escrow foot and loan term rows.

### G3. Provenance cell — `.pt-provenance-cell`, `ui.provenance(label)`, `ui.evidence(kind)`

The core domain component: a figure and the evidence behind it. The badge vocabulary is
closed and maps 1:1 to states the code can actually prove:

| kind | Badge | Variant | Meaning |
| :--- | :--- | :--- | :--- |
| `verified` | `VERIFIED` | success | Matched to an official statement and reconciled |
| `posted` | `POSTED LEDGER` | success | Derived from posted, immutable journal lines |
| `unposted` | `UNPOSTED` | neutral | No journal entry yet |
| `calculated` | `CALCULATED` | info | Deterministically computed from posted lines |
| `statement` | `FROM STATEMENT` | info | Reported by servicer/bank statement |
| `no_statement` | `NO STATEMENT` | neutral | No statement evidence on file |
| `no_posted` | `NO POSTED LINES` | neutral | Account has no posted lines yet |
| `movement` | `FROM MOVEMENTS` | neutral | Accumulated from recorded escrow movements |
| `estimated` | `ESTIMATED` | neutral | Manual input or external valuation |
| `allocated` | `FULLY ALLOCATED` | success | Every cent allocated |
| `partial` | `PARTIALLY ALLOCATED` | warning | Residual cents remain |
| `unallocated` | `UNALLOCATED` | error | Cannot post |
| `needs_review` | `NEEDS REVIEW` | warning | Awaiting a human decision |
| `awaiting_post` | `AWAITING POST` | info | Allocated, not yet posted |
| `in_flight` | `IN-FLIGHT` | warning | Obligation recorded, cash not settled |
| `action` | `ACTION REQUIRED` | warning | A gate must be satisfied first |
| `rule_match` | `RULE MATCH` | rule | Matched a saved vendor rule |
| `attested` | `USER-ATTESTED` | rule | Self-attestation of external review — **not** verified approval |
| `awaiting_data` | `AWAITING DATA` | neutral | Nothing ingested for this period |

**Deliberate omission.** The V2 spec's `[CPA APPROVED]` and `[SYSTEM RECONCILED]` are not
implemented: the application has no CPA role, no approval record and no independent
reconciliation sign-off, and rendering such a badge would misstate the evidence. CBD-2
is surfaced as `USER-ATTESTED` with the README's caveat in its tooltip.

### G4. Loan / escrow panel — `.pt-escrow`

Title, securing property, calculations vs reported balances, and projected tax/insurance/
shortage. Balances use `ui.stat(boxed=true, on_surface=true)`; the reported value shows
`—` (never `$0.00`) when no statement exists, with a `NO STATEMENT` provenance line.

### G5. Review queue item — `ui.review_item_open/close`

Merchant, amount, provenance badges, account/service/residual facts, an optional rule
suggestion band (`ACCEPT SUGGESTION`), and one action row that carries the governance
gates. Replaces the three competing posting layouts on `/reconciliation` (audit O2).

### G6. Batch action bar — `.pt-batchbar`

Navy chrome bar for applying one rule to many rows: pattern, category, property, count
and a single confirm action with an explicit confirmation step.

### G7. Media gallery — `.pt-gallery`, `ui.gallery_item(image_url, caption, is_cover, alt)`

16:9 tiles, cover badge, caption strip; actions revealed on hover **and** on focus-within
so they stay keyboard-reachable. The lightbox is a `ui.modal_open(size="xl")` variant
using `.pt-lightbox__image`.

### G8. Detail card — `.pt-detail-card`

Small information tile: depreciation basis, lease terms, loan terms.

### G9. Governance gate — `ui.cbd_gate(...)`

See §E.

---

## H. Document navigation (folder explorer)

The vault is navigated rather than filtered. Four components carry that, and they are
deliberately JS-free: the tree is `<details>`, the breadcrumb is links, and the folder cards
are links, so a folder can be opened with scripting switched off entirely.

### H1. Explorer — `.pt-explorer`

Two panes as a grid: the folder tree, then the contents of the open folder. One column
below `900px`, so on a phone the tree sits above the pane it navigates. Panes are ordinary
`.pt-card` / `.pt-card__body`, so nothing new is styled by this class beyond the columns.

### H2. Folder tree — `.pt-tree`, `.pt-tree__node`, `.pt-tree__summary`, `.pt-tree__link`, `.pt-tree__icon`, `.pt-tree__name`, `.pt-tree__count`, `.pt-tree__group`

* `.pt-tree__node` is a real `<details>`, opened (`open`) when it is the branch you are in;
  its `<summary>` carries the disclosure triangle, so expansion needs no script.
* `.pt-tree__link` is one row — icon, name, count — used for the root row, the folder row
  inside a `<summary>`, and each leaf folder. `.pt-tree__group` indents a node's children
  behind a divider so depth is visible without a second colour.
* `.pt-tree__count` is the number of documents the row holds. It follows the active search,
  the root row included, so the tree never claims more documents than the pane can show.
* The row for the open folder is marked `is-active` **and** `aria-current="page"`.
* **Branded for the vault only.** Any screen with a two-level identity (a property, then a
  kind of thing within it) can reuse it: the row content is a name and a count, nothing
  document-specific.

### H3. Folder card — `.pt-folders`, `.pt-folder`, `.pt-folder__top`, `.pt-folder__icon`, `.pt-folder__name`, `.pt-folder__meta`

Auto-filling grid of the subfolders of the open folder, each a link showing name plus
`N documents · size`. Folders come before the file table, the way a file manager lists a
folder's own subfolders ahead of its files. There is no "current" variant: the card grid
lists what is *inside* a folder, so the current folder is marked in the tree and breadcrumb.

### H4. Breadcrumb — `.pt-breadcrumb`, `.pt-breadcrumb__link`, `.pt-breadcrumb__here`, `.pt-breadcrumb__sep`

The path back up, one crumb per level of the tree. Every crumb but the last is a link
(`__link`); the last is the folder you are standing in, so it is text marked `__here` with
`aria-current="page"` — a link to the page you are on is noise for a keyboard user.
Separators are `aria-hidden="true"`. Wrapped in `<nav aria-label="Folder path">` so it
appears in the landmark list as what it is.

---

## I. Adding a component

1. Confirm no existing component covers the pattern; extend a variant instead.
2. Add the class rules to `components.css` using tokens only (`03`).
3. Add the macro to `ui.html` with a docstring comment stating purpose and required data.
4. Document it in this file: purpose, variants, required/optional data, behaviour,
   responsive rule, accessibility notes.
5. Run `python scripts/check_design_system.py` — it must pass.

*End of component library.*
