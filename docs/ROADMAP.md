# Thermal Watch v1.1 roadmap

Status: **planned work**. Items in this document are not claims about the current v1.0.1 release.

## Track A - Hardware Adaptation and AI Setup

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

Planned sequence:

1. Network-interface foundation
2. Live receive/transmit rates
3. Per-process network attribution
4. Connection metadata
5. Network telemetry and history
6. Network sessions
7. Network incidents
8. Network analytics
9. Baselines and anomalies
10. Cross-system correlation with thermal and workload evidence

The initial design explicitly excludes packet-content inspection. Network Intelligence should record bounded connection and transfer metadata, preserve monitoring gaps, and avoid claiming causation from correlation.

## Future AI/tool integration

A future read-only evidence interface may expose the operations documented in [AI_TOOL_INTERFACE.md](AI_TOOL_INTERFACE.md). It will follow one rule:

> Thermal Watch owns facts. AI owns conversation and explanation.

AI clients will not be permitted to create or rewrite measurements, telemetry, incidents, sessions, reports, or monitoring gaps.
