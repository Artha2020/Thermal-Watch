"""Release-gate query benchmark: builds a realistic 30-day telemetry store IN THE SANDBOX and times
the 24h / 7d / 30d range queries plus the report and history paths against it.

Reference figures from the Storage v2 work (rough, machine-dependent, NOT pass/fail thresholds):
    24h ~17ms, 7d ~98ms, 30d ~590ms
The point is to catch a meaningful REGRESSION, not to chase a number. Nothing here is optimised
unless a real regression shows up."""
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: sandbox, never production
sys.stdout.reconfigure(encoding="utf-8")

from app import (  # noqa: E402
    DATA_DIR, TELEMETRY_BUCKET_SECONDS, TELEMETRY_DB_PATH, INCIDENTS_PATH, SESSIONS_PATH,
    open_telemetry_db, read_telemetry_file, read_sensor_summaries, read_incidents_file,
    build_report_payload, period_bounds, build_timeline, answer_question, scalar_sensor_ref,
    compute_idle_metric_period_trend,
)
import json  # noqa: E402

REFERENCE_MS = {"24h": 17.0, "7d": 98.0, "30d": 590.0}
REGRESSION_FACTOR = 3.0  # only flag something several times slower than the reference


def build_store(days=30):
    """One bucket per minute for `days` days, with a couple of per-sensor rows on every bucket -
    the shape a real month of monitoring leaves behind."""
    conn = open_telemetry_db()
    now = time.time()
    start = now - days * 86400
    rows, sensor_rows = [], []
    ts = start
    i = 0
    while ts < now:
        scalars = {"cpu_temp": {"avg": 50.0 + (i % 30), "min": 45.0, "max": 85.0, "count": 30},
                  "cpu_power": {"avg": 60.0 + (i % 40), "min": 20.0, "max": 190.0, "count": 30},
                  "cpu_util": {"avg": float(i % 100), "min": 0.0, "max": 100.0, "count": 30},
                  "gpu_hotspot_temp": {"avg": 60.0 + (i % 35), "min": 40.0, "max": 99.0, "count": 30},
                  "gpu_core_temp": {"avg": 55.0 + (i % 25), "min": 38.0, "max": 88.0, "count": 30},
                  "gpu_power": {"avg": 120.0 + (i % 200), "min": 15.0, "max": 350.0, "count": 30},
                  "mem_pct": {"avg": 40.0 + (i % 30), "min": 20.0, "max": 95.0, "count": 30}}
        rows.append((ts, ts + TELEMETRY_BUCKET_SECONDS, 30, json.dumps(scalars)))
        for key, name in (("drive:0", "Storage 0"), ("dimm:0", "DIMM 0")):
            sensor_rows.append((ts, key, key, "parent", name, "Temperature", "drive" if "drive" in key else "ram",
                                0, 40.0 + (i % 15), 35.0, 60.0, 30))
        ts += TELEMETRY_BUCKET_SECONDS
        i += 1
    conn.execute("BEGIN")
    conn.executemany("INSERT OR IGNORE INTO buckets (start_timestamp, end_timestamp, sample_count, "
                    "scalars_json) VALUES (?, ?, ?, ?)", rows)
    conn.executemany("INSERT OR IGNORE INTO sensor_readings (start_timestamp, sensor_key, identifier, "
                    "parent, name, sensor_type, component, unverified, avg, min, max, count) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", sensor_rows)
    conn.execute("COMMIT")
    conn.close()
    return len(rows), len(sensor_rows)


def timed(label, fn, runs=3):
    best = None
    result = None
    for _ in range(runs):
        t0 = time.perf_counter()
        result = fn()
        elapsed = (time.perf_counter() - t0) * 1000.0
        best = elapsed if best is None else min(best, elapsed)
    print(f"  {label:<34} {best:8.1f} ms")
    return best, result


def main():
    assert str(DATA_DIR) == str(_verify_sandbox.SANDBOX_DIR), "benchmarks must never run on production"
    print(f"=== Building a 30-day telemetry store in {DATA_DIR}")
    t0 = time.perf_counter()
    buckets, sensors = build_store(30)
    print(f"  {buckets} buckets + {sensors} sensor rows in {time.perf_counter() - t0:.1f}s "
          f"({TELEMETRY_DB_PATH.stat().st_size / 1048576.0:.1f} MB on disk)")

    now = time.time()
    print("\n=== Range queries (best of 3) ===")
    results = {}
    for label, seconds in (("24h", 86400), ("7d", 7 * 86400), ("30d", 30 * 86400)):
        ms, rows = timed(f"read_telemetry_file({label})", lambda s=seconds: read_telemetry_file(since_ts=now - s))
        results[label] = ms
        print(f"      -> {len(rows)} buckets")

    print("\n=== Reference comparison (rough Storage v2 figures, not pass/fail) ===")
    regressions = []
    for label, reference in REFERENCE_MS.items():
        measured = results[label]
        ratio = measured / reference
        verdict = "OK" if ratio <= REGRESSION_FACTOR else "REGRESSION"
        if verdict == "REGRESSION":
            regressions.append((label, reference, measured))
        print(f"  {label:<5} reference ~{reference:6.1f} ms   measured {measured:7.1f} ms   "
              f"({ratio:4.2f}x)  {verdict}")

    print("\n=== Derived paths against the same populated store ===")
    timed("read_sensor_summaries(30d)", lambda: read_sensor_summaries(now - 30 * 86400, now))
    timed("build_timeline(24h)", lambda: build_timeline(now - 86400, now))
    timed("idle trend (30d)", lambda: compute_idle_metric_period_trend(scalar_sensor_ref("cpu_temp"), 30))
    bounds = period_bounds("DAILY", date.fromtimestamp(now - 86400))
    timed("build_report_payload(daily)", lambda: build_report_payload(bounds, now=now))
    monthly = period_bounds("MONTHLY", date.fromtimestamp(now - 40 * 86400).replace(day=1))
    timed("build_report_payload(monthly)", lambda: build_report_payload(monthly, now=now))
    timed("answer_question(why ... last night)",
          lambda: answer_question("why did my pc run hot last night", now=now))

    print("")
    if regressions:
        print("BENCHMARK REGRESSIONS DETECTED:")
        for label, reference, measured in regressions:
            print(f"  {label}: {measured:.1f} ms vs ~{reference:.1f} ms reference "
                  f"(> {REGRESSION_FACTOR}x)")
        return 1
    print("NO MEANINGFUL QUERY REGRESSION - all range queries within "
          f"{REGRESSION_FACTOR}x of their reference figures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
