"""THE SAFETY GATE: proves that running the complete verification suite cannot modify, create or
delete anything in the real Thermal Watch data directory.

This does not merely inspect verification scripts for dangerous-looking calls. Instead it hashes
EVERY file in the production directory, runs every verify_*.py for real as a subprocess, and
re-hashes.
Any byte that changed, any file that appeared, any file that vanished is a failure naming the exact
path.

Deliberately measured against the WHOLE directory rather than a list of known store names: a future
store, a stray .tmp, a leftover .verify_backup or a SQLite -wal/-shm sidecar would all be missed by
a hand-maintained list, and those are exactly the files a harness bug would leave behind.

Run this whenever a verify script is added or changed. It is also the honest answer to "is the
suite safe to run against a machine with real history?" - if this passes, yes; if it cannot be run,
the answer is not "probably".
"""
import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

PRODUCTION_DIR = Path(__file__).resolve().parent.parent
TOOLS_DIR = Path(__file__).resolve().parent
# Excluded from the fingerprint because they are CODE/BUILD artifacts, not user data, and change for
# reasons that have nothing to do with a harness misbehaving.
EXCLUDED_DIRS = {"__pycache__", "LibreHardwareMonitor", ".git", "Thermal watch UI redesign"}


def fingerprint(root):
    """{relative_path: (size, sha256)} for every file under root, skipping build/code artifacts.
    Hashing content rather than mtime deliberately - a file rewritten with identical bytes is not a
    data loss, and a file whose mtime is untouched but whose bytes changed absolutely is."""
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        try:
            data = path.read_bytes()
        except OSError as e:
            out[str(rel)] = ("UNREADABLE", str(e))
            continue
        out[str(rel)] = (len(data), hashlib.sha256(data).hexdigest())
    return out


def diff(before, after):
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(k for k in set(before) & set(after) if before[k] != after[k])
    return created, deleted, modified


def detect_concurrent_writer(root, settle_seconds=3.0):
    """Is something OTHER than this gate already writing to the production tree?

    This gate proves a negative - that the suite touched nothing - by diffing hashes across the run.
    That reasoning only holds if nothing else is writing meanwhile. A LIVE Thermal Watch instance
    writes telemetry every minute, closes workload sessions and appends to the event log, all of
    which are correct behaviour but land in the diff and read exactly like an isolation violation.

    So: sample twice a few seconds apart before starting. Any change means a concurrent writer, and
    the gate says so up front rather than blaming the suite afterwards."""
    first = fingerprint(root)
    time.sleep(settle_seconds)
    second = fingerprint(root)
    created, deleted, modified = diff(first, second)
    return created + deleted + modified


def main():
    scripts = sorted(p for p in TOOLS_DIR.glob("verify_*.py") if p.name != Path(__file__).name)
    print(f"=== Production directory: {PRODUCTION_DIR}")

    busy = detect_concurrent_writer(PRODUCTION_DIR)
    if busy:
        print("=== WARNING: the production tree is being written to by something else RIGHT NOW:")
        for name in busy:
            print(f"      {name}")
        print("      A running Thermal Watch instance (or an editor) will make the isolation result")
        print("      below meaningless - its writes cannot be told apart from the suite's.")
        print("      Close Thermal Watch and re-run before treating any failure as a release blocker.")

    before = fingerprint(PRODUCTION_DIR)
    data_files = [k for k in before if k.startswith("thermal_watch_")]
    print(f"=== Fingerprinted {len(before)} file(s), of which {len(data_files)} are data stores:")
    for k in sorted(data_files):
        print(f"      {k}  ({before[k][0]} bytes)")
    if not data_files:
        print("      (none present - the gate still proves the suite CREATES none, which is equally required)")

    print(f"\n=== Running {len(scripts)} verify script(s) as real subprocesses...")
    failures, started = [], time.time()
    for script in scripts:
        proc = subprocess.run([sys.executable, str(script)], cwd=str(PRODUCTION_DIR),
                              capture_output=True, text=True, encoding="utf-8", errors="replace")
        ok = proc.returncode == 0
        print(f"      {'ok  ' if ok else 'FAIL'}  {script.name}")
        if not ok:
            failures.append(script.name)
    print(f"=== Suite finished in {time.time() - started:.0f}s "
          f"({len(scripts) - len(failures)} passed, {len(failures)} failed)")
    if failures:
        # Reported, NOT fatal to this gate: a verify script can fail for its own reasons (two in
        # this repo have long-standing test-infra failures). This gate answers one question only -
        # did the suite touch real data - and a failing script is if anything MORE likely to leave
        # something behind, so the fingerprint check below still has to run and still has to pass.
        print(f"      note: {', '.join(failures)} exited non-zero; isolation is still checked below")

    after = fingerprint(PRODUCTION_DIR)
    created, deleted, modified = diff(before, after)

    print("\n=== ISOLATION RESULT")
    if not (created or deleted or modified):
        print(f"  PASS: all {len(before)} production file(s) byte-for-byte identical - the suite "
              f"created nothing, deleted nothing and modified nothing")
        print("\nVERIFICATION ISOLATION HOLDS, NO PRODUCTION DATA WAS TOUCHED")
        return 0

    for label, items in (("MODIFIED", modified), ("DELETED", deleted), ("CREATED", created)):
        for name in items:
            detail = ""
            if label == "MODIFIED":
                detail = f"  ({before[name][0]} -> {after[name][0]} bytes)"
            print(f"  FAIL {label}: {name}{detail}")
    print(f"\nVERIFICATION ISOLATION VIOLATED: {len(modified)} modified, {len(deleted)} deleted, "
          f"{len(created)} created")
    return 1


if __name__ == "__main__":
    sys.exit(main())
