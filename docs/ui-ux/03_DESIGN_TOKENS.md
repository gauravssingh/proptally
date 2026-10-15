# PropTally — Design Tokens

**Document:** `03_DESIGN_TOKENS.md`
**Status:** Normative. Implemented in `web/static/css/tokens.css`.
**Date:** October 2026

Every value in the interface comes from a token in this document. A raw hex, px font
size, radius, or shadow in a template or component rule is a defect (`02` §5.2).

Contrast ratios below were measured with WCAG 2.1 relative luminance. The requirement is
**4.5:1** for body text, **3.0:1** for large text (≥18.66px bold / 24px) and for
non-text UI boundaries. Values marked ✅ pass; values used only as fills or decorations
are marked accordingly.

---

## 1. Token file

```css
/* web/static/css/tokens.css — the only place raw values may appear. */
:root { /* … see §2–§10 … */ }
```

Linked first, before `base.css` and `components.css` (`04` §1). A future dark theme is a
`[data-theme="dark"]` block that re-points the *semantic* tokens only; component rules
never reference a raw value, so they need no change.

---

## 2. Colour

### 2.1 Chrome (dark surfaces)

| Token | Value | Use |
| :--- | :--- | :--- |
| `--pt-navy-950` | `#020617` | Mobile app bar, bottom nav, drawer panel — the deepest chrome layer. |
| `--pt-navy-900` | `#0f172a` | Desktop sidebar, environment strip, "total" ledger row. |
| `--pt-navy-800` | `#1e293b` | Active nav item, chrome borders, hover on chrome. |
| `--pt-navy-700` | `#334155` | Chrome hover, dividers on dark. |

> **Resolves audit M1.** The stylesheet declared `.bg-navy-900` as both `#0b1329` and
> `#0f172a`, and hardcoded a third navy in the drawer panel. There is now one ramp.

### 2.2 Surfaces (light)

| Token | Value | Use |
| :--- | :--- | :--- |
| `--pt-bg` | `#f1f5f9` | Main application canvas (clear separation from white cards). |
| `--pt-surface` | `#ffffff` | Primary content containers: cards, tables, modals, flyouts. |
| `--pt-surface-sunken` | `#eaf0f6` | Table headers, KPI footers, inset stat cells, filter bars. |
| `--pt-surface-hover` | `#f1f5f9` | Table row and list-item hover. |
| `--pt-surface-selected` | `#d1fae5` | Selected transaction row (subtle emerald tint). |

### 2.3 Text

| Token | Value | Contrast | Use |
| :--- | :--- | :--- | :--- |
| `--pt-text` | `#0f172a` | 17.85 white / 15.55 sunken ✅ | Primary body, headings, table cell values. |
| `--pt-text-secondary` | `#334155` | 10.35 white / 9.02 sunken ✅ | Supporting body, secondary cells. |
| `--pt-text-muted` | `#64748b` | 4.76 white ✅ | Metadata, captions, KPI sub-labels on white surfaces. |
| `--pt-text-muted-sunken` | `#475569` | 6.57 sunken / 7.56 white ✅ | Metadata, captions, inset stat labels on sunken surfaces. |
| `--pt-text-disabled` | `#98a2b3` | exempt | Disabled control labels only. |
| `--pt-text-on-dark` | `#f1f5f9` | 16.30 navy-900 ✅ | Chrome body text. |
| `--pt-text-on-dark-muted` | `#cbd5e1` | 12.02 navy-900 ✅ | Chrome secondary text, version strings. |

> **Resolves Sunken Surface Contrast.** Normal text on `--pt-surface-sunken` (`#eaf0f6`) requires
> a dedicated darker tone to pass WCAG AA (≥ 4.5:1). `--pt-text-muted-sunken` (`#475569`) provides
> a measured **6.57:1** ratio on sunken surfaces (comfortably exceeding the 4.5:1 threshold) while
> maintaining clear hierarchy below `--pt-text-secondary` (`#334155`).

### 2.4 Semantic families

Each family is a tinted badge pair (`‑bg` + `‑text`), a border, and a solid fill. All
`‑text` on `‑bg` pairs pass 4.5:1.

