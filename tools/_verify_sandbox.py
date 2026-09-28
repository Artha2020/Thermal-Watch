"""Verification isolation. Import this BEFORE `app` in every verify script - it is the reason a
test cannot touch real Thermal Watch history.

The guarantee is structural, not a convention. Importing this module points
THERMAL_WATCH_DATA_DIR at a fresh temporary directory and then imports `app` itself, so by the
time any verify script says `import app`, every store constant in that module
(EVENT_LOG_PATH, INCIDENTS_PATH, SESSIONS_PATH, EXPERIMENTS_PATH, TELEMETRY_DB_PATH, ...) already
resolves inside the sandbox. A verify script no longer promises not to delete the production event
log; it has no way to NAME it. The old `fresh_files()` helpers those scripts open with therefore
became harmless without needing to be rewritten one by one - they unlink sandbox paths.

This exists because the promise-based version failed: on 2026-08-12 a verify script's fixture
setup unlinked the real thermal_watch_events.log and destroyed ~166 KB of irreplaceable history.
tools/verify_isolation.py is the gate that proves this module actually holds, by hashing every
production file before and after a full suite run.

Two guards below turn a mis-ordered import into a loud failure rather than a silent write to real
data:
  - importing this after `app` is already in sys.modules raises, because the store constants would
    already have been resolved against the production directory;
  - after importing `app`, DATA_DIR is asserted to be inside the sandbox.
"""
import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

if "app" in sys.modules:
    raise RuntimeError(
        "_verify_sandbox must be imported BEFORE app - app's store paths are resolved at import "
        "time, so importing it first would bind them to the REAL data directory."
    )

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SANDBOX_DIR = Path(tempfile.mkdtemp(prefix="thermal_watch_verify_"))
PRODUCTION_DIR = Path(__file__).resolve().parent.parent
os.environ["THERMAL_WATCH_DATA_DIR"] = str(SANDBOX_DIR)
# Phase 12 - the evidence pipe: a verify script that constructs a real App() also starts a real
# EvidencePipeServer. Same reasoning as THERMAL_WATCH_DATA_DIR above - without this, a test run
# would bind the SAME pipe name a real, already-running Thermal Watch instance uses (harmless
# most of the time since Windows allows multiple named-pipe instances, but if no real instance
# happens to be running, the test process would transiently answer on the production pipe name).
_TEST_PIPE_NAME = f"ThermalWatchEvidence.verify.{os.getpid()}"
os.environ["THERMAL_WATCH_PIPE_NAME"] = _TEST_PIPE_NAME

import app as _app  # noqa: E402  - deliberately after the environment is set
import thermal_watch_evidence_pipe as _pipe_mod  # noqa: E402  - same reason

if Path(_app.DATA_DIR).resolve() != SANDBOX_DIR.resolve():
    raise RuntimeError(f"sandbox failed: app.DATA_DIR is {_app.DATA_DIR}, expected {SANDBOX_DIR}")
for name in ("EVENT_LOG_PATH", "INCIDENTS_PATH", "ACTIVE_INCIDENTS_PATH", "SESSIONS_PATH",
             "ACTIVE_SESSIONS_PATH", "TELEMETRY_JSONL_PATH", "TELEMETRY_DB_PATH", "EXPERIMENTS_PATH"):
    store = Path(getattr(_app, name)).resolve()
    if store.parent != SANDBOX_DIR.resolve():
        raise RuntimeError(f"sandbox failed: {name} resolves to {store}, outside the sandbox")
if _pipe_mod.PIPE_NAME != _TEST_PIPE_NAME:
    raise RuntimeError(f"sandbox failed: pipe name is {_pipe_mod.PIPE_NAME!r}, expected the test-only name")


@atexit.register
def _cleanup():
    shutil.rmtree(SANDBOX_DIR, ignore_errors=True)
