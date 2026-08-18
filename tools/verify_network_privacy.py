"""Deterministic Network Intelligence persistence/export privacy boundary checks."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401 - redirects every store before importing app
import app as tw  # noqa: E402


PROCESS_SENTINEL = "PRIVATE_PROCESS_SENTINEL.exe"
LOCAL_SENTINEL = "10.23.45.67:54321"
REMOTE_SENTINEL = "203.0.113.77:443"


def check(condition, message):
    assert condition, f"FAIL: {message}"
    print(f"  PASS: {message}")


def contains_private_row(value):
    text = json.dumps(value, sort_keys=True, default=str)
    return any(s in text for s in (PROCESS_SENTINEL, LOCAL_SENTINEL, REMOTE_SENTINEL))


def main():
    print("=== Network persistence/export privacy boundary ===")
    with patch.object(tw.App, "worker", lambda self: None):
        root = tw.App()
    root.withdraw()

    # Feed unmistakable private row data into the live-only attributes used by the dashboard.
    root.last_connections = [{"pid": 4242, "name": PROCESS_SENTINEL, "protocol": "TCP",
                              "local": LOCAL_SENTINEL, "remote": REMOTE_SENTINEL,
                              "state": "ESTABLISHED"}]
    root.last_net_procs = {"capture_active": True, "processes": [
        {"pid": 4242, "name": PROCESS_SENTINEL, "rx_bytes": 1234, "tx_bytes": 5678}]}
    root.last_net = {"adapter": {"name": "Privacy test adapter", "type": "Ethernet",
                                  "media_connect_state": "Connected",
                                  "in_octets": 100000, "out_octets": 50000},
                     "down_mbps": 12.5, "up_mbps": 3.25}
    root.last_context.update({"net_down_mbps": 12.5, "net_up_mbps": 3.25,
                              "net_rx_bytes": 100000, "net_tx_bytes": 50000})

    check(root.last_connections[0]["remote"] == REMOTE_SENTINEL,
          "connection endpoint is available to the live UI state")
    check(root.last_net_procs["processes"][0]["name"] == PROCESS_SENTINEL,
          "per-process attribution is available to the live UI state")

    with patch.object(tw, "read_incidents_file", return_value=[]), \
         patch.object(tw, "read_sessions_file", return_value=[]), \
         patch.object(tw, "read_telemetry_file", return_value=[]), \
         patch.object(tw, "bridge_tier1_age", return_value=None), \
         patch.object(tw, "bridge_status", return_value=None):
        evidence = root._build_evidence_snapshot()
    check(not contains_private_row(evidence), "evidence snapshot contains no raw connection/process row")
    check(evidence["live"]["network"]["tcp_connections"] == 1,
          "evidence snapshot retains only the intended aggregate TCP count")
    check("connections" not in evidence["live"]["network"],
          "evidence schema has no raw connection-history field")
    check("processes" not in evidence["live"]["network"],
          "evidence schema has no per-process network-history field")

    root._telemetry_observe_tick([])
    bucket = root.telemetry_bucket
    bucket["end_timestamp"] = bucket["start_timestamp"] + 1
    root._persist_telemetry_bucket(bucket)
    persisted = tw.read_telemetry_file()
    check(bool(persisted), "network telemetry bucket persisted")
    check(not contains_private_row(persisted), "persisted telemetry contains no live connection/process data")
    scalar_keys = set(persisted[0]["scalars"])
    check({"net_down_mbps", "net_up_mbps", "net_rx_bytes", "net_tx_bytes"} <= scalar_keys,
          "persisted telemetry retains only intended network rates/totals")
    check(not ({"process", "processes", "local", "remote", "connections"} & scalar_keys),
          "telemetry scalar schema exposes no endpoint/process fields")

    exported = tw.build_telemetry_json_export(persisted)
    check(not contains_private_row(exported), "telemetry JSON export contains no live connection/process data")
    check(set(tw.CSV_COLUMNS).isdisjoint({"local", "remote", "connections", "network_processes"}),
          "incident CSV export schema contains no raw connection fields")
    check(not contains_private_row(tw.build_json_export([])),
          "incident JSON export cannot acquire live connection/process rows")

    root = tw.destroy_test_app(root) if hasattr(tw, "destroy_test_app") else root
    if root is not None:
        root.stop_event.set()
        root.destroy()
    print("\nALL NETWORK PRIVACY CHECKS PASSED")


if __name__ == "__main__":
    main()