| Family | `-bg` | `-text` | `-border` | `-solid` | Pair ratio | Meaning |
| :--- | :--- | :--- | :--- | :--- | --: | :--- |
| `--pt-success-*` | `#dcfce7` | `#166534` | `#bbf7d0` | `#15803d` | 6.49 ✅ | Verified, posted, allocated, reconciled, money-in. |
| `--pt-warning-*` | `#fef3c7` | `#92400e` | `#fde68a` | `#d97706` | 6.37 ✅ | Human action required: review, authorization, shortage, filter-empty. |
| `--pt-error-*` | `#ffe4e6` | `#9f1239` | `#fecdd3` | `#be123c` | 6.68 ✅ | Negative, unallocated, failed, destructive. |
| `--pt-info-*` | `#dbeafe` | `#1e40af` | `#bfdbfe` | `#1d4ed8` | 7.15 ✅ | Statement-derived, awaiting post, informational. |
| `--pt-neutral-*` | `#f1f5f9` | `#334155` | `#e2e8f0` | — | 9.45 ✅ | Unknown, personal, not applicable, disabled. |
| `--pt-rule-*` | `#e0e7ff` | `#3730a3` | `#c7d2fe` | `#4338ca` | 8.06 ✅ | Secondary/accent: vendor rules, batch actions, classification. |

`--pt-secondary-*` is the `--pt-rule-*` family; the "secondary colour" of the brand is
indigo, used only for classification/automation affordances so that emerald keeps a
single meaning (money-in / primary action).

### 2.5 Accent (brand)

| Token | Value | Use | Contrast |
| :--- | :--- | :--- | :--- |
| `--pt-accent` | `#047857` | Primary button fill, focus ring, links, active nav | white label **5.48** ✅ |
| `--pt-accent-hover` | `#065f46` | Primary hover/active | white label 7.0 ✅ |
| `--pt-accent-soft` | `#d1fae5` | Subtle emerald tint background | — |
| `--pt-accent-border` | `#a7f3d0` | Subtle brand border (decoupled from success) | — |
| `--pt-accent-text` | `#047857` | Emerald text on light surfaces | 5.48 white ✅ |
| `--pt-accent-on-dark` | `#34d399` | Emerald on navy chrome | 9.29 navy-900 ✅ |
| `--pt-accent-decor` | `#10b981` | Dots, bars, chart fills (never text) | — |

> **Deliberate change.** The app fills primary buttons with `emerald-700` (`#047857`, **5.48:1**).
> `--pt-accent-border` decouples primary accent outlines from operational success borders.

### 2.6 Borders, dividers, and overlays

| Token | Value | Use |
| :--- | :--- | :--- |
| `--pt-border` | `#e2e8f0` | Card, table, input default border. |
| `--pt-border-strong` | `#cbd5e1` | Hover/emphasis border, input hover. |
| `--pt-border-chrome` | `#1e293b` | Border between chrome layers. |
| `--pt-divider` | `#f1f5f9` | Row/table dividers inside a surface. |
| `--pt-focus-ring` | `2px solid var(--pt-accent)` | Focus indicator (`outline`). |
| `--pt-white-alpha-08` | `rgba(255, 255, 255, 0.08)` | Subtle chrome surface badge bg. |
| `--pt-white-alpha-12` | `rgba(255, 255, 255, 0.12)` | Dark surface neutral badge bg. |
| `--pt-white-alpha-20` | `rgba(255, 255, 255, 0.2)` | Dark surface badge hover bg. |
| `--pt-white-alpha-30` | `rgba(241, 245, 249, 0.3)` | Dark surface border default. |
| `--pt-white-alpha-35` | `rgba(241, 245, 249, 0.35)`| Dark surface border emphasis. |
| `--pt-badge-dark-accent-bg` | `rgba(4, 120, 87, 0.28)` | Brand badge bg on navy chrome. |
| `--pt-badge-dark-accent-border` | `rgba(52, 211, 153, 0.45)` | Brand badge border on navy chrome. |
| `--pt-badge-dark-rule-bg` | `rgba(99, 102, 241, 0.28)` | Rule badge bg on navy chrome. |
| `--pt-badge-dark-rule-border` | `rgba(199, 210, 254, 0.45)` | Rule badge border on navy chrome. |

### 2.7 Financial semantics

Strict separation between financial movement and operational status:

