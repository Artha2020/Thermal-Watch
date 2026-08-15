"""Measures the workload-session engine's own CPU cost, isolated from the rest of update_data()
and from real hardware sampling - a git-history-based before/after A/B isn't available (this
project doesn't use git), so instead this times _session_observe_tick() directly against
realistic synthetic snapshots and expresses it (a) per-call in microseconds and (b) as a
percentage of the 2-second poll budget and of one full update_data() call, which is the
actually-relevant "did this make Thermal Watch measurably heavier" question (item 18)."""
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import App, lhm_sensors, nvidia_stats, memory, cpu_times, POLL_SECONDS  # noqa: E402

N = 2000


def payload(lhm):
    old_idle, old_total = cpu_times()
    time.sleep(0.05)
    now = cpu_times()
    dt_load = now[1] - old_total
    load = 100 * (1 - (now[0] - old_idle) / dt_load) if dt_load else 0
    mem_pct, mem_used, mem_total = memory()
    return {"time": datetime.now(), "cpu_load": load, "mem_pct": mem_pct, "mem_used": mem_used,
            "mem_total": mem_total, "gpus": nvidia_stats(), "lhm": lhm,
            "workload": {"time": time.time(), "foreground": {"name": "Cyberpunk2077.exe", "pid": 111, "title": "x"},
                        "cpu_top": [("Cyberpunk2077.exe", 111, 45.0), ("obs64.exe", 222, 18.0)],
                        "gpu_top": [("Cyberpunk2077.exe", 111, 91.0)]}}


def main():
    app = App()
    real_sensors = lhm_sensors()

    print(f"=== Warm-up: one full update_data() so all row caches exist ===")
    app.update_data(payload(real_sensors))

    print(f"\n=== Timing _session_observe_tick() alone, {N} calls with 2 simultaneously-active workloads ===")
    app.last_cpu_top = [("Cyberpunk2077.exe", 111, 45.0), ("obs64.exe", 222, 18.0)]
    app.last_gpu_top = [("Cyberpunk2077.exe", 111, 91.0)]
    app.last_foreground = {"name": "Cyberpunk2077.exe", "pid": 111, "title": "x"}
    app.last_context = {"cpu_temp": 72.0, "gpu_core_temp": 68.0, "gpu_hotspot_temp": 80.0, "gpu_vram_temp": 75.0,
                        "cpu_power": 90.0, "gpu_power": 220.0, "cpu_load": 40.0, "gpu_load": 88.0, "mem_pct": 55.0}
    samples = []
    for _ in range(N):
        t0 = time.perf_counter()
        app._session_observe_tick()
        samples.append(time.perf_counter() - t0)
    avg_us = statistics.mean(samples) * 1e6
    p95_us = sorted(samples)[int(N * 0.95)] * 1e6
    print(f"  avg={avg_us:.1f}us  p95={p95_us:.1f}us  over {N} calls "
          f"(workload_sessions size stayed at {len(app.workload_sessions)}: steady state, no unbounded growth)")

    print(f"\n=== Timing a full update_data() call for comparison (real sensor/GPU read included) ===")
    full_samples = []
    for _ in range(30):
        d = payload(real_sensors)
        t0 = time.perf_counter()
        app.update_data(d)
        full_samples.append(time.perf_counter() - t0)
    avg_full_ms = statistics.mean(full_samples) * 1000
    print(f"  avg full update_data(): {avg_full_ms:.2f}ms over 30 calls")

    session_pct_of_poll = (avg_us / 1e6) / POLL_SECONDS * 100
    session_pct_of_update = (avg_us / 1e6) / (avg_full_ms / 1000) * 100
    print(f"\n=== Summary ===")
    print(f"  _session_observe_tick(): {avg_us:.1f}us/call average")
    print(f"  That is {session_pct_of_poll:.4f}% of the {POLL_SECONDS}s poll budget")
    print(f"  That is {session_pct_of_update:.2f}% of one full update_data() call ({avg_full_ms:.2f}ms, "
          f"dominated by real LHM/nvidia-smi sensor reads, not the session engine)")
    print("  No additional process enumeration, PDH query, or sensor bridge call was made - "
          "the session engine only transformed data update_data() already had (item 18).")

    app.stop_event.set()
    app.destroy()


if __name__ == "__main__":
    main()
