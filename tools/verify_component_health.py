"""Task-9 verification for GPU sub-sensors, motherboard/chipset, RAM, and fan health."""
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import App, lhm_sensors, nvidia_stats, memory, cpu_times  # noqa: E402


def payload():
    old_idle, old_total = cpu_times()
    time.sleep(0.3)
    now = cpu_times()
    dt_load = now[1] - old_total
    load = 100 * (1 - (now[0] - old_idle) / dt_load) if dt_load else 0
    mem_pct, mem_used, mem_total = memory()
    return {"time": datetime.now(), "cpu_load": load, "mem_pct": mem_pct, "mem_used": mem_used,
            "mem_total": mem_total, "gpus": nvidia_stats(), "lhm": lhm_sensors()}


def row_frames(panel):
    """The panel's real data rows. A Panel.body ALSO permanently contains the placeholder Label
    built by App._make_empty_label() - created once at build() time and shown/hidden by
    _toggle_visible() rather than created and destroyed, which is the entire point of the row-cache
    render optimization. It is a Label, not a row Frame, so filtering by widget class separates the
    real rows from it precisely. (Counting every child instead is what made this script fail for
    several phases: the placeholder contributed a row with no Labels in it, and r[0] raised
    IndexError.)"""
    return [w for w in panel.body.winfo_children() if w.winfo_class() == "Frame"]


def assert_placeholder_hidden(panel, name):
    """The optimization's real contract: while rows exist the placeholder must still be PRESENT
    (never destroyed) but NOT packed. This is strictly stronger than the child-count it replaces -
    a plain count would pass even if the placeholder were visible on top of a populated panel."""
    labels = [w for w in panel.body.winfo_children() if w.winfo_class() == "Label"]
    assert len(labels) == 1, f"FAIL: {name} should keep exactly one placeholder label, found {len(labels)}"
    assert not labels[0].winfo_manager(), \
        f"FAIL: {name} shows its '{labels[0].cget('text')}' placeholder while real rows are rendered"


def rows(panel):
    out = []
    for row in row_frames(panel):
        out.append([w.cget("text") for w in row.winfo_children() if w.winfo_class() == "Label"])
    return out


def main():
    sensors = lhm_sensors()

    print("=== 1. GPU Core/Hotspot/VRAM raw sensor entries ===")
    for s in sensors:
        if s.get("Name") in ("GPU Hot Spot", "GPU Memory Junction") and "gpu" in s.get("Parent", "").lower():
            print(f"  {s}")
    gpus = nvidia_stats()
    print(f"  GPU Core (nvidia-smi): {gpus[0].get('temp') if gpus else 'N/A'}")

    print("\n=== 2. motherboard/chipset temperature sensors ===")
    for s in sensors:
        if s.get("SensorType") == "Temperature" and "superio" in s.get("Parent", "").lower():
            print(f"  {s['Name']:20} {s['Value']}")

    print("\n=== 3. DIMM temperature sensors ===")
    for s in sensors:
        if "memory" in s.get("Parent", "").lower() and s.get("Name", "").startswith("DIMM"):
            print(f"  {s['Name']:10} {s['Value']}  <- {s['Parent']}")

    print("\n=== 4. every Fan sensor + parent ===")
    for s in sensors:
        if s.get("SensorType") == "Fan":
            print(f"  {s['Name']:16} {s['Value']:>8}  <- {s['Parent']}")

    print("\n=== 5. GPU 0-RPM idle: no false failure (design check) ===")
    print("  GPU fans are NEVER passed through _update_cpu_fan_alert (only 'CPU Fan' is) -> no alert path exists for them. PASS by construction.")

    print("\n=== 6. unused mobo fan headers at 0 RPM: no alert (design check) ===")
    print("  Only fan named exactly 'CPU Fan' feeds _update_cpu_fan_alert; System Fan #1/#3/#4/#5/#6/Pump Fan never do. PASS by construction.")

    print("\n=== full app run: render + widget check ===")
    app = App()
    app.update_data(payload())

    print("GPU THERMAL panel rows:", rows(app.gpu_thermal_panel))
    print("MOTHERBOARD panel rows:", rows(app.mobo_panel))
    print("RAM panel rows:", rows(app.ram_panel))
    fan_rows = rows(app.fan_panel)
    print("FAN panel rows:", fan_rows)
    cpu_fan_row = next((r for r in fan_rows if r[0] == "CPU FAN"), None)
    other_fan_rows = [r for r in fan_rows if r[0] != "CPU FAN"]
    assert cpu_fan_row is not None
    assert cpu_fan_row[-1] in ("OK", "STALLED"), cpu_fan_row
    assert all(r[-1] == "--" for r in other_fan_rows), "FAIL: non-CPU fan got a fabricated status"
    for panel_name in ("fan_panel", "gpu_thermal_panel", "mobo_panel", "ram_panel"):
        assert_placeholder_hidden(getattr(app, panel_name), panel_name)
    print("  PASS: only CPU Fan has a status verdict, all other fans show '--', and every populated "
          "panel keeps its placeholder present-but-hidden")

    print("\n=== 7/8/9. debounce, immediate-red, immediate-recovery, independent state ===")
    from app import GPU_HOTSPOT_ZONES, RAM_ZONES
    # two independent sensors driven through Yellow with real timing, at once, to prove no shared state
    z1 = app._update_sensor_zone("verify:hotspot", "TEST GPU Hotspot", 90.0, "°C", GPU_HOTSPOT_ZONES)  # YELLOW
    z2 = app._update_sensor_zone("verify:dimm", "TEST DIMM", 58.0, "°C", RAM_ZONES)  # YELLOW
    assert app.sensor_zone_state["verify:hotspot"]["confirmed"] == "GREEN", "must not alert before 3s"
    assert app.sensor_zone_state["verify:dimm"]["confirmed"] == "GREEN", "must not alert before 3s"
    time.sleep(3.2)
    app._update_sensor_zone("verify:hotspot", "TEST GPU Hotspot", 90.5, "°C", GPU_HOTSPOT_ZONES)
    # do NOT re-touch verify:dimm - proves its state didn't advance just because hotspot's did
    assert app.sensor_zone_state["verify:hotspot"]["confirmed"] == "YELLOW", "hotspot should confirm after 3s"
    assert app.sensor_zone_state["verify:dimm"]["confirmed"] == "GREEN", "FAIL: dimm state leaked from hotspot's debounce"
    print("  PASS: independent debounce state confirmed (hotspot alerted, untouched dimm did not)")

    app._update_sensor_zone("verify:dimm", "TEST DIMM", 80.0, "°C", RAM_ZONES)  # RED, immediate
    assert app.sensor_zone_state["verify:dimm"]["confirmed"] == "RED"
    assert app.events[0]["kind"] == "CRIT"
    print("  PASS: RED fires immediately, no debounce")

    app._update_sensor_zone("verify:dimm", "TEST DIMM", 30.0, "°C", RAM_ZONES)  # GREEN, immediate
    assert app.sensor_zone_state["verify:dimm"]["confirmed"] == "GREEN"
    assert "sensor:verify:dimm" not in app.active_alerts
    print("  PASS: recovery to GREEN is immediate")

    app.stop_event.set()
    app.destroy()
    print("\nALL CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