| Token | Value | Rule / Contrast |
| :--- | :--- | :--- |
| `--pt-money-positive` | `#047857` | Income and inflows (rent, deposits). 5.48 ✅ |
| `--pt-money-negative` | `#be123c` | Expenses and outflows (repairs, interest). 6.29 ✅ |
| `--pt-money-neutral` | `var(--pt-text)` | Balance sheet movements: mortgage principal paydown and escrow funding (cash movement that is *not* an operating expense). |
| `--pt-money-unavailable` | `var(--pt-text-muted)` | No evidence recorded. Rendered as `—`, never `$0.00`. |
| `--pt-money-positive-on-dark` | `var(--pt-accent-on-dark)` | Positive inflow on dark chrome (`#34d399`). 9.29 ✅ |
| `--pt-money-negative-on-dark` | `#fda4af` | Negative outflow on dark chrome. 9.44 ✅ |

Rules:
1. Rental income and gross receipts use positive emerald.
2. Operating expenses, repairs, property taxes and mortgage interest use negative rose.
3. Mortgage principal paydown and escrow account funding must remain neutral.
4. Equity and property valuations must not be classified as operating income.
5. Every amount carries a sign glyph and tabular numerals.

### 2.8 Dedicated Data Visualization Palette

Visualizations require a distinct series palette that avoids reusing the brand accent indiscriminately:

| Token | Value | Assigned Metric / Meaning | Contrast vs White | Standard Applied |
| :--- | :--- | :--- | --: | :--- |
| `--pt-chart-1` | `#047857` | Rental income & gross inflows | 5.48:1 ✅ | Text & Graphical UI |
| `--pt-chart-2` | `#2563eb` | Operating expenses & vendor disbursements | 5.17:1 ✅ | Text & Graphical UI |
| `--pt-chart-3` | `#7c3aed` | Property valuation & net equity | 5.70:1 ✅ | Text & Graphical UI |
| `--pt-chart-4` | `#d97706` | Mortgage interest service (fill/segment) | 3.19:1 ✅ | Graphical UI Boundary only (≥ 3:1) |
| `--pt-chart-5` | `#0891b2` | Mortgage principal paydown (fill/segment) | 3.68:1 ✅ | Graphical UI Boundary only (≥ 3:1) |
| `--pt-chart-6` | `#64748b` | Other cash movements & reserve transfers | 4.76:1 ✅ | Text & Graphical UI |
| `--pt-chart-7` | `#be123c` | Negative cash flow & deficits | 6.29:1 ✅ | Text & Graphical UI |

**Accessible Text Variants for Chart Labels:**
| Token | Value | Target Metric | Contrast vs White | WCAG Status |
| :--- | :--- | :--- | --: | :--- |
| `--pt-chart-text-4` | `#92400e` | Mortgage Interest label text | **6.37:1** | Pass AA (≥ 4.5:1) |
| `--pt-chart-text-5` | `#0e7490` | Mortgage Principal label text | **4.87:1** | Pass AA (≥ 4.5:1) |

Requirements:
- Consistent color-to-metric mapping across charts, waterfall breakdowns, and stacked bars.
- Do not assign colors based on random series order.
- Always accompany chart colors with labels, legends, or tabular tooltips; never rely on color alone.
- In legends (`.pt-chart-legend`), label text uses `--pt-text-secondary` (`#334155`, 10.35:1) while swatches carry the graphical series token.
- Graphical fills (`#d97706` and `#0891b2`) are never applied directly to body text; text labels consume `--pt-chart-text-4` and `--pt-chart-text-5`.

---

## 3. Typography

**Sans** `--pt-font-sans`: `-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif`
**Mono** `--pt-font-mono`: `ui-monospace, SFMono-Regular, "SF Mono", Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace`

System stacks only — no webfont request, preserving zero egress.

