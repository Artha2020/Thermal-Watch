# Thermal Watch v1.1 roadmap

Status: **Track B and the read-only AI/tool interface are implemented and shipped in v1.1.0.
Track A (hardware self-service setup) remains planned work and is not a feature of the current
release.**

## Track A - Hardware Adaptation and AI Setup

**Status: planned, not implemented.**

Planned components:

1. `HardwareProbe` - produce a privacy-limited inventory of hardware, provider state, sensors, and unresolved semantic fields.
2. `CompatibilityResolver` - preserve built-in detection and apply only validated, approved mappings.
3. `ProfileValidator` - enforce strict schemas and validate mappings against a fresh hardware snapshot.
4. `ProfileStore` - atomically persist approved local profiles outside application source, with disable and rollback support.
5. `AISetupExport` - generate a self-contained package containing authoritative diagnostic JSON, AI instructions, the profile schema, and an optional human-readable summary.
6. Import/review/apply UI - show hardware match, provider changes, mappings, and unresolved fields before explicit approval.
7. Intel/Iris Xe provider expansion - add provider observations only where real hardware evidence and regression tests support them.
8. External-PC validation - verify bridge startup, provider loading, sensor enumeration, mapping, persistence, and rollback on representative non-development hardware.

Required deterministic coverage includes:

- fully supported hardware does not prompt for setup;
- unresolved core GPU support prompts only after initialization settles;
- optional missing motherboard, RAM, fan, or voltage sensors do not create false prompts;
- diagnostics contain required technical evidence and no private identity or runtime history;
- valid profiles are accepted only after approval;
- hardware mismatch, nonexistent identifiers, wrong sensor types, duplicate mappings, unknown providers, unknown properties, malformed JSON, executable/script/path payloads, and threshold/calibration injection are rejected;
- approved profiles survive restart and can be disabled or rolled back;
- invalid persisted profiles fall back to built-in detection;
- built-in behavior remains unchanged without a profile;
- existing NVIDIA behavior, thresholds, incidents, debounce, persistence, and telemetry semantics remain unchanged; and
- tests redirect profile storage through `THERMAL_WATCH_DATA_DIR` and pass the isolation gate.

This is a self-service local handoff design. It has no Thermal Watch compatibility cloud, central profile database, account requirement, diagnostic upload, automatic profile download, or central AI service. Direct AI adapters are not part of this phase.

## Track B - Network Intelligence

**Status: implemented and shipped in v1.1.0**, including real elevated end-to-end validation
against genuine external traffic (sustained real-world application traffic, zero ETW buffers
lost, per-process attribution correctly tracking the dominant receiving process against Windows
adapter counters, with no application/PID/adapter-specific implementation logic).

Delivered sequence:

1. Network-interface foundation — done
2. Live receive/transmit rates — done
3. Per-process network attribution — done, validated end-to-end with real elevated ETW capture
4. Connection metadata — done
5. Network telemetry and history — done
6. Network sessions — done
7. Network incidents — done
8. Network analytics — done
9. Baselines and anomalies — done
10. Cross-system correlation with thermal and workload evidence — done

As designed, packet-content inspection is never performed. Network Intelligence records only
bounded connection and transfer metadata, preserves monitoring gaps explicitly, and never claims
causation from correlation.

## AI/tool integration

**Status: implemented and shipped in v1.1.0.** A read-only evidence interface exposes the
operations documented in [AI_TOOL_INTERFACE.md](AI_TOOL_INTERFACE.md) to an optional, user-
configured AI provider (Nox, any OpenAI-compatible endpoint, or a custom integration). It follows
one rule:

> Thermal Watch owns facts. AI owns conversation and explanation.

AI clients are not permitted to create or rewrite measurements, telemetry, incidents, sessions,
reports, or monitoring gaps — enforced structurally: the evidence interface is read-only end to
end, and every AI-provider answer is additionally reviewed by a deterministic Grounding Guard
before display, which corrects or redacts claims that contradict the evidence retrieved that
turn and strips fabricated evidence-ID citations. AI integration is entirely optional; Thermal
Watch is fully functional with no provider configured.
