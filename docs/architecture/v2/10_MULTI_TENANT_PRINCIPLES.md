# PropTally Multi-Tenant Principles

**Document:** `10_MULTI_TENANT_PRINCIPLES.md`
**Author:** Principal Solution Architect
**Status:** Normative — binding on new code
**Date:** October 2026
**Context:** The rules the self-hosted multi-tenant platform is built on. Each one names where it is enforced in code and how it is proved. Rules that are *not* fully enforced yet are listed in §9 rather than implied to hold.

Companion documents: `09_MULTI_TENANT_EXECUTION_PLAN.md` (the plan and its milestones),
`ADR-009` (database-per-portfolio isolation), `ADR-010` (local, zero-egress
authentication), `11_AUTHENTICATION_AND_SIGNIN_FLOW.md` (the screens and flows).

---

## 1. Storage topology

**P1 — One control plane for identity, one database per portfolio for money.**
Identity, membership, sessions, invitations and reset tokens live in the control
plane (`storage/system.db`); every ledger, property, mortgage, transaction and
document-record table lives in `storage/tenants/<tenant_id>/proptally.db`.

*Enforced by:* `db/system_models.py` (the control models; `SYSTEM_TABLES` at
`db/system_models.py:268` is the explicit table list the control plane is created
from) against `db/models.py` (26 tenant-scoped tables). `db/tenant_manager.py`
creates and opens per-tenant databases and vaults.
*Proved by:* `tests/test_multitenant_isolation.py`, which asserts isolation at the
raw-SQL level — the other portfolio's file contains zero rows — rather than only
through the routes.

**P2 — The control plane holds no ledger data.** Nothing that belongs to a portfolio
is stored centrally "for convenience", because a central row is a row that has no
portfolio to isolate it.

**P3 — Isolation is by file, not by predicate.** A tenant table carries no
`tenant_id` column. There is no query to forget to filter; the only database session
a route can reach is the one for the portfolio its session names.

---

## 2. The portfolio of a request

**P4 — The active portfolio comes from the session cookie and nothing else.** Never
from a form field, query parameter, header or path segment. A request cannot ask for
a different portfolio than its session holds.

*Enforced by:* `web.auth.resolve_context()` and `web.auth.AuthenticationDependency`,
which resolve the cookie to exactly one `TenantContext` (tenant, membership, role,
ledger engine, vault paths) and hand it to the route. `web/routes/auth.py:665`
(`/auth/switch-tenant`) is the single place that changes the session's portfolio, and
it only accepts a tenant the user is already a member of.

**P5 — Every protected router mounts the shared dependency.** CSRF, session validity
and the role check are in one place so a route added later cannot omit any of them by
oversight.

*Enforced by:* `web.app` mounting `AuthenticationDependency` on each protected
router; mutations call `TenantContext.require_mutation()` / `require_admin()` /
`require_work()` (`web/auth.py:230-247`) from that context rather than re-deriving
authority locally.

**P6 — A route that needs a second portfolio may not open one.** Cross-portfolio
views read through an explicit, audited helper rather than by building a second
engine inline. (Owner-facing export and the portfolio list are the implemented
cases.)

---

## 3. Identity

**P7 — Identity is per person; membership is per portfolio.** One `users` row can
hold several `tenant_memberships`, and the app never silently picks one: a user with
more than one membership is sent to the chooser and returned to the page they asked
for (`web/routes/auth.py:208`).

**P8 — The email address is the identity key.** It is normalised on write
(`web.auth.normalise_email`, `web/auth.py:351`); an invitation is pinned to one
address and cannot be accepted with another.

**P9 — Joining an existing account requires that account's password.** If the invited
address already has an account, acceptance succeeds only when the presented password
verifies against it — otherwise the answer is "an account already exists for this
email; sign in to accept the invitation" (`web/auth.py:739`). An invitation is never
a way to take over an account.

**P10 — Self-registration is open only on an empty instance.** While the instance has
no account, `/auth/register` creates the first account and portfolio, and that account
becomes the instance administrator; once an account exists, registration closes and
the only way in is an invitation (`ADR-010` §6, `web.auth.count_users`).

---

## 4. Authority

**P11 — Four membership roles, checked by the shared dependency.** `OWNER`,
`MANAGER`, `CPA_REVIEWER`, `READ_ONLY` (`web/auth.py:89` describes each in the words
the UI shows). Authority is read from the membership row resolved from the session;
no route infers it from a request.

**P12 — Ownership is not conferred by invitation.** An invitation can only grant
`MANAGER`, `CPA_REVIEWER` or `READ_ONLY` (`web.auth.INVITABLE_ROLES`); ownership is
granted afterwards by promotion, and only by an owner.

**P13 — A portfolio always keeps at least one owner.** Demotion and removal are
refused when they would leave the portfolio ownerless (`web.auth._owner_count`,
`web/auth.py:425`).

**P14 — The acting user is taken from the session.** Audit attribution
(`actor_id`, `posting_actor`) is derived from the authenticated context
(`web/auth.py:263-287`); no route can set the actor, or the reviewer identity on an
authorization record, by writing a field it received.

---

## 5. Credentials and sessions

**P15 — Argon2id, constant-time verification, and uniform failure.** Login against a
non-existent account still performs a hash so timing does not reveal whether the
address exists, and all credential failures render one message with the recovery
action attached (`ADR-010` §1, `web/auth.py:130-135`).

