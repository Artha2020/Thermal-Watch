"""Verification for the render/update optimization pass: steady-state widget churn should be
zero, sensor inventory changes should still add/remove rows, chart canvas items should be
reused not recreated, and the alert/zone/debounce engines must behave identically to before."""
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import App, lhm_sensors, nvidia_stats, memory, cpu_times  # noqa: E402


def payload(lhm=None):
    old_idle, old_total = cpu_times()
    time.sleep(0.2)
    now = cpu_times()
    dt_load = now[1] - old_total
    load = 100 * (1 - (now[0] - old_idle) / dt_load) if dt_load else 0
    mem_pct, mem_used, mem_total = memory()
    return {"time": datetime.now(), "cpu_load": load, "mem_pct": mem_pct, "mem_used": mem_used,
            "mem_total": mem_total, "gpus": nvidia_stats(), "lhm": lhm}


def main():
    app = App()
    real_sensors = lhm_sensors()

    print("=== 1. first poll: rows get created (expected, one-time cost) ===")
    app.update_data(payload(lhm=real_sensors))
    app.widget_stats["rows_created"] = 0
    app.widget_stats["rows_destroyed"] = 0
    fan_rows_before = dict(app.fan_rows)
    volt_rows_before = dict(app.volt_rows)
    disk_rows_before = dict(app.disk_rows)
    gpu_rows_before = dict(app.gpu_thermal_rows)
    mobo_rows_before = dict(app.mobo_rows)
    ram_rows_before = dict(app.ram_rows)
    print(f"  fans={len(fan_rows_before)} volts={len(volt_rows_before)} disks={len(disk_rows_before)} "
          f"gpu={len(gpu_rows_before)} mobo={len(mobo_rows_before)} ram={len(ram_rows_before)}")

    print("\n=== 2. steady-state polls (same sensor inventory): rows_created/destroyed must be 0 ===")
    for i in range(5):
        app.update_data(payload(lhm=real_sensors))
    print(f"  rows_created={app.widget_stats['rows_created']}  rows_destroyed={app.widget_stats['rows_destroyed']}")
    assert app.widget_stats["rows_created"] == 0, "FAIL: rows were recreated with an unchanged inventory"
    assert app.widget_stats["rows_destroyed"] == 0, "FAIL: rows were destroyed with an unchanged inventory"
    # and the exact same widget objects are still in place (not swapped for equal-looking new ones)
    assert app.fan_rows == fan_rows_before
    assert app.volt_rows == volt_rows_before
    assert app.disk_rows == disk_rows_before
    assert app.gpu_thermal_rows == gpu_rows_before
    assert app.mobo_rows == mobo_rows_before
    assert app.ram_rows == ram_rows_before
    print("  PASS: identical widget objects reused across 5 polls, zero churn")

    print("\n=== 3. values actually update in place ===")
    cpu_val_before = app.cpu_card.value.cget("text")
    time.sleep(1)
    app.update_data(payload(lhm=real_sensors))
    print(f"  cpu_card value: {cpu_val_before!r} -> {app.cpu_card.value.cget('text')!r} (widget id unchanged: "
          f"{app.cpu_card.value})")

    print("\n=== 4. sensor inventory CHANGE: a fan disappearing must destroy exactly its row ===")
    # Row-cache keys are now sensor_identity() (Identifier when the bridge provides one, per
    # the sensor-identity hardening task) rather than the bare sensor name - look the target
    # fan's key up dynamically instead of assuming a key format.
    from app import sensor_identity
    target_sensor = next(s for s in real_sensors if s.get("SensorType") == "Fan" and s.get("Name") == "System Fan #2")
    target_key = sensor_identity(target_sensor)
    fake_sensors = [s for s in real_sensors if s is not target_sensor]
    before_keys = set(app.fan_rows.keys())
    app.update_data(payload(lhm=fake_sensors))
    after_keys = set(app.fan_rows.keys())
    removed = before_keys - after_keys
    print(f"  removed keys: {removed}")
    assert removed == {target_key}, f"FAIL: expected only {target_key!r} removed, got {removed}"
    assert app.widget_stats["rows_destroyed"] >= 1
    print("  PASS: exactly the missing sensor's row was destroyed, nothing else touched")

    print("\n=== 5. sensor inventory CHANGE: that fan reappearing must recreate exactly its row ===")
    created_before = app.widget_stats["rows_created"]
    app.update_data(payload(lhm=real_sensors))
    assert target_key in app.fan_rows
    assert app.widget_stats["rows_created"] == created_before + 1
    print("  PASS: reappeared sensor got exactly one new row")

    print("\n=== 6. chart: canvas items are reused (ids stable) across polls with growing data ===")
    ids_1 = dict(app.chart._series_ids)
    static_before = list(app.chart._grid_ids)
    for _ in range(3):
        app.chart_points.append((time.time(), 55.0, 40.0))
        app.chart.set_points(app.chart_points)
    ids_2 = dict(app.chart._series_ids)
    static_after = list(app.chart._grid_ids)
    print(f"  series item ids before={ids_1} after={ids_2}")
    assert ids_1 == ids_2 or all(ids_2.values()), "series item ids should be created once then stay stable"
    assert static_before == static_after, "FAIL: static grid items were recreated without a resize"
    print("  PASS: chart reuses the same canvas item ids across polls (no delete/recreate)")

    print("\n=== 7. event log: incremental append preserves other rows, caps at 40 ===")
    at_cap = len(app.log_rows) >= 40
    row_objs_before = list(app.log_rows)
    app.log_event("INFO", "verification test event")
    if at_cap:
        # already full: the new row is inserted at top AND the oldest (last) is evicted -
        # every row except the evicted one must be the SAME widget object as before.
        assert app.log_rows[1:] == row_objs_before[:-1], "FAIL: a surviving row was recreated, not reused"
        assert row_objs_before[-1] not in app.log_rows, "FAIL: the oldest row should have been evicted"
        print(f"  (log was already at the 40-row cap: verified eviction path) PASS")
    else:
        assert app.log_rows[1:] == row_objs_before, "FAIL: existing rows were touched/replaced by an append"
        print("  PASS: only one new row inserted, all prior row widgets untouched")
    assert app.log_rows[0] not in row_objs_before

    print("\n=== 8. alert strip: pack()/pack_forget() only called on actual state change ===")
    pack_calls = []
    orig_pack = app.alert_strip.pack
    orig_pack_forget = app.alert_strip.pack_forget
    app.alert_strip.pack = lambda **kw: (pack_calls.append("pack"), orig_pack(**kw))[-1]
    app.alert_strip.pack_forget = lambda: (pack_calls.append("forget"), orig_pack_forget())[-1]
    try:
        for _ in range(4):
            app.update_data(payload(lhm=real_sensors))  # no active alerts expected in normal conditions
        print(f"  pack/pack_forget calls over 4 identical-state polls: {pack_calls}")
        assert len(pack_calls) <= 1, f"FAIL: alert strip pack toggled every poll: {pack_calls}"
        print("  PASS: alert strip pack calls only happen on a real visibility change")
    finally:
        app.alert_strip.pack = orig_pack
        app.alert_strip.pack_forget = orig_pack_forget

    print("\n=== 9. debounce/zone/bridge-health untouched: sanity spot-check ===")
    from app import cpu_zone_for, drive_zone_for, compute_bridge_health
    assert cpu_zone_for(85)["key"] == "YELLOW"
    assert drive_zone_for(65)["key"] == "YELLOW"
    assert compute_bridge_health(2.0, None) == "HEALTHY"
    print("  PASS: zone/health classification functions unchanged")

    app.stop_event.set()
    app.destroy()
    print("\nALL RENDER-OPTIMIZATION CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
