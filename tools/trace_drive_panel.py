"""Trace sensors.json -> lhm_sensors() -> update_data() -> disk_panel widgets.

Instantiates the real App, feeds it one real poll payload (using the actual
lhm_sensors()/nvidia_stats()/cpu_times() functions app.py uses), and prints
what update_data actually saw and rendered - to find exactly where drive
readings are lost, rather than guessing.
"""
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import App, lhm_sensors, nvidia_stats, memory, cpu_times  # noqa: E402


def main():
    print("--- step 1: raw lhm_sensors() the app itself would call ---")
    sensors = lhm_sensors()
    print(f"sensor count: {len(sensors)}")
    storage = [s for s in sensors if "storage" in s.get("Parent", "").lower()]
    print(f"storage-parent sensors: {len(storage)}")
    for s in storage:
        print(f"  {s}")

    print("\n--- step 2: build a real update_data() payload, exactly like worker() does ---")
    old_idle, old_total = cpu_times()
    time.sleep(0.3)
    now = cpu_times()
    dt_load = now[1] - old_total
    load = 100 * (1 - (now[0] - old_idle) / dt_load) if dt_load else 0
    mem_pct, mem_used, mem_total = memory()
    gpus = nvidia_stats()
    payload = {"time": datetime.now(), "cpu_load": load, "mem_pct": mem_pct, "mem_used": mem_used,
               "mem_total": mem_total, "gpus": gpus, "lhm": sensors}

    print("\n--- step 3: run it through the real App.update_data() ---")
    app = App()
    app.update_data(payload)

    print("\n--- step 4: inspect application state after update_data ---")
    print(f"self._lhm sensor count: {len(getattr(app, '_lhm', []))}")

    print("\n--- step 5: inspect the ACTUAL rendered disk_panel widget tree ---")
    children = app.disk_panel.body.winfo_children()
    print(f"disk_panel.body has {len(children)} direct child widgets")
    for w in children:
        print(f"  widget: {w} class={w.winfo_class()}")
        for sub in w.winfo_children():
            print(f"    -> {sub} class={sub.winfo_class()} text={sub.cget('text') if 'text' in sub.keys() else ''!r}")

    app.stop_event.set()
    app.destroy()


if __name__ == "__main__":
    main()
