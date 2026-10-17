# PropTally Authentication & Sign-In Flow

**Document:** `11_AUTHENTICATION_AND_SIGNIN_FLOW.md`
**Author:** Principal Solution Architect
**Status:** Current — describes the implemented screens
**Date:** October 2026
**Context:** Every screen a person meets before they have a portfolio, and how they move
between them. Companion to `10_MULTI_TENANT_PRINCIPLES.md` (the rules these screens must
obey), `ADR-010` (the authentication decision) and `docs/ui-ux/` (the design system).

The surface is six page templates — sign-in, invitation, registration, onboarding
redemption, reset and set-password — plus the shared `auth_base.html` shell, one router
(`web/routes/auth.py`, sectioned `# --- Sign in`, `Registration`, `Onboarding codes`,
`Invitations`, `Password reset`, `Account screen`) and one stylesheet
(`static/css/auth.css`, sections 1–8). The console's own door (`admin/login.html`) is the
seventh, documented in `12_SUPER_ADMIN_CONSOLE.md`.

---

## 1. Surfaces

`templates/auth_base.html` provides two shells for unauthenticated pages.

| Surface | Classes | Used by | Why |
| :--- | :--- | :--- | :--- |
| Scene panel — neighbourhood | `.auth-root`, `.auth-scene`, `.auth-panel`, `.auth-form` (`auth.css` §4) | `login.html`, `invite.html`, `admin/login.html` | The scene is the product's first impression and its contrast halo carries legibility for the form |
| Scene panel — calm | same classes, `register-calm.svg` | `register.html`, `join.html` | Same first impression for the two pages that *start* a portfolio, with artwork that holds still |
| Plain card | `.pt-auth`, `.pt-auth__card` (`components.css`) | `reset.html`, `reset_password.html` | Lighter pages, no artwork |

`auth.css` is linked only from `auth_base.html`, so the authenticated shell never loads it.

Both shells share the same behaviour script (`auth_base.html:36-126`), which is
progressive enhancement only — every flow works with scripting off:

* `[data-password-toggle]` — reveal/hide a password, with `aria-pressed` and a label that
  changes (`:42`)
* `[data-pane-toggle]` — swap between the sign-in and reset steps inside one box, animating
  the box's height and moving focus to the incoming heading (`:81`)
* `form[data-loading-form]` — set `data-loading="true"`, swap the button label, and disable
  the submit so a form cannot be sent twice (`:115`)

---

## 2. Sign in — `/auth/login` (`routes/auth.py`, `login_form` / `login_submit`)

One box, two steps. The sign-in step is `data-pane="signin"`; the reset step is
`data-pane="forgot"` (`templates/login.html:38`, `:141`). The reset step opens by itself
when the server has a verdict to show.

An already-signed-in visitor is redirected to their destination rather than shown the form
(`:199`). A signed-in user with more than one membership is sent to the portfolio chooser
and returned to the page they asked for (`:245`); the app never silently opens the oldest
portfolio.

| `error_kind` | Words shown | Recovery offered |
| :--- | :--- | :--- |
| `credentials` | "That email and password did not match" | link to the reset step, both fields marked `aria-invalid` |
| `throttled` | "One moment" — too many attempts, sign-in paused for a few minutes | states plainly that nothing is wrong with the account |
| `expired` | "That took a while" — the form timed out and nothing was submitted | try again |
| `nomember` | "No portfolio on this account" | ask the operator to add them to one |
| `invite_dead` | "That invitation cannot be used" — accepted, withdrawn or expired | ask for a new invitation, or sign in |

The route names the kind of failure; the words and the recovery live in one map in the
template (`login.html:44-65`), so a failure reads the same wherever it is raised. An
unknown kind falls back to "Could not sign in" rather than rendering nothing.

The form carries `next` (validated by `_safe_next`), a hidden `csrf_token`, and
`autocomplete` on both fields. The email field takes `autofocus`, except after a credential
failure, where the password does — the reader has already proved the email.

---

## 3. Accepting an invitation — `/auth/invite/{token}` (`invite_form` / `invite_submit`)

The invitation page states, all of it read from the invitation row through one helper
(`_render_invite`): the portfolio being joined, the role as a badge
and in words, the address the invitation is pinned to, and the day the link stops working.

| Field | Behaviour |
| :--- | :--- |
| Your name | Optional, remembered across a failed attempt |
| Email | `readonly`, not `disabled` — the address is the identity being granted access, so it stays readable, selectable and announced (`invite.html`, styled at `auth.css:339`) |
| Password | `minlength` matching the server's minimum, show/hide toggle, help text stating that an existing account on this host uses that account's password |

States and failures:

