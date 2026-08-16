# Thermal Watch AI compatibility protocol

Status: **draft protocol for Thermal Watch v1.1**. Thermal Watch v1.0.1 does not yet export diagnostics or import compatibility profiles.

This portable, self-service protocol is provider-neutral. Thermal Watch does not require or privilege a particular AI assistant. The diagnostic JSON is the authoritative input; the packaged instructions and schema define the task and output contract.

The minimum AI setup package contains:

- `ThermalWatch-Hardware-Diagnostic.json`
- `AI_SETUP.md`
- `compatibility-profile-v1.schema.json`

An optional `ThermalWatch-Hardware-Diagnostic.txt` may summarize the same allowlisted technical facts for a person. It does not override the JSON.

## AI responsibilities

An assisting AI must:

- use only providers reported as available in the diagnostic;
- use only sensor identifiers and sensor types present in the diagnostic hardware tree;
- map only semantic fields allowed by the profile schema;
- leave a field unresolved when evidence is insufficient;
- return declarative JSON matching the requested schema version; and
- preserve unverified status for ambiguous motherboard sensors.

An assisting AI must never:

- invent a sensor, identifier, value, provider, or hardware capability;
- infer a measurement for an unmonitored period;
- return executable code, commands, shell fragments, or scripts;
- include DLL paths, URLs, or arbitrary filesystem paths;
- change thermal thresholds or incident behavior;
- inject calibration values, offsets, or correction factors in schema version 1; or
- claim that its proposal has already been accepted or activated.

Thermal Watch performs the final deterministic validation against a fresh hardware snapshot. The user performs the final approval. AI output alone cannot change monitoring behavior.

There is no account, central AI service, compatibility database, diagnostic-upload service, or automatic profile retrieval in this protocol. The user decides whether and where to share the package.

## Reusable AI instruction

```text
You are configuring Thermal Watch for this PC.

Read the supplied Thermal Watch hardware diagnostic. The diagnostic JSON is authoritative.

Determine which unresolved Thermal Watch semantic fields can be mapped using only providers and sensor identifiers actually present in the diagnostic.

Return only a candidate Thermal Watch compatibility profile matching the documented schema version.

Never invent sensors, sensor identifiers, values, providers, thresholds, calibration data, offsets, or hardware capabilities.

Never return executable code, shell commands, scripts, DLL paths, URLs, or arbitrary filesystem paths.

Leave fields unresolved when the diagnostic does not contain sufficient evidence.
```

## Candidate profile output

The output must be one JSON object conforming to [`schemas/compatibility-profile-v1.schema.json`](schemas/compatibility-profile-v1.schema.json). Markdown fences, commentary before or after the object, and generic configuration dictionaries are not part of the protocol.

Example shape:

```json
{
  "schema_version": 1,
  "profile_name": "Intel CPU and integrated GPU candidate",
  "hardware_match": {
    "cpu_model": "Model exactly as reported by the diagnostic",
    "gpu_models": [
      "Model exactly as reported by the diagnostic"
    ],
    "motherboard_vendor": null,
    "motherboard_model": null
  },
  "mappings": {
    "cpu_temp": {
      "provider": "librehardwaremonitor",
      "sensor_identifier": "/identifier/from/the/diagnostic",
      "sensor_type": "Temperature"
    }
  },
  "notes": [
    "Fields without sufficient evidence were left unresolved."
  ]
}
```

The example deliberately uses placeholders rather than claiming identifiers for real hardware.

## Ownership of facts

**Thermal Watch owns facts. AI owns conversation and explanation.**

An AI may analyze exported evidence and propose a mapping. It must not create, rewrite, or backfill telemetry, incidents, sessions, reports, measurements, coverage, or monitoring gaps.
