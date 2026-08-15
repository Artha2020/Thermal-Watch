"""Verification for active-incident durability across close/crash/restart."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import App, INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH, GPU_HOTSPOT_ZONES  # noqa: E402


def fresh_files():
    for p in (INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH):
        if p.exists():
            p.unlink()
    tmp = ACTIVE_INCIDENTS_PATH.with_suffix(".tmp")
    if tmp.exists():
        tmp.unlink()


def tick_cpu(app, temp):
    app._update_cpu_zone(temp)
    app._incident_observe("cpu", "cpu", "CPU Package", "/amdcpu/0/temperature/0", temp)


def tick_gpu_hotspot(app, temp):
    app._update_sensor_zone("gpu_hotspot", "RTX 3090 GPU Hotspot", temp, "°C", GPU_HOTSPOT_ZONES)
    app._incident_observe("sensor:gpu_hotspot", "gpu_hotspot", "RTX 3090 GPU Hotspot",
                          "/gpu-nvidia/0/temperature/2", temp)


def read_active_file():
    if not ACTIVE_INCIDENTS_PATH.exists():
        return {}
    return json.loads(ACTIVE_INCIDENTS_PATH.read_text(encoding="utf-8")).get("incidents", {})


def open_cpu_incident(app):
    tick_cpu(app, 82.0)
    time.sleep(1.6); tick_cpu(app, 82.1)
    time.sleep(1.7); tick_cpu(app, 82.2)
    assert "cpu" in app.incidents_active
    return app.incidents_active["cpu"]["incident_id"]


def main():
    fresh_files()

    print("=== A. normal incident (no restart): unchanged behavior ===")
    app = App()
    inc_id = open_cpu_incident(app)
    time.sleep(3.2); tick_cpu(app, 95.0)  # escalate ORANGE (its own debounce already elapsed above +now)
    tick_cpu(app, 40.0)  # immediate recovery
    assert not app.incidents_active
    closed = app.incidents_recent[0]
    assert closed["incident_id"] == inc_id
    assert closed["monitoring_gaps"] == []
    assert closed["duration_exact"] is True
    print(f"  PASS: normal incident closed with monitoring_gaps=[] duration_exact=True (id={inc_id})")
    app.stop_event.set(); app.destroy()
    fresh_files()

    print("\n=== B. restart while still hot ===")
    app1 = App()
    inc_id_b = open_cpu_incident(app1)
    orig_start = app1.incidents_active["cpu"]["start_timestamp"]
    orig_peak = app1.incidents_active["cpu"]["peak_value"]
    on_disk = read_active_file()
    assert "cpu" in on_disk and on_disk["cpu"]["incident_id"] == inc_id_b, "FAIL: not persisted to active file"
    print(f"  incident persisted to {ACTIVE_INCIDENTS_PATH.name} before 'crash' (no clean close called)")
    app1.stop_event.set(); app1.destroy()  # simulate crash: no close(), no final save - already saved by _incident_open

    app2 = App()  # fresh process, loads active file in build()
    assert "cpu" in app2.incident_restore_pending, "FAIL: did not load pending restore"
    assert "cpu" not in app2.incidents_active, "FAIL: should NOT be in incidents_active before reconciliation"
    # sensor is STILL hot after restart - let the real debounce engine reconfirm from scratch
    tick_cpu(app2, 83.0)
    assert "cpu" not in app2.incidents_active, "FAIL: must not open a NEW incident while restore is pending"
    time.sleep(1.6); tick_cpu(app2, 83.1)
    time.sleep(1.7); tick_cpu(app2, 83.2)
    assert app2.cpu_zone_confirmed == "YELLOW"
    assert "cpu" not in app2.incidents_active, "FAIL: opened a duplicate before reconciliation ran"
    app2._reconcile_restored_incidents()  # bypass the 8s real-time wait for the test
    assert "cpu" in app2.incidents_active, "FAIL: did not resume after reconciliation"
    resumed = app2.incidents_active["cpu"]
    assert resumed["incident_id"] == inc_id_b, "FAIL: got a new incident_id instead of resuming"
    assert resumed["start_timestamp"] == orig_start, "FAIL: start time was reset"
    assert resumed["peak_value"] == orig_peak, "FAIL: peak was reset"
    assert len(resumed["monitoring_gaps"]) == 1
    assert len(app2.incidents_recent) == 0, "FAIL: should not have been closed"
    print(f"  PASS: resumed same incident_id={resumed['incident_id']}, start/peak preserved, "
          f"gap recorded ({resumed['monitoring_gaps'][0]['gap_seconds']:.1f}s)")
    app2.stop_event.set(); app2.destroy()
    fresh_files()

    print("\n=== C. recovered while offline ===")
    app1 = App()
    inc_id_c = open_cpu_incident(app1)
    peak_c = app1.incidents_active["cpu"]["peak_value"]
    app1.stop_event.set(); app1.destroy()

    app2 = App()
    assert "cpu" in app2.incident_restore_pending
    # sensor is NOMINAL now - real value observed, no alert ever confirmed
    tick_cpu(app2, 45.0)
    assert app2.cpu_zone_confirmed == "GREEN"
    assert "cpu" not in app2.incidents_active
    app2._reconcile_restored_incidents()
    assert "cpu" not in app2.incidents_active
    assert "cpu" not in app2.incident_restore_pending
    closed = next(i for i in app2.incidents_recent if i["incident_id"] == inc_id_c)
    assert closed["recovery_during_monitoring_gap"] is True
    assert closed["duration_exact"] is False
    assert closed["recovery_value"] is None, "FAIL: must not fabricate an exact recovery value"
    assert closed["peak_value"] == peak_c
    assert closed["last_observed_alert_timestamp"] is not None
    assert closed["first_observed_recovered_timestamp"] is not None
    assert closed["first_observed_recovered_value"] == 45.0
    print(f"  PASS: closed with recovery_during_monitoring_gap=True, duration_exact=False, "
          f"recovery_value=None (not fabricated), first_observed_recovered_value=45.0")
    app2.stop_event.set(); app2.destroy()
    fresh_files()

    print("\n=== D. escalation while offline (Yellow -> restart -> Red) ===")
    app1 = App()
    inc_id_d = open_cpu_incident(app1)  # opens at YELLOW
    assert app1.incidents_active["cpu"]["max_zone"] == "YELLOW"
    app1.stop_event.set(); app1.destroy()

    app2 = App()
    tick_cpu(app2, 101.0)  # RED - immediate, no debounce
    assert app2.cpu_zone_confirmed == "RED"
    assert "cpu" not in app2.incidents_active, "FAIL: must still wait for reconciliation"
    app2._reconcile_restored_incidents()
    resumed = app2.incidents_active["cpu"]
    assert resumed["incident_id"] == inc_id_d, "FAIL: created a second incident instead of preserving the old one"
    assert resumed["starting_zone"] == "YELLOW"
    assert resumed["max_zone"] == "RED", "FAIL: max_zone not updated to reflect current Red state"
    print(f"  PASS: same incident_id={resumed['incident_id']}, starting_zone=YELLOW preserved, max_zone updated to RED")
    app2.stop_event.set(); app2.destroy()
    fresh_files()

    print("\n=== E. multiple simultaneous incidents restore independently ===")
    app1 = App()
    cpu_id = open_cpu_incident(app1)
    tick_gpu_hotspot(app1, 90.0)
    time.sleep(1.6); tick_gpu_hotspot(app1, 90.1)
    time.sleep(1.7); tick_gpu_hotspot(app1, 90.2)
    assert "sensor:gpu_hotspot" in app1.incidents_active
    gpu_id = app1.incidents_active["sensor:gpu_hotspot"]["incident_id"]
    assert cpu_id != gpu_id
    app1.stop_event.set(); app1.destroy()

    app2 = App()
    assert set(app2.incident_restore_pending.keys()) == {"cpu", "sensor:gpu_hotspot"}
    tick_cpu(app2, 82.0); time.sleep(1.6); tick_cpu(app2, 82.1); time.sleep(1.7); tick_cpu(app2, 82.2)
    tick_gpu_hotspot(app2, 88.0); time.sleep(1.6); tick_gpu_hotspot(app2, 88.1); time.sleep(1.7); tick_gpu_hotspot(app2, 88.2)
    app2._reconcile_restored_incidents()
    assert app2.incidents_active["cpu"]["incident_id"] == cpu_id
    assert app2.incidents_active["sensor:gpu_hotspot"]["incident_id"] == gpu_id
    print(f"  PASS: both incidents resumed independently with original ids ({cpu_id}, {gpu_id})")
    app2.stop_event.set(); app2.destroy()
    fresh_files()

    print("\n=== F. corrupt active-state file: app must still launch normally ===")
    for label, content in (("malformed", "{not valid json!!"), ("truncated", '{"incidents": {"cpu": {"incident_id"'),
                           ("empty", ""), ("wrong type", '{"incidents": "not a dict"}'),
                           ("array not object", "[1,2,3]")):
        ACTIVE_INCIDENTS_PATH.write_text(content, encoding="utf-8")
        app = App()
        assert app.incident_restore_pending == {}, f"FAIL ({label}): should restore nothing from corrupt data"
        app.stop_event.set(); app.destroy()
        print(f"  PASS ({label}): app started fine, restore_pending empty")
    if ACTIVE_INCIDENTS_PATH.exists():
        ACTIVE_INCIDENTS_PATH.unlink()
    print("  PASS (missing file): implicitly covered by every earlier scenario's fresh_files()")

    print("\n=== G. restored incident whose sensor no longer exists ===")
    fake_incident = {
        "incident_id": "drive-999999", "start_timestamp": time.time() - 300, "end_timestamp": None,
        "component": "drive", "sensor_name": "GHOST DRIVE", "sensor_identifier": None,
        "starting_zone": "YELLOW", "max_zone": "YELLOW", "start_value": 65.0, "peak_value": 68.0,
        "recovery_value": None, "foreground_process": None, "foreground_title": None,
        "top_cpu_processes": [], "top_gpu_processes": [], "context_peak": {}, "samples": [],
        "last_observed_timestamp": time.time() - 300, "last_observed_value": 68.0, "last_observed_zone": "YELLOW",
        "monitoring_gaps": [], "monitoring_gap_seconds": 0.0, "alert_key": "disk:GHOST",
    }
    ACTIVE_INCIDENTS_PATH.write_text(json.dumps({"saved_at": time.time(), "incidents": {"disk:GHOST": fake_incident}}),
                                     encoding="utf-8")
    app = App()
    assert "disk:GHOST" in app.incident_restore_pending
    # This session's update_data() never calls _incident_observe("disk:GHOST", ...) - the drive is gone.
    app._reconcile_restored_incidents()
    assert "disk:GHOST" not in app.incidents_active
    assert "disk:GHOST" not in app.incident_restore_pending
    closed = next(i for i in app.incidents_recent if i["incident_id"] == "drive-999999")
    assert closed["close_reason"] == "sensor_unavailable"
    assert closed["recovery_during_monitoring_gap"] is False
    assert closed["first_observed_recovered_value"] is None
    print("  PASS: closed with close_reason='sensor_unavailable', not silently deleted, not fabricated as recovered")
    app.stop_event.set(); app.destroy()
    fresh_files()

    print("\n=== H. idempotency: reconciliation/restoration never double-appends a completed incident ===")
    app1 = App()
    inc_id_h = open_cpu_incident(app1)
    app1.stop_event.set(); app1.destroy()

    app2 = App()
    tick_cpu(app2, 40.0)  # nominal -> will close as recovered-during-gap
    app2._reconcile_restored_incidents()
    count_after_first_reconcile = sum(1 for i in app2.incidents_recent if i["incident_id"] == inc_id_h)
    assert count_after_first_reconcile == 1
    lines_in_file = sum(1 for l in INCIDENTS_PATH.read_text(encoding="utf-8").splitlines() if l.strip())
    assert lines_in_file == 1
    # Re-run restoration/reconciliation again against the SAME (now-closed) incident id, simulating
    # a stale active-state entry surviving a crash between the JSONL append and the active-file update.
    ACTIVE_INCIDENTS_PATH.write_text(json.dumps({"saved_at": time.time(), "incidents": {
        "cpu": {**{k: v for k, v in app2.incidents_recent[0].items()}, "alert_key": "cpu"}}}), encoding="utf-8")
    app2.load_active_incidents()
    assert "cpu" not in app2.incident_restore_pending, "FAIL: stale already-completed entry should be discarded"
    app2._reconcile_restored_incidents()
    lines_in_file_after = sum(1 for l in INCIDENTS_PATH.read_text(encoding="utf-8").splitlines() if l.strip())
    assert lines_in_file_after == 1, f"FAIL: duplicate append, file now has {lines_in_file_after} lines"
    print(f"  PASS: re-running restoration against an already-completed incident appended nothing new "
          f"(file still has {lines_in_file_after} line)")
    app2.stop_event.set(); app2.destroy()
    fresh_files()

    print("\nALL DURABILITY CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
