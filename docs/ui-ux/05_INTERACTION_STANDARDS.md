# PropTally — Interaction and UX Standards

**Document:** `05_INTERACTION_STANDARDS.md`
**Status:** Normative
**Date:** October 2026

---

## 1. Principles

1. **One action, one meaning.** `Post` always writes to the ledger. `Save` never does.
   A control's verb is the same on every screen.
2. **Reversible or confirmed.** Reads and navigation are immediate. Anything that writes
   to an immutable ledger is either confirmed explicitly or reversible by a recorded
   compensating entry.
3. **State is visible.** After an action the screen states what changed, in the region it
   changed, and by how much.
4. **The server is the authority.** Client-side validation prevents wasted round trips;
   it never replaces a domain check. When the domain rejects, the message is shown on the
   field or in the section that owns it.
5. **No dead affordances.** A control that looks interactive is interactive (audit: the
   `/transactions` filter strip renders non-interactive `All Accounts`/`All Doors` chips).

---

## 2. Creating and editing records

| Pattern | Rule |
| :--- | :--- |
| Entry point | One primary button in the page header (`+ New …`), or a row action for record-scoped creation. |
| Surface | Peak-6-field forms use a modal (`size="md"`); longer flows use a full screen section. |
| Field labels | Visible labels, sentence case, no placeholder-only labelling. Required fields marked `*` and `required`. |
| Defaults | Pre-fill what the system already knows (account, date = today, direction) and nothing it does not. |
| Save/Cancel | Footer order is Cancel then the primary action, right-aligned. Cancel is `secondary`; the primary is `primary`/`neutral`. |
| Unsaved changes | Cancelling a dirty modal asks for confirmation; navigating away with a dirty formwarns via `beforeunload`. |
| Success | The screen re-renders with the new state **and** states the change: `ui.alert(variant="success")` naming the record and the amount. |
| Failure | The action's own section shows `ui.alert(variant="error")` with the domain message; field-level errors sit under the field with `aria-invalid`. |

---

## 3. Destructive actions

* Never the first-positioned or default focus target.
* Always `variant="destructive"` and always confirmed: either a `size="sm"` confirmation
  modal stating the exact record and consequence, or a single `confirm()` where the action
  is trivially reversible (e.g. photo cover change).
* The confirmation states the blast radius in words and numbers: *"Delete this photo?
  This removes 1 file from the local vault."*
* Posted journal entries cannot be deleted; the UI must not offer it. Corrections are
  compensations (`ARCHITECTURE.md`) and are labelled `Reverse` / `Compensating entry`.
* The confirm button repeats the verb (`Delete photo`), never "OK".

---

## 4. Validation and feedback

| Situation | Presentation |
| :--- | :--- |
| Field-level | `.pt-field--invalid` + `.pt-field__error` with `role="alert"`, wired via `aria-describedby` and `aria-invalid`. |
| Form-level | `ui.alert(variant="error")` at the top of the form body. |
| Cross-field (allocation) | Persistent inline summary: allocated total, residual, and an explicit "over-allocated by $X" state. The submit button is disabled while the residual is invalid, **and** the reason is stated in text. |
| Governance gate | The gate control itself is the error surface: the blocked action explains which gate is unsatisfied (CBD-1 vs CBD-2) and why the other does not substitute. |
| Success | `ui.alert(variant="success")` with `role="status"` so assistive technology announces it. |
| Partial success | Batch operations report applied vs skipped counts with the reason for skips. |

**Timing.** Validate on submit, and on blur after the first submit attempt. Never validate
while the user is still typing the first time.

---

## 5. List, detail and navigation

* **List → detail:** the whole row's primary cell is a link; row actions are separate
  controls, never nested inside the link.
* **Detail → list:** a back link in the page header, at the same position on every screen.
  Browser back must also work — no client-side routing.
* **Breadcrumbs** appear only when depth > 2 (property → section). The shell has none today.
* **Filtering** is server-side and reflected in the URL, so a filtered view is shareable,
  bookmarkable and survives a refresh (`/reconciliation?q=APPLE&gate=CBD2`).
* **Empty results after filtering** must differ from "no data at all": filtered-empty names
  the filter and offers a reset (`06` §5).
* **Counts in the shell** always describe the whole queue, never the current filter
  (resolves audit C4). A page showing a filtered subset labels it as such next to the
  figure.

---

## 6. Reviewing and approving records

The reconciliation queue is the app's most consequential workflow, so it has the
strictest rules:

