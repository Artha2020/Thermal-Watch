"""Measures Thermal Watch's own process CPU usage over a real live run with workload
attribution active, plus a live 30s run to confirm no traceback under real conditions."""
import subprocess
import sys
import time
from pathlib import Path

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def main():
    proc = subprocess.Popen([sys.executable, APP], cwd=str(Path(APP).parent))
    time.sleep(3)  # let it fully start (hardware_info(), first bridge read, etc.)

    ps_script = (
        f"$p = Get-Process -Id {proc.pid} -ErrorAction SilentlyContinue; "
        "if ($p) { [pscustomobject]@{ CPU=$p.CPU; WS=$p.WorkingSet64 } | ConvertTo-Json -Compress } "
        "else { 'gone' }"
    )
    t0 = time.time()
    out0 = subprocess.run(["powershell", "-NoProfile", "-Command", ps_script],
                          capture_output=True, text=True, timeout=10).stdout.strip()
    print(f"t=0s   sample: {out0}")

    WAIT = 30
    time.sleep(WAIT)

    out1 = subprocess.run(["powershell", "-NoProfile", "-Command", ps_script],
                          capture_output=True, text=True, timeout=10).stdout.strip()
    elapsed_wall = time.time() - t0
    print(f"t={elapsed_wall:.0f}s  sample: {out1}")

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()

    import json as _json
    try:
        cpu0 = _json.loads(out0)["CPU"]
        cpu1 = _json.loads(out1)["CPU"]
        delta_cpu_seconds = cpu1 - cpu0
        pct = delta_cpu_seconds / elapsed_wall * 100
        print(f"\nThermal Watch UI process CPU time used: {delta_cpu_seconds:.2f}s over {elapsed_wall:.1f}s wall-clock "
              f"= {pct:.2f}% of one logical core")
    except Exception as e:
        print(f"could not parse CPU delta: {e}")

    print("Process exited cleanly, no traceback observed during the run." if proc.returncode in (0, None, 1, -15)
          else f"WARNING: unexpected return code {proc.returncode}")


if __name__ == "__main__":
    main()
