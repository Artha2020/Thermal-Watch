# Thermal Watch AI setup instructions

Status: **planned for Thermal Watch v1.1**. This workflow is not implemented in the current v1.0.1 release.

Thermal Watch is designed to remain useful without an AI assistant. Its monitoring, telemetry, incidents, sessions, history, analytics, reports, and current Ask Thermal Watch interface are deterministic application features.

This document is intended to be included in a self-contained setup package for the user's own AI assistant. The package will also contain the authoritative `ThermalWatch-Hardware-Diagnostic.json` and `compatibility-profile-v1.schema.json` files. The AI should not need access to Thermal Watch source code or prior product knowledge.

The planned v1.1 workflow lets a user bring a sanitized hardware inventory to an AI assistant of their choice. The AI proposes a declarative compatibility profile; Thermal Watch independently validates the proposal against hardware actually present and requires the user to approve it.

## Planned workflow

1. Run Thermal Watch.
2. Thermal Watch scans the PC through its available hardware providers.
3. If core hardware support remains incomplete after provider initialization and recovery, choose **CONFIGURE WITH YOUR AI** to export a sanitized, self-contained setup package.
4. Give the diagnostic to a preferred AI assistant, such as Nox, ChatGPT, Claude, Gemini, Ollama, LM Studio, or another compatible assistant.
5. The AI returns only a candidate Thermal Watch compatibility profile using the documented schema.
6. Import the candidate profile into Thermal Watch.
7. Thermal Watch validates every provider and sensor mapping against a fresh local hardware snapshot.
8. Review the proposed provider changes, mappings, and unresolved fields, then explicitly choose **APPLY PROFILE** or **CANCEL**.

The current v1.0.1 interface does not include **CONFIGURE WITH YOUR AI**, hardware-diagnostic export, or **IMPORT AI PROFILE**. Do not expect those controls until an implementation is completed, tested, and released.

AI setup is optional. **CONTINUE WITH CURRENT SUPPORT** leaves Thermal Watch running with its built-in detection and every provider that is already available.

## Instructions for the assisting AI

You are assisting with configuration of Thermal Watch for the computer described by the attached hardware diagnostic.

Thermal Watch has already inspected the machine. Use only the hardware providers, sensor identifiers, and sensor types actually contained in `ThermalWatch-Hardware-Diagnostic.json`. That JSON file is authoritative.

Determine whether unresolved Thermal Watch semantic fields can be mapped to sensors present in the diagnostic. Return only one candidate compatibility-profile JSON object conforming to `compatibility-profile-v1.schema.json`.

Do not invent sensors, identifiers, measurements, providers, or hardware capabilities. Do not change thresholds or introduce calibration values or offsets. Do not provide executable code, commands, PowerShell, shell scripts, DLL paths, URLs, or arbitrary filesystem paths as profile content. If the diagnostic does not contain enough evidence to map a field safely, leave it unresolved.

### Semantic field contract

| Thermal Watch field | Meaning | Required sensor type |
| --- | --- | --- |
| `cpu_temp` | CPU package temperature | `Temperature` |
| `cpu_power` | CPU package power | `Power` |
| `gpu_core_temp` | GPU core temperature | `Temperature` |
| `gpu_hotspot_temp` | GPU hotspot temperature | `Temperature` |
| `gpu_memory_temp` | GPU memory-junction temperature | `Temperature` |
| `gpu_load` | Overall GPU computational load | `Load` |
| `gpu_power` | GPU package or board power | `Power` |
| `gpu_fan` | GPU fan-speed observation | `Fan` |

A name that merely resembles a field is not enough. The identifier and exact sensor type must appear in the diagnostic, and Thermal Watch will verify both again against a fresh local snapshot.

## Trust boundary

AI output is never treated as authoritative. A candidate profile cannot activate unless Thermal Watch confirms that:

- the schema is supported and contains no unknown configuration;
- every provider is locally available and supported;
- every referenced sensor identifier exists in a fresh snapshot;
- every sensor type matches the semantic field being mapped;
- mappings are unique and do not inject thresholds or calibration;
- the profile contains no executable commands, scripts, DLL paths, URLs, or arbitrary filesystem paths; and
- the user explicitly approves the validated result.

An invalid or outdated profile must leave built-in detection operational. Approved profiles will be stored separately from application source and will be disableable or rollback-capable without reinstalling Thermal Watch.

No Thermal Watch server participates in this workflow. The package moves only where the user chooses to take it, and Thermal Watch does not upload diagnostics or retrieve profiles automatically.

## Privacy model

The planned diagnostic export uses an allowlist and contains only technical compatibility information. It must exclude usernames, email addresses, machine identity, serial numbers, credentials, browser or document data, unrelated process history, and personal telemetry history.

See [AI Compatibility Protocol](AI_COMPATIBILITY_PROTOCOL.md) for AI-facing rules and [`schemas/`](schemas/) for the proposed v1 schemas.