| Situation | Result |
| :--- | :--- |
| Live invitation | The invitation page |
| Password too short | Same page, same portfolio, name kept, the policy reason from `accept_invite` |
| Address already has an account and the password does not match it | "An account already exists for this email; sign in to accept the invitation." |
| Already a member of that portfolio | "This account is already a member of that portfolio." |
| Form expired (CSRF) | Same page with "Your form expired. Please try again." |
| Spent, withdrawn or expired token | The **sign-in page with the reason** (`_invite_is_gone` → `error_kind="invite_dead"`), because by then the reader may already have an account and the useful next step is to sign in or ask for a new invitation |
| Success | Session issued, signed in, redirected to the dashboard |

Two defects fixed here are worth not re-introducing. A spent token used to render
`login.html` with an `error` variable that template never reads, so the reader landed on an
unexplained sign-in page; and that path called `issue_pre_session_csrf()` twice, which on a
cookie-less first visit renders one token beside a different cookie and made the reader's
next submission fail as *expired* (principle **P17**).

---

## 4. First run — `/auth/register` (`register_form` / `register_submit`)

Creates the person, their portfolio, the OWNER membership and a session in one step. An
existing account is never created again; joining somebody else's portfolio goes through an
invitation. It confers **no** platform authority: what it creates is a tenant.

Fields: name, portfolio name, email, password — and an **invitation code** whenever one is
required. The rule is stated by `web.auth.registration_requires_code` and turns on whether
anybody has claimed the host:

| The host | The page asks for a code? | Why |
| :--- | :--- | :--- |
| Nobody has registered, no operator exists | **No** | There is no one to ask and nothing to mint one with. This is the bootstrap case. |
| An operator exists (a super admin) | **Yes** | They can mint codes, so the door is theirs to open one grant at a time rather than standing open to anyone who can reach the host. |
| `PROPTALLY_ALLOW_REGISTRATION=true` | Only if a global code is configured | The explicit "wide open anyway" override, honoured as written. |

The code typed there is interchangeable with the one minted in the console: a live
single-use `TenantOnboardingInvite` is redeemed on this page exactly as it is at
`/auth/join`, and it is spent by whichever page used it. `PROPTALLY_PORTFOLIO_INVITE_CODE`
— the older single global secret — is still accepted in the same field.

The page stays *reachable* while only a code opens the door, because it is the page that
code is typed on; submitting without a valid one is refused and creates nothing.

The platform's own operator — the **super admin** — is a different principal and does not
appear here at all: it is created on the host with `scripts/create_super_admin.py` and signs
in at its own door, `/admin/login` (`12_SUPER_ADMIN_CONSOLE.md`).

### Onboarding codes — `/auth/join?code=…` (`join_form` / `join_submit`)

The other way a portfolio begins, and the one that works on a *busy* instance. An operator
mints a code in the console (`/admin/onboarding`) and hands the link over; redeeming it
creates the person, their portfolio and an `OWNER` membership, then signs them in.

| Situation | Result |
| :--- | :--- |
| Live code | The redemption form, prefilled with the suggested portfolio name and, if the code is pinned, the address (`readonly`) |
| Code pinned to another address | Same page, states the address it was issued for |
| Password too short | Same page, the policy reason; **the code is not spent** |
| That address already has an account | "An account with that email already exists; sign in instead." |
| Already redeemed, withdrawn or expired | Same page: invalid, already used, or expired — one answer for all three, so a wrong guess learns nothing |
| Success | Session issued, signed in, redirected to the dashboard |

Two properties are deliberate. It is **not** gated by `registration_is_open`: the code is the
permission, which is what lets an operator open one portfolio at a time after ordinary
registration has closed. And the claim is a conditional `UPDATE … WHERE accepted_at IS NULL`
performed *before* anything is created, so simultaneous redemptions produce exactly one
portfolio.

---

## 5. Password reset

Reset tokens are never issued to a web requester — this deployment has no mailer
(`ADR-010` §2). The flow:

1. **Get a token.** The installation operator runs
   `python scripts/issue_password_reset.py --email user@example.com` on the host and hands
   the token over out of band (`README.md:139`). `/auth/reset` (`:411`) and the
   reset step inside the sign-in box explain exactly this, from one shared partial
   (`templates/components/reset_help.html`) so the two cannot drift.
2. **Present it.** `POST /auth/reset/token` (`:421`). A live token is forwarded to
   `/auth/reset/<token>`; a dead one keeps the reader on the step with the reason. Neither
   answer says anything about any account. The link is a real form post, so this works with
   no script at all.
3. **Set the password.** `/auth/reset/{token}` (`:456`, `:465`) sets it, revokes every other
   session for that account, clears the cookie and returns to sign-in — a reset does not
   sign you in, so the new credentials are proved immediately.

Token lifetime is 2 hours, single use (`RESET_LIFETIME`, `consume_password_reset`).

---

## 6. Signed-in account actions

All on `/account` (`:492`) except switching, and all behind the shared dependency:

