"""Measures the telemetry-history engine's own cost (Storage v2: SQLite), isolated from real
hardware sampling (item 20): per-2s-tick accumulation cost, bucket finalization/persist cost
(one transaction, buckets + sensor_readings), and history query cost (read_telemetry_file +
downsample_series) for 24h/7d/30d ranges against a REALISTIC synthetic 30-day store (12
per-sensor entries/bucket: 2 drives + 4 DIMMs + 6 motherboard temps, a typical real system - not
a worst-case pathological one), plus SensorHistoryWindow._recompute() end-to-end cost (query +
downsample + chart render) for the same ranges. Reports before/after against the Storage v1
(JSONL) numbers measured in the prior task for the same synthetic dataset shape."""
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import (App, SensorHistoryWindow, TELEMETRY_DB_PATH, TELEMETRY_BUCKET_SECONDS,  # noqa: E402
                 TELEMETRY_SCALAR_KEYS, TELEMETRY_DOWNSAMPLE_GROUPING, TELEMETRY_RANGE_SECONDS,
                 _new_telemetry_bucket, open_telemetry_db, read_telemetry_file,
                 normalize_bucket_series, downsample_series, scalar_sensor_ref)

N_TICKS = 2000
DAYS = 30
BUCKETS_PER_DAY = 86400 // TELEMETRY_BUCKET_SECONDS
SENSOR_IDS = [f"/synthetic/sensor/{i}" for i in range(12)]

# Storage v1 (JSONL) numbers from the prior task's measurement, same synthetic dataset shape,
# same machine - kept here only for the printed before/after comparison, not used in any check.
V1_JSONL_MS = {"24h": 43.2, "7d": 284.9, "30d": 1429.6}


