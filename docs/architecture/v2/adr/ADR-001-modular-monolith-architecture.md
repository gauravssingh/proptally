# ADR-001: Modular Monolith Architecture vs. Microservices

* **Status:** Accepted
* **Date:** October 2026
* **Deciders:** Principal Solution Architect, Senior Python Engineer

## Context
PropTally is a bespoke financial reconciliation and tax workpaper system for an individual real estate investor (client). The application ingests statements, parses transactions, decomposes mortgages, maintains a double-entry ledger, matches settlements, and serves a local web dashboard.

Architectural options considered:
1. **Microservices:** Separate independently deployable services for ingestion, parsing, accounting, AI, and frontend.
2. **Modular Monolith:** A single Python backend (FastAPI) structured into strictly isolated internal domain packages (`properties`, `mortgages`, `accounting`, `ingestion`, `reconciliation`), serving or pairing with a React SPA.

## Decision
We choose a **Modular Monolith**. 

All domain boundaries are enforced via Python package structures and service interfaces rather than network RPCs. The database is shared across domains within a single SQLite process.

## Consequences
### Positive
* **Operational Simplicity:** Zero container orchestration (no Kubernetes, Docker Swarm, or Consul needed).
* **High Performance & Transactional Integrity:** Ingestion, ledger posting, and reconciliation occur in atomic database transactions without distributed consensus protocols (2PC or Saga).
* **Development Velocity:** Fast local iteration, unified test suites, and instant refactoring across domain boundaries.
* **Low Resource Footprint:** Runs comfortably as a lightweight systemd service or single-command process on the client's Linux server.

### Negative / Trade-Offs
* Requires disciplined internal code boundaries to prevent circular dependencies between domains. (Enforced via Ruff and architectural linting).