| Action | Route | Notes |
| :--- | :--- | :--- |
| Invite somebody | `POST /account/invites` (`:587`) | Owner only. The generated link is shown on the page for the owner to pass on |
| Withdraw an invitation | `POST /account/invites/{id}/revoke` (`:649`) | The link stops working immediately |
| Change a member's role | `POST /account/members/{id}/role` (`:611`) | Cannot leave the portfolio without an owner |
| Remove a member | `POST /account/members/{id}/remove` (`:633`) | Same guard; the person keeps their account |
| Change my password | `POST /account/password` (`:566`) | Requires the current password; revokes other sessions |
| Switch portfolio | `POST /auth/switch-tenant` (`:665`) | Only to a portfolio the session's user is a member of |
| Sign out | `POST /auth/logout` (`:256`) | Requires the CSRF token, so a hostile page cannot sign somebody out silently |

Lifetimes and limits: session 12 hours (`SESSION_LIFETIME`), invitation 7 days (`INVITE_LIFETIME`),
reset 2 hours (`:77`), 5 failed sign-ins per 15 minutes per address+email pair (`:80`).

---

## 7. Interaction rules

**Tab order follows the task, not the layout.** On the sign-in form the order is
email → password → show-password → **Sign in** → Forgot password? (`login.html:83`,
`:96`, `:101`, `:111`, `:126`). The reset link sits after the submit in the document
*and* on screen: it used to sit in the password label's row, which put an escape hatch
between the two fields somebody actually has to fill in. Verified by pressing Tab from
the email field on the running server, not by reading the markup.

* No positive `tabindex` anywhere — order is document order.
* The pane swap moves focus to the incoming step's heading (`auth_base.html:98`), and the
  heading is `pt-sr-only` on the sign-in step because the box is self-evident visually.
* Leaving a step clears it: typed values, a revealed password and the previous verdict all
  reset (`auth_base.html:64`), so a step never shows the previous attempt's complaint.
* Failures arrive with `.auth-reveal` (a short fade), disabled under
  `prefers-reduced-motion`; the alert carries `role="alert"` via `ui.alert`.
* Help text is wired with `aria-describedby` to the field it describes (the
  `pt-field__help` ids in `invite.html`).
* The submit shows a spinner and disables itself; the label states what is happening.

---

## 8. The artwork underneath

The sign-in and invitation pages sit on `static/img/login-neighborhood.svg`.

* The asset is served as an `<img>`, so page CSS and JavaScript cannot reach inside it.
  Every animation therefore lives in the SVG's own `<style>` block (clouds, the three
  portfolio pins, the sun's bloom, the kite, the birds, the car on the crescent), and all of
  it is switched off under `prefers-reduced-motion`.
* The scene carries its own contrast halo at **720,365 of a 1440×900 viewBox**; the form is
  centred on it, which is why the fields are frosted rather than sitting on an opaque card.
* **Motion stays out of the centre band** (roughly x 520–920, y 240–560): anything moving
  there competes with the form and, at most sizes, reads as flicker behind the glass.
* The form is positioned by `.auth-panel`'s large-screen rule (`auth.css` §4), not by
  absolute coordinates in markup, so a phone gets a bare form rather than a cropped scene.
* CSS transforms on SVG elements default to rotating about the middle of the viewBox. Any
  animated group inside a translated wrapper needs `transform-box: fill-box` (or an explicit
  `transform-origin`) or it swings around the viewBox corner instead of turning in place.

---

## 9. Verification

Checked against the running server rather than by reading the code:

| Check | Result |
| :--- | :--- |
| Tab order from the email field | email → password → show-password → Sign in → Forgot password? |
| Spent invitation token | the sign-in page states "That invitation cannot be used" |
| Sign-in immediately after that page | judged as *credentials*, **not** CSRF-expired — the pre-session token and cookie agree |
| Live invitation | states the portfolio, the role and "This link stops working on …" |
| Refused short password | same page, same portfolio, the typed name kept, the policy reason shown |
| Full flow on a scratch instance | register → invite → accept as a fresh visitor: session issued, dashboard renders, READ_ONLY membership row written |
| Suite | `tests/test_user_management.py`, `tests/test_tenant_hardening.py`, `tests/test_roles_and_authority.py`, `tests/test_password_reset.py`, `tests/test_multitenant_isolation.py`, `tests/test_multitenant_deployment.py` |

Known gaps in this surface, recorded rather than implied: `register.html`,
`reset.html` and `reset_password.html` still use the plain card while sign-in and
invitation use the scene panel, so the family reads as two products; and the gaps in
`10_MULTI_TENANT_PRINCIPLES.md` §9 apply here too (nothing writes `AccessEvent`; the
pending-invitation list does not filter expired rows; invitation creation does not
validate the email address).

---

*End of Authentication & Sign-In Flow (revision 1, October 2026).*
