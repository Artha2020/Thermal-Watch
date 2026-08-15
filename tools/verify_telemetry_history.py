"""Verification for Long-Term Telemetry History + Sensor Drill-Down (Storage v2: SQLite): bucket
aggregation correctness, missing-value semantics, multi-sensor independence, stable identity,
shutdown/restart bucket policy, JSONL-to-SQLite migration (idempotent, malformed-line-tolerant),
indexed range-query correctness at scale, DB corruption resilience, transactional
re-persist safety, retention pruning, monitoring-gap rendering, range filtering, downsampling
spike preservation, coverage math, incident/session overlays, drill-down correctness, old-sensor
readability, JSON export, and that no history work runs on the live poll or touches the main
dashboard's row-cache discipline."""
import inspect
import json
import sqlite3
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import (App, SensorHistoryWindow, TELEMETRY_DB_PATH, TELEMETRY_JSONL_PATH,  # noqa: E402
                 TELEMETRY_BUCKET_SECONDS, TELEMETRY_RETENTION_DAYS, TELEMETRY_GAP_BUCKETS,
                 TELEMETRY_SCALAR_KEYS, _new_telemetry_bucket, _bucket_agg_new, _bucket_agg_add,
                 _bucket_agg_result, _sensor_bucket_key, read_telemetry_file, open_telemetry_db,
                 migrate_telemetry_jsonl_to_sqlite, build_telemetry_json_export,
                 prune_telemetry_buckets, filter_buckets_by_range, compute_coverage,
                 normalize_bucket_series, downsample_series, overlapping_incidents,
                 overlapping_sessions, scalar_sensor_ref, sensor_identity)


def fresh_files():
    for p in (TELEMETRY_DB_PATH, TELEMETRY_JSONL_PATH,
             TELEMETRY_DB_PATH.with_suffix(".db-wal"), TELEMETRY_DB_PATH.with_suffix(".db-shm"),
             TELEMETRY_JSONL_PATH.with_suffix(".jsonl.migrated")):
        if p.exists():
            p.unlink()
    for p in TELEMETRY_DB_PATH.parent.glob(f"{TELEMETRY_DB_PATH.stem}.corrupt-*"):
        p.unlink()


def tick(app, cpu_temp=None, sensor_samples=()):
    app.last_context = {"cpu_temp": cpu_temp}
    app._telemetry_observe_tick(list(sensor_samples))


def isolate(app):
    """Neutralizes the background worker/poll machinery on a freshly-constructed App() used for
    direct telemetry-engine manipulation in these tests. Without mainloop() running, .after()
    callbacks should never fire - but Tk's event loop CAN get pumped by other widget-lifecycle
    activity across a long test run (proven: forced app.update() calls do trigger a scheduled
    self.after(100, self.poll) even with no explicit mainloop()), and when that happens poll()
    drains the real worker thread's REAL hardware samples into whatever bucket a test is
    manually building, silently corrupting its expected sample_count/values.
    Reassigning app.poll to a no-op does NOT help - Tkinter's self.after(100, self.poll) already
    captured the ORIGINAL bound method as a closure at __init__ time, immune to reassignment.
    The only real fix is cancelling the actual scheduled Tcl 'after' events via 'after info' -
    the standard idiom for enumerating every pending after-callback on this interpreter, proven
    to work above even under forced event pumping."""
    app.stop_event.set()
    for after_id in app.tk.eval("after info").split():
        try:
            app.after_cancel(after_id)
        except Exception:
            pass


def find_bucket(buckets, start_timestamp, tol=0.01):
    """Finds the bucket matching a known start_timestamp - used instead of asserting on the
    TOTAL row count in the store, since these tests share one real on-disk database across many
    App() instances and (rarely, per isolate()'s docstring) real background telemetry can end up
    alongside a test's own synthetic bucket. Filtering to the specific bucket a test actually
    created still fully verifies the behavior under test, without assuming total isolation the
    shared store can't always guarantee."""
    return next((b for b in buckets if abs(b["start_timestamp"] - start_timestamp) < tol), None)


def write_jsonl_buckets(buckets, path=None):
    path = path or TELEMETRY_JSONL_PATH
    with path.open("w", encoding="utf-8") as f:
        for b in buckets:
            f.write(json.dumps(b) + "\n")


