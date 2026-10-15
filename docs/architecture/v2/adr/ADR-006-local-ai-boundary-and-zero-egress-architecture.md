# ADR-006: Local AI Boundary & Zero-Egress Architecture

* **Status:** Accepted
* **Date:** October 2026
* **Deciders:** Principal Solution Architect, Security Architect

## Context
The client possesses a dedicated on-premise Linux server equipped with modern NVIDIA GPUs (RTX 3060, RTX 4070, RTX 5060). The client desires intelligent features (semantic transaction tagging, conversational portfolio analytics, receipt fallback) while maintaining 100% privacy, air-gapped security, and zero exposure of sensitive financial records to external commercial cloud LLMs (OpenAI, Anthropic).

We must define the structural boundaries, authorities, and security limits of the AI subsystem.

## Decision
1. **Local-Only Inference:**
   All AI inference runs strictly on the client's local server using **Ollama** or **vLLM**, accessed via an OpenAI-compatible REST endpoint on `127.0.0.1:11434`. Zero data leaves the local network.
2. **Zero Calculation Authority:**
   The AI model is strictly prohibited from performing mathematical calculations, amortization, journal entry balancing, or direct database mutations. All calculations remain 100% deterministic, executed by Python domain services.
3. **Guarded Tool Execution Layer:**
   The AI interacts with application data solely through a read-only, validated tool interface (`get_property_summary`, `get_transactions`, `get_cash_flow`).
4. **Offline Resilience:**
   The application does not depend on AI availability. If the GPU server or Ollama daemon is offline, the application continues to operate with 100% functionality using deterministic regex rules.

## Consequences
### Positive
* **Total Privacy & Compliance:** No financial PII, account numbers, or tax data ever egress to third-party cloud infrastructure.
* **Zero Hallucination Risk in Financials:** Financial figures are calculated by deterministic Python and SQL code, never generated probabilistically by a neural net.
* **Conversational Convenience:** client gains natural-language querying capabilities directly on their own hardware.

### Negative / Trade-Offs
* Local LLMs require server VRAM and compute resources, which must be managed alongside other workloads on the host.
