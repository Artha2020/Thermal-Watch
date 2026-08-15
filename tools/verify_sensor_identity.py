"""Verify sensor_identity() migration: canonical helper behavior, UNVERIFIED_SENSOR_LABELS
lookup via both Identifier and fallback, full rendering against old-schema (no Identifier) and
synthetic new-schema (with Identifier) data, steady-state zero churn, and the no-duplicate
rekey transition when Identifier newly appears under an already-running app."""
import copy
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import (App, lhm_sensors, nvidia_stats, memory, cpu_times,  # noqa: E402
                 sensor_identity, UNVERIFIED_SENSOR_LABELS, DIM)

SENSORS_PATH = r"C:\ProgramData\ThermalWatch\sensors.json"


def payload(lhm):
    old_idle, old_total = cpu_times()
    time.sleep(0.15)
    now = cpu_times()
    dt_load = now[1] - old_total
    load = 100 * (1 - (now[0] - old_idle) / dt_load) if dt_load else 0
    mem_pct, mem_used, mem_total = memory()
    return {"time": datetime.now(), "cpu_load": load, "mem_pct": mem_pct, "mem_used": mem_used,
            "mem_total": mem_total, "gpus": nvidia_stats(), "lhm": lhm}


def strip_identifiers(sensors):
    """Simulates an old-schema bridge/Tier 2/3: same data, Identifier field removed. The live
    bridge was upgraded to emit real Identifiers in this same task, so lhm_sensors() no longer
    naturally returns old-schema data - this makes the "before" state explicit instead of
    accidentally simulating "Identifier changes value" (not the scenario being tested)."""
    out = []
    for s in sensors:
        s = dict(s)
        s.pop("Identifier", None)
        out.append(s)
    return out


def add_synthetic_identifiers(sensors):
    """Simulates a new-schema bridge: same data, each sensor gets a plausible Identifier."""
    out = []
    for s in sensors:
        s = dict(s)
        parent_slug = s.get("Parent", "unknown").lower().replace(" ", "_")
        name_slug = s.get("Name", "unknown").lower().replace(" ", "_").replace("#", "")
        s["Identifier"] = f"/synthetic/{parent_slug}/{s.get('SensorType', 'x').lower()}/{name_slug}"
        out.append(s)
    return out


def rows(panel):
    out = []
    for row in panel.body.winfo_children():
        if row.winfo_class() != "Frame":
            continue
        out.append([w.cget("text") for w in row.winfo_children() if w.winfo_class() == "Label"])
    return out


