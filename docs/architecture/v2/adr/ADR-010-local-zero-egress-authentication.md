# ADR-010: Local, Zero-Egress Authentication with Double-Submit CSRF

* **Status:** Accepted
* **Date:** October 2026
* **Deciders:** Principal Security Architect, Principal Systems Architect
* **Relates to:** ADR-006 (local AI boundary / zero egress), `06_SECURITY_AND_DEPLOYMENT.md §3`

## Context
Adding accounts to a self-hosted deployment introduces the first identity layer this
application has had. `06_SECURITY_AND_DEPLOYMENT.md §3.1` already specified Argon2id
password hashing and signed, HTTP-only, SameSite cookies, but left open where identity
comes from and how state-changing requests are protected.

The options were:

1. A hosted identity provider (OAuth/OIDC), or a self-hosted IdP service.
2. Local accounts with a session table, implemented on the standard library plus an
   Argon2id implementation, with no external service and no outbound network calls.

## Decision
We select **local, zero-egress authentication** (option 2).

1. **Passwords** are hashed with **Argon2id** (`argon2-cffi`). Verification is a
   constant-time library comparison, and a login attempt against a non-existent account
   still performs a hash so response timing does not reveal account existence.
2. **Sessions** are random 32-byte tokens in an HTTP-only, SameSite=Lax cookie. The
   database stores only the **SHA-256 digest** of the token, so a leaked control-plane
   database yields no usable sessions. Sessions expire after 12 hours, are revoked on
   sign-out, and are all revoked when the password changes.
3. **CSRF** is defended by **double-submit tokens**: an authenticated session carries its
   own CSRF token, and every unsafe method must echo it in a hidden form field or the
   `X-CSRF-Token` header. Unauthenticated forms (sign-in, registration, invitation,
   password reset) use the same mechanism with a pre-session cookie issued when the form
   is rendered.
4. **The checks live in one shared dependency** mounted on every protected router, so a
   route added later cannot omit the CSRF check or the role check.
5. **Cookies are marked `Secure` when the request arrived over HTTPS** (including behind a
   reverse proxy that sets `X-Forwarded-Proto`) and not otherwise, overridable with
   `PROPTALLY_SECURE_COOKIES`. A `Secure` cookie on a plain-HTTP deployment is never sent
   back, which would lock an owner out of their own app.
6. **Tenant self-registration is closed by default** once the instance has a tenant, and
   open while it has none, so a fresh deployment is bootstrapable without shipping a
   default password. Registering creates a **tenant** and confers no platform authority.
   The platform's own operator is the **super admin**, created on the host by
   `scripts/create_super_admin.py` and signing in at `/admin/login`
   (`12_SUPER_ADMIN_CONSOLE.md`); no web route can mint one. Joining somebody else's
   portfolio is possible only through a single-use invitation.

## Consequences
### Positive
* **No egress and no third-party dependency.** No account data, email address or login
  event leaves the host, consistent with ADR-006 and the privacy premise of the product.
* **No mail transport required.** Password reset is issued by a local operator command
  (`scripts/issue_password_reset.py`) and handed to the verified account holder out of
  band. The public reset page never creates or displays a token; local host access is the
  recovery authority when no mail transport is configured.
* **No default credentials ship.** The bootstrap path replaces a seeded admin password,
  which is the most common way a self-hosted financial app is compromised.
* **CSRF protection is structural**, not per-route discipline.

### Negative / Trade-Offs
* **No password reset by email.** Recovery depends on the operator being reachable; if
  the operator is unavailable the account is locked out until they act.
* **No multi-factor authentication**, no WebAuthn, and no SSO. An instance that must join
  an existing corporate identity provider is out of scope for this decision.
* **Login throttling is in-process** (5 failures per 15 minutes per address+email pair).
  It is a speed bump for a single self-hosted service, not a distributed rate limiter:
  a deployment running N worker processes allows roughly N× the attempts, because each
  process keeps its own counter. Everything else that must be shared already is —
  sessions, password changes and revocations are rows in the control-plane database, so
  those hold across workers. Making the throttle hold too means storing attempt
  timestamps in `system.db` and counting them on read; that is a small change, and it
  is the one thing to do before running multiple workers if a hard attempt limit
  matters. Running more than one worker is otherwise supported and documented in the
  README, with `tests/test_multitenant_deployment.py` asserting both the shared
  behaviour and this exception.
* **Session data is server-side and single-database.** A session is valid only on the
  instance whose control plane holds it; there is no cross-instance session sharing.

## Alternatives Considered
* **Hosted identity provider** — rejected: it contradicts the zero-egress premise, adds a
  per-user external dependency, and requires client secrets and callback URLs that are
  hostile to a LAN-only deployment.
* **Self-hosted IdP service (Keycloak, Authentik)** — rejected for this scope: another
  daemon to operate, patch and back up, for a feature set (SSO, MFA) that v1 does not
  need. The local session table is a few hundred lines and no extra moving parts.
* **Stateless signed cookies (JWT-style)** — rejected: revoking a session on sign-out or
  password change requires server-side state anyway, and a digest lookup in the control
  plane gives revocation for free.
