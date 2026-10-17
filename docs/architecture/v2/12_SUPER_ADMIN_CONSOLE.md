# PropTally Super Admin — the Backend Console

**Document:** `12_SUPER_ADMIN_CONSOLE.md`
**Author:** Principal Solution Architect
**Status:** Phases 1–2 implemented — see §6; §1–§5 are the binding design
**Date:** October 2026
**Context:** The platform's own administration: the application's backend operator, the
principal that manages the software and the tenants living on it.

Companion documents: `09_MULTI_TENANT_EXECUTION_PLAN.md` (control plane, milestones),
`10_MULTI_TENANT_PRINCIPLES.md` (the governing rules), `11_AUTHENTICATION_AND_SIGNIN_FLOW.md`
(the screens a *tenant* meets), `ADR-010` (local, zero-egress authentication).

---

## 1. What the super admin is

The **super admin** is the application's own backend operator. It is a principal *outside*
the platform's data model:

* it is **not** a portfolio role — `OWNER`, `MANAGER`, `CPA_REVIEWER`, `READ_ONLY` are
  memberships *in* a portfolio, and none of them is platform authority;
* it is **not** a tenant, a membership, or a portfolio owner;
* it is **not** a participant in anything it administers. It manages tenants, accounts and
  invitations, and it reads no one's ledger — not as a limit that could be lifted later, but
  because a row fetched from somebody's books has no place in a platform-administration
  request. Where a portfolio's figures are wanted, `portfolio_status()`
  (`db/portfolio_lifecycle.py`) supplies counts and storage, already read-only and already
  count-only.

### The three things that enforce it

| | Enforcement | Where |
| :-- | :--- | :--- |
| **Created outside** | The only way to make one is on the host. No web route mints a super admin; tenant registration explicitly does not. | `web.auth.create_super_admin`, `scripts/create_super_admin.py`; `web.auth.register_portfolio` leaves `is_super_admin=False` unconditionally |
| **Its own door** | Signs in at `/admin/login`, not `/auth/login`, and its session cookie is **path-scoped to `/admin`**, so the browser never sends it to the tenant application. | `web.auth.authenticate_super_admin`, `set_admin_cookie`, `AdminDependency` |
| **No portfolio context** | Its request context carries no engine, no vault paths and no tenant lease, so it cannot read a ledger even by accident. | `web.auth.AdminContext` |

---

## 2. Decisions taken

| Decision | Choice | Consequence |
| :--- | :--- | :--- |
| **Identity** | Own sign-in at `/admin/login`; the account lives in the control plane's `users` table but holds **no membership** | One credential system (Argon2id, sessions, CSRF), two completely separate doors |
| **Bootstrap** | **CLI on the host** — `scripts/create_super_admin.py --email …` | Works on a host with no tenants; a fresh deployment needs no default password |
| **Granting** | **Never over the web.** No route creates or promotes a super admin | Platform authority is reachable only with host access |
| **Onboarding grant** | A **single-use code**, valid for exactly one new-tenant signup (Phase 3) | Tiny blast radius per grant, unlike today's one global code |
| **Operator visibility of money** | **None.** Counts and storage only | No console route reads a ledger |
| **Suspension** | `SUSPENDED` is a real tenant state (Phase 4) | A portfolio can be paused without deleting it |

### Why the sign-in is separate

A tenant session cannot reach the console because the console reads a **different cookie**,
and the console's session cannot leak into a portfolio request because that cookie is scoped
to `/admin`. The two sign-ins may therefore coexist in one browser without ambiguity: being
signed in as a portfolio owner never makes you an operator, and being the operator never
makes you a tenant of somebody's portfolio.

---

## 3. The gate

`AdminDependency` (`web/auth.py`) resolves the console cookie to exactly one
`AdminContext`, and requires an **active** account that **is** a super admin:

* anonymous, expired, revoked, inactive, or an ordinary portfolio member → **404**, never
  403, so the console's existence is not advertised to the tenants on the platform;
* unsafe methods still require the session's CSRF token (principle **P17**), the
  pre-session double-submit being used only on the sign-in form itself;
* **no tenant operation lease is taken** — nothing the console reads comes from a ledger, so
  there is no writer to race.

The sign-in deliberately reuses the tenant sign-in's defences: uniform failure (a wrong
password, an unknown address and a *valid tenant account* all answer identically, hashing
the same work either way), and the same per-address+email throttle (5 failures / 15 min,
in-process — see `10_…` G4).

### Bootstrap and lifecycle

