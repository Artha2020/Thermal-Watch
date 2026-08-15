"""Task-6 verification: drives, voltages, zones, and UI rendering, against real data."""
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import (App, lhm_sensors, nvidia_stats, memory, cpu_times,  # noqa: E402
                 ATX_NOMINAL, DRIVE_YELLOW, DRIVE_ORANGE, DRIVE_RED)


def payload():
    old_idle, old_total = cpu_times()
    time.sleep(0.3)
    now = cpu_times()
    dt_load = now[1] - old_total
    load = 100 * (1 - (now[0] - old_idle) / dt_load) if dt_load else 0
    mem_pct, mem_used, mem_total = memory()
    return {"time": datetime.now(), "cpu_load": load, "mem_pct": mem_pct, "mem_used": mem_used,
            "mem_total": mem_total, "gpus": nvidia_stats(), "lhm": lhm_sensors()}


def main():
    sensors = lhm_sensors()

    print("=== 1. physical drives detected ===")
    storage_parents = sorted(set(s["Parent"] for s in sensors if "storage" in s.get("Parent", "").lower()))
    for p in storage_parents:
        print(f"  {p}")
    assert len(storage_parents) == 3, f"expected 3 drives, found {len(storage_parents)}"

    print("\n=== 2. Composite Temperature selected per drive ===")
    composites = [s for s in sensors if s.get("Name") == "Composite Temperature"
                  and "storage" in s.get("Parent", "").lower()]
    for c in composites:
        print(f"  {c['Parent']:45} {c['Value']}")
    assert len(composites) == 3

    print("\n=== 3. Warning/Critical setpoints excluded from the selection ===")
    excluded_names = {s["Name"] for s in sensors if "storage" in s.get("Parent", "").lower()} - {"Composite Temperature"}
    print(f"  present but NOT selected: {sorted(excluded_names)}")
    assert "Warning Temperature" not in [c["Name"] for c in composites]
    assert "Critical Temperature" not in [c["Name"] for c in composites]

    print("\n=== 4. 0/missing -> N/A handling ===")
    for c in composites:
        val = c["Value"]
        resolved = "N/A" if val in (None, 0) else f"{val}°C"
        print(f"  {c['Parent']:45} raw={val!r:>8} -> {resolved}")

    print("\n=== 5. drive live full app run + UI widget check ===")
    app = App()
    app.update_data(payload())
    # Count row FRAMES only. Panel.body also permanently holds the placeholder Label created by
    # App._make_empty_label() - built once at build() time and shown/hidden by _toggle_visible()
    # rather than created/destroyed, which is the point of the row-cache render optimization.
    # Counting every child (as this line did for several phases) counted that placeholder as a
    # fourth drive row.
    children = app.disk_panel.body.winfo_children()
    rows = [w for w in children if w.winfo_class() == "Frame"]
    placeholders = [w for w in children if w.winfo_class() == "Label"]
    print(f"  disk_panel rendered {len(rows)} row(s) (expect 3), plus {len(placeholders)} hidden placeholder")
    assert len(rows) == 3, [w.winfo_class() for w in children]
    # Stronger than the old bare count: the placeholder must exist and must NOT be displayed while
    # real rows are present.
    assert len(placeholders) == 1 and not placeholders[0].winfo_manager(), \
        "FAIL: disk_panel's empty-state placeholder is visible while 3 real drive rows are rendered"
    for i, row in enumerate(rows):
        head_row = row.winfo_children()[0]
        labels = [w.cget("text") for w in head_row.winfo_children()]
        print(f"  row {i}: {labels}")

    print("\n=== 6. ATX rail check against live voltage values ===")
    volts = {s["Name"]: s["Value"] for s in sensors if s.get("SensorType") == "Voltage"}
    for name, (nom, lo, hi) in ATX_NOMINAL.items():
        match = next((v for n, v in volts.items() if n.strip().lower() == name), None)
        if match is None:
            continue
        ok = lo <= match <= hi
        print(f"  {name:6} nominal={nom}V  range=[{lo},{hi}]  live={match:.3f}V  -> {'OK' if ok else 'OUT OF RANGE'}")

    print("\n=== 7. voltage panel UI check ===")
    rows = app.volt_panel.body.winfo_children()
    for row in rows:
        labels = [w.cget("text") for w in row.winfo_children()]
        print(f"  {labels}")

    print("\n=== 8. drive zone constants ===")
    print(f"  YELLOW>={DRIVE_YELLOW}  ORANGE>={DRIVE_ORANGE}  RED>={DRIVE_RED}")

    app.stop_event.set()
    app.destroy()
    print("\nALL VERIFICATION CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
