# Thermal Watch

> **Windows hardware monitoring that remembers what happened.**
>
> **Thermal Watch gives your AI trustworthy eyes into your PC.**

[![Latest release](https://img.shields.io/github/v/release/Artha2020/Thermal-Watch?display_name=tag&sort=semver)](https://github.com/Artha2020/Thermal-Watch/releases/latest)
![Windows](https://img.shields.io/badge/platform-Windows-0078D4?logo=windows)
![Python](https://img.shields.io/badge/Python-3.14%20validated-3776AB?logo=python&logoColor=white)
[![Downloads](https://img.shields.io/github/downloads/Artha2020/Thermal-Watch/total)](https://github.com/Artha2020/Thermal-Watch/releases)

**Live thermals • Incident history • Workload attribution • Network Intelligence • Trends • Reports • Evidence-based diagnostics • Optional AI analysis**

## [Download Thermal Watch v1.1.0 for Windows](https://github.com/Artha2020/Thermal-Watch/releases/tag/v1.1.0)

Thermal Watch combines live sensor data with persistent telemetry, incidents, workload sessions, timelines, reports, network activity, and trend analysis. It is designed to examine recorded thermal behavior without presenting guesses as measurements.

Version 1.1.0 was developed and validated around the project's current target PC. Sensor availability and labeling depend on hardware, firmware, drivers, privileges, vendor tools, and LibreHardwareMonitor support; this release does not claim universal hardware compatibility.

Thermal Watch collects and preserves deterministic hardware evidence and remains the sole authority for what was actually measured. An AI assistant can now optionally query that evidence directly through a bounded, read-only interface — see [Available now in v1.1.0](#available-now-in-v110) below. Thermal Watch itself bundles no AI model and works fully without one.

### Available now in v1.1.0

- Deterministic live hardware monitoring and persistent telemetry
- Thermal incidents, workload sessions, history, timelines, analytics, trends, and reports
- Evidence coverage and explicit monitoring-gap handling
- Ask Thermal Watch, with two modes: a deterministic **Evidence** query interface (the original, still default) and an optional **AI Analysis** mode that routes a question through a configured AI provider
- **Network Intelligence** — per-adapter monitoring, per-process bandwidth attribution via a real elevated ETW capture, connection inspection, network-aware incidents/sessions/baselines/analytics, all without packet-content inspection
- **Optional AI provider integration** — Nox, any OpenAI-compatible endpoint (e.g. a local Ollama or LM Studio server), or a custom injected provider; fully optional, with "no AI configured" remaining a complete, unaffected first-class state
- A structured, read-only, versioned evidence tool catalog (7 operations) an AI provider can query — see [AI Tool Interface](docs/AI_TOOL_INTERFACE.md)
- **Grounding Guard** — every AI answer is deterministically checked against the evidence actually retrieved that turn before display; contradicted claims are corrected or redacted, fabricated evidence-ID citations are stripped, and monitoring gaps are never presented as "confirmed safe"
- **Evidence IDs** — every incident, session, and monitoring-coverage gap carries a stable, citable identifier
- API keys for a configured AI provider are encrypted with Windows DPAPI (current-user-scoped), never stored in plaintext

### Planned for a future release

- Self-service AI-assisted **hardware setup**, using a sanitized diagnostic package and provider-neutral compatibility profiles, for machines where built-in detection is incomplete
- Strict local validation and explicit user approval before any hardware-compatibility profile is activated

This hardware-setup workflow remains roadmap work, not a feature in the current v1.1.0 release — see [AI-assisted setup](docs/AI_SETUP.md) and [AI compatibility protocol](docs/AI_COMPATIBILITY_PROTOCOL.md) for the design. It is unrelated to the AI Analysis/evidence-query integration above, which is real and available now. The design requires no Thermal Watch account, compatibility cloud, diagnostic upload, central AI service, or automatic profile download.

**Real, available now — AI Analysis evidence query:**

```text
        USER'S QUESTION (Ask Thermal Watch, AI Analysis mode)
                        |
                        v
          CONFIGURED AI PROVIDER (optional)
      Nox / OpenAI-compatible (e.g. Ollama) / Custom
                        |
                        v
        Dynamic Thermal Watch tool discovery
                        |
                        v
              EvidenceBroker (read-only)
                        |
                        v
             Thermal Watch evidence
                        |
                        v
                  AI answer
                        |
                        v
               Grounding Guard
                        |
                        v
             Final displayed answer
```

**Planned, not yet implemented — hardware self-service setup:**

```text
                    USER'S PC
                        |
                        v
                 THERMAL WATCH
                        |
                  HardwareProbe
                        |
                        v
       Sanitized diagnostic + instructions + schema
                        |
                        v
                 USER'S OWN AI
      Nox / ChatGPT / Claude / Ollama / etc.
                        |
                        v
          Candidate compatibility profile
                        |
                        v
       Thermal Watch validation -> user approval
                        |
                        v
             CompatibilityResolver
```

Documents:

- [AI Tool Interface](docs/AI_TOOL_INTERFACE.md) — the real, implemented read-only evidence interface
- [AI-assisted hardware setup](docs/AI_SETUP.md) — planned, not yet implemented
- [AI compatibility protocol](docs/AI_COMPATIBILITY_PROTOCOL.md) — planned, not yet implemented
- [v1.1 roadmap](docs/ROADMAP.md)

## Overview

The main dashboard polls every two seconds and presents live CPU package temperature, GPU core temperature, system memory use, utilization, clocks, power, fans, storage, and additional hardware sensors when the machine exposes them. Thermal Watch records one-minute aggregate telemetry for longer-term analysis while keeping the user interface responsive and the raw monitoring path separate from reporting.

The application is local and dependency-light at runtime. It uses Windows APIs, PowerShell, `nvidia-smi` where available, and the bundled LibreHardwareMonitor library. The elevated sensor bridge is separated from the unprivileged UI process.

## Features

### Live monitoring

- CPU package temperature, utilization, clock, and power where available.
- NVIDIA GPU core temperature, utilization, memory use, clocks, power, and fan percentage through `nvidia-smi`.
- GPU hotspot and memory-junction temperatures when exposed by LibreHardwareMonitor.
- Motherboard and chipset temperatures, DIMM/RAM sensors, HDD/SSD/NVMe temperatures, fan speeds, voltages, controls, clocks, and power sensors when the hardware and sensor backend expose them.
- Live event log, temperature history chart, alert strip, and two-second status updates.
- Sensor identity handling that prefers stable LibreHardwareMonitor identifiers and preserves honest “unverified” labeling for ambiguous sensors.

Missing sensors remain unavailable rather than being synthesized. A value shown on one machine is not evidence that the same sensor exists or is correctly identified on another.

### Thermal zones and incidents

Thermal Watch classifies supported temperature sensors into severity zones with component-specific thresholds. Zone transitions are debounced so a single noisy sample does not immediately become an incident.

An incident records the observed component, sensor identity, start and end times, peak and recovery values, maximum severity, duration, process context, monitoring gaps, and close reason. Active incidents are snapshotted so they can be reconciled after a clean shutdown, crash, or restart. Incident History provides range, component, severity, and workload filtering; incident details; summaries; and CSV or JSON export.

### Workload attribution and sessions

Thermal Watch samples foreground, CPU, and GPU process activity and associates observed workloads with thermal context. Attribution is deliberately non-causal: a process may be reported as dominant or associated with an incident, but the application does not claim that correlation proves the process caused the temperature.

Sustained CPU or GPU activity can form a workload session. Sessions retain duration, observed process IDs, temperature and power aggregates, time in thermal zones, monitoring gaps, and linked incidents. Active sessions are persisted and reconciled across restarts.

### Network Intelligence

Thermal Watch monitors network activity with the same evidence discipline as thermal telemetry: adapter identity, live/idle baselines, and active TCP/UDP connections (protocol, local/remote endpoints, state) without inspecting packet contents.

Per-process bandwidth attribution runs through a real elevated ETW capture (Microsoft-Windows-Kernel-Network) owned by the same sensor bridge that reads hardware sensors, decoded and aggregated per PID/process name with no hardcoded process list. Sustained connectivity loss is tracked through the existing incident engine (a network incident behaves like any other component incident: debounce, history, timeline, durability across restart). Workload sessions carry network context (average/peak download and upload) alongside their existing CPU/GPU aggregates, and per-workload network baselines/anomaly detection follow the same minimum-evidence rules as thermal baselines.

### History and analysis

- **Incident History** — filter and inspect recorded thermal episodes, copy summaries, and export selected or filtered records.
- **Timeline** — merge incidents, workload sessions, hardware-change markers, significant log entries, and explicit unmonitored gaps on one chronological view.
- **Application Analytics** — rank workloads using recorded sessions and associated incidents, with per-workload detail.
- **Trend Intelligence** — compare retained periods for workload temperatures, hotspot-to-core deltas, thermal efficiency, idle behavior, incident frequency, and health scores when enough evidence exists.
- **Cooling recommendations** — derive conservative recommendations from repeated recorded patterns rather than one-off spikes.
- **Fan Intelligence** — compare fan levels with observed cooling response using persisted telemetry and minimum sample requirements.
- **Predictive Maintenance** — project observed temperature, health, and incident trends. These are trend projections with confidence and coverage constraints, not hardware-failure predictions.
- **Reports** — generate and retain daily, weekly, and monthly reports for completed periods. Missing reports are generated on the next run, so the PC does not need to be on at the exact period boundary. Reports can be exported as JSON, CSV, or text.
- **Experiments** — mark a hardware or cooling change, such as cleaning, repasting, or adding a fan, then compare retained before/after telemetry and sessions after sufficient time has elapsed. Experiment markers are user-authored annotations; measurements remain sourced from recorded data.
- **Sensor History** — inspect retained history for a selected sensor, including available min/max series and supported comparisons, and export the displayed range as JSON.

### Ask Thermal Watch

Ask Thermal Watch has two modes, selectable per question.

**Evidence mode** is the original deterministic query interface over the application's recorded evidence; it is not a language model and does not call a hosted AI service, whether or not AI is configured. It recognizes questions about status, incidents, timelines, workloads, trends, explanations, and recommendations across periods such as today, yesterday, last night, this week, last month, or a requested number of hours/days/weeks.

Examples include:

- "Why did my PC run hot last night?"
- "What happened yesterday?"
- "Which apps ran hottest in the last 3 days?"
- "Were there any incidents last month?"
- "Is my CPU getting worse over time?"
- "What do you recommend?"

Answers are assembled from telemetry, incidents, sessions, reports, and timeline evidence. Unknown questions receive an explicit limitation instead of a guessed answer. Evidence mode is always available and remains the default and fallback regardless of AI configuration.

**AI Analysis mode** is optional and routes a question through a configured AI provider, which retrieves current data through Thermal Watch's own read-only evidence tools rather than being handed a data dump. Every AI answer is reviewed by Grounding Guard before it is ever displayed: claims are checked against the evidence actually retrieved that turn, contradicted claims are corrected or redacted, fabricated evidence-ID citations are stripped, and monitoring gaps are never presented as "safe" or "confirmed" — Thermal Watch says plainly when it cannot determine what happened during an unmonitored period. AI requests run off the UI thread and never block monitoring.

### AI integration (optional)

Thermal Watch can optionally be paired with an AI provider so AI Analysis mode has something to route questions to. AI is fully optional: with no provider configured, Thermal Watch behaves exactly as in v1.0.1, with no network calls of any kind related to AI.

Supported providers:

- **OpenAI-compatible** — any local or remote endpoint speaking the OpenAI chat-completions protocol (for example, a local Ollama or LM Studio server). This is the only provider configurable entirely through the Settings screen. Loopback endpoints (`127.0.0.1`/`localhost`) work out of the box; a non-loopback endpoint requires an explicit "allow remote" opt-in, and endpoints with embedded credentials or query strings are rejected outright.
- **Nox** — for integration with a Nox AI assistant; requires a transport supplied in code, not just through the Settings screen.
- **Custom** — an injected callback for other integrations; also requires code-level wiring.

API keys, where used, are never stored in plaintext. They are encrypted with Windows DPAPI (`CryptProtectData`, current-user-scoped) before being written to `thermal_watch_ai_config.json`; the plaintext key exists only in memory for the duration of a request and is never written to any log, export, or evidence store.

AI Analysis exposes exactly seven read-only evidence operations to a provider (system status, current sensors, network status, top network processes, recent incidents, recent sessions, coverage) through a versioned, dynamically-discovered tool catalog — see [AI Tool Interface](docs/AI_TOOL_INTERFACE.md). A provider is never handed direct file, database, process, or network-mutation access.

### Evidence IDs

Every incident, workload session, and monitoring-coverage gap carries a stable, human-citable identifier (for example `INC-20260817-0042`, `SES-20260817-0018`, `NET-20260817-0007`, `COV-20260817-0003`), assigned once when the record is finalized and never recomputed. These IDs let an AI Analysis answer point at exactly which record backs a claim, and let a fabricated or foreign citation be detected and stripped by Grounding Guard.

## Screenshots

### Main dashboard

Live CPU, GPU, memory, cooling, voltage, storage, motherboard, and RAM telemetry from the target system.

![Thermal Watch main dashboard showing live hardware telemetry](docs/screenshots/main-dashboard.png)

### Incident History

Recorded thermal incidents with severity, peak, duration, workload context, filtering, and per-incident evidence.

![Thermal Watch Incident History](docs/screenshots/incident-history.png)

### Ask Thermal Watch

Deterministic analysis of recorded evidence, including explicit monitoring gaps and non-causal workload language.

![Ask Thermal Watch answering a thermal-history question](docs/screenshots/ask-thermal-watch.png)

See [`docs/screenshots/README.md`](docs/screenshots/README.md) for capture and privacy requirements. These are real windows from the current public build, not design mockups.

New v1.1.0 screens (Network Intelligence, Ask Thermal Watch's AI Analysis mode, and AI Settings) are not yet captured here and will be added in a follow-up update.

## How Thermal Watch Thinks About Evidence

Thermal Watch distinguishes three states that monitoring tools often blur together:

1. **Observed** — a sensor value or process context was actually recorded.
2. **Derived** — a trend, aggregate, score, recommendation, or projection was computed from recorded evidence under explicit minimum-data rules.
3. **Not monitored** — no telemetry exists for that interval.

One-minute telemetry buckets include timestamps and sample counts. Coverage calculations compare recorded buckets with the requested window. Gaps are shown explicitly in timelines, incident/session durability records, reports, and Ask responses. Low-coverage reports and answers carry caveats. Thermal Watch does not fill an offline interval with invented values or claim knowledge about what the hardware did while it was not monitoring.

## Requirements and hardware support

- Windows with PowerShell and Tk support.
- Administrator approval for the sensor bridge when low-level hardware access is needed. The main UI remains unprivileged.
- A supported Python 3 installation for source runs. The v1.1.0 development build was validated with Python 3.14.
- `nvidia-smi` on `PATH` for NVIDIA-specific metrics. Thermal Watch continues with the sources that remain available if it is absent.
- LibreHardwareMonitor support for the motherboard, CPU, GPU, memory, storage, and controller sensors being queried.

Some sensors require administrator access. Some GPUs do not expose hotspot, memory-junction, or fan data. Fan readings may be RPM or percentage depending on the source. Sensor names and availability can change with firmware, drivers, LibreHardwareMonitor versions, or hardware revisions.

Thermal Watch v1.1.0 was tested against the project's current development/target PC and genuine hardware sensors. Other systems should be treated as new validation targets, especially for sensor identity and threshold interpretation.

## Running Thermal Watch

### Packaged application

The PyInstaller onedir build is located at:

```text
dist\ThermalWatch\ThermalWatch.exe
```

Run `ThermalWatch.exe`. The bundle includes `sensor_bridge.ps1`, the application icon, and the LibreHardwareMonitor directory. The executable has no console window. If the privileged bridge is missing or stale, the application can make a rate-limited recovery attempt and may display a UAC prompt.

`dist/` is generated output and is intentionally excluded from Git. Distribute a reviewed build artifact rather than committing the folder to source control.

### Source

From the repository root:

```powershell
cd 'path\to\Thermal-Watch'
.\launch.bat
```

`launch.bat` invokes `launch.ps1`. The launcher starts or reuses the elevated, session-persistent sensor bridge and starts the UI through `pythonw.exe`. Python and `pythonw.exe` must be available on `PATH`.

For an unelevated diagnostic/source run:

```powershell
cd 'path\to\Thermal-Watch'
python .\app.py
```

That path can still provide metrics available without privileged low-level sensor access, but some CPU, motherboard, fan, voltage, or storage sensors may be unavailable.

## Building the Windows executable

PyInstaller is a build-time requirement, not an application runtime dependency. From the repository root:

```powershell
python -m PyInstaller .\ThermalWatch.spec
```

The spec uses repository-relative paths and produces:

```text
dist\ThermalWatch\ThermalWatch.exe
```

The build includes `sensor_bridge.ps1`, `thermal_watch.ico`, and the bundled `LibreHardwareMonitor/` directory. `build/` and `dist/` are ignored generated output. Review the complete distribution, third-party notices, and hardware behavior before publishing a binary.

## Data and privacy

Thermal Watch does not require a cloud account and does not send telemetry to a hosted service. Monitoring data stays in local files beside `app.py` for source runs or beside `ThermalWatch.exe` for frozen runs, unless `THERMAL_WATCH_DATA_DIR` is set before startup.

Persistent stores include:

- `thermal_watch_telemetry.db` — one-minute telemetry aggregates and per-sensor summaries.
- `thermal_watch_reports.db` — generated daily, weekly, and monthly reports.
- `thermal_watch_incidents.jsonl` and `thermal_watch_active_incidents.json`.
- `thermal_watch_sessions.jsonl` and `thermal_watch_active_sessions.json`.
- `thermal_watch_experiments.jsonl` — user-authored hardware-change markers.
- `thermal_watch_events.log`.
- `thermal_watch_evidence.json` — the periodic snapshot AI Analysis mode reads from; contains the same categories of data as the stores above, nothing additional.
- `thermal_watch_ai_config.json` — AI provider/endpoint/model configuration, present only if AI has been configured. The API key, if any, is stored DPAPI-encrypted (current-user-scoped), never in plaintext.

The elevated bridge writes current sensor/status snapshots under `%ProgramData%\ThermalWatch`. These files and all application stores, including `thermal_watch_ai_config.json`, are excluded from Git. Exports are created only through user-selected save locations and may contain hardware names, process names, window titles, or workload history; review them before sharing. AI Analysis conversation text is never written to any of these stores — it exists only in the Ask window for the current session.

Telemetry, incidents, sessions, and event history use a 30-day retention window. Reports have their own persistent database.

## Verification and testing

The repository contains 50 feature verification scripts plus `tools/verify_isolation.py`, which runs the complete verification set (51 scripts total) in redirected temporary data directories and compares production files byte-for-byte before and after the run.

The v1.1.0 release pass reported:

- 50/50 feature verification scripts passing, including the Network Intelligence and AI provider/settings/grounding suites added since v1.0.1.
- Verification isolation passing with production data unchanged.
- The packaged executable launching without a console window, reading genuine target-PC sensors, persisting data beside the executable, and shutting down cleanly.

Additional tools cover telemetry/query benchmarks, monitoring overhead, session overhead, bridge resilience, soak analysis, stress workloads, and targeted hardware diagnostics. Several tools are intentionally hardware-specific or operational rather than unit tests; read their module documentation before running them. The isolation gate should not be run while Thermal Watch itself is writing to the production directory.

## Known limitations

- Windows only.
- Hardware coverage depends on Windows APIs, vendor tools, privileges, and LibreHardwareMonitor support.
- Validation is specific to the current target PC; other hardware requires validation.
- Process/workload attribution describes observed association, not proven causation.
- Trend and maintenance outputs require sufficient retained coverage and cannot reconstruct time when the application was not running.
- Retention limits long-horizon comparisons; old telemetry, incidents, and sessions are pruned.
- Ask Thermal Watch's Evidence mode supports a defined deterministic question set rather than open-ended natural-language reasoning; AI Analysis mode is open-ended but strictly grounded to retrieved evidence.
- Thresholds are monitoring classifications, not replacements for vendor thermal limits, firmware protections, or professional hardware diagnosis.
- Hardware coverage shown in the committed screenshots reflects the validated target PC and is not a promise of identical sensor coverage on other systems.
- Nox and Custom AI providers require a transport/handler injected in code; neither is usable purely through the Settings screen or a JSON config file — only OpenAI-compatible is fully self-service from the UI.
- AI is entirely optional and off by default; monitoring gaps remain unknowable by design and are never backfilled or guessed by any AI answer; there is no packet-content inspection anywhere in the network features, only connection metadata and aggregate byte/rate counters.

## License and third-party components

Thermal Watch does not currently include a top-level project license. Until one is added, the repository should not imply that reuse or redistribution rights have been granted for the Thermal Watch source itself.

Thermal Watch bundles **LibreHardwareMonitor 0.9.6** (`LibreHardwareMonitor.exe` and `LibreHardwareMonitorLib.dll`) and its accompanying dependencies. LibreHardwareMonitor is licensed under the **Mozilla Public License 2.0**, while some bundled dependencies use other licenses.

- Project: <https://github.com/LibreHardwareMonitor/LibreHardwareMonitor>
- Bundled notices: [`third_party/THIRD_PARTY_NOTICES.md`](third_party/THIRD_PARTY_NOTICES.md)
- Bundled license texts: [`third_party/licenses/`](third_party/licenses/)
- Upstream license material: <https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/tree/v0.9.6/Licenses>

The Thermal Watch Windows package (v1.0.0 onward, including v1.1.0) includes the applicable bundled third-party notices and license texts under `third_party/`, installed beside the application. Those notices apply only to the identified third-party components and do not grant rights to the Thermal Watch source itself. The v1.1.0 AI provider integration (`ai/`) uses only the Python standard library and introduces no new third-party dependency or license obligation.
