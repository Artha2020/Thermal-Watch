"""Read-only local CLI for Thermal Watch evidence (v1.1 Phase 11).

Protocol: one JSON object on stdin, one JSON object on stdout. The caller never supplies a
filesystem path; this process reads only Thermal Watch's fixed evidence snapshot beside app.py.
"""
from __future__ import annotations

import json
import base64
import copy
import sys
from pathlib import Path


EVIDENCE_SNAPSHOT_PATH = Path(__file__).resolve().parent / "thermal_watch_evidence.json"
ADAPTER_VERSION = "1.0"
MAX_LIMIT = 100
TOOL_CATALOG_VERSION = 1
TOOL_CATALOG_SCHEMA = "thermal-watch-tool-catalog"


def _parameters(*, limit=False):
    properties = {}
    if limit:
        properties["limit"] = {
            "type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
            "description": "Maximum number of newest records to return.",
        }
    return {"type": "object", "properties": properties, "required": [], "additionalProperties": False}


def _evidence_response(data_type, description):
    return {
        "type": "object",
        "description": description,
        "required": ["ok", "operation", "evidence_status", "provenance", "data"],
        "properties": {
            "ok": {"type": "boolean"},
            "operation": {"type": "string"},
            "adapter_version": {"type": "string"},
            "evidence_schema_version": {"type": ["string", "null"]},
            "generated_at": {"type": ["number", "null"], "description": "Unix timestamp for the evidence snapshot."},
            "evidence_status": {"type": "string", "enum": ["observed", "derived", "unavailable", "monitoring_gap"]},
            "provenance": {"type": "object"},
            "data": {"type": data_type},
        },
        "additionalProperties": True,
    }


# Canonical public catalog. Request validation, discovery, and provider tool schemas all
# derive from this one map. Keep operation names stable and make future changes additive.
OPERATIONS = {
    "describe_operations": {
        "description": "Return the versioned Thermal Watch evidence-tool catalog and current capability state.",
        "parameters": _parameters(),
        "response": {
            "type": "object", "required": ["ok", "operation", "tool_catalog_version", "operations", "semantics"],
            "properties": {
                "ok": {"type": "boolean"}, "operation": {"const": "describe_operations"},
                "tool_catalog_version": {"type": "integer"}, "operations": {"type": "object"},
                "semantics": {"type": "object"},
            },
            "additionalProperties": True,
        },
        "read_only": True,
        "capability_requirement": "always available; does not require a readable evidence snapshot",
    },
    "get_system_status": {
        "description": "Return recorded system identity, uptime, and sensor-bridge health.",
        "parameters": _parameters(),
        "response": _evidence_response("object", "System identity and bridge-health evidence."),
        "read_only": True,
        "capability_requirement": "a readable evidence snapshot containing system or bridge status",
    },
    "get_current_sensors": {
        "description": "Return the latest CPU, GPU, and memory readings where supported by this machine.",
        "parameters": _parameters(),
        "response": _evidence_response("object", "Latest component readings; unsupported readings remain null."),
        "read_only": True,
        "capability_requirement": "conditionally available per CPU, GPU, and memory sensor support",
    },
    "get_network_status": {
        "description": "Return current aggregate adapter connectivity and network-rate evidence.",
        "parameters": _parameters(),
        "response": _evidence_response("object", "Current aggregate network evidence without raw connection rows."),
        "read_only": True,
        "capability_requirement": "a readable snapshot containing network adapter evidence",
    },
    "get_top_network_processes": {
        "description": "Return processes with the highest current network rates from the latest sampling interval.",
        "parameters": _parameters(limit=True),
        "response": _evidence_response("array", "Current per-process network rates; no packet content is exposed."),
        "read_only": True,
        "capability_requirement": "conditionally available when elevated ETW per-process capture is active",
    },
    "get_recent_incidents": {
        "description": "Return recent persisted Thermal Watch incident evidence.",
        "parameters": _parameters(limit=True),
        "response": _evidence_response("array", "Recent incident records plus monitoring-coverage limits."),
        "read_only": True,
        "capability_requirement": "a readable snapshot containing the recent-incident collection",
    },
    "get_recent_sessions": {
        "description": "Return recent persisted Thermal Watch workload-session evidence.",
        "parameters": _parameters(limit=True),
        "response": _evidence_response("array", "Recent workload-session records plus monitoring-coverage limits."),
        "read_only": True,
        "capability_requirement": "a readable snapshot containing the recent-session collection",
    },
    "get_coverage": {
        "description": "Return monitoring coverage and explicit limits for unmonitored periods.",
        "parameters": _parameters(),
        "response": _evidence_response("object", "Coverage evidence and the boundary on unmonitored time."),
        "read_only": True,
        "capability_requirement": "a readable snapshot containing monitoring-coverage evidence",
    },
}


