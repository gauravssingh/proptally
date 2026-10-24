# PropTally Engineering Guidelines & Preferences

## 1. Network & Exposure Preferences
- **Cloudflare Tunnels:** **NEVER** spin up or use free, public Cloudflare quick tunnels (`trycloudflare.com`). **ALWAYS** use the operator's own private named Cloudflare Tunnel on the operator's own domain.
- **PropTally Ingress:** Two deployments run side by side on purpose, so neither steals the other's port:
  * `com.proptally.server` — the older checkout (`~/projects/proptally`), `0.0.0.0:8080`.
  * `com.proptally.mt` — **this multi-tenant checkout** (`~/projects/proptally-mt`), `PROPTALLY_PORT=8141`, run from its own `.venv` via `scripts/run_server.sh`.
- **Public URL:** Exposed exclusively through the operator's private Cloudflare Zero Trust tunnel. **The public hostname is gated by Cloudflare Access** (email OTP; allow-list restricted to the operator; 24h session) — anonymous requests receive `302` to `*.cloudflareaccess.com`, so any test that curls the public URL must expect a redirect rather than app HTML. Loopback (`127.0.0.1:8080`), LAN, and tailnet paths bypass the gate and are unaffected.
- **Private access paths:** loopback (`127.0.0.1:8080` for the old checkout, `127.0.0.1:8141` for this one), the operator's LAN, and the operator's tailnet all reach the services directly. The specific host names, tailnet address and LAN address vary per machine and are deliberately **not** recorded in this repository — read them from the local environment.

## 2. Privacy & Computation
- 100% offline document ingestion and calculation. No external cloud LLM or OCR APIs during runtime processing.
- Double-entry balance checks to the single penny (`Opening + Net == Closing`).
- Tamper-evident vault storage with SHA-256 digests.

## 3. Engineering Workflow & Anti-Overengineering Principles
- **Proportional Execution (No Scope Inflation):** Match solution complexity strictly to task complexity. For localized UI, markup, or cosmetic fixes (e.g. padding, class flags, icon changes), diagnose the pinpoint cause and apply the direct fix. Do NOT inflate minor tweaks into multi-file defensive refactorings, global CSS changes, or speculative redesigns.
- **Batched Inquiries (No Micro-Query Loops):** When answering informational questions or inspecting database state, write one consolidated, comprehensive query or search instead of chaining 10–15 piecemeal commands or incremental Python one-liners. Answer clearly once the fact is established.
- **Right-Sized Verification:** Do not build heavy test harnesses, simulate authenticated web client sessions, or write redundant unit tests for trivial visual/markup adjustments. Use existing targeted test suites and design-system checks without over-complicating the verification loop.

