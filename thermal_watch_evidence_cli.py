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
# Matches app.py's real INCIDENT_RETENTION_DAYS/SESSION_RETENTION_DAYS - the actual maximum
# history Thermal Watch still has on disk. A wider max would silently promise data that was
# already pruned.
MAX_DAYS = 30
TOOL_CATALOG_VERSION = 1
TOOL_CATALOG_SCHEMA = "thermal-watch-tool-catalog"


def _parameters(*, limit=False, days=False):
    properties = {}
    if limit:
        properties["limit"] = {
            "type": "integer", "minimum": 1, "maximum": MAX_LIMIT,
            "description": "Maximum number of newest records to return.",
        }
    if days:
        properties["days"] = {
            "type": "integer", "minimum": 1, "maximum": MAX_DAYS,
            "description": "How many days of history to include, ending now. Omit or 1 for the "
                            "default last-24h snapshot; up to " + str(MAX_DAYS) +
                            " reaches further back into Thermal Watch's retained history "
                            "(only available over the live pipe, not the on-disk snapshot file).",
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
        "parameters": _parameters(limit=True, days=True),
        "response": _evidence_response("array", "Recent incident records plus monitoring-coverage limits."),
        "read_only": True,
        "capability_requirement": "a readable snapshot containing the recent-incident collection",
    },
    "get_recent_sessions": {
        "description": "Return recent persisted Thermal Watch workload-session evidence.",
        "parameters": _parameters(limit=True, days=True),
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
    "get_experiments": {
        "description": "Return persisted hardware-change experiment markers (e.g. 'installed new "
                        "fans'), or - given experiment_id - a full before/after comparison report "
                        "for one marker. Call with no experiment_id first to discover valid ids.",
        "parameters": {
            "type": "object",
            "properties": {
                "experiment_id": {
                    "type": "string",
                    "description": "Return the full before/after report for this one marker "
                                    "instead of the marker list. Get valid ids from a call with "
                                    "no experiment_id first.",
                },
            },
            "required": [], "additionalProperties": False,
        },
        "response": _evidence_response(
            "object", "Either the marker list or one marker's full before/after report - a "
                       "report always carries a non-causal caveat, even when it shows a real "
                       "measured change."),
        "read_only": True,
        "capability_requirement": "only available over the live Thermal Watch pipe, not the on-disk snapshot file",
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
        # Experiment markers/reports aren't embedded in the snapshot at all (unlike incidents/
        # sessions, which at least carry a 24h subset) - there's no snapshot signal to gate on
        # either way, so this reports "available" whenever a snapshot loads; the file-based CLI
        # transport (no experiments_fetcher, ever) degrades honestly at dispatch time instead,
        # same pattern as get_recent_incidents/get_recent_sessions with days>1.
        "get_experiments": _availability(
            "available", True, "marker discovery is available; a live pipe connection is needed for a full report"),
    }


def describe_operation_catalog(loader=_load_snapshot):
    snapshot, load_error = loader()
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


def _recent_records(operation, snapshot, coverage, parameters, component, snapshot_key, compact_fn, history_fetcher):
    """Shared dispatch for get_recent_incidents/get_recent_sessions. days<=1 (the default) is
    served from the already-embedded 24h snapshot field exactly as before - byte-for-byte
    unchanged behavior. days>1 needs data the snapshot doesn't carry, so it's only honored when
    a history_fetcher was supplied (the in-process pipe path); otherwise it degrades honestly
    (evidence_unavailable) rather than silently answering from the narrower 24h window."""
    days = parameters.get("days", 1)
    limit = parameters.get("limit", 25)
    scope_label = "recorded " + component + " only"
    if days > 1:
        if history_fetcher is None:
            return _error("history_unavailable",
                           "history beyond the last 24h is only available over the live Thermal "
                           "Watch pipe, not this snapshot file")
        rows, wide_coverage, fetch_error = history_fetcher(component, days)
        if fetch_error:
            return fetch_error
        result = _response(operation, snapshot, [compact_fn(r) for r in rows[:limit]],
                            _coverage_status(wide_coverage), wide_coverage)
        result["data_scope"] = (scope_label + ", over the requested " + str(days) + "-day window; "
                                 "no record can establish what happened outside monitored time")
        return result
    rows = list(snapshot.get(snapshot_key) or [])[:limit]
    result = _response(operation, snapshot, [compact_fn(r) for r in rows], _coverage_status(coverage), coverage)
    result["data_scope"] = scope_label + "; no record can establish what happened outside monitored time"
    return result


def _compact_experiment_marker(record):
    return _select_fields(record, ("experiment_id", "created_timestamp", "change_timestamp",
                                    "description", "component"))


def _compact_experiment_report(report):
    fields = _select_fields(report, (
        "experiment", "component_label", "bounds", "insufficient_reason", "workload_trends",
        "idle", "health_score", "confounds", "direction", "confidence", "primary_source",
        "primary", "caveat",
    ))
    if isinstance(fields.get("experiment"), dict):
        fields["experiment"] = _compact_experiment_marker(fields["experiment"])
    return fields


def _experiments_response(operation, snapshot, parameters, experiments_fetcher):
    """get_experiments dispatch. Unlike every other operation, this one has NOTHING in the
    snapshot to fall back to (experiment markers/reports were never embedded in it) - it is
    pipe-only from the start, not "pipe-only above a 1-day default" like get_recent_incidents/
    get_recent_sessions. experiments_fetcher: (experiment_id_or_None) -> (result, error_dict);
    result is the raw marker list when experiment_id is None, or one compute_experiment_report()
    dict (already carrying its EXPERIMENT_CAVEAT text - added by the fetcher, which is the one
    side of this split that actually knows that constant) when experiment_id names a real marker.
    """
    if experiments_fetcher is None:
        return _error("history_unavailable",
                       "experiment markers and reports are only available over the live Thermal "
                       "Watch pipe, not this snapshot file")
    experiment_id = parameters.get("experiment_id")
    result, fetch_error = experiments_fetcher(experiment_id)
    if fetch_error:
        return fetch_error
    if experiment_id is None:
        markers = [_compact_experiment_marker(m) for m in result]
        response = _response(operation, snapshot, {"markers": markers},
                              "observed" if markers else "unavailable")
        response["data_scope"] = ("experiment markers only; call again with experiment_id from "
                                   "this list for a full before/after report")
        return response
    data = _compact_experiment_report(result)
    status = "unavailable" if data.get("bounds") is None or data.get("direction") is None else "derived"
    response = _response(operation, snapshot, data, status)
    response["data_scope"] = ("a computed before/after comparison for one marked change; "
                               "correlation with the marked change is not proof of causation")
    return response


def handle_request(request, loader=_load_snapshot, history_fetcher=None, experiments_fetcher=None):
    """loader: () -> (snapshot_dict_or_None, error_dict_or_None), matching _load_snapshot()'s
    contract. Defaults to the on-disk snapshot (this CLI's normal behavior); the in-process
    named-pipe server (thermal_watch_evidence_pipe.py, wired up in app.py) passes a loader that
    reads a live cached snapshot instead, with zero duplication of validation/dispatch/formatting.

    history_fetcher: optional (component, days) -> (rows, coverage_dict_or_None, error_dict_or_None).
    component is "incidents" or "sessions". Used only by get_recent_incidents/get_recent_sessions
    when `days` > 1 is requested - see _recent_records(). Only the pipe path supplies this, since
    only it can read Thermal Watch's full retained history directly.

    experiments_fetcher: optional (experiment_id_or_None) -> (result, error_dict_or_None), used
    only by get_experiments - see _experiments_response(). Only the pipe path supplies this.
    """
    operation, parameters, validation_error = _validate_request(request)
    if validation_error:
        return validation_error
    if operation == "describe_operations":
        return describe_operation_catalog(loader)

    snapshot, load_error = loader()
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
        return _recent_records(operation, snapshot, coverage, parameters, "incidents",
                                "recent_incidents_24h", _compact_incident, history_fetcher)
    if operation == "get_recent_sessions":
        return _recent_records(operation, snapshot, coverage, parameters, "sessions",
                                "recent_sessions_24h", _compact_session, history_fetcher)
    if operation == "get_experiments":
        return _experiments_response(operation, snapshot, parameters, experiments_fetcher)
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