**P16 — Sessions are opaque, stored as digests, expiring and revocable.** 32-byte
token in an HTTP-only, SameSite=Lax cookie; the database holds only its SHA-256
digest; 12-hour lifetime (`web/auth.py:75`); revoked on sign-out and all revoked when
the password changes (`web/auth.py:672`).

**P17 — CSRF is defence in depth, and the pre-session token is issued exactly once
per render.** Authenticated forms echo the session's token; unauthenticated forms
(sign-in, registration, invitation, reset) use a pre-session cookie issued when the
form is rendered. **Rendering must call `issue_pre_session_csrf()` once and use both
of its halves** (`web/auth.py:1070`): on a cookie-less first visit each call mints a
different token, so calling it twice renders one token beside a different cookie and
the reader's next submission is refused as expired. This is not hypothetical — it was
a live defect on the expired-invitation path until `937a473`.

**P18 — Login throttling is per address+email pair, in process.** 5 failures per 15
minutes (`web/auth.py:80-81`), a speed bump rather than a distributed limiter: with N
workers each process counts separately (`ADR-010`, Trade-offs).

---

## 6. Zero egress

**P19 — There is no mail transport, and none is required.** Invitations are links the
owner passes on; password resets are minted by the installation operator locally
(`web.auth.issue_password_reset`, `web/auth.py:824`) and handed over out of band. The
public reset page never creates or displays a token, and no account data, address or
login event leaves the host (`ADR-006`, `ADR-010`).

**P20 — Invitations are single use, expiring and withdrawable.** 7-day lifetime
(`web/auth.py:76`); acceptance claims the row atomically, so two simultaneous accepts
produce exactly one membership (`web/auth.py:739`); the owner can withdraw one from
`/account`, after which the link stops working. Reset tokens are single use and expire
after 2 hours (`web/auth.py:77`).

---

## 7. Failure behaviour

**P21 — A failure states what happened and offers the way forward.** The route names
the kind of failure; the template owns the words and the recovery action, so the same
failure reads the same wherever it is raised (see
`11_AUTHENTICATION_AND_SIGNIN_FLOW.md` §2).

**P22 — An unusable invitation explains itself.** A spent, withdrawn or expired token
renders the sign-in screen *with the reason*, and a way to ask for a new invitation —
it never drops the reader on an unexplained sign-in page.

**P23 — Failures never enumerate accounts.** Neither the reset step nor sign-in
reveals whether an address has an account.

---

## 8. Tenancy inside the ledger

**P24 — Per-entity ledger accounts.** A property, financial account or loan gets its
own ledger codes (`properties.asset_ledger_code`,
`financial_accounts.ledger_account_code`, `loan_accounts.*_ledger_code`) so two
portfolios' entities can never collide on an account code.
*Enforced by:* `domain.accounting.coa.ensure_entity_ledger_accounts()`,
`domain.accounting.metrics.load_ledger_code_map()`.

**P25 — A new tenant starts empty.** No sample data, no demo fixtures: a freshly
provisioned portfolio has its standard chart of accounts and the vendor rules that
are generic, and nothing that looks like somebody's records
(`tests/test_empty_tenant.py`).

---

## 9. Known gaps

Recorded here rather than left implied. Each is a defect against the principles above,
not a design decision.

| # | Gap | Where | Effect |
| :-- | :--- | :--- | :--- |
| G1 | `AccessEvent` is never written. The model and its table exist. | `db/system_models.py:218`; nothing inserts a row | The invited / promoted / removed history the table promises does not exist |
| G2 | `pending_invites()` does not filter expired rows | `web/auth.py:414`; the empty state at `templates/account.html:218` claims expired invitations are excluded | An owner sees invitations that can no longer be accepted, under copy that says otherwise |
| G3 | Invitation creation accepts any string as an email and does not de-duplicate | `web/auth.py:695` (self-registration does check for `@` at `web/auth.py:616`) | A malformed or duplicated invitation can be created and cannot be accepted |
| G4 | Login throttling is per process, not shared | `web/auth.py:312`; see `ADR-010` | With N workers, roughly N× the attempts are allowed |

---

## 10. Enforcement map

| Principle | Enforced in | Proved by |
| :--- | :--- | :--- |
| P1–P3 storage topology | `db/system_models.py:268`, `db/tenant_manager.py`, `db/models.py` | `tests/test_multitenant_isolation.py` |
| P4–P6 request portfolio | `web/auth.resolve_context()`, `web/routes/auth.py:665` | `tests/test_tenant_hardening.py`, `tests/test_multitenant_isolation.py` |
| P7–P10 identity | `web/auth.py:351-380`, `web/routes/auth.py:208`, `web/auth.py:739` | `tests/test_user_management.py` |
| P11–P14 authority | `web/auth.py:172-287`, `web/auth.py:425`, `web/routes/auth.py:611-648` | `tests/test_roles_and_authority.py` |
| P15–P18 credentials, sessions, CSRF | `web/auth.py:75-86`, `web/auth.py:896`, `web/auth.py:1070` | `tests/test_tenant_hardening.py`, `tests/test_multitenant_deployment.py` |
| P19–P20 zero egress, invitations | `web/auth.py:824`, `web/auth.py:739`, `web/routes/auth.py:649` | `tests/test_user_management.py`, `tests/test_tenant_hardening.py` |
| P21–P23 failure behaviour | `web/routes/auth.py:196-407`, template failure maps | `937a473` verification (see `11_…` §9) |
| P24–P25 ledger tenancy | `domain/accounting/coa.py` | `tests/test_multitenant_ingestion.py`, `tests/test_empty_tenant.py` |

---

*End of Multi-Tenant Principles (revision 1, October 2026).*
