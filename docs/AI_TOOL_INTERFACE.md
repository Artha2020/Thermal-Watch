# Future read-only AI tool interface

Status: **architectural direction for a future Thermal Watch version; not implemented in v1.0.1**.

Thermal Watch may expose a structured, read-only evidence interface so an AI assistant can ask bounded questions without reading internal databases or controlling the monitoring engine directly.

Candidate operations:

- `get_system_status`
- `get_current_sensors`
- `get_network_status`
- `get_top_network_processes`
- `get_incidents`
- `get_sessions`
- `get_timeline`
- `get_sensor_history`
- `get_network_history`
- `get_reports`
- `get_maintenance_status`

Operations that depend on Network Intelligence are unavailable until that roadmap track is implemented.

## Architectural boundary

**Thermal Watch owns facts. AI owns conversation and explanation.**

The interface is read-only. It must preserve Thermal Watch's evidence semantics, access only requested bounded data, and expose monitoring coverage and gaps. An AI must not create, rewrite, delete, or backfill telemetry, incidents, sessions, reports, measurements, or monitoring gaps.

This document does not authorize direct Nox integration, automatic code downloading, diagnostic uploads, or remote control. Any future adapter remains optional and must preserve deterministic validation and user approval.