| Token | Size / line-height | Weight | Tracking | Applied to |
| :--- | :--- | :--- | :--- | :--- |
| `--pt-fs-kpi` | 24px / 1.2 | 700 | `-0.02em` | KPI card value. |
| `--pt-fs-title` | 24px / 1.3 | 700 | `-0.02em` | `h1` page title. |
| `--pt-fs-section` | 18px / 1.4 | 600 | `-0.01em` | `h2` card/section title. |
| `--pt-fs-subsection` | 16px / 1.5 | 600 | normal | `h3` modal/panel title. |
| `--pt-fs-body` | 14px / 1.5 | 400 | normal | Body copy, table content. |
| `--pt-fs-supporting` | 13px / 1.5 | 400 | normal | Dense table cells, help text, financial amounts. |
| `--pt-fs-caption` | 12px / 1.4 | 500 | normal | Metadata, sub-labels, captions. **Floor.** |
| `--pt-fs-label` | 12px / 1.4 | 600 | normal | Form labels. |
| `--pt-fs-badge` | 12px / 1.3 | 600 | normal | Badges, chips, filter pills. |
| `--pt-fs-th` | 12px / 1.4 | 600 | `0.04em` | Table headers (uppercase). |

Weights: `--pt-fw-regular: 400`, `--pt-fw-medium: 500`, `--pt-fw-semibold: 600`,
`--pt-fw-bold: 700`. Tracking: `--pt-tracking-tight: -0.02em`,
`--pt-tracking-wide: 0.04em` (body text uses the browser default, so no token exists for
`normal`).

Numeric rule: `--pt-font-mono` + `font-variant-numeric: tabular-nums` for every currency
amount, rate, percentage, date, account number, and id. Implemented as the `.pt-num`
utility so alignment is not re-decided per table.

> **Resolves audit M5/M10.** The 9-value scale including `9px`/`10px`/`11px` is replaced
> by 11 roles on a 12/13/14/16/18/24/30 scale, with 12px as the floor.

---

## 4. Spacing

4px base grid; nine steps. Nothing in the interface uses an off-scale gap.

| Token | Value | Typical use |
| :--- | :--- | :--- |
| `--pt-space-1` | 4px | Icon/label gap, badge padding-block. |
| `--pt-space-2` | 8px | Inline gaps, compact stack. |
| `--pt-space-3` | 12px | Form field gap, table cell padding-block. |
| `--pt-space-4` | 16px | Card grid gutter, section inner padding. |
| `--pt-space-5` | 20px | Card padding. |
| `--pt-space-6` | 24px | Section spacing, modal padding. |
| `--pt-space-8` | 32px | Page padding, major section spacing. |
| `--pt-space-10` | 40px | Page top spacing. |
| `--pt-space-12` | 48px | Empty-state padding. |

**Vertical rhythm rules:** `h2` → content 16px; card → card 20px; page header → first
section 24px; form row → form row 16px; label → control 4px.

---

## 5. Layout and dimensions

| Token | Value | Notes |
| :--- | :--- | :--- |
| `--pt-sidebar-w` | 256px | Fixed; collapses to drawer below `lg`. |
| `--pt-topbar-h` | 44px | Desktop environment strip. |
| `--pt-appbar-h` | 52px | Mobile top app bar. |
| `--pt-bottomnav-h` | 72px | Mobile bottom navigation (includes safe-area padding). |
| `--pt-drawer-w` | 288px | Mobile/tablet slide-over. |
| `--pt-content-max` | 1280px | Content column cap. |
| `--pt-content-pad-x` | 32px / 16px | Desktop / mobile. |
| `--pt-card-pad` | 20px | Standard card padding. |
| `--pt-card-pad-lg` | 24px | Hero/statement card padding. |
| `--pt-table-row-h` | 40px | Default. |
| `--pt-table-row-h-compact` | 32px | Dense tables (>50 rows). |
| `--pt-input-h` | 36px | Text/select/date input. |
| `--pt-input-h-sm` | 32px | Inline/filter inputs. |
| `--pt-btn-h` | 36px | Default button. |
| `--pt-btn-h-sm` | 32px | Table-row and header actions. |
| `--pt-btn-h-xs` | 28px | Inline/badge-level actions. |
| `--pt-icon-sm` | 14px | Inline with 12–13px text. |
| `--pt-icon-md` | 16px | Default UI icon. |
| `--pt-icon-lg` | 20px | Nav item icon. |
| `--pt-icon-xl` | 24px | App bar/menu icon. |
| `--pt-modal-w-sm` | 448px | Confirm / single-field. |
| `--pt-modal-w-md` | 576px | Forms. |
| `--pt-modal-w-lg` | 768px | Split allocator. |
| `--pt-modal-w-xl` | 1024px | Rules manager / tables. |
| `--pt-modal-max-h` | 90vh | Applied with internal scroll. |

