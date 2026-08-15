"""Verification isolation. Import this BEFORE `app` in every verify script - it is the reason a
test cannot touch real Thermal Watch history.

The guarantee is structural, not a convention. Importing this module points
THERMAL_WATCH_DATA_DIR at a fresh temporary directory and then imports `app` itself, so by the
time any verify script says `import app`, every store constant in that module
(EVENT_LOG_PATH, INCIDENTS_PATH, SESSIONS_PATH, EXPERIMENTS_PATH, TELEMETRY_DB_PATH, ...) already
    resolves inside the sandbox. A verification script has no way to name the production event log;
    fixture-cleanup helpers can only unlink sandbox paths. tools/verify_isolation.py proves this
    guarantee across the full suite by hashing every production file before and after the run.

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

import app as _app  # noqa: E402  - deliberately after the environment is set

if Path(_app.DATA_DIR).resolve() != SANDBOX_DIR.resolve():
    raise RuntimeError(f"sandbox failed: app.DATA_DIR is {_app.DATA_DIR}, expected {SANDBOX_DIR}")
for name in ("EVENT_LOG_PATH", "INCIDENTS_PATH", "ACTIVE_INCIDENTS_PATH", "SESSIONS_PATH",
             "ACTIVE_SESSIONS_PATH", "TELEMETRY_JSONL_PATH", "TELEMETRY_DB_PATH", "EXPERIMENTS_PATH"):
    store = Path(getattr(_app, name)).resolve()
    if store.parent != SANDBOX_DIR.resolve():
        raise RuntimeError(f"sandbox failed: {name} resolves to {store}, outside the sandbox")


@atexit.register
def _cleanup():
    shutil.rmtree(SANDBOX_DIR, ignore_errors=True)
