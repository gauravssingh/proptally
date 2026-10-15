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
6. **Self-registration is closed by default** once the instance has an account, and open
   while it has none, so a fresh deployment is bootstrapable without shipping a default
   password. The first account becomes the instance administrator. Joining somebody
   else's portfolio is possible only through a single-use invitation.

## Consequences
### Positive
* **No egress and no third-party dependency.** No account data, email address or login
  event leaves the host, consistent with ADR-006 and the privacy premise of the product.
* **No mail transport required.** Password reset surfaces a single-use token to the
  operator instead of emailing it; the deployment needs no SMTP configuration.
* **No default credentials ship.** The bootstrap path replaces a seeded admin password,
  which is the most common way a self-hosted financial app is compromised.
* **CSRF protection is structural**, not per-route discipline.

### Negative / Trade-Offs
* **No password reset by email.** Recovery depends on the operator being reachable; if
  the operator is unavailable the account is locked out until they act.
* **No multi-factor authentication**, no WebAuthn, and no SSO. An instance that must join
  an existing corporate identity provider is out of scope for this decision.
* **Login throttling is in-process** (5 failures per 15 minutes per address+email pair).
  It is a speed bump for a single self-hosted service, not a distributed rate limiter;
  running multiple worker processes multiplies the effective allowance.
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