---

## 6. Border radius

| Token | Value | Use |
| :--- | :--- | :--- |
| `--pt-radius-sm` | 4px | Badges, chips, tags, small controls. |
| `--pt-radius-md` | 6px | Buttons, inputs, selects, nav items. |
| `--pt-radius-lg` | 8px | Cards, tables, panels, table wrappers. |
| `--pt-radius-xl` | 12px | Modals, drawer panel, hero panel, KPI cards. |
| `--pt-radius-full` | 9999px | Pills, dots, avatars, toggle tracks. |

One radius per component class, decided here — not per screen (audit M4).

---

## 7. Borders and shadows

Border widths: `--pt-border-w: 1px`, `--pt-border-w-strong: 2px` (focus ring, selected
tile, statement total row).

| Token | Value | Use |
| :--- | :--- | :--- |
| `--pt-shadow-xs` | `0 1px 2px rgba(15,23,42,.05)` | KPI/stat card resting state. |
| `--pt-shadow-sm` | `0 1px 3px rgba(15,23,42,.08), 0 1px 2px rgba(15,23,42,.04)` | Cards, buttons. |
| `--pt-shadow-md` | `0 4px 12px rgba(15,23,42,.08), 0 2px 4px rgba(15,23,42,.04)` | Hover elevation, dropdowns. |
| `--pt-shadow-lg` | `0 20px 32px -8px rgba(15,23,42,.24), 0 8px 16px -8px rgba(15,23,42,.12)` | Modals, drawers, lightbox. |

Shadows are navy-tinted (`15,23,42`), replacing the three raw-black shadows the audit
found. Elements are flat at rest and elevate at most one level on hover; modals are the
only `lg` users.

---

## 8. Z-index

| Token | Value | Layer |
| :--- | :--- | :--- |
| `--pt-z-sticky` | 30 | Mobile app bar, sticky table headers. |
| `--pt-z-bottomnav` | 40 | Mobile bottom navigation. |
| `--pt-z-backdrop` | 50 | Modal scrim. |
| `--pt-z-modal` | 51 | Modal panel. |
| `--pt-z-drawer-scrim` | 60 | Drawer scrim. |
| `--pt-z-drawer` | 61 | Drawer panel. |
| `--pt-z-toast` | 80 | Notifications. |

The current sheet mixes `z-30`, `z-40`, `z-50`, and a raw `100/101/102` for the drawer;
the drawer now sits *above* modals by token, deliberately, because it is the outermost
navigation layer.

---

## 9. Motion

| Token | Value | Use |
| :--- | :--- | :--- |
| `--pt-transition-fast` | 120ms | Colour/hover. |
| `--pt-transition` | 180ms | Panels, dropdowns. |
| `--pt-ease` | `cubic-bezier(.2, 0, .2, 1)` | Default easing. |

Only three animated things exist: hover colour (120ms), overlay enter/leave (180ms), and
the status dot pulse. All are wrapped in `@media (prefers-reduced-motion: reduce)` to
become instant/no-op. The audit's `animate-in`/`fade-in`/`zoom-in-95` classes — which
never resolved to anything — are removed rather than implemented; modals appear without
entrance animation by design (`02` §1.2: no decorative motion).

---

## 10. Breakpoints

CSS custom properties cannot be used inside media queries, so these are the normative
values that component rules must use:

| Token | Value | Columns | Shell |
| :--- | --: | :--- | :--- |
| `--pt-bp-sm` | 640px | 2 | KPI cards 2-up. |
| `--pt-bp-md` | 768px | 2 | Grids 2-up; shell still drawer-based. |
| `--pt-bp-lg` | 1024px | 4 | 256px sidebar becomes fixed; drawer and bottom nav are removed. |
| `--pt-bp-xl` | 1280px | 12 | Multi-column statement layouts. |

> **Deliberate change.** The sidebar currently appears at 768px (`md:`), where a 256px
> sidebar plus a 4-column KPI grid does not fit. The switch moved to 1024px so tablets
> and small laptops get the drawer and bottom navigation.
