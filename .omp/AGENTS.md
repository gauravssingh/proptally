# PropTally — multi-tenant checkout (`proptally-mt`)

@../GEMINI.md

## Runtime
- Live service: launchd `com.proptally.mt`, port 8141, this checkout, its own `.venv`; the plist runs `scripts/run_server.sh`. The older checkout (`com.proptally.server`) stays separate on 8080 — never merge the two.
- After any Python, template, or static change:
  1. `launchctl kickstart -k gui/$(id -u)/com.proptally.mt`
  2. `lsof -nP -iTCP:8141 -sTCP:LISTEN` must show **exactly one** PID
  3. `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8141/auth/login` must be `200`
- Never start `main.py` on 8141 to check a change. Use `PROPTALLY_STORAGE_ROOT=/tmp/<name>` on a scratch port (8199+) and stop it afterwards.

## Hard constraints
- Runtime ingestion and calculation are fully offline: no cloud LLM or OCR calls while processing documents.
- The ledger must balance to the penny: `Opening + Net == Closing`.
- The public hostname sits behind Cloudflare Access; an anonymous `curl` returns `302`, not app HTML. Test app responses over loopback.
- Do not record hostnames, LAN, or tailnet addresses in the repo or in memory; read them from the local environment.

## Checks
- `.venv/bin/pytest -q`; `.venv/bin/ruff check src tests`.
- UI changes: prove the surface with the lightest live check — one page load or screenshot. Do not build a harness or simulate sessions for a cosmetic tweak.
- Tenant data: `storage/tenants/<tenant>/proptally.db`. Back it up with `sqlite3 <db> ".backup '<path>'"` before bulk ledger or transaction writes.

## Product conventions
- Store money as integer cents; show dollars in every UI, report, and error message.
- No gradients. Reuse the existing design tokens and components; do not introduce a second styling convention.
- Do not surface storage internals (SHA-256 digests, "offline storage" claims) in user-facing cards.
- Toasts top-right; saving must not reload the page.
- Plain US-English labels, not AI-flavored terminology.
- Generated review artifacts (`review/`, `proptally-code-review-*`) stay uncommitted.
