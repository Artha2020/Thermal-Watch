"""Deterministic regression test for a real launcher defect found during v1.1 RC packaged
testing: launch.ps1 resolved its target interpreter as `(Get-Command pythonw.exe).Source`.
Get-Command, for a plain application/exe lookup (unlike its behavior for cmdlets/functions),
returns EVERY match found on PATH, not just the first. On a machine with more than one
pythonw.exe on PATH, `.Source` silently became an array instead of a single path string, and
Start-Process -FilePath with an array argument started more than one Thermal Watch UI process
from a single launch.bat invocation - reproduced live during RC testing (two real App() instances
briefly ran concurrently against the same production data directory).

The fix (launch.ps1) breaks the resolution out into `Resolve-ThermalWatchPythonw`, which pipes
through `Select-Object -First 1` before reading `.Source` - deterministically the same
interpreter a bare `pythonw.exe` invocation would already resolve to (highest PATH-priority
match), just guaranteed to be a single value. A dot-source guard (`if ($MyInvocation.
InvocationName -eq '.') { return }`) lets this function be loaded and called in isolation without
triggering the script's real elevation/Start-Process side effects.

This test does not touch git, does not modify launch.ps1, does not elevate anything, does not
start app.py or sensor_bridge.ps1, and does not hardcode this machine's Python installation
path(s) - it discovers a real pythonw.exe dynamically and copies it into a throwaway temp
directory structure to construct the multiple-installs scenario.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCH_PS1 = REPO_ROOT / "launch.ps1"
POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

sys.stdout.reconfigure(encoding="utf-8")

FAILURES = []
CHECKS = 0


def check(name, condition, detail=""):
    global CHECKS
    CHECKS += 1
    ok = bool(condition)
    print(f"[{'PASS' if ok else 'FAIL'}] {CHECKS:2d}. {name}")
    if detail:
        print(f"        {detail}")
    if not ok:
        FAILURES.append(name)


def find_real_pythonw():
    """A real pythonw.exe on THIS machine, discovered dynamically - never hardcoded."""
    found = shutil.which("pythonw")
    if found:
        return Path(found)
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.exists():
        return candidate
    raise RuntimeError("no real pythonw.exe could be discovered on this machine to copy for the test")


def run_resolve(path_value):
    """Dot-sources launch.ps1 (loads Resolve-ThermalWatchPythonw, skips the real launcher body
    per its own dot-source guard) inside a child process with a fully-controlled PATH, then calls
    the function and prints its result plus a scalar/array marker."""
    script = (
        f". '{LAUNCH_PS1}'; "
        "$r = Resolve-ThermalWatchPythonw; "
        "Write-Output ('RESULT:' + $r); "
        "Write-Output ('ISARRAY:' + ($r -is [array])); "
        "Write-Output ('COUNT:' + @($r).Count)"
    )
    proc = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=30,
        env={"PATH": path_value, "SystemRoot": r"C:\Windows"},
    )
    return proc


def parse_field(stdout, prefix):
    for line in stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return None


print("=" * 78)
print("setup")
print("=" * 78)
check("launch.ps1 exists", LAUNCH_PS1.exists())
check("Resolve-ThermalWatchPythonw is defined in launch.ps1 (not just described in a comment)",
      "function Resolve-ThermalWatchPythonw" in LAUNCH_PS1.read_text(encoding="utf-8"))
check("launch.ps1 has a dot-source guard so loading it for a test has no launcher side effects",
      "$MyInvocation.InvocationName -eq '.'" in LAUNCH_PS1.read_text(encoding="utf-8"))

real_pythonw = find_real_pythonw()
check(f"discovered a real pythonw.exe on this machine to copy ({real_pythonw})", real_pythonw.exists())

tmp_root = Path(tempfile.mkdtemp(prefix="tw_launch_interp_test_"))
try:
    print()
    print("=" * 78)
    print("1. sanity: with the REAL (unmodified) environment PATH, resolution is still singular")
    print("=" * 78)
    import os
    baseline = run_resolve(os.environ.get("PATH", ""))
    check("baseline run against the real PATH exits cleanly", baseline.returncode == 0,
          detail=baseline.stderr.strip()[:400] if baseline.returncode != 0 else "")
    baseline_result = parse_field(baseline.stdout, "RESULT:")
    check("baseline RESULT is non-empty", bool(baseline_result))
    check("baseline result is not an array", parse_field(baseline.stdout, "ISARRAY:") == "False")
    check("baseline result count is exactly 1", parse_field(baseline.stdout, "COUNT:") == "1")

    print()
    print("=" * 78)
    print("2. THE REGRESSION SCENARIO: three separate pythonw.exe installs on PATH")
    print("=" * 78)
    fake_dirs = []
    for i in range(3):
        d = tmp_root / f"install_{i}"
        d.mkdir(parents=True)
        shutil.copy2(real_pythonw, d / "pythonw.exe")
        fake_dirs.append(d)

    rigged_path = ";".join(str(d) for d in fake_dirs) + r";C:\Windows\System32;C:\Windows\System32\WindowsPowerShell\v1.0"
    result = run_resolve(rigged_path)
    check("resolution against a PATH with 3 pythonw.exe installs exits cleanly", result.returncode == 0,
          detail=result.stderr.strip()[:400] if result.returncode != 0 else "")

    is_array = parse_field(result.stdout, "ISARRAY:")
    count = parse_field(result.stdout, "COUNT:")
    resolved = parse_field(result.stdout, "RESULT:")

    check("with 3 installs on PATH, the resolved interpreter is STILL a single value, not an array",
          is_array == "False", detail=f"ISARRAY={is_array}")
    check("with 3 installs on PATH, exactly one path is returned",
          count == "1", detail=f"COUNT={count}")
    check("the resolved path is the FIRST install on PATH (existing 'first on PATH wins' "
          "selection behavior is preserved, not changed)",
          resolved is not None and Path(resolved) == (fake_dirs[0] / "pythonw.exe"),
          detail=f"resolved={resolved!r} expected={fake_dirs[0] / 'pythonw.exe'}")
    check("the resolved path is NOT the second or third install",
          resolved not in (str(fake_dirs[1] / "pythonw.exe"), str(fake_dirs[2] / "pythonw.exe")))

    print()
    print("=" * 78)
    print("3. Start-Process -FilePath with this resolved value can only ever start ONE process")
    print("=" * 78)
    # A genuinely scalar [string] passed to -FilePath starts exactly one process by
    # Start-Process's own contract; an [array] is exactly the shape that previously let more
    # than one process start from a single invocation. This check asserts the type guarantee
    # PowerShell itself enforces, using the actual resolved value from check 2 above - not a
    # separate synthetic case.
    check("the value that would be passed to Start-Process -FilePath is a plain string "
          "(the only shape Start-Process treats as a single target)",
          is_array == "False" and count == "1")
finally:
    shutil.rmtree(tmp_root, ignore_errors=True)

print()
print("=" * 78)
summary = f"{CHECKS - len(FAILURES)}/{CHECKS} checks passed"
print(summary)
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL LAUNCH INTERPRETER RESOLUTION CHECKS PASSED")