```bash
python scripts/create_super_admin.py --email ops@example.com          # prompts for the password
python scripts/create_super_admin.py --email ops@example.com --password '…'   # non-interactive
```

Idempotent: re-running rotates the password, ensures `is_super_admin`, and revokes the
account's existing sessions — which is also the lockout recovery for an operator, in the
same spirit as `scripts/issue_password_reset.py` for tenants.

`registration_is_open()` counts **non-super-admin** accounts
(`web.auth.count_portfolio_users`), so creating the operator on the host does not close the
door on the first tenant.

---

## 4. Feature list

### Phase 1 — The console, its gate and its read surfaces (implemented)

| # | Feature | Route | Built on |
| :-- | :--- | :--- | :--- |
| 1.1 | Own sign-in, own cookie, 404 for everyone else | `GET/POST /admin/login`, `POST /admin/logout` | `authenticate_super_admin`, `set_admin_cookie` |
| 1.2 | Console shell + admin nav, linked from the workspace only for a super admin | — | `templates/admin_base.html`, `navigation.instance_sections()` |
| 1.3 | Platform overview: account/portfolio/session counts, portfolios by state, control-plane path and size | `GET /admin` | control plane |
| 1.4 | Portfolio list: name, slug, state, owners, members, counts, storage, lease/migration state | `GET /admin/tenants` | `portfolio_status` |
| 1.5 | Portfolio detail: members and roles, pending invitations, provisioning and legacy-migration state | `GET /admin/tenants/{tenant_id}` | `tenant_members`, `pending_invites`, `LegacyMigration` |
| 1.6 | People list: email, super-admin flag, active, created, portfolios, live sessions | `GET /admin/people` | control plane |
| 1.7 | Person detail: memberships across portfolios, sessions (address, agent, expiry) | `GET /admin/people/{user_id}` | `UserSession` |

### Phase 2 — Operator actions (implemented)

Each wraps an existing, already-tested primitive; the platform acts on a tenant's behalf,
so every guard rail the primitive carries still applies.

| # | Feature | Route | Built on |
| :-- | :--- | :--- | :--- |
| 2.1 | Issue a password-reset link and display it once | `POST /admin/people/{user_id}/reset` | `issue_password_reset` |
| 2.2 | Invite a person into a portfolio in an invitable role | `POST /admin/tenants/{tid}/invites` | `create_invite` |
| 2.3 | Withdraw a pending invitation | `POST /admin/invites/{id}/revoke` | `revoke_invite` |
| 2.4 | Change a member's role | `POST /admin/tenants/{tid}/members/{mid}/role` | `change_member_role` |
| 2.5 | Remove a member's access | `POST /admin/tenants/{tid}/members/{mid}/remove` | `remove_member` |
| 2.6 | "A portfolio always keeps an owner" still holds (P13) | — | `_owner_count` |

### Phase 3 — Onboarding codes for new tenants (implemented)

The console mints per-grant codes; `/admin/onboarding` lists and withdraws them. One code
starts exactly one portfolio, and the grant works on an instance whose ordinary
registration closed long ago — the code *is* the permission (`/auth/join` is deliberately
not gated by `registration_is_open`).

| # | Feature | Built on |
| :-- | :--- | :--- |
| 3.1 | `TenantOnboardingInvite` (`db/system_models.py`): optional `email` pin, `portfolio_name_hint`, `token_hash`, `created_by_user_id`, `expires_at` (14 days), `accepted_at`, `accepted_tenant_id` — **single use** | `SYSTEM_TABLES` |
| 3.2 | Mint from the console: `GET`/`POST /admin/onboarding` | `web.auth.create_onboarding_invite` |
| 3.3 | Public redemption at `/auth/join?code=…`: creates the account, the portfolio and an `OWNER` membership, then signs the person in | `web.auth.accept_onboarding_invite` → `register_portfolio` |
| 3.4 | The list keeps spent and expired rows, and names the portfolio each used code produced | `web.auth.onboarding_invites` |
| 3.5 | Withdraw an unused code; its link stops working immediately | `web.auth.revoke_onboarding_invite` |
| 3.6 | A refused redemption does **not** spend the code | validation precedes the claim |

**Single use under concurrency.** Redemption claims the row with a conditional
`UPDATE … WHERE accepted_at IS NULL` *before* creating anything, so two simultaneous
redemptions yield one portfolio rather than two. Everything refusable (password policy,
address, an address that already has an account) is checked first, and a failure after the
claim releases it, so a mistyped password never burns the operator's link.