def main():
    fresh_files()

    print("=== 1. 30 ticks produce exactly one 60s bucket, sample_count=30, then a clean new bucket ===")
    app = App()
    isolate(app)
    app.telemetry_bucket = _new_telemetry_bucket(time.time())
    expected_start = app.telemetry_bucket["start_timestamp"]
    for _ in range(29):
        tick(app, cpu_temp=70.0)
    assert app.telemetry_bucket["sample_count"] == 29
    app.telemetry_bucket["start_timestamp"] = time.time() - (TELEMETRY_BUCKET_SECONDS + 1)
    expected_start = app.telemetry_bucket["start_timestamp"]  # the age-forced value, not the original
    tick(app, cpu_temp=70.0)  # 30th sample - also crosses the bucket-age threshold
    assert app.telemetry_bucket["sample_count"] == 0, "FAIL: a fresh bucket should have started"
    persisted = read_telemetry_file()
    mine = find_bucket(persisted, expected_start)
    assert mine is not None, f"FAIL: expected a persisted bucket at start={expected_start}, got {persisted}"
    assert mine["sample_count"] == 30, f"FAIL: expected sample_count=30, got {mine['sample_count']}"
    persisted = [mine]  # test 2 below reads persisted[0] - keep it pointed at OUR bucket specifically
    print(f"  PASS: bucket finalized at sample_count=30, new bucket started clean at 0")
    # Destroyed immediately (not deferred to script end, unlike earlier revisions of this file) -
    # a long-lived-but-idle App() instance turned out to be exactly the contamination source
    # isolate() alone didn't fully rule out; destroying each instance the moment its own test is
    # done removes any chance of a stale instance's activity leaking into a LATER test's fresh
    # database file (all instances share the one real TELEMETRY_DB_PATH).
    app.stop_event.set(); app.destroy()

    print("\n=== 2. average/min/max are mathematically correct ===")
    agg = _bucket_agg_new()
    for v in (60.0, 80.0, 70.0):
        _bucket_agg_add(agg, v)
    r = _bucket_agg_result(agg)
    assert r == {"avg": 70.0, "min": 60.0, "max": 80.0, "count": 3}, f"FAIL: {r}"
    cpu_scalar = persisted[0]["scalars"]["cpu_temp"]
    assert cpu_scalar == {"avg": 70.0, "min": 70.0, "max": 70.0, "count": 30}, f"FAIL: {cpu_scalar}"
    print(f"  PASS: {r}  and persisted bucket's cpu_temp = {cpu_scalar}")

    print("\n=== 3. missing samples never become zero ===")
    agg2 = _bucket_agg_new()
    for v in (50.0, None, None, 90.0):
        _bucket_agg_add(agg2, v)
    r2 = _bucket_agg_result(agg2)
    assert r2["count"] == 2 and r2["avg"] == 70.0, f"FAIL: None samples affected the average: {r2}"
    assert _bucket_agg_result(_bucket_agg_new()) is None, "FAIL: an all-missing metric must be None, not 0"
    print(f"  PASS: {r2} (2 real samples only), fully-missing aggregate -> None")

    print("\n=== 4. multiple sensors (drives/DIMMs) aggregate fully independently ===")
    fresh_files()
    app2 = App()
    isolate(app2)
    app2.telemetry_bucket = _new_telemetry_bucket(time.time())
    drive_a = "/nvme/0/temperature/0"
    drive_b = "/nvme/1/temperature/0"
    for _ in range(5):
        tick(app2, sensor_samples=[(drive_a, "Drive A", "Storage A", "Temperature", "drive", 40.0),
                                   (drive_b, "Drive B", "Storage B", "Temperature", "drive", 80.0)])
    bucket = app2.telemetry_bucket
    a_entry = bucket["sensors"][_sensor_bucket_key(drive_a)]
    b_entry = bucket["sensors"][_sensor_bucket_key(drive_b)]
    assert a_entry["agg"]["sum"] / a_entry["agg"]["count"] == 40.0
    assert b_entry["agg"]["sum"] / b_entry["agg"]["count"] == 80.0
    assert a_entry["agg"]["count"] == 5 and b_entry["agg"]["count"] == 5
    print(f"  PASS: drive A avg=40.0, drive B avg=80.0, fully independent accumulators")
    app2.stop_event.set(); app2.destroy()

    print("\n=== 5/6. drive and DIMM identity stay stable across ticks (same key, no duplicates) ===")
    fresh_drive = {"Identifier": "/nvme/2/temperature/0", "Parent": "Storage NVMe", "Name": "Composite Temperature",
                  "SensorType": "Temperature"}
    fresh_dimm = {"Identifier": "/lpc/nct6687d/0/temperature/9", "Parent": "SuperIO", "Name": "DIMM #1",
                 "SensorType": "Temperature"}
    app3 = App()
    isolate(app3)
    app3.telemetry_bucket = _new_telemetry_bucket(time.time())
    for i in range(4):
        tick(app3, sensor_samples=[
            (sensor_identity(fresh_drive), "NVMe", "Storage NVMe", "Temperature", "drive", 50.0 + i),
            (sensor_identity(fresh_dimm), "DIMM #1", "SuperIO", "Temperature", "ram", 35.0 + i),
        ])
    assert len(app3.telemetry_bucket["sensors"]) == 2, \
        f"FAIL: expected exactly 2 sensor entries (1 drive + 1 dimm), got {len(app3.telemetry_bucket['sensors'])}"
    drive_entry = app3.telemetry_bucket["sensors"][_sensor_bucket_key(sensor_identity(fresh_drive))]
    dimm_entry = app3.telemetry_bucket["sensors"][_sensor_bucket_key(sensor_identity(fresh_dimm))]
    assert drive_entry["agg"]["count"] == 4 and dimm_entry["agg"]["count"] == 4
    assert drive_entry["component"] == "drive" and dimm_entry["component"] == "ram"
    print("  PASS: 4 ticks each, same identity every time, no duplicate/rekeyed entries")
    app3.stop_event.set(); app3.destroy()

    print("\n=== 7/8. shutdown policy: partial bucket (sample_count>0) is finalized+persisted; restart starts clean ===")
    fresh_files()
    app4 = App()
    isolate(app4)
    app4.telemetry_bucket = _new_telemetry_bucket(time.time())
    expected_start = app4.telemetry_bucket["start_timestamp"]
    for _ in range(7):
        tick(app4, cpu_temp=65.0)
    assert app4.telemetry_bucket["sample_count"] == 7
    app4.close()  # real shutdown path - must finalize+persist the partial bucket
    on_disk = read_telemetry_file()
    mine = find_bucket(on_disk, expected_start)
    assert mine is not None and mine["sample_count"] == 7, \
        f"FAIL: partial bucket (start={expected_start}) should have been persisted with sample_count=7, got {mine}"
    print(f"  PASS: partial 7-sample bucket persisted on clean shutdown (documented policy: finalize, don't discard)")

    app5 = App()  # "restart"
    isolate(app5)
    assert app5.telemetry_bucket["sample_count"] == 0, "FAIL: a fresh app must start a clean empty bucket"
    assert time.time() - app5.telemetry_bucket["start_timestamp"] < 5
    app5.stop_event.set(); app5.destroy()
    print("  PASS: restart begins a brand new, empty bucket")

    print("\n=== (shutdown policy, empty case): an app that never ticked persists nothing extra on close ===")
    fresh_files()
    app6 = App()
    isolate(app6)
    empty_start = app6.telemetry_bucket["start_timestamp"]
    assert app6.telemetry_bucket["sample_count"] == 0
    before = len(read_telemetry_file())
    app6.close()
    after = len(read_telemetry_file())
    # Asserts the actual claim under test - close() adds nothing when sample_count==0 - via
    # count DELTA rather than an absolute "==0", and confirms specifically that OUR empty
    # bucket's own start_timestamp never appears (find_bucket(...) is None), rather than assuming
    # the shared on-disk store has nothing else in it at all (see find_bucket()'s docstring).
    assert after == before, f"FAIL: an empty bucket must add nothing (before={before}, after={after})"
    assert find_bucket(read_telemetry_file(), empty_start) is None, \
        "FAIL: the empty bucket's own start_timestamp should never appear on disk"
    print("  PASS: zero-sample bucket discarded silently, nothing spurious written")

    print("\n=== 9. missing history entirely (no DB, no legacy JSONL) starts cleanly ===")
    fresh_files()
    assert read_telemetry_file() == []
    app7 = App()  # must not crash
    isolate(app7)
    app7.stop_event.set(); app7.destroy()
    assert TELEMETRY_DB_PATH.exists(), "FAIL: a fresh SQLite store should be created on first startup"
    print("  PASS: no crash, empty list returned, a fresh SQLite store is created")

    print("\n=== 10a. migration tolerates a malformed line in the legacy JSONL without losing valid buckets ===")
    fresh_files()
    good1 = {"start_timestamp": 1000.0, "end_timestamp": 1060.0, "sample_count": 30,
            "scalars": {"cpu_temp": {"avg": 60.0, "min": 55.0, "max": 65.0, "count": 30}}, "sensors": {}}
    good2 = {"start_timestamp": 1060.0, "end_timestamp": 1120.0, "sample_count": 30,
            "scalars": {"cpu_temp": {"avg": 62.0, "min": 58.0, "max": 66.0, "count": 30}}, "sensors": {}}
    with TELEMETRY_JSONL_PATH.open("w", encoding="utf-8") as f:
        f.write(json.dumps(good1) + "\n")
        f.write("{not valid json at all\n")
        f.write(json.dumps(good2) + "\n")
        f.write("\n")  # a blank line too - must also be tolerated
    conn = open_telemetry_db()
    migrated = migrate_telemetry_jsonl_to_sqlite(conn)
    conn.close()
    assert migrated == 2, f"FAIL: expected 2 valid buckets migrated despite 1 bad line, got {migrated}"
    loaded = read_telemetry_file()
    assert len(loaded) == 2 and loaded[0]["start_timestamp"] == 1000.0 and loaded[1]["start_timestamp"] == 1060.0
    print(f"  PASS: {migrated} valid buckets migrated, the malformed/blank lines skipped without aborting")

    print("\n=== 10b. migration is idempotent: a second run (fresh connection) migrates nothing more ===")
    conn2 = open_telemetry_db()
    migrated_again = migrate_telemetry_jsonl_to_sqlite(conn2)
    conn2.close()
    assert migrated_again == 0, f"FAIL: re-running migration should be a no-op, migrated {migrated_again} more"
    assert len(read_telemetry_file()) == 2, "FAIL: bucket count changed after a no-op re-migration"
    print("  PASS: second migration run is a true no-op (marker-gated), bucket count unchanged")

    print("\n=== 10b2. App.init_telemetry_store() renames the legacy JSONL aside after a real migration ===")
    fresh_files()
    recent_now = time.time()
    # Realistic near-now timestamps this time (good1/good2 above are 1970-epoch fixtures for the
    # isolated-migration test and would be immediately deleted by the retention prune that runs
    # right after migration inside a real App startup - that prune is correct, not a bug).
    recent1 = {"start_timestamp": recent_now - 120, "end_timestamp": recent_now - 60, "sample_count": 30,
              "scalars": {"cpu_temp": {"avg": 60.0, "min": 55.0, "max": 65.0, "count": 30}}, "sensors": {}}
    recent2 = {"start_timestamp": recent_now - 60, "end_timestamp": recent_now, "sample_count": 30,
              "scalars": {"cpu_temp": {"avg": 62.0, "min": 58.0, "max": 66.0, "count": 30}}, "sensors": {}}
    write_jsonl_buckets([recent1, recent2])
    app_mig = App()  # triggers init_telemetry_store() -> migration -> prune -> rename
    isolate(app_mig)
    after_migration = read_telemetry_file()
    assert find_bucket(after_migration, recent1["start_timestamp"]) is not None, \
        f"FAIL: App startup should have migrated recent1, got {after_migration}"
    assert find_bucket(after_migration, recent2["start_timestamp"]) is not None, \
        f"FAIL: App startup should have migrated recent2, got {after_migration}"
    assert not TELEMETRY_JSONL_PATH.exists(), "FAIL: the legacy JSONL should be renamed aside, not left in place"
    assert TELEMETRY_JSONL_PATH.with_suffix(".jsonl.migrated").exists(), \
        "FAIL: expected a .jsonl.migrated backup of the original file"
    app_mig.stop_event.set(); app_mig.destroy()
    print("  PASS: legacy JSONL renamed to .jsonl.migrated after a real App-driven migration")

    print("\n=== 10c. a corrupt SQLite file is backed up and replaced with a fresh usable store, never crashes ===")
    fresh_files()
    TELEMETRY_DB_PATH.write_bytes(b"this is not a sqlite database file, just garbage bytes" * 50)
    conn3 = open_telemetry_db()
    assert conn3 is not None, "FAIL: open_telemetry_db() must recover from a corrupt file, not return None"
    conn3.execute("SELECT 1 FROM buckets")  # must not raise - schema exists on the fresh store
    conn3.close()
    backups = list(TELEMETRY_DB_PATH.parent.glob(f"{TELEMETRY_DB_PATH.stem}.corrupt-*"))
    assert len(backups) == 1, f"FAIL: expected exactly 1 corrupt-file backup, got {backups}"
    app7b = App()  # must launch fine against the now-fresh store
    isolate(app7b)
    app7b.stop_event.set(); app7b.destroy()
    print(f"  PASS: corrupt .db backed up to {backups[0].name}, fresh store created, app still launches")

    print("\n=== 10d. re-persisting the SAME bucket (start_timestamp) never duplicates sensor rows ===")
    fresh_files()
    app7c = App()
    isolate(app7c)
    app7c.telemetry_bucket = _new_telemetry_bucket(1_800_000_000.0)
    tick(app7c, sensor_samples=[("/nvme/0/temperature/0", "NVMe", "Storage", "Temperature", "drive", 45.0)])
    app7c._persist_telemetry_bucket(dict(app7c.telemetry_bucket, end_timestamp=1_800_000_060.0))
    app7c._persist_telemetry_bucket(dict(app7c.telemetry_bucket, end_timestamp=1_800_000_060.0))  # re-persist
    on_disk = read_telemetry_file()
    matching = [b for b in on_disk if b["start_timestamp"] == 1_800_000_000.0]
    assert len(matching) == 1, f"FAIL: expected exactly 1 bucket row, got {len(matching)}"
    # Checked directly via SQL COUNT, not through read_telemetry_file()'s sensor_key-scoped
    # fetch (item 20's optimization means 'sensors' is only ever populated for one requested key).
    conn_check = open_telemetry_db()
    sensor_row_count = conn_check.execute(
        "SELECT COUNT(*) FROM sensor_readings WHERE start_timestamp = ?", (1_800_000_000.0,)).fetchone()[0]
    conn_check.close()
    assert sensor_row_count == 1, \
        f"FAIL: re-persisting the same bucket duplicated sensor rows: {sensor_row_count} rows found"
    app7c.stop_event.set(); app7c.destroy()
    print("  PASS: INSERT OR REPLACE + delete-then-insert keeps re-persisting the same bucket fully idempotent")

    print("\n=== 10e. indexed range query returns exactly the right buckets at scale (5000+ rows) ===")
    fresh_files()
    conn4 = open_telemetry_db()
    base_ts = 1_700_000_000.0
    n = 5000
    empty_scalars_json = json.dumps({k: None for k in TELEMETRY_SCALAR_KEYS})
    conn4.execute("BEGIN")
    for i in range(n):
        ts = base_ts + i * 60
        conn4.execute("INSERT INTO buckets (start_timestamp, end_timestamp, sample_count, scalars_json) "
                     "VALUES (?, ?, ?, ?)", (ts, ts + 60, 30, empty_scalars_json))
    conn4.commit()
    conn4.close()
    since = base_ts + (n - 1000) * 60
    tail = read_telemetry_file(since_ts=since)
    assert len(tail) == 1000, f"FAIL: expected exactly 1000 buckets in range, got {len(tail)}"
    assert tail[0]["start_timestamp"] >= since
    assert tail == sorted(tail, key=lambda b: b["start_timestamp"]), "FAIL: must return oldest-first"
    full = read_telemetry_file()
    assert len(full) == n
    print(f"  PASS: indexed since_ts query returned exactly {len(tail)}/{n} buckets, correctly ordered")

    print("\n=== 10f. JSON export produces a portable, correctly-enveloped snapshot ===")
    payload = build_telemetry_json_export(tail[:5], scalar_sensor_ref("cpu_temp"), {"range": "test"})
    assert payload["count"] == 5 and len(payload["buckets"]) == 5
    assert payload["sensor"]["key"] == "cpu_temp" and payload["filters"]["range"] == "test"
    assert json.loads(json.dumps(payload))["count"] == 5, "FAIL: export payload must be JSON-serializable"
    print("  PASS: export envelope has count/filters/sensor/buckets, round-trips through json.dumps cleanly")

    print("\n=== 11. 30-day retention pruning is correct (pure function + real SQLite DELETE) ===")
    now = time.time()
    buckets = [
        {"start_timestamp": now - 40 * 86400, "end_timestamp": now - 40 * 86400 + 60, "sample_count": 1},
        {"start_timestamp": now - 31 * 86400, "end_timestamp": now - 31 * 86400 + 60, "sample_count": 1},
        {"start_timestamp": now - 10 * 86400, "end_timestamp": now - 10 * 86400 + 60, "sample_count": 1},
        {"start_timestamp": now - 60, "end_timestamp": now, "sample_count": 1},
    ]
    kept = prune_telemetry_buckets(buckets, retention_days=TELEMETRY_RETENTION_DAYS, now=now)
    assert len(kept) == 2, f"FAIL: expected 2 buckets within 30 days, got {len(kept)}"
    assert all(now - b["end_timestamp"] <= TELEMETRY_RETENTION_DAYS * 86400 for b in kept)
    print(f"  PASS: {len(kept)}/{len(buckets)} buckets survived a {TELEMETRY_RETENTION_DAYS}-day retention prune (pure function)")

    fresh_files()
    app_prune = App()
    isolate(app_prune)
    my_starts = [b["start_timestamp"] + i * 0.001 for i, b in enumerate(buckets)]  # tiny offset, avoid PK clash
    conn5 = open_telemetry_db()
    conn5.execute("BEGIN")
    for i, b in enumerate(buckets):
        cols = ["start_timestamp", "end_timestamp", "sample_count"]
        vals = [my_starts[i], b["end_timestamp"], b["sample_count"]]
        conn5.execute(f"INSERT INTO buckets ({', '.join(cols)}) VALUES (?, ?, ?)", vals)
        conn5.execute("INSERT INTO sensor_readings (start_timestamp, sensor_key, avg, min, max, count) "
                     "VALUES (?, 'prune-test-sensor', 1.0, 1.0, 1.0, 1)", (vals[0],))
    conn5.commit()
    conn5.close()
    app_prune.prune_telemetry_history()
    remaining = read_telemetry_file()
    # Filtered to OUR 4 known buckets specifically (see find_bucket()'s docstring) - the 2 old
    # ones (40d, 31d) must be gone, the 2 recent ones (10d, just-now) must remain.
    mine_remaining = [find_bucket(remaining, ts) is not None for ts in my_starts]
    assert mine_remaining == [False, False, True, True], \
        f"FAIL: expected [old, old, recent, recent] -> [gone, gone, kept, kept], got {mine_remaining}"
    conn6 = open_telemetry_db()
    orphan_sensors = conn6.execute(
        "SELECT COUNT(*) FROM sensor_readings WHERE sensor_key = 'prune-test-sensor'").fetchone()[0]
    conn6.close()
    assert orphan_sensors == 2, f"FAIL: sensor_readings for OUR pruned buckets should cascade-delete too, got {orphan_sensors} left"
    app_prune.stop_event.set(); app_prune.destroy()
    print(f"  PASS: App.prune_telemetry_history()'s real SQL DELETE kept exactly our 2 recent buckets, "
          f"dropped our 2 old ones, and their sensor_readings rows deleted together")

    print("\n=== 12. monitoring gaps render as GAPS, never an interpolated line ===")
    gap_buckets = [
        {"start_timestamp": now - 3600, "end_timestamp": now - 3540, "scalars": {"cpu_temp": {"avg": 50.0, "min": 48.0, "max": 52.0, "count": 30}}},
        {"start_timestamp": now - 3540, "end_timestamp": now - 3480, "scalars": {"cpu_temp": {"avg": 51.0, "min": 49.0, "max": 53.0, "count": 30}}},
        # a real ~10 minute gap here (app closed) before telemetry resumes
        {"start_timestamp": now - 2880, "end_timestamp": now - 2820, "scalars": {"cpu_temp": {"avg": 55.0, "min": 53.0, "max": 57.0, "count": 30}}},
    ]
    points = normalize_bucket_series(gap_buckets, scalar_sensor_ref("cpu_temp"))
    displayed = downsample_series(points, 1, now - 3600)
    gap_flags = [p["gap_before"] for p in displayed]
    assert gap_flags == [False, False, True], f"FAIL: expected a gap only before the 3rd point, got {gap_flags}"
    print(f"  PASS: gap_before flags = {gap_flags} - only the point after the real gap is flagged")

    print("\n=== 13. range filtering (1h/6h/24h/7d/30d) returns exactly the right buckets ===")
    multi_day = [{"start_timestamp": now - days * 86400, "end_timestamp": now - days * 86400 + 60, "sample_count": 1}
                for days in (0.01, 0.5, 2, 10, 25, 40)]
    for window_s, expected in ((3600, 1), (86400, 2), (7 * 86400, 3), (30 * 86400, 5)):
        got = filter_buckets_by_range(multi_day, window_s, now=now)
        assert len(got) == expected, f"FAIL: window={window_s}s expected {expected} buckets, got {len(got)}"
    assert filter_buckets_by_range(multi_day, None, now=now) == multi_day
    print("  PASS: 1h/24h/7d/30d windows and the unfiltered 'All' case all return exactly the right buckets")

    print("\n=== 14. downsampling preserves short spikes (never averages a max away) ===")
    spike_buckets = []
    base = now - 3600
    for i in range(6):
        peak = 95.0 if i == 3 else 55.0  # one real spike hidden among 6 native buckets
        spike_buckets.append({"start_timestamp": base + i * 60, "end_timestamp": base + i * 60 + 60,
                             "scalars": {"cpu_temp": {"avg": peak, "min": peak - 2, "max": peak, "count": 30}}})
    pts = normalize_bucket_series(spike_buckets, scalar_sensor_ref("cpu_temp"))
    grouped = downsample_series(pts, 6, base)  # all 6 folded into 1 display group
    assert len(grouped) == 1
    assert grouped[0]["metric"]["max"] == 95.0, f"FAIL: spike max lost during downsampling: {grouped[0]['metric']}"
    assert grouped[0]["metric"]["avg"] < 95.0, "sanity: the average should be pulled down by the other 5 buckets"
    print(f"  PASS: downsampled group max={grouped[0]['metric']['max']} (spike preserved), "
          f"avg={grouped[0]['metric']['avg']:.1f} (smoothed, as expected for an average)")

    print("\n=== 15. coverage percentage is mathematically correct ===")
    cov_buckets = [{"sample_count": 1} for _ in range(720)] + [{"sample_count": 0} for _ in range(720)]
    valid, expected, pct = compute_coverage(cov_buckets, 86400, bucket_seconds=60)
    assert valid == 720 and expected == 1440 and abs(pct - 50.0) < 1e-9, f"FAIL: {valid}/{expected} = {pct}%"
    print(f"  PASS: 720 valid / 1440 expected = {pct:.0f}% coverage, matches the spec's worked example exactly")

    print("\n=== 16. incident overlays match timestamps (and component) correctly ===")
    incidents = [
        {"incident_id": "in-range", "component": "cpu", "start_timestamp": now - 1800, "end_timestamp": now - 1700, "max_zone": "ORANGE"},
        {"incident_id": "out-of-range", "component": "cpu", "start_timestamp": now - 100000, "end_timestamp": now - 99000, "max_zone": "RED"},
        {"incident_id": "wrong-component", "component": "gpu_core", "start_timestamp": now - 1800, "end_timestamp": now - 1700, "max_zone": "RED"},
        {"incident_id": "still-open", "component": "cpu", "start_timestamp": now - 200, "end_timestamp": None, "max_zone": "YELLOW"},
    ]
    got = overlapping_incidents(incidents, now - 3600, now, component="cpu")
    got_ids = {i["incident_id"] for i in got}
    assert got_ids == {"in-range", "still-open"}, f"FAIL: {got_ids}"
    print(f"  PASS: overlapping_incidents matched exactly {got_ids} (out-of-range and wrong-component excluded)")

    print("\n=== 17. session overlays match timestamps correctly ===")
    sessions = [
        {"session_id": "s-in-range", "workload_key": "cyberpunk2077.exe", "start_timestamp": now - 1800, "end_timestamp": now - 1000},
        {"session_id": "s-out-of-range", "workload_key": "blender.exe", "start_timestamp": now - 100000, "end_timestamp": now - 99000},
        {"session_id": "s-no-end", "workload_key": "python.exe", "start_timestamp": now - 500, "end_timestamp": None},
    ]
    got_s = overlapping_sessions(sessions, now - 3600, now)
    got_s_ids = {s["session_id"] for s in got_s}
    assert got_s_ids == {"s-in-range"}, f"FAIL: {got_s_ids} (a session missing end_timestamp must never be guessed at)"
    print(f"  PASS: overlapping_sessions matched exactly {got_s_ids}")

    print("\n=== 18. sensor drill-down opens the CORRECT sensor by stable identity (not some other sensor's data) ===")
    fresh_files()
    now = time.time()  # refreshed - the `now` above is from test 11 and may be stale by this point
    drive_x = "/nvme/0/temperature/0"
    drive_y = "/nvme/1/temperature/0"
    bucket_multi = {"start_timestamp": now - 120, "end_timestamp": now - 60, "sample_count": 30, "scalars": {},
                   "sensors": {
                       _sensor_bucket_key(drive_x): {"identifier": drive_x, "parent": "Storage A", "name": "Drive X",
                                                     "sensor_type": "Temperature", "component": "drive",
                                                     "unverified": False, "avg": 35.0, "min": 33.0, "max": 37.0, "count": 30},
                       _sensor_bucket_key(drive_y): {"identifier": drive_y, "parent": "Storage B", "name": "Drive Y",
                                                     "sensor_type": "Temperature", "component": "drive",
                                                     "unverified": False, "avg": 70.0, "min": 68.0, "max": 72.0, "count": 30},
                   }}
    # Written as a legacy JSONL fixture and migrated on startup - exercises the real ingest path
    # (migration) rather than poking SQLite rows directly, same as a genuine upgrade would.
    write_jsonl_buckets([bucket_multi])
    app8 = App()
    isolate(app8)
    assert find_bucket(read_telemetry_file(), bucket_multi["start_timestamp"]) is not None, \
        "FAIL: the fixture bucket should have migrated into SQLite on startup"
    win_x = SensorHistoryWindow(app8, {"kind": "sensor", "key": _sensor_bucket_key(drive_x), "label": "Drive X",
                                       "unit": "°C", "is_temp": True, "component": "drive"})
    win_x.range_var.set("1h"); win_x._recompute()
    assert "Average: 35" in win_x.summary_label.cget("text"), f"FAIL: {win_x.summary_label.cget('text')}"
    win_y = SensorHistoryWindow(app8, {"kind": "sensor", "key": _sensor_bucket_key(drive_y), "label": "Drive Y",
                                       "unit": "°C", "is_temp": True, "component": "drive"})
    win_y.range_var.set("1h"); win_y._recompute()
    assert "Average: 70" in win_y.summary_label.cget("text"), f"FAIL: {win_y.summary_label.cget('text')}"
    print("  PASS: Drive X's window shows 35°C, Drive Y's window shows 70°C - never cross-contaminated")

    print("\n=== 19. old sensor history remains readable when that sensor is no longer currently present ===")
    # bucket_multi above already references drive_x/drive_y identities that don't correspond to
    # any LIVE sensor on THIS machine (synthetic) - reading/displaying it must still work fine.
    win_old = SensorHistoryWindow(app8, {"kind": "sensor", "key": _sensor_bucket_key(drive_x), "label": "Drive X (removed)",
                                         "unit": "°C", "is_temp": True, "component": "drive"})
    win_old.range_var.set("7d"); win_old._recompute()
    assert "N/A" not in win_old.summary_label.cget("text").split("\n")[1], \
        f"FAIL: old sensor's real historical data should still display: {win_old.summary_label.cget('text')}"
    win_x.destroy(); win_y.destroy(); win_old.destroy()
    app8.stop_event.set(); app8.destroy()
    print("  PASS: a sensor identity absent from the current live inventory still reads its old history fine")

    print("\n=== 20. no telemetry-history work runs on the live 2s poll (only the cheap tick-accumulate call) ===")
    src = inspect.getsource(App.update_data)
    for forbidden in ("read_telemetry_file(", "downsample_series(", "compute_coverage(",
                     "overlapping_incidents(", "overlapping_sessions(", "SensorHistoryWindow(",
                     "open_telemetry_db(", "migrate_telemetry_jsonl_to_sqlite("):
        assert forbidden not in src, f"FAIL: update_data() must never call {forbidden} on the 2s poll"
    assert "_telemetry_observe_tick(" in src, "FAIL: the (cheap, tick-local) accumulation call should be present"
    print("  PASS: update_data() only accumulates into the current bucket - no DB I/O, no query, no chart math")

    print("\n=== 21. main dashboard render-optimization discipline is unaffected by the new click bindings ===")
    fresh_files()
    app9 = App()
    isolate(app9)
    real_sensors_local = None
    from app import lhm_sensors, nvidia_stats, memory, cpu_times
    from datetime import datetime as _dt

    def _payload():
        nonlocal real_sensors_local
        if real_sensors_local is None:
            real_sensors_local = lhm_sensors()
        old_idle, old_total = cpu_times()
        time.sleep(0.05)
        now2 = cpu_times()
        dt_load = now2[1] - old_total
        load = 100 * (1 - (now2[0] - old_idle) / dt_load) if dt_load else 0
        mem_pct, mem_used, mem_total = memory()
        return {"time": _dt.now(), "cpu_load": load, "mem_pct": mem_pct, "mem_used": mem_used,
                "mem_total": mem_total, "gpus": nvidia_stats(), "lhm": real_sensors_local}

    app9.update_data(_payload())
    app9.widget_stats["rows_created"] = 0
    app9.widget_stats["rows_destroyed"] = 0
    for _ in range(4):
        app9.update_data(_payload())
    assert app9.widget_stats["rows_created"] == 0 and app9.widget_stats["rows_destroyed"] == 0, \
        f"FAIL: drill-down click bindings introduced widget churn: {app9.widget_stats}"
    app9.stop_event.set(); app9.destroy()
    print("  PASS: zero widget churn across steady polls with drill-down bindings active - dashboard layout unaffected")

    # Every App() instance in this file is now destroyed immediately after its own test - see
    # the comment on app's destroy() call in test 1 for why that matters here specifically.
    fresh_files()
    print("\nALL TELEMETRY HISTORY CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
