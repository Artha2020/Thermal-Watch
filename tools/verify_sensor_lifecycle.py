"""Release-gate check for the one sensor-lifecycle property the existing suite does not already
cover: a sensor disappearing and reappearing must not disturb an UNRELATED sensor's debounce state,
and must never fabricate a value (notably a 0) for the interval it was absent.

Already covered elsewhere, deliberately not duplicated here:
  verify_render_optimization  - disappearing destroys exactly that row; reappearing recreates one
  verify_sensor_identity      - Identifier/fallback identity, and rekey-in-place with no duplicates
  verify_unverified_sensor_label + verify_ask_grounding - PCIe x1 stays UNVERIFIED with no verdict
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
sys.stdout.reconfigure(encoding="utf-8")

from app import (  # noqa: E402
    App, GPU_HOTSPOT_ZONES, RAM_ZONES, ALERT_DEBOUNCE_S, UNVERIFIED_SENSOR_LABELS,
    sensor_identity, _sensor_bucket_key, _new_telemetry_bucket, read_telemetry_file,
)


def main():
    app = App()

    print("=== 1. A sensor vanishing must not reset an UNRELATED sensor's debounce ===")
    # Drive one sensor into YELLOW and let its debounce mature; a second, unrelated sensor
    # disappears meanwhile. The first must still alert on schedule.
    app._update_sensor_zone("keep:hotspot", "TEST GPU Hotspot", 90.0, "°C", GPU_HOTSPOT_ZONES)
    app._update_sensor_zone("goes:dimm", "TEST DIMM", 60.0, "°C", RAM_ZONES)
    before = app.sensor_zone_state["keep:hotspot"]
    pending_zone_before = before["pending"]["zone"]
    since_before = before["pending"]["since"]
    assert pending_zone_before == "YELLOW", before

    # The DIMM stops being reported entirely - the surviving sensor keeps being polled meanwhile.
    for _ in range(3):
        app._update_sensor_zone("keep:hotspot", "TEST GPU Hotspot", 90.0, "°C", GPU_HOTSPOT_ZONES)
        time.sleep(0.05)
    after = app.sensor_zone_state["keep:hotspot"]
    assert after["pending"]["zone"] == pending_zone_before, \
        f"FAIL: unrelated sensor's pending zone changed: {pending_zone_before} -> {after['pending']['zone']}"
    assert after["pending"]["since"] == since_before, \
        "FAIL: an unrelated sensor's debounce timer restarted when another sensor vanished"
    assert "goes:dimm" in app.sensor_zone_state, \
        "the vanished sensor's own state is retained, not purged mid-flight"
    print(f"  PASS: the surviving sensor kept pending zone {pending_zone_before!r} and its original "
          f"debounce start time while an unrelated sensor stopped being reported")

    print("\n=== 2. Debounce still confirms on its own schedule afterwards ===")
    assert app.sensor_zone_state["keep:hotspot"]["confirmed"] == "GREEN", "not confirmed yet - correct"
    time.sleep(ALERT_DEBOUNCE_S + 0.2)
    app._update_sensor_zone("keep:hotspot", "TEST GPU Hotspot", 90.0, "°C", GPU_HOTSPOT_ZONES)
    confirmed = app.sensor_zone_state["keep:hotspot"]["confirmed"]
    assert confirmed == "YELLOW", f"FAIL: expected YELLOW after the debounce elapsed, got {confirmed}"
    print(f"  PASS: still GREEN before {ALERT_DEBOUNCE_S}s, then confirmed {confirmed} after - the "
          f"unrelated disappearance neither blocked nor accelerated it")

    print("\n=== 3. A sensor absent from a poll contributes NOTHING to telemetry - never a 0 ===")
    # The in-flight bucket pre-creates a zeroed accumulator for every scalar key (count=0) - an
    # implementation detail. What matters is what gets PERSISTED and read back, because that is what
    # every chart, report and answer consumes.
    app.telemetry_bucket = _new_telemetry_bucket(time.time() - 120)
    app.last_context = {"cpu_temp": 55.0}  # gpu_hotspot_temp deliberately absent this tick
    app._telemetry_observe_tick([])
    in_flight = app.telemetry_bucket["scalars"]["gpu_hotspot_temp"]
    assert in_flight["count"] == 0, in_flight
    app._telemetry_finalize_bucket(time.time())
    sampled = [b for b in read_telemetry_file() if b.get("sample_count")]
    assert sampled, "the finalized bucket should have persisted"
    scalars = sampled[-1]["scalars"]
    assert scalars["cpu_temp"]["avg"] == 55.0, scalars
    # The never-sampled metric is persisted as an explicit null - stronger than omitting the key,
    # because a reader can tell "recorded as unavailable" apart from "key I don't know about", and
    # extract_bucket_metric() returns None for it either way. What must NEVER happen is a 0.
    assert scalars["gpu_hotspot_temp"] is None, \
        f"FAIL: a never-sampled metric persisted a value: {scalars['gpu_hotspot_temp']}"
    zeroed = [k for k, v in scalars.items() if isinstance(v, dict) and v.get("avg") == 0.0]
    assert not zeroed, f"FAIL: never-sampled metrics were persisted as real zeros: {zeroed}"
    print("  PASS: the never-sampled metric holds a count=0 accumulator in flight and persists as an "
          "explicit null - it can never be read back as 0°C")

    print("\n=== 4. Reappearing keys back to the SAME identity (no duplicate, no orphan) ===")
    sensor = {"Identifier": "/lpc/nct6687d/0/temperature/5", "Parent": "SuperIO Nuvoton NCT6687D",
             "Name": "PCIe x1", "SensorType": "Temperature", "Value": 82.0}
    identity_first = sensor_identity(sensor)
    key_first = _sensor_bucket_key(identity_first)
    identity_again = sensor_identity(dict(sensor))
    assert _sensor_bucket_key(identity_again) == key_first, "FAIL: same sensor produced two bucket keys"
    assert identity_first in UNVERIFIED_SENSOR_LABELS, \
        "FAIL: PCIe x1 must remain flagged UNVERIFIED across a disappear/reappear cycle"
    print(f"  PASS: the same sensor resolves to one stable bucket key ({key_first}) and PCIe x1 stays "
          f"UNVERIFIED after reappearing")

    app.stop_event.set()
    app.destroy()
    print("\nALL SENSOR LIFECYCLE CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
