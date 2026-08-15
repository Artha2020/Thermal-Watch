"""Verify the PCIe x1 unverified-label change: correct row text/status/color, other mobo
sensors unchanged, footer updated, no widget recreation across polls, no alert ever fires."""
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import App, lhm_sensors, nvidia_stats, memory, cpu_times, DIM  # noqa: E402


def payload():
    old_idle, old_total = cpu_times()
    time.sleep(0.2)
    now = cpu_times()
    dt_load = now[1] - old_total
    load = 100 * (1 - (now[0] - old_idle) / dt_load) if dt_load else 0
    mem_pct, mem_used, mem_total = memory()
    return {"time": datetime.now(), "cpu_load": load, "mem_pct": mem_pct, "mem_used": mem_used,
            "mem_total": mem_total, "gpus": nvidia_stats(), "lhm": lhm_sensors()}


def rows(panel):
    out = []
    for row in panel.body.winfo_children():
        if row.winfo_class() != "Frame":
            continue  # skip the (unpacked) empty-state placeholder label
        out.append([w.cget("text") for w in row.winfo_children() if w.winfo_class() == "Label"])
    return out


def main():
    app = App()
    real_sensors = lhm_sensors()
    app.update_data(payload())

    print("=== 1. mobo panel rows ===")
    for r in rows(app.mobo_panel):
        print(f"  {r}")

    pcie_row = next((r for r in rows(app.mobo_panel) if r[0] == "PCIE X1*"), None)
    assert pcie_row is not None, "FAIL: PCIe x1 row not found as 'PCIE X1*'"
    print(f"\n  PCIe x1 row: {pcie_row}")
    assert pcie_row[2] == "UNVERIFIED", f"FAIL: expected status UNVERIFIED, got {pcie_row[2]}"
    temp_val = float(pcie_row[1].replace("°C", ""))
    assert 70 <= temp_val <= 100, f"FAIL: PCIe x1 temp {temp_val} outside sane observed range"
    print("  PASS: PCIE X1* shows live temperature + UNVERIFIED status")

    print("\n=== 2. status color is neutral DIM, not a thermal-health color ===")
    pcie_key = next(k for k, v in app.mobo_rows.items() if v["name"].cget("text") == "PCIE X1*")
    status_widget = app.mobo_rows[pcie_key]["status"]
    color = status_widget.cget("fg")
    print(f"  status widget fg = {color}")
    assert color == DIM, f"FAIL: expected neutral DIM color, got {color}"
    print("  PASS: neutral color, not green/yellow/orange/red")

    print("\n=== 3. other motherboard sensors unchanged (no suffix, still '--') ===")
    for r in rows(app.mobo_panel):
        if r[0] == "PCIE X1*":
            continue
        assert not r[0].endswith("*"), f"FAIL: unrelated sensor got a suffix: {r}"
        assert r[2] == "--", f"FAIL: unrelated sensor status changed: {r}"
    print("  PASS: CPU/System/VRM MOS/PCH/CPU Socket/M2 #1 all still show '--', untouched")

    print("\n=== 4. footer explains the asterisk ===")
    footer_text = app.mobo_footer_label.cget("text")
    print(f"  footer: {footer_text!r}")
    assert footer_text == "* Sensor label unverified · raw reading only"
    print("  PASS")

    print("\n=== 5. no alert ever generated from this sensor, even at 80-86C ===")
    alert_keys_before = set(app.active_alerts.keys())
    for _ in range(3):
        app.update_data(payload())
    alert_keys_after = set(app.active_alerts.keys())
    mobo_alert_keys = [k for k in alert_keys_after if "pcie" in k.lower()]
    print(f"  active_alerts keys: {sorted(alert_keys_after)}")
    assert not mobo_alert_keys, f"FAIL: an alert was generated for PCIe x1: {mobo_alert_keys}"
    print("  PASS: no alert/zone/threshold ever applied to this sensor")

    print("\n=== 6. render optimization intact: zero widget churn across steady-state polls ===")
    app.widget_stats["rows_created"] = 0
    app.widget_stats["rows_destroyed"] = 0
    mobo_rows_before = dict(app.mobo_rows)
    for _ in range(4):
        app.update_data(payload())
    print(f"  rows_created={app.widget_stats['rows_created']} rows_destroyed={app.widget_stats['rows_destroyed']}")
    assert app.widget_stats["rows_created"] == 0 and app.widget_stats["rows_destroyed"] == 0
    assert app.mobo_rows == mobo_rows_before, "FAIL: mobo row widgets were recreated, not reused"
    print("  PASS: identical widget objects reused, zero churn (render-optimization guarantee intact)")

    app.stop_event.set()
    app.destroy()
    print("\nALL CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