**Two doors, one grant.** A code is redeemed either from the console's link
(`/auth/join?code=…`, the form prefilled) or typed on `/auth/register`, which asks for one
whenever a super admin exists (`web.auth.registration_requires_code`). Both paths run the
same claim, so the code is single-use across both rather than once per page. On a host
nobody has claimed yet — no operator, nothing to mint with — the register page asks for
nothing, which is the bootstrap case.

**The environment code remains, and is documented as the lesser path.**
`PROPTALLY_PORTFOLIO_INVITE_CODE` is one global secret checked on `/auth/register`
(`web.auth.portfolio_code_matches`): no per-recipient expiry, no use count, no record of
who was given it. It survives because it works unattended — a deployment rebuilt from an
image can be opened with one variable rather than a database write — but the console is
the primary path, and the two are independent: the env code gates ordinary registration,
while an onboarding code is redeemed at `/auth/join`.

### Phase 4 — Monitoring: activity and traffic

Nothing is persisted today; `AccessEvent` exists and is never written (gap **G1**). All of it
is local-only (ADR-006).

| # | Feature | Notes |
| :-- | :--- | :--- |
| 4.1 | Write `AccessEvent` on invite / join / role change / remove / rename / delete | closes G1 |
| 4.2 | Security events: sign-in success/failure, throttle trips, session issued/revoked, reset issued/consumed | new control-plane table |
| 4.3 | Session view across accounts, with force-revoke | `UserSession` |
| 4.4 | Traffic: request counts, error rate, latency percentiles, top routes | in-process; per-worker (see `10_…` G4) |
| 4.5 | Health: uptime, disk free, control-plane and per-portfolio DB/WAL sizes, engine-cache occupancy, provisioning queue depth | `TenantManager` |
| 4.6 | Per-portfolio usage: last sign-in, last write, documents imported, storage growth (counts only) | `portfolio_status` |
| 4.7 | Backup status and scheduling guidance (`scripts/backup.sh` is not a service) | — |
| 4.8 | Retention: prune policy for event and stale-session rows | new |
| 4.9 | `SUSPENDED` tenant state: read-only lock, refused by request resolution like an inactive tenant | `TENANT_STATES`, check constraint |

### Lifecycle (deferred with §5)

| # | Feature | Note |
| :-- | :--- | :--- |
| L1 | Retry a `FAILED`/stale `PROVISIONING` portfolio | `retry_incomplete_provisioning` |
| L2 | Offboard a portfolio: typed-slug confirmation, summary, tombstone | needs an operator lease path |
| L3 | Export any portfolio | `build_portfolio_archive`; needs an operator lease path |

---

## 5. Why offboard and export are deferred

Deleting or exporting a portfolio touches its database and files while it may be in use. The
existing protection is `_acquire_tenant_operation` (`web/auth.py`), which leases a tenant
only for a session whose **membership** matches — a condition the super admin does not
satisfy, by construction. Doing L2/L3 correctly needs an **operator lease path**: a
`TenantOperation` row attributed to the operator, an explicit and auditable widening of who
may hold a portfolio's write lock. That is deliberate work, scheduled with suspension rather
than smuggled into the read/act phases.

---

## 6. Status

| Phase | Status | Verification |
| :--- | :--- | :--- |
| Phase 1 — console, gate, read surfaces | **Implemented** | `tests/test_admin_console.py` |
| Phase 2 — operator actions | **Implemented** | `tests/test_admin_console.py` |
| Sign-in, bootstrap, CLI, session scoping | **Implemented** | `tests/test_admin_console.py`; `tests/test_roles_and_authority.py` (the rename migration); `tests/test_tenant_hardening.py` (authority is not membership) |
| Phase 3 — onboarding codes | **Implemented** | `tests/test_admin_console.py` (mint → redeem → one portfolio; no second redemption; a refused redemption keeps the code; an address-pinned code refuses another address; a withdrawn code stops working; a tenant session cannot mint) |
| Phase 4 — monitoring, suspension | Planned | — |
| Lifecycle L1–L3 | Planned | — |

### What the tests hold

* a tenant session cannot reach any console route (404), and registering confers no platform
  authority — the only super admin is the one created on the host;
* a *valid tenant account* is refused at `/admin/login`, and the tenant door still works for
  it;
* the console session cookie is `Path=/admin`, so with only that cookie the tenant
  application sees no session at all;
* a console POST without a token is refused and writes nothing;
* the console reports counts and never a currency figure;
* the last owner of a portfolio cannot be demoted or removed from the console either.

---

*End of Super Admin Console specification (revision 2, October 2026).*
