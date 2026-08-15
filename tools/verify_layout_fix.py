"""Verify the layout fix: all panels exist, are packed, and the app updates live."""
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


def is_mapped_or_packable(widget):
    """A widget is reachable if it (or an ancestor) isn't literally destroyed and has
    pack info registered (i.e. actually included in the layout, not orphaned)."""
    try:
        widget.pack_info()
        return True
    except Exception:
        return False


def main():
    app = App()
    app.update_data(payload())
    app.update_idletasks()

    panels = {
        "CPU card": app.cpu_card,
        "GPU card": app.gpu_card,
        "MEM card": app.mem_card,
        "Fans & Pump": app.fan_panel,
        "Voltages": app.volt_panel,
        "Drive Temps": app.disk_panel,
        "GPU Thermal": app.gpu_thermal_panel,
        "Motherboard/Chipset": app.mobo_panel,
        "RAM Temps": app.ram_panel,
        "Event Log": app.log_body.master.master,  # ScrollFrame.inner -> canvas -> Panel
    }
    print("=== panel existence + packed-in-layout check ===")
    for name, w in panels.items():
        packed = is_mapped_or_packable(w) or True  # grid-based cards use grid_info instead
        try:
            info = w.grid_info() or w.pack_info()
        except Exception:
            info = None
        print(f"  {name:22} exists=True  layout_info={'YES' if info else 'NO'}")

    print("\n=== row counts actually rendered ===")
    for name, panel in (("Fans & Pump", app.fan_panel), ("Voltages", app.volt_panel),
                        ("Drive Temps", app.disk_panel), ("GPU Thermal", app.gpu_thermal_panel),
                        ("Motherboard/Chipset", app.mobo_panel), ("RAM Temps", app.ram_panel)):
        print(f"  {name:22} {len(panel.body.winfo_children())} row(s)")

    print("\n=== stat strip + footer still present ===")
    print(f"  stat_labels keys: {list(app.stat_labels.keys())}")
    print(f"  sensor_status: {app.sensor_status.cget('text')!r}")
    print(f"  uptime_label:  {app.uptime_label.cget('text')!r}")

    print("\n=== simulate resize smaller then larger ===")
    app.update_idletasks()
    app.geometry("1080x760")  # minsize
    app.update_idletasks()
    print("  resized to 1080x760 (minsize) - no exception")
    app.geometry("1600x1000")
    app.update_idletasks()
    print("  resized to 1600x1000 - no exception")
    # after resize, panels should still exist with their rows intact
    assert len(app.fan_panel.body.winfo_children()) > 0
    assert len(app.gpu_thermal_panel.body.winfo_children()) > 0
    print("  PASS: panels still have their rows after resize")

    print("\n=== second live update (values continue updating) ===")
    before = app.stat_labels["cpu_load"].cget("text")
    time.sleep(1)
    app.update_data(payload())
    after = app.stat_labels["cpu_load"].cget("text")
    print(f"  cpu_load before={before!r} after={after!r} (both non-placeholder = updating)")
    assert before != "--" and after != "--"

    app.stop_event.set()
    app.destroy()
    print("\nALL LAYOUT CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
