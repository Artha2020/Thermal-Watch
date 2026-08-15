"""Samples sensors.json every 5s for several minutes, logging PCIe x1 alongside CPU/PCH/VRM
MOS/System/GPU Core/GPU Hot Spot, and applies a CPU stress load partway through to see whether
PCIe x1 reacts to anything at all. Read-only against the live bridge - no code changes."""
import json
import subprocess
import sys
import time
from pathlib import Path

SENSORS_PATH = r"C:\ProgramData\ThermalWatch\sensors.json"
STRESS_SCRIPT = str(Path(__file__).with_name("stress_cpu.py"))


def read():
    d = json.load(open(SENSORS_PATH, encoding="utf-8-sig"))
    by_name = {}
    for s in d["sensors"]:
        if s.get("SensorType") != "Temperature":
            continue
        name = s.get("Name")
        parent = s.get("Parent", "")
        if "superio" in parent.lower() and name in ("CPU", "System", "VRM MOS", "PCH", "PCIe x1"):
            by_name[name] = s["Value"]
        if "nvidia" in parent.lower() and name == "GPU Core":
            by_name["GPU Core"] = s["Value"]
        if "nvidia" in parent.lower() and name == "GPU Hot Spot":
            by_name["GPU Hot Spot"] = s["Value"]
    return by_name, time.time() - d["timestamp"]


def main():
    total_seconds = 180
    interval = 5
    stress_at = 60  # start CPU stress at t=60s
    stress_duration = 60

    print(f"{'t':>5} {'CPU':>6} {'System':>7} {'VRM MOS':>8} {'PCH':>6} {'PCIe x1':>8} "
          f"{'GPU Core':>9} {'GPU Hotspot':>12} {'age':>5}")

    stress_proc = None
    start = time.time()
    values = {"CPU": [], "PCIe x1": []}
    pcie_min, pcie_max = None, None

    while time.time() - start < total_seconds:
        t = time.time() - start
        if stress_proc is None and t >= stress_at:
            print(f"  --- starting {stress_duration}s CPU stress (all cores) ---")
            stress_proc = subprocess.Popen([sys.executable, STRESS_SCRIPT, "--seconds", str(stress_duration)])
        try:
            vals, age = read()
        except Exception as e:
            print(f"{t:5.0f} read error: {e}")
            time.sleep(interval)
            continue
        pcie = vals.get("PCIe x1")
        if pcie is not None:
            pcie_min = pcie if pcie_min is None else min(pcie_min, pcie)
            pcie_max = pcie if pcie_max is None else max(pcie_max, pcie)
            values["PCIe x1"].append(pcie)
        if "CPU" in vals:
            values["CPU"].append(vals["CPU"])
        print(f"{t:5.0f} {vals.get('CPU', '-'):>6} {vals.get('System', '-'):>7} "
              f"{vals.get('VRM MOS', '-'):>8} {vals.get('PCH', '-'):>6} {pcie if pcie is not None else '-':>8} "
              f"{vals.get('GPU Core', '-'):>9} {vals.get('GPU Hot Spot', '-'):>12} {age:5.1f}")
        time.sleep(interval)

    if stress_proc:
        stress_proc.wait()

    print(f"\nPCIe x1 range observed: min={pcie_min} max={pcie_max} delta={pcie_max - pcie_min if pcie_min is not None else 'N/A'}")
    if values["CPU"]:
        print(f"CPU range observed: min={min(values['CPU'])} max={max(values['CPU'])}")


if __name__ == "__main__":
    main()
