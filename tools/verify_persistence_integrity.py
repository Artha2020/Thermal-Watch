"""Release-gate verification of every persistent store's integrity and recovery semantics: normal
reopen, empty, missing, malformed, truncated, corrupt-SQLite and duplicate/idempotent writes.

All of it runs against SANDBOX copies - _verify_sandbox is imported before app, so every store
constant points into a temp directory and no production file is ever the subject of a corruption
test. This exists because the recovery paths are the ones a user only exercises on their worst day,
and an untested recovery path is indistinguishable from a data-loss bug."""
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
sys.stdout.reconfigure(encoding="utf-8")

import app as appmod  # noqa: E402
from app import (  # noqa: E402
    App, EVENT_LOG_PATH, INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH, SESSIONS_PATH, ACTIVE_SESSIONS_PATH,
    TELEMETRY_DB_PATH, REPORTS_DB_PATH, EXPERIMENTS_PATH, DATA_DIR,
    read_incidents_file, read_sessions_file, read_experiments_file, read_event_log_file,
    read_telemetry_file, read_reports, open_telemetry_db, open_reports_db, save_report,
    append_experiment, new_experiment_record, atomic_write_lines, backup_store,
    build_report_payload, period_bounds, _new_telemetry_bucket,
)

NOW = time.time()

JSONL_STORES = [("incidents", INCIDENTS_PATH, read_incidents_file),
               ("sessions", SESSIONS_PATH, read_sessions_file),
               ("experiments", EXPERIMENTS_PATH, read_experiments_file),
               ("event log", EVENT_LOG_PATH, read_event_log_file)]

VALID_LINE = {
    "incidents": {"incident_id": "i1", "component": "cpu", "start_timestamp": NOW - 600,
                 "end_timestamp": NOW - 300, "duration_seconds": 300, "max_zone": "ORANGE"},
    "sessions": {"session_id": "s1", "workload": "game.exe", "workload_key": "game.exe",
                "start_timestamp": NOW - 600, "end_timestamp": NOW - 300, "duration_seconds": 300},
    "experiments": {"experiment_id": "e1", "change_timestamp": NOW - 86400, "description": "New fan",
                   "component": "gpu"},
    "event log": {"ts": NOW - 60, "kind": "WARN", "text": "something happened"},
}


def clear_all():
    for path in (EVENT_LOG_PATH, INCIDENTS_PATH, ACTIVE_INCIDENTS_PATH, SESSIONS_PATH,
                 ACTIVE_SESSIONS_PATH, EXPERIMENTS_PATH, TELEMETRY_DB_PATH, REPORTS_DB_PATH):
        for p in [path] + list(path.parent.glob(path.name + "*")):
            if p.exists() and p.is_file():
                p.unlink()


