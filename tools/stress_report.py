"""Headless CPU stress test with a real before/during/after sensor report.

Reuses Thermal Watch's own sensor-reading functions (no GUI needed) so the
numbers reported here match what the app itself would show.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import cpu_times, memory, nvidia_stats, lhm_sensors, THRESH_CPU, TJMAX  # noqa: E402


def burn(stop_at: float) -> None:
    x = 0.0001
    while time.time() < stop_at:
        for _ in range(200_000):
            x = x * 1.0000001 + 0.0000001


def cpu_package_temp(sensors):
    temps = [s for s in sensors if s.get("SensorType") == "Temperature"
             and "cpu" in (s.get("Name", "") + s.get("Parent", "")).lower()]
    pick = next((s for s in temps if any(k in s.get("Name", "").lower() for k in ("package", "tctl", "tdie"))),
                temps[0] if temps else None)
    return float(pick["Value"]) if pick and pick.get("Value") not in (None, 0) else None


def cpu_clock(sensors):
    c = [s for s in sensors if s.get("SensorType") == "Clock" and s.get("Name") == "Cores (Average)"]
    return float(c[0]["Value"]) if c else None


def cpu_power(sensors):
    p = [s for s in sensors if s.get("SensorType") == "Power" and s.get("Name") == "Package"
         and "cpu" in s.get("Parent", "").lower()]
    return float(p[0]["Value"]) if p else None


def sample_load(prev):
    old_idle, old_total = prev
    now = cpu_times()
    dt = now[1] - old_total
    load = 100 * (1 - (now[0] - old_idle) / dt) if dt else 0.0
    return load, now


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 60
    workers = os.cpu_count()

    print("--- baseline (idle) ---")
    prev = cpu_times()
    time.sleep(1)
    idle_load, prev = sample_load(prev)
    idle_sensors = lhm_sensors()
    idle_temp = cpu_package_temp(idle_sensors)
    print(f"idle CPU load: {idle_load:.0f}%   idle CPU temp: "
          f"{idle_temp:.1f}°C" if idle_temp is not None else f"idle CPU load: {idle_load:.0f}%   idle CPU temp: N/A")

    print(f"\n--- stressing {workers} logical core(s) for {seconds}s ---")
    stop_at = time.time() + seconds
    procs = [mp.Process(target=burn, args=(stop_at,), daemon=True) for _ in range(workers)]
    for p in procs:
        p.start()

    temps, loads, clocks, powers = [], [], [], []
    while time.time() < stop_at:
        time.sleep(1)
        load, prev = sample_load(prev)
        sensors = lhm_sensors()
        t = cpu_package_temp(sensors)
        clk = cpu_clock(sensors)
        pw = cpu_power(sensors)
        if t is not None:
            temps.append(t)
        loads.append(load)
        if clk is not None:
            clocks.append(clk)
        if pw is not None:
            powers.append(pw)
        remaining = int(stop_at - time.time())
        print(f"  t-{remaining:>3d}s  load={load:5.1f}%  temp={t if t is not None else 'N/A'}  "
              f"clock={clk if clk is not None else 'N/A'}MHz  power={pw if pw is not None else 'N/A'}W")

    for p in procs:
        p.join(timeout=2)
        if p.is_alive():
            p.terminate()

    print("\n--- summary ---")
    if temps:
        print(f"CPU temp: peak {max(temps):.1f}°C, avg {sum(temps)/len(temps):.1f}°C, "
              f"min {min(temps):.1f}°C  (threshold {THRESH_CPU:.0f}°C, Tjmax {TJMAX:.0f}°C)")
        over = max(temps) >= THRESH_CPU
        print(f"Held under threshold: {'NO - exceeded ' + str(THRESH_CPU) + ' deg C' if over else 'YES'}")
    else:
        print("CPU temp: no sensor data (bridge not elevated / not running)")
    if loads:
        print(f"CPU load: peak {max(loads):.1f}%, avg {sum(loads)/len(loads):.1f}%")
    if clocks:
        print(f"CPU clock (avg-of-cores): peak {max(clocks):.0f}MHz, avg {sum(clocks)/len(clocks):.0f}MHz")
    if powers:
        print(f"CPU package power: peak {max(powers):.1f}W, avg {sum(powers)/len(powers):.1f}W")

    print("\n--- cooldown (5s after load stops) ---")
    time.sleep(5)
    cool_sensors = lhm_sensors()
    cool_temp = cpu_package_temp(cool_sensors)
    print(f"CPU temp 5s after load stopped: {cool_temp:.1f}°C" if cool_temp is not None else "N/A")


if __name__ == "__main__":
    main()
