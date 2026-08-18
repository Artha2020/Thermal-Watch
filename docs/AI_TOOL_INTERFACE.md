# Read-only AI tool interface

Status: **implemented and shipped in Thermal Watch v1.1.0.**

Thermal Watch exposes a structured, read-only, versioned evidence interface so an optional AI assistant can ask bounded questions without reading internal databases or controlling the monitoring engine directly.

Current operations (tool catalog version 1):

- `get_system_status`
- `get_current_sensors`
- `get_network_status`
- `get_top_network_processes`
- `get_recent_incidents`
- `get_recent_sessions`
- `get_coverage`

This is a deliberately bounded initial set drawn from the larger set of operations originally
considered for this interface (which also included timeline, sensor-history, network-history,
report, and maintenance-status queries). Those remain candidates for a future catalog version,
not a promise; the catalog is versioned specifically so it can grow without breaking existing
callers. All seven current operations depend on evidence that is available regardless of whether
Network Intelligence has observed recent activity — monitoring gaps are reported explicitly
rather than hidden.

## Architectural boundary

**Thermal Watch owns facts. AI owns conversation and explanation.**

The interface is read-only. It preserves Thermal Watch's evidence semantics, accesses only requested bounded data, and exposes monitoring coverage and gaps. An AI cannot create, rewrite, delete, or backfill telemetry, incidents, sessions, reports, measurements, or monitoring gaps — the interface has no primitive that would let it.

Every provider (Nox, OpenAI-compatible, or Custom) reaches this interface through the same bounded tool-call door (`EvidenceBroker`). Beyond that door, every AI-provider answer is additionally reviewed by a deterministic Grounding Guard before it is ever displayed to the user: claims are checked against the evidence actually retrieved that turn, contradicted claims are corrected or redacted, fabricated or foreign evidence-ID citations are stripped, and correlation is never presented as causation. Stable evidence IDs (for example `INC-20260817-0042`) let an answer cite exactly which record backs a claim.

This document does not authorize automatic code downloading, diagnostic uploads, or remote control. Every adapter remains optional; Thermal Watch is fully functional with no AI provider configured, and configuring one requires explicit user setup through the Settings screen (for OpenAI-compatible) or code-level wiring (for Nox/Custom).