def build_synthetic_store():
    conn = open_telemetry_db()
    now = time.time()
    total = DAYS * BUCKETS_PER_DAY
    scalars_json = json.dumps({k: {"avg": 60.0, "min": 55.0, "max": 65.0, "count": 30} for k in TELEMETRY_SCALAR_KEYS})
    conn.execute("BEGIN")
    for i in range(total):
        start = now - (total - i) * TELEMETRY_BUCKET_SECONDS
        conn.execute("INSERT INTO buckets (start_timestamp, end_timestamp, sample_count, scalars_json) "
                     "VALUES (?, ?, ?, ?)", (start, start + TELEMETRY_BUCKET_SECONDS, 30, scalars_json))
        for j, sid in enumerate(SENSOR_IDS):
            conn.execute(
                "INSERT INTO sensor_readings (start_timestamp, sensor_key, identifier, parent, name, "
                "sensor_type, component, unverified, avg, min, max, count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (start, sid, sid, "Synthetic", f"Sensor {j}", "Temperature", "drive", 0, 40.0, 35.0, 45.0, 30))
    conn.commit()
    conn.close()
    size_mb = TELEMETRY_DB_PATH.stat().st_size / 1e6
    print(f"  built {total} synthetic buckets ({DAYS} days x {BUCKETS_PER_DAY}/day, 12 sensors each), "
          f"store size = {size_mb:.1f} MB")


def main():
    print(f"=== Building a realistic {DAYS}-day synthetic SQLite store (12 sensors/bucket) ===")
    build_synthetic_store()

    print(f"\n=== Timing _telemetry_observe_tick() alone, {N_TICKS} calls, 12 active sensors/tick ===")
    app = App()
    app.telemetry_bucket = _new_telemetry_bucket(time.time())
    app.last_context = {"cpu_temp": 65.0, "cpu_util": 30.0, "cpu_power": 90.0, "gpu_core_temp": 68.0,
                        "gpu_hotspot_temp": 80.0, "gpu_vram_temp": 75.0, "gpu_util": 88.0, "gpu_power": 220.0,
                        "gpu_vram_used_mb": 8000.0, "mem_pct": 55.0}
    samples = [(sid, f"Sensor {j}", "Synthetic", "Temperature", "drive", 40.0 + j)
              for j, sid in enumerate(SENSOR_IDS)]
    durations = []
    for _ in range(N_TICKS):
        t0 = time.perf_counter()
        app._telemetry_observe_tick(list(samples))
        durations.append(time.perf_counter() - t0)
        if app.telemetry_bucket["sample_count"] >= 30:
            app.telemetry_bucket = _new_telemetry_bucket(time.time())  # keep buckets small during timing
    avg_us = statistics.mean(durations) * 1e6
    p95_us = sorted(durations)[int(N_TICKS * 0.95)] * 1e6
    print(f"  avg={avg_us:.1f}us  p95={p95_us:.1f}us over {N_TICKS} ticks  "
          f"({(avg_us / 1e6) / 2.0 * 100:.4f}% of the 2s poll budget)")

    print(f"\n=== Bucket finalization + persist cost (1 SQLite transaction: buckets + 12 sensor_readings rows) ===")
    fin_times = []
    for _ in range(50):
        app.telemetry_bucket = _new_telemetry_bucket(time.time())
        app._telemetry_observe_tick(list(samples))
        t0 = time.perf_counter()
        app._telemetry_finalize_bucket(time.time())
        fin_times.append(time.perf_counter() - t0)
    print(f"  avg finalize+persist: {statistics.mean(fin_times) * 1000:.2f}ms over 50 calls "
          f"(happens once per {TELEMETRY_BUCKET_SECONDS}s, not per poll)")
    app.stop_event.set(); app.destroy()

    print(f"\n=== History query cost: read_telemetry_file(since_ts) + downsample_series() - Storage v2 (SQLite) ===")
    v2_ms = {}
    for label in ("24h", "7d", "30d"):
        window = TELEMETRY_RANGE_SECONDS[label]
        since_ts = time.time() - window
        t0 = time.perf_counter()
        buckets = read_telemetry_file(since_ts=since_ts)
        t1 = time.perf_counter()
        points = normalize_bucket_series(buckets, scalar_sensor_ref("cpu_temp"))
        displayed = downsample_series(points, TELEMETRY_DOWNSAMPLE_GROUPING[label], since_ts)
        t2 = time.perf_counter()
        total_ms = (t2 - t0) * 1000
        v2_ms[label] = total_ms
        print(f"  {label:>4s}: read={(t1 - t0) * 1000:6.1f}ms ({len(buckets):5d} buckets)  "
              f"downsample={(t2 - t1) * 1000:5.1f}ms -> {len(displayed)} display points  "
              f"total={total_ms:6.1f}ms")

    print(f"\n=== SensorHistoryWindow._recompute() end-to-end (query + downsample + chart render) ===")
    app2 = App()
    win = SensorHistoryWindow(app2, scalar_sensor_ref("cpu_temp"))
    for label in ("24h", "7d", "30d"):
        win.range_var.set(label)
        t0 = time.perf_counter()
        win._recompute()
        win.update_idletasks()
        t1 = time.perf_counter()
        print(f"  {label:>4s}: {(t1 - t0) * 1000:.1f}ms (includes Tk canvas redraw)")
    win.destroy()
    app2.stop_event.set(); app2.destroy()

    print(f"\n=== Storage v1 (JSONL) -> Storage v2 (SQLite) query-cost comparison, same dataset shape ===")
    for label in ("24h", "7d", "30d"):
        v1 = V1_JSONL_MS[label]
        v2 = v2_ms[label]
        change = "faster" if v2 < v1 else "slower"
        factor = (v1 / v2) if v2 > 0 else float("inf")
        print(f"  {label:>4s}: v1={v1:7.1f}ms  ->  v2={v2:7.1f}ms   ({factor:.1f}x {change})")

    print("\nNo additional hardware poll, process enumeration, or PDH query was added anywhere "
          "in this measurement - every number above is pure computation over already-collected "
          "or synthetic data (item 20).")

    for suffix in ("", "-wal", "-shm"):
        p = Path(str(TELEMETRY_DB_PATH) + suffix) if suffix else TELEMETRY_DB_PATH
        p.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
