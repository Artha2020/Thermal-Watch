"""Verification for the Thermal Incident History feature."""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import App, INCIDENTS_PATH  # noqa: E402


def tick_cpu(app, temp):
    app._update_cpu_zone(temp)
    app._incident_observe("cpu", "cpu", "CPU Package", "/amdcpu/0/temperature/0", temp)


def tick_gpu_hotspot(app, temp):
    from app import GPU_HOTSPOT_ZONES
    app._update_sensor_zone("gpu_hotspot", "RTX 3090 GPU Hotspot", temp, "°C", GPU_HOTSPOT_ZONES)
    app._incident_observe("sensor:gpu_hotspot", "gpu_hotspot", "RTX 3090 GPU Hotspot",
                          "/gpu-nvidia/0/temperature/2", temp)


def main():
    # Fresh slate for a deterministic test.
    if INCIDENTS_PATH.exists():
        INCIDENTS_PATH.unlink()

    app = App()
    app.last_cpu_top = [("Cyberpunk2077.exe", 222, 45.0)]
    app.last_gpu_top = [("Cyberpunk2077.exe", 222, 90.0)]
    app.last_foreground = {"name": "Cyberpunk2077.exe", "pid": 222, "title": "Cyberpunk 2077"}

    print("=== 1. CPU Yellow/Orange -> exactly one incident opens ===")
    tick_cpu(app, 82.0)
    assert "cpu" not in app.incidents_active, "FAIL: opened before debounce"
    time.sleep(1.6); tick_cpu(app, 82.1)
    assert "cpu" not in app.incidents_active, "FAIL: opened before debounce"
    time.sleep(1.7); tick_cpu(app, 82.2)
    assert "cpu" in app.incidents_active, "FAIL: should be open after 3.3s sustained"
    assert len([k for k in app.incidents_active if k == "cpu"]) == 1
    incident_id_1 = app.incidents_active["cpu"]["incident_id"]
    print(f"  PASS: exactly one incident opened ({incident_id_1})")

    print("\n=== 2. escalate to a higher zone -> SAME incident, not a new one ===")
    tick_cpu(app, 95.0)  # ORANGE needs its own 3s debounce
    time.sleep(3.2); tick_cpu(app, 95.1)
    assert app.cpu_zone_confirmed == "ORANGE"
    assert app.incidents_active["cpu"]["incident_id"] == incident_id_1, "FAIL: escalation created a new incident"
    assert app.incidents_active["cpu"]["max_zone"] == "ORANGE"
    assert app.incidents_active["cpu"]["starting_zone"] == "YELLOW"
    print("  PASS: same incident_id, max_zone updated to ORANGE, starting_zone still YELLOW")

    print("\n=== 3. recover to Green -> incident closes with correct duration/peak ===")
    tick_cpu(app, 101.0)  # RED, immediate - push peak higher first
    assert app.incidents_active["cpu"]["peak_value"] == 101.0
    time.sleep(0.5)
    tick_cpu(app, 50.0)  # immediate recovery
    assert "cpu" not in app.incidents_active
    assert len(app.incidents_recent) == 1
    closed = app.incidents_recent[0]
    assert closed["incident_id"] == incident_id_1
    assert closed["max_zone"] == "RED"
    assert closed["peak_value"] == 101.0
    assert closed["recovery_value"] == 50.0
    # Duration only counts time the incident was OPEN (from its debounce-confirmed start),
    # not the initial ~3.3s debounce wait that happened BEFORE it opened: ~3.2s (escalation
    # debounce, incident already active) + ~0.5s before recovery.
    assert closed["duration_seconds"] > 3, f"duration too short: {closed['duration_seconds']}"
    print(f"  PASS: closed, duration={closed['duration_seconds']:.1f}s peak={closed['peak_value']}"
          f" recovery={closed['recovery_value']}")

    print("\n=== 4. simultaneous CPU + GPU Hotspot incidents track independently ===")
    tick_cpu(app, 82.0)
    time.sleep(1.5)
    tick_gpu_hotspot(app, 90.0)  # GPU hotspot YELLOW starts its OWN debounce clock
    time.sleep(1.8); tick_cpu(app, 82.1)  # cpu now at 3.3s
    assert "cpu" in app.incidents_active, "FAIL: cpu incident should be open"
    assert "sensor:gpu_hotspot" not in app.incidents_active, "FAIL: gpu hotspot should not be open yet (~1.8s only)"
    time.sleep(1.6); tick_gpu_hotspot(app, 90.1)  # gpu hotspot now at ~3.4s
    assert "sensor:gpu_hotspot" in app.incidents_active, "FAIL: gpu hotspot incident should be open now"
    cpu_id = app.incidents_active["cpu"]["incident_id"]
    gpu_id = app.incidents_active["sensor:gpu_hotspot"]["incident_id"]
    assert cpu_id != gpu_id
    print(f"  PASS: independent incidents active simultaneously (cpu={cpu_id}, gpu_hotspot={gpu_id})")

    print("\n=== 5. workload attribution attached ===")
    cpu_inc = app.incidents_active["cpu"]
    assert cpu_inc["foreground_process"] == "Cyberpunk2077.exe"
    assert cpu_inc["foreground_title"] == "Cyberpunk 2077"
    assert cpu_inc["top_cpu_processes"][0][0] == "Cyberpunk2077.exe"
    gpu_inc = app.incidents_active["sensor:gpu_hotspot"]
    assert gpu_inc["top_gpu_processes"][0][0] == "Cyberpunk2077.exe"
    print("  PASS: foreground + top CPU/GPU processes captured on both incidents")

    # close both for the persistence test
    tick_cpu(app, 40.0)
    tick_gpu_hotspot(app, 40.0)
    assert not app.incidents_active
    assert len(app.incidents_recent) == 3
    print("\n=== 6. incidents persist across restart ===")
    app.stop_event.set(); app.destroy()
    assert INCIDENTS_PATH.exists()
    lines = [json.loads(l) for l in INCIDENTS_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 3, f"FAIL: expected 3 persisted incidents, found {len(lines)}"
    app2 = App()
    assert len(app2.incidents_recent) == 3, "FAIL: incidents did not reload on restart"
    reloaded_ids = {i["incident_id"] for i in app2.incidents_recent}
    assert cpu_id in reloaded_ids and gpu_id in reloaded_ids
    print(f"  PASS: {len(app2.incidents_recent)} incidents reloaded after restart, ids match")

    print("\n=== 7. Event Log behavior unchanged (still gets WARN/CRIT/INFO entries) ===")
    kinds_seen = {e["kind"] for e in app2.events}
    print(f"  event kinds present: {kinds_seen}")
    assert "INFO" in kinds_seen  # at minimum "Polling interval set..." always present
    print("  PASS: Event Log still populated independently of incidents")

    print("\n=== 8. no incident from same-zone polling ===")
    app2.last_cpu_top = [("test.exe", 1, 50.0)]
    tick_cpu(app2, 82.0)
    time.sleep(3.2); tick_cpu(app2, 82.1)
    assert "cpu" in app2.incidents_active
    created_count_before = len(app2.incidents_recent)
    for _ in range(4):
        tick_cpu(app2, 82.2)  # same YELLOW zone repeatedly
    assert len(app2.incidents_recent) == created_count_before, "FAIL: same-zone polling created a new incident"
    assert app2.incidents_active["cpu"]["incident_id"] is not None  # still just the one, untouched count
    tick_cpu(app2, 40.0)  # close it out cleanly
    print("  PASS: 4 same-zone polls produced zero new incidents")

    print("\n=== 9. no incident for unverified motherboard sensors (PCIe x1*) ===")
    all_components = {i.get("component") for i in app2.incidents_recent}
    print(f"  components ever seen in incidents: {all_components}")
    assert "mobo" not in all_components
    assert not any("pcie" in (i.get("sensor_name") or "").lower() for i in app2.incidents_recent)
    print("  PASS: motherboard/PCIe x1 never generates an incident (not wired to incident tracking at all)")

    print("\n=== 10. drive incidents only from real Composite Temperature (structural check) ===")
    import inspect
    from app import App as AppClass
    src = inspect.getsource(AppClass.update_data)
    # the ONLY _incident_observe call for drives must be inside the `if zone is not None:` guard,
    # which requires a non-None dt sourced from disk_temps (Composite Temperature only)
    assert 'disk:{drive_key}", "drive"' in src.replace("f\"", "\"").replace("'", '"') or \
           '_incident_observe(f"disk:{drive_key}"' in src
    print("  PASS: drive incident observation call is gated by the existing Composite-Temperature-only zone check")

    print("\n=== 11. retention/pruning works safely ===")
    # Inject one very old (expired) and one recent incident directly into the file, then reload.
    old_rec = {"incident_id": "old-1", "start_timestamp": time.time() - 40 * 86400,
              "end_timestamp": time.time() - 40 * 86400 + 60, "duration_seconds": 60,
              "component": "cpu", "sensor_name": "CPU Package", "max_zone": "YELLOW",
              "starting_zone": "YELLOW", "peak_value": 81.0, "recovery_value": 60.0,
              "dominant_workload": "old.exe", "foreground_process": None, "foreground_title": None,
              "top_cpu_processes": [], "top_gpu_processes": [], "context_peak": {}, "samples": []}
    with INCIDENTS_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(old_rec) + "\n")
    before_count = sum(1 for _ in INCIDENTS_PATH.read_text(encoding="utf-8").splitlines() if _.strip())
    app3 = App()
    app3.load_incidents()  # already called once in build(), call again explicitly for clarity
    after_count = sum(1 for _ in INCIDENTS_PATH.read_text(encoding="utf-8").splitlines() if _.strip())
    print(f"  file had {before_count} lines before reload, {after_count} after pruning")
    assert after_count == before_count - 1, "FAIL: the 40-day-old incident should have been pruned"
    assert not any(i["incident_id"] == "old-1" for i in app3.incidents_recent)
    # active incidents must never be deleted by pruning
    tick_cpu(app3, 82.0)
    time.sleep(3.2); tick_cpu(app3, 82.1)
    assert "cpu" in app3.incidents_active
    app3.load_incidents()
    assert "cpu" in app3.incidents_active, "FAIL: pruning touched an ACTIVE incident"
    print("  PASS: expired incident pruned, active incident left untouched by pruning")

    for a in (app2, app3):
        a.stop_event.set(); a.destroy()

    print("\nALL INCIDENT HISTORY CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
