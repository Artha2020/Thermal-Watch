"""Static release-hygiene gate for the public repository.

This intentionally avoids hardware access. It catches public-release regressions that
should never require a GPU, UAC prompt, or live sensor bridge to detect.
"""
from __future__ import annotations

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
failures: list[str] = []


def check(name: str, condition: bool) -> None:
    print(f"[{'PASS' if condition else 'FAIL'}] {name}")
    if not condition:
        failures.append(name)


app = (ROOT / "app.py").read_text(encoding="utf-8")
launch = (ROOT / "launch.ps1").read_text(encoding="utf-8")
spec = (ROOT / "ThermalWatch.spec").read_text(encoding="utf-8")
readme = (ROOT / "README.md").read_text(encoding="utf-8")

check("public app version is v1.1.1", 'APP_VERSION = "1.1.1"' in app)
check("launcher targets the PyInstaller output directory",
      "dist\\ThermalWatch\\ThermalWatch.exe" in launch)
check("stale ThermalWatchSafe output path is absent",
      "ThermalWatchSafe" not in launch)
check("v3 bridge is packaged", "sensor_bridge_v3.ps1" in spec)
check("sensor snapshot helper is packaged", "sensor_snapshot.ps1" in spec)
check("unused PresentMon sampler is absent",
      "IntelPresentMonTemperatureSampler" not in app and "PresentMonAPI2.dll" not in app)
check("Arc limit is driver-derived when available",
      'intel_sampler.max_temp' in app and 'DRIVER THERMAL LIMIT UNAVAILABLE' in app)
check("public build has no hardcoded electricity tariff",
      re.search(r"ELECTRICITY_RATE_MXN_PER_KWH\s*=\s*\d", app) is None)
check("public build has no hardcoded monitor wattage",
      re.search(r"MONITOR_ESTIMATED_WATTS\s*=\s*\d", app) is None)
check("cost assumptions are opt-in via environment",
      "THERMAL_WATCH_ELECTRICITY_RATE_MXN_PER_KWH" in app
      and "THERMAL_WATCH_MONITOR_ESTIMATED_WATTS" in app)
check("README documents Intel Arc support", "Intel Arc GPU core temperature" in readme)
check("README documents opt-in cost configuration", "Optional local cost assumptions" in readme)

if failures:
    print("\nFAILED:")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)

print("\nPUBLIC RELEASE HYGIENE PASSED")