def main():
    assert str(DATA_DIR) == str(_verify_sandbox.SANDBOX_DIR), \
        f"FAIL: refusing to run corruption tests outside the sandbox (DATA_DIR={DATA_DIR})"
    print(f"=== Sandbox: every store under {DATA_DIR}")
    clear_all()

    print("\n=== 1. MISSING store: every reader returns empty rather than raising ===")
    for name, path, reader in JSONL_STORES:
        assert not path.exists()
        assert reader() == [], f"FAIL: {name} reader on a missing file"
    assert read_telemetry_file() == [], "FAIL: telemetry reader on a missing database"
    assert read_reports() == [], "FAIL: reports reader on a missing database"
    print(f"  PASS: all {len(JSONL_STORES)} JSONL readers plus telemetry and reports return [] when the "
          f"store does not exist")

    print("\n=== 2. EMPTY store: still empty, still no exception ===")
    for name, path, reader in JSONL_STORES:
        path.write_text("", encoding="utf-8")
        assert reader() == [], f"FAIL: {name} reader on an empty file"
    print("  PASS: a zero-byte store reads as empty for every JSONL reader")

    print("\n=== 3. MALFORMED lines are skipped; the surrounding VALID records survive ===")
    for name, path, reader in JSONL_STORES:
        good = json.dumps(VALID_LINE[name])
        path.write_text("\n".join([good, "{not json at all", "", "[]", "null", good]) + "\n",
                        encoding="utf-8")
        records = reader()
        assert len(records) == 2, f"FAIL: {name} expected the 2 valid records, got {len(records)}"
    print("  PASS: unparseable lines, blank lines, wrong-shaped JSON and nulls are skipped without "
          "losing the valid records around them")

    print("\n=== 4. TRUNCATED final line (the realistic crash-mid-append case) ===")
    for name, path, reader in JSONL_STORES:
        good = json.dumps(VALID_LINE[name])
        truncated = good[:len(good) // 2]  # a half-written record, no trailing newline
        path.write_text(good + "\n" + truncated, encoding="utf-8")
        records = reader()
        assert len(records) == 1, f"FAIL: {name} lost its complete record to a truncated tail"
    print("  PASS: a half-written final record is discarded and every complete record before it is kept "
          "- the exact shape of a crash during append")

    print("\n=== 5. CORRUPT SQLite: preserved aside, fresh store created, app still starts ===")
    for label, path, opener, reader in (("telemetry", TELEMETRY_DB_PATH, open_telemetry_db, read_telemetry_file),
                                        ("reports", REPORTS_DB_PATH, open_reports_db, read_reports)):
        for stale in path.parent.glob(f"{path.stem}.corrupt-*"):
            stale.unlink()
        path.write_bytes(b"\x00\x01 definitely not a sqlite file " * 40)
        conn = opener()
        assert conn is not None, f"FAIL: {label} store must recover from corruption, not return None"
        conn.close()
        preserved = list(path.parent.glob(f"{path.stem}.corrupt-*"))
        assert preserved, f"FAIL: the corrupt {label} file must be moved aside, never deleted"
        assert preserved[0].stat().st_size > 0, "the preserved copy must still hold the original bytes"
        assert reader() == [], f"FAIL: the fresh {label} store should be empty"
    app = App()
    app.stop_event.set(); app.destroy()
    print("  PASS: both SQLite stores rename the corrupt file aside (never delete it), create a fresh "
          "store, and App() still starts afterwards")

    print("\n=== 6. TRUNCATED SQLite header - a partially-written database file ===")
    clear_all()
    conn = open_telemetry_db()
    conn.close()
    original = TELEMETRY_DB_PATH.read_bytes()
    TELEMETRY_DB_PATH.write_bytes(original[:len(original) // 3])
    conn = open_telemetry_db()
    assert conn is not None, "FAIL: a truncated database must still recover"
    conn.close()
    assert read_telemetry_file() == []
    print("  PASS: a database truncated to a third of its bytes is recovered the same way as a corrupt one")

    print("\n=== 7. MALFORMED active-state snapshots do not block startup ===")
    clear_all()
    for path in (ACTIVE_INCIDENTS_PATH, ACTIVE_SESSIONS_PATH):
        path.write_text('{"saved_at": 123, "incidents": [ truncated', encoding="utf-8")
    app = App()
    app.update()
    app.stop_event.set(); app.destroy()
    print("  PASS: half-written active-incident and active-session snapshots are tolerated and the app "
          "starts normally")

    print("\n=== 8. NORMAL reopen: what was written is what comes back ===")
    clear_all()
    conn = open_telemetry_db()
    bucket = _new_telemetry_bucket(NOW - 120)
    bucket["scalars"]["cpu_temp"] = {"sum": 100.0, "min": 49.0, "max": 51.0, "count": 2}
    app = App()
    app._persist_telemetry_bucket(bucket)
    app.stop_event.set(); app.destroy()
    conn.close()
    reread = read_telemetry_file()
    assert len(reread) == 1 and reread[0]["scalars"]["cpu_temp"]["avg"] == 50.0, reread
    print(f"  PASS: a persisted telemetry bucket reopens with its averaged value intact "
          f"({reread[0]['scalars']['cpu_temp']['avg']}°C from sum 100/count 2)")

    print("\n=== 9. Duplicate / idempotent writes ===")
    clear_all()
    experiment = new_experiment_record("Repasted GPU", NOW - 10 * 86400, "gpu", now=NOW)
    assert append_experiment(experiment) and append_experiment(experiment)
    stored = read_experiments_file()
    assert len(stored) == 2, "the marker store appends; de-duplication is the caller's job here"
    ids = {e["experiment_id"] for e in stored}
    assert len(ids) == 1, "an identical record keeps its id - it is the same logical marker twice"
    bounds = period_bounds("DAILY", date.fromtimestamp(NOW - 86400))
    payload = build_report_payload(bounds, now=NOW)
    assert save_report(payload) and save_report(payload)
    assert len(read_reports()) == 1, "FAIL: report generation must be idempotent on (type, period)"
    assert save_report(payload, replace=True) and len(read_reports()) == 1
    print("  PASS: report generation is idempotent on its logical id (two writes -> one report), and an "
          "explicit replace still leaves exactly one")

    print("\n=== 10. atomic_write_lines: the original survives a failed rewrite ===")
    clear_all()
    INCIDENTS_PATH.write_text(json.dumps(VALID_LINE["incidents"]) + "\n", encoding="utf-8")
    before = INCIDENTS_PATH.read_bytes()
    real_replace = Path.replace

    def exploding_replace(self, target):
        raise OSError("simulated failure between temp write and rename")

    Path.replace = exploding_replace
    try:
        ok = atomic_write_lines(INCIDENTS_PATH, ['{"incident_id": "replacement"}'])
    finally:
        Path.replace = real_replace
    assert ok is False, "FAIL: a failed rewrite must report failure"
    assert INCIDENTS_PATH.read_bytes() == before, \
        "FAIL: the original store must be untouched when the atomic replace fails"
    assert not INCIDENTS_PATH.with_name(INCIDENTS_PATH.name + ".tmp").exists(), \
        "FAIL: the temp file must be cleaned up after a failed rewrite"
    print("  PASS: when the rename fails the pre-existing store is byte-identical and no .tmp is left "
          "behind - a crash mid-prune cannot truncate history")

    print("\n=== 11. atomic_write_lines succeeds normally, and never leaves a temp file ===")
    assert atomic_write_lines(INCIDENTS_PATH, [json.dumps(VALID_LINE["incidents"])])
    assert len(read_incidents_file()) == 1
    assert not INCIDENTS_PATH.with_name(INCIDENTS_PATH.name + ".tmp").exists()
    print("  PASS: normal rewrite replaces the store and cleans up after itself")

    print("\n=== 12. backup_store: copies aside, refuses nothing, never moves the original ===")
    assert backup_store(DATA_DIR / "does_not_exist.jsonl") is None, "nothing to back up -> None"
    target = backup_store(INCIDENTS_PATH, tag="pre-migration")
    assert target is not None and target.exists() and "pre-migration" in target.name
    assert INCIDENTS_PATH.exists(), "FAIL: backup_store must COPY, never move the original away"
    assert target.read_bytes() == INCIDENTS_PATH.read_bytes()
    print(f"  PASS: backup_store copied to {target.name}, left the original in place, and returns None "
          f"when there is nothing to protect")

    print("\n=== 13. Nothing in this script escaped the sandbox ===")
    for path in (EVENT_LOG_PATH, INCIDENTS_PATH, TELEMETRY_DB_PATH, REPORTS_DB_PATH, EXPERIMENTS_PATH):
        assert str(path.parent) == str(_verify_sandbox.SANDBOX_DIR), path
    print(f"  PASS: every store touched above lives under {_verify_sandbox.SANDBOX_DIR}")

    clear_all()
    print("\nALL PERSISTENCE INTEGRITY CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