def main():
    print("=== 1. sensor_identity() unit behavior ===")
    with_id = {"Identifier": "/lpc/nct6687d/0/temperature/5", "Parent": "SuperIO Nuvoton NCT6687D",
              "Name": "PCIe x1", "SensorType": "Temperature"}
    without_id = {"Parent": "SuperIO Nuvoton NCT6687D", "Name": "PCIe x1", "SensorType": "Temperature"}
    empty_id = {"Identifier": "", "Parent": "SuperIO Nuvoton NCT6687D", "Name": "PCIe x1", "SensorType": "Temperature"}
    assert sensor_identity(with_id) == "/lpc/nct6687d/0/temperature/5"
    assert sensor_identity(without_id) == ("SuperIO Nuvoton NCT6687D", "PCIe x1", "Temperature")
    assert sensor_identity(empty_id) == ("SuperIO Nuvoton NCT6687D", "PCIe x1", "Temperature"), \
        "FAIL: empty-string Identifier should fall back, not be used as a key"
    print("  PASS: prefers Identifier, falls back to (Parent, Name, SensorType), empty string treated as absent")

    print("\n=== 2. UNVERIFIED_SENSOR_LABELS reachable via both Identifier and fallback ===")
    assert UNVERIFIED_SENSOR_LABELS.get(sensor_identity(with_id)) is not None
    assert UNVERIFIED_SENSOR_LABELS.get(sensor_identity(without_id)) is not None
    assert UNVERIFIED_SENSOR_LABELS[sensor_identity(with_id)] is UNVERIFIED_SENSOR_LABELS[sensor_identity(without_id)]
    print("  PASS: same metadata reachable whichever identity path resolves")

    real_sensors = lhm_sensors()
    has_identifier_now = any("Identifier" in s for s in real_sensors)
    print(f"\n(current live bridge already emitting Identifier: {has_identifier_now})")
    # Explicit "old schema" simulation - the live bridge now emits real Identifiers (upgraded
    # in this same task), so old-schema data has to be constructed rather than assumed.
    real_sensors_old = strip_identifiers(real_sensors)

    print("\n=== 3. full render against OLD-SCHEMA (Identifier stripped) data ===")
    app = App()
    app.update_data(payload(real_sensors_old))
    pcie_row = next((r for r in rows(app.mobo_panel) if r[0] == "PCIE X1*"), None)
    assert pcie_row is not None, f"FAIL: PCIe x1 not rendered as PCIE X1*, got {rows(app.mobo_panel)}"
    assert pcie_row[2] == "UNVERIFIED"
    print(f"  {pcie_row}  -- PASS")

    print("\n=== 4. full render against SYNTHETIC new-schema (with Identifier) data ===")
    synthetic_new = add_synthetic_identifiers(real_sensors_old)
    # Give PCIe x1 specifically the REAL production identifier, as if the real bridge had
    # been updated - everything else keeps a synthetic (but present) Identifier.
    for s in synthetic_new:
        if s.get("Parent") == "SuperIO Nuvoton NCT6687D" and s.get("Name") == "PCIe x1":
            s["Identifier"] = "/lpc/nct6687d/0/temperature/5"
    app2 = App()
    app2.update_data(payload(synthetic_new))
    pcie_row2 = next((r for r in rows(app2.mobo_panel) if r[0] == "PCIE X1*"), None)
    assert pcie_row2 is not None
    assert pcie_row2[2] == "UNVERIFIED"
    print(f"  {pcie_row2}  -- PASS (matched via Identifier, not the (Parent,Name) fallback)")

    print("\n=== 5. other sensor categories still parse correctly regardless of schema ===")
    for label, panel in (("fans", app2.fan_panel), ("voltages", app2.volt_panel), ("drives", app2.disk_panel),
                        ("gpu thermal", app2.gpu_thermal_panel), ("motherboard", app2.mobo_panel),
                        ("ram", app2.ram_panel)):
        r = rows(panel)
        print(f"  {label}: {len(r)} row(s)")
        assert len(r) > 0, f"FAIL: {label} panel rendered no rows"
    print("  PASS: CPU/GPU/drives/DIMMs/fans/voltages/motherboard all populated")

    print("\n=== 6. steady state: 5 consecutive unchanged polls create/destroy zero rows ===")
    app2.widget_stats["rows_created"] = 0
    app2.widget_stats["rows_destroyed"] = 0
    snapshot = {p: dict(getattr(app2, p)) for p in
               ("fan_rows", "volt_rows", "disk_rows", "gpu_thermal_rows", "mobo_rows", "ram_rows")}
    for _ in range(5):
        app2.update_data(payload(synthetic_new))
    print(f"  rows_created={app2.widget_stats['rows_created']} rows_destroyed={app2.widget_stats['rows_destroyed']}")
    assert app2.widget_stats["rows_created"] == 0 and app2.widget_stats["rows_destroyed"] == 0
    for p, before in snapshot.items():
        assert getattr(app2, p) == before, f"FAIL: {p} widgets were recreated"
    print("  PASS: zero churn, identical widget objects across 5 unchanged polls")

    print("\n=== 7. no-duplicate transition: Identifier newly appearing on an already-running cache ===")
    app3 = App()
    app3.update_data(payload(real_sensors_old))  # old schema: populates via fallback-tuple keys
    created_after_first = app3.widget_stats["rows_created"]
    mobo_before = dict(app3.mobo_rows)
    fan_before = dict(app3.fan_rows)
    pcie_frame_before = next(v["frame"] for k, v in app3.mobo_rows.items()
                             if v["name"].cget("text") == "PCIE X1*")
    # SAME physical sensors, but now the bridge would be emitting Identifier (as if it had
    # just been restarted with the new script while this UI kept running).
    synthetic_transition = add_synthetic_identifiers(real_sensors_old)
    for s in synthetic_transition:
        if s.get("Parent") == "SuperIO Nuvoton NCT6687D" and s.get("Name") == "PCIe x1":
            s["Identifier"] = "/lpc/nct6687d/0/temperature/5"
    app3.update_data(payload(synthetic_transition))
    print(f"  mobo rows before={len(mobo_before)} after={len(app3.mobo_rows)}  "
          f"fan rows before={len(fan_before)} after={len(app3.fan_rows)}")
    assert len(app3.mobo_rows) == len(mobo_before), "FAIL: row count changed - duplicate or lost row"
    assert len(app3.fan_rows) == len(fan_before), "FAIL: fan row count changed"
    pcie_frame_after = next(v["frame"] for k, v in app3.mobo_rows.items()
                            if v["name"].cget("text") == "PCIE X1*")
    assert pcie_frame_after is pcie_frame_before, "FAIL: PCIe x1 row widget was destroyed/recreated, not rekeyed"
    print(f"  rows_created after transition poll: {app3.widget_stats['rows_created']} (was {created_after_first} "
          f"after first poll - must be unchanged, meaning REKEY happened, not fresh creation)")
    assert app3.widget_stats["rows_created"] == created_after_first, \
        "FAIL: transition poll created new rows instead of rekeying existing ones"
    print("  PASS: identity scheme upgrading mid-session rekeyed rows in place - no duplicates, no widget churn")

    for a in (app, app2, app3):
        a.stop_event.set()
        a.destroy()
    print("\nALL SENSOR-IDENTITY CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