EVIDENCE_SEMANTICS = {
    "statuses": {
        "observed": "Thermal Watch recorded the returned evidence.",
        "derived": "Thermal Watch computed the value from recorded evidence; it is not a direct sensor reading.",
        "unavailable": "Thermal Watch cannot establish this evidence from available sensors or records.",
        "monitoring_gap": "Evidence exists, but monitoring coverage is incomplete.",
    },
    "rules": {
        "missing_values": "null or a missing value means unavailable; it never means zero.",
        "unmonitored_time": "Thermal Watch cannot determine what happened during periods it did not monitor.",
        "causation": "Workload correlation or coincidence must not be represented as proven causation.",
    },
    "preserved_metadata": ["units", "timestamps", "provenance", "coverage"],
    "provenance_authority": "Thermal Watch",
}


def _error(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def _status_for_values(value):
    if isinstance(value, dict):
        values = list(value.values())
    elif isinstance(value, list):
        values = value
    else:
        values = [value]
    return "observed" if any(v is not None for v in values) else "unavailable"


def _coverage_status(coverage):
    pct = coverage.get("coverage_pct") if isinstance(coverage, dict) else None
    if pct is None:
        return "unavailable"
    return "observed" if pct >= 100.0 else "monitoring_gap"


def _validate_request(request):
    if not isinstance(request, dict):
        return None, None, _error("invalid_request", "request must be a JSON object")
    allowed = {"operation", "parameters"}
    unknown = sorted(set(request) - allowed)
    if unknown:
        return None, None, _error("unknown_request_field", f"unknown request field: {unknown[0]}")
    operation = request.get("operation")
    if operation not in OPERATIONS:
        return None, None, _error("unknown_operation", "operation is not allowlisted")
    parameters = request.get("parameters", {})
    if not isinstance(parameters, dict):
        return None, None, _error("invalid_parameters", "parameters must be a JSON object")
    schema = OPERATIONS[operation]["parameters"]
    properties = schema.get("properties", {})
    unknown = sorted(set(parameters) - set(properties))
    if unknown:
        return None, None, _error("unknown_parameter", f"parameter is not allowlisted: {unknown[0]}")
    missing = [name for name in schema.get("required", []) if name not in parameters]
    if missing:
        return None, None, _error("missing_parameter", f"required parameter is missing: {missing[0]}")
    for name, rule in properties.items():
        if name not in parameters:
            continue
        value = parameters[name]
        if rule.get("type") == "integer" and (
                type(value) is not int or value < rule.get("minimum", value) or value > rule.get("maximum", value)):
            return None, None, _error(
                "invalid_parameter", f"{name} must be an integer from {rule.get('minimum')} to {rule.get('maximum')}")
    return operation, parameters, None


def _load_snapshot():
    try:
        value = json.loads(EVIDENCE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, _error("evidence_unavailable", "Thermal Watch evidence snapshot is unavailable")
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, _error("evidence_invalid", "Thermal Watch evidence snapshot could not be read")
    if not isinstance(value, dict):
        return None, _error("evidence_invalid", "Thermal Watch evidence snapshot has an invalid shape")
    return value, None


def _has_value(value):
    if isinstance(value, dict):
        return any(_has_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_value(item) for item in value)
    return value is not None


def _availability(state, available, reason, **details):
    result = {"state": state, "available": bool(available), "reason": reason}
    if details:
        result["details"] = details
    return result


def _operation_availability(snapshot, load_error):
    unavailable = "Thermal Watch evidence snapshot is currently unavailable"
    if load_error or not isinstance(snapshot, dict):
        return {
            name: (_availability("available", True, "tool discovery does not require live evidence")
                   if name == "describe_operations" else _availability("unavailable", False, unavailable))
            for name in OPERATIONS
        }
    live = snapshot.get("live") if isinstance(snapshot.get("live"), dict) else {}
    network = live.get("network") if isinstance(live.get("network"), dict) else {}
    component_state = {}
    for name in ("cpu", "gpu", "memory"):
        value = live.get(name)
        component_state[name] = {"available": _has_value(value),
                                 "reason": "recorded values present" if _has_value(value) else "no supported reading available"}
    sensor_available = any(item["available"] for item in component_state.values())
    process_active = network.get("per_process_capture_active") is True
    return {
        "describe_operations": _availability("available", True, "tool discovery is available"),
        "get_system_status": _availability(
            "available" if _has_value(snapshot.get("system")) or live.get("bridge_health") is not None else "unavailable",
            _has_value(snapshot.get("system")) or live.get("bridge_health") is not None,
            "system or bridge evidence present" if _has_value(snapshot.get("system")) or live.get("bridge_health") is not None
            else "system and bridge evidence unavailable"),
        "get_current_sensors": _availability(
            "conditional", sensor_available,
            "availability varies by component and hardware support", components=component_state),
        "get_network_status": _availability(
            "available" if _has_value(network) else "unavailable", _has_value(network),
            "network evidence present" if _has_value(network) else "network adapter evidence unavailable"),
        "get_top_network_processes": _availability(
            "conditional", process_active,
            "per-process network capture active" if process_active else "per-process network capture unavailable"),
        "get_recent_incidents": _availability(
            "available" if "recent_incidents_24h" in snapshot else "unavailable",
            "recent_incidents_24h" in snapshot,
            "incident collection present" if "recent_incidents_24h" in snapshot else "incident collection unavailable"),
        "get_recent_sessions": _availability(
            "available" if "recent_sessions_24h" in snapshot else "unavailable",
            "recent_sessions_24h" in snapshot,
            "session collection present" if "recent_sessions_24h" in snapshot else "session collection unavailable"),
        "get_coverage": _availability(
            "available" if isinstance(snapshot.get("coverage_24h"), dict) else "unavailable",
            isinstance(snapshot.get("coverage_24h"), dict),
            "coverage evidence present" if isinstance(snapshot.get("coverage_24h"), dict) else "coverage evidence unavailable"),
    }


def describe_operation_catalog():
    snapshot, load_error = _load_snapshot()
    availability = _operation_availability(snapshot, load_error)
    operations = {}
    for name, definition in OPERATIONS.items():
        item = copy.deepcopy(definition)
        item["name"] = name
        item["availability"] = availability[name]
        operations[name] = item
    return {
        "ok": True,
        "operation": "describe_operations",
        "adapter_version": ADAPTER_VERSION,
        "tool_catalog_schema": TOOL_CATALOG_SCHEMA,
        "tool_catalog_version": TOOL_CATALOG_VERSION,
        "provenance": {"authority": "Thermal Watch", "read_only": True},
        "operations": operations,
        "semantics": copy.deepcopy(EVIDENCE_SEMANTICS),
    }


def _response(operation, snapshot, data, evidence_status, coverage=None):
    result = {
        "ok": True,
        "operation": operation,
        "adapter_version": ADAPTER_VERSION,
        "evidence_schema_version": snapshot.get("schema_version"),
        "generated_at": snapshot.get("generated_at"),
        "evidence_status": evidence_status,
        "provenance": {"authority": "Thermal Watch", "source": "local_evidence_snapshot", "read_only": True},
        "data": data,
    }
    if coverage is not None:
        result["coverage"] = coverage
        if _coverage_status(coverage) == "monitoring_gap":
            result["monitoring_limit"] = {
                "can_establish_events_during_unmonitored_time": False,
                "statement": "Thermal Watch cannot determine what happened during periods it did not monitor.",
                "incident_monitoring_gap_seconds_scope": "applies only inside each recorded incident; it does not describe unmonitored time outside recorded incidents",
            }
    return result


def _select_fields(record, names):
    return {name: record.get(name) for name in names if name in record}


def _compact_incident(record):
    # Phase 14 - Evidence IDs: evidence_id/source_type are purely additive alongside the existing
    # incident_id - an older, pre-Phase-14 record simply has no "evidence_id" key yet, and
    # _select_fields() already omits any field not present rather than fabricating one.
    fields = _select_fields(record, (
        "incident_id", "evidence_id", "start_timestamp", "end_timestamp", "duration_seconds",
        "component", "sensor_name", "sensor_identifier", "starting_zone", "max_zone",
        "start_value", "peak_value", "recovery_value", "dominant_workload", "foreground_process",
        "monitoring_gap_seconds", "monitoring_gaps", "context_peak", "close_reason",
    ))
    fields["source_type"] = "network_incident" if record.get("component") == "network" else "incident"
    return fields


def _compact_session(record):
    fields = _select_fields(record, (
        "session_id", "evidence_id", "workload_key", "display_name", "start_timestamp",
        "end_timestamp", "duration_seconds", "cpu", "gpu", "memory", "network",
        "monitoring_gap_seconds", "monitoring_gaps", "uncertain",
    ))
    fields["source_type"] = "session"
    return fields


def handle_request(request):
    operation, parameters, validation_error = _validate_request(request)
    if validation_error:
        return validation_error
    if operation == "describe_operations":
        return describe_operation_catalog()

    snapshot, load_error = _load_snapshot()
    if load_error:
        return load_error
    coverage = snapshot.get("coverage_24h") or {}
    live = snapshot.get("live") or {}
    if operation == "get_system_status":
        data = {"system": snapshot.get("system"), "bridge_health": live.get("bridge_health")}
        return _response(operation, snapshot, data, _status_for_values(data))
    if operation == "get_current_sensors":
        data = {key: live.get(key) for key in ("cpu", "gpu", "memory")}
        status = "observed" if any(_status_for_values(v or {}) == "observed" for v in data.values()) else "unavailable"
        return _response(operation, snapshot, data, status)
    if operation == "get_network_status":
        data = dict(live.get("network") or {})
        data.pop("top_processes", None)
        return _response(operation, snapshot, data, _status_for_values(data))
    if operation == "get_top_network_processes":
        network = live.get("network") or {}
        rows = []
        for source_row in network.get("top_processes") or []:
            down = source_row.get("down_mbps")
            up = source_row.get("up_mbps")
            rows.append({
                "pid": source_row.get("pid"), "process_name": source_row.get("name"),
                "current_download_mbps": down, "current_upload_mbps": up,
                "current_combined_mbps": ((down or 0.0) + (up or 0.0))
                    if down is not None or up is not None else None,
                "measurement": "current rate from the latest Thermal Watch sampling interval",
            })
        rows.sort(key=lambda row: row["current_combined_mbps"] or 0.0, reverse=True)
        rows = rows[: parameters.get("limit", 5)]
        status = "observed" if rows else ("unavailable" if not network.get("per_process_capture_active") else "observed")
        return _response(operation, snapshot, rows, status)
    if operation == "get_recent_incidents":
        rows = [_compact_incident(row) for row in
                list(snapshot.get("recent_incidents_24h") or [])[: parameters.get("limit", 25)]]
        result = _response(operation, snapshot, rows, _coverage_status(coverage), coverage)
        result["data_scope"] = "recorded incidents only; no record can establish what happened outside monitored time"
        return result
    if operation == "get_recent_sessions":
        rows = [_compact_session(row) for row in
                list(snapshot.get("recent_sessions_24h") or [])[: parameters.get("limit", 25)]]
        result = _response(operation, snapshot, rows, _coverage_status(coverage), coverage)
        result["data_scope"] = "recorded sessions only; no record can establish what happened outside monitored time"
        return result
    return _response(operation, snapshot, coverage, _coverage_status(coverage), coverage)


def main():
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--request-base64":
            raw = base64.b64decode(sys.argv[2], validate=True).decode("utf-8")
        elif len(sys.argv) == 1:
            raw = sys.stdin.read()
        else:
            raise ValueError("unsupported arguments")
        request = json.loads(raw)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        response = _error("malformed_json", "stdin must contain one valid JSON request")
    else:
        response = handle_request(request)
    sys.stdout.write(json.dumps(response, separators=(",", ":"), ensure_ascii=False) + "\n")
    return 0 if response.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