1. **Suggest → accept.** A rule match is a *suggestion*; accepting it is an explicit user
   action that states what will be posted (`ACCEPT SUGGESTION`).
2. **Assign → post.** Manual assignment posts in one step; the button says
   `Assign & Post →` and the target category and property are visible in the control before
   the click.
3. **Gates are per-gate.** CBD-1 (client authorisation) and CBD-2 (external review) are
   shown as separate controls with distinct copy. Neither satisfies the other, and the UI
   must never present a single "approve" that implies both. CBD-2 copy states that the
   attestation is a self-attestation, not verified CPA approval.
4. **Batch actions are confirmed and scoped.** The batch bar names the pattern, the count
   and the destination, and the confirm step restates the count in words.
5. **After posting**, the row leaves the queue and the success message reports the count
   and destination accounts.

---

## 7. Financial display conventions

These are presentation rules only. No accounting calculation or semantics change.

| Convention | Rule |
| :--- | :--- |
| Currency | `$1,234.56`; thousands separators; two decimals; never `$1234.5`. Rendered only through `ui.amount`. |
| Negative amounts | Signed form `-$1,234.56` in rose. An expense line in a statement uses `mode="expense"`. |
| Zero | `$0.00` in neutral tone. Distinct from unavailable. |
| Unavailable | `—` in muted tone with a title explaining what is missing. Never `$0.00` (audit C7). |
| Sign glyph | Always present on signed values, so colour is never the only signal. |
| Percentages | One decimal for rates (`3.125%`), whole numbers for LTV (`74%`). From `format_bps`. |
| Dates | `Mar 28, 2026`. Periods are named with explicit endpoints and never imply coverage the data does not have (`helpers.period_label`). |
| Debit vs credit | Direction is stated in words (`OUTFLOW (Expense)`) and by sign; ledger-side debit/credit language is not shown to clients. |
| Estimated vs verified | Every derived or estimated figure carries an evidence badge (`04` §G3). |
| Actual vs projected | Projected figures (escrow projections) are labelled `Projected`; actuals are unlabelled and evidenced. |
| Assets / liabilities / equity | Never netted without labelling: debt is shown as a liability amount, equity as its own figure with the `[ESTIMATED]` badge where valuation-driven. |
| Missing rows | A statement omits nothing: unavailable lines render with `—` and a provenance note rather than disappearing. |
| Alignment | Right-aligned, mono, tabular everywhere a figure is compared vertically. |
| Totals | The final total row is navy chrome (`kind="total"`), subtotals sunken, and a 2px rule marks a carried subtotal — so hierarchy is legible in greyscale and in print. |

---

## 8. Feedback after action

| Action | Feedback |
| :--- | :--- |
| Post allocation | Success alert with count and destination; row updates in place; queue badge decrements. |
| Batch rule apply | Success alert naming the rule and count applied. |
| Manual entry | Success alert naming the transaction; register updates. |
| Upload photo | Tile appears with `Cover` badge if chosen; caption preserved. |
| Filter change | URL updates, result count and filtered amount update together. |
| Nothing to do | The empty state — never a blank region. |

No action reports success with only a page reload. Where an outcome cannot be shown in
place, the alert states the resulting state (`Posted 3 transactions to <door> · Repairs`).

---

## 9. Loading and incomplete data

* Server-rendered pages do not show spinners; the browser's own progress is the indicator.
  `.pt-spinner` and `.pt-progress` exist for in-page async work (uploads).
* A slow/partial dataset renders the sections that have data and marks the rest
  `AWAITING DATA` rather than failing the page (the dashboard's "awaiting ingestion" banner
  becomes a provenance badge plus one explanatory alert, not a paragraph in the ledger).
* A section that fails renders `ui.alert(variant="error")` in place; the page still works.

---

## 10. Keyboard and pointer parity

Every action available by pointer is available by keyboard: overlays are opened by real
buttons, closed by Escape and by a named close control, and focus returns to the opener.
No action is bound to `hover` only — hover reveals, focus reveals equally
(`.pt-gallery__item:hover, .pt-gallery__item:focus-within`).

**Order follows the task, not the layout.** Focusable controls appear in the document in
the order somebody completes them, and layout never reorders what the keyboard does: no
positive `tabindex`, and no link parked inside a label row. On the sign-in form the reset
link sits *after* the submit — it used to sit beside the password label, which put an escape
hatch between the two fields the reader actually has to fill in. Where a control is placed
visually by something other than document order, fix the position in CSS rather than moving
the control; `tests/` and the live check both read the order as
`email → password → show-password → Sign in → reset link`.
