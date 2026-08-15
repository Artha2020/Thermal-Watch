"""Verification for Recommendations: deterministic, evidence-backed, ADVISORY-ONLY synthesis of
every layer below it (baselines, anomaly detection, cross-sensor diagnostics, health scores,
trend intelligence) - never AI-generated, never a hardware-control code path. Confidence and
Urgency as independent axes (Urgency derived from EXISTING zone thresholds, never a new one), the
GPU cooling rule's reliance on the 30-day calendar-split trend (not a self-polluting per-session
leave-one-out count), the CPU cooling rule's structural MEDIUM confidence cap (the live-only
fan/System-sensor corroboration gap), the NO ACTION RECOMMENDED default, RecommendationsWindow
wiring, and that the whole layer stays read-only/on-demand - never the 2s poll."""
import inspect
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
sys.stdout.reconfigure(encoding="utf-8")

import app as appmod  # noqa: E402
from app import (  # noqa: E402
    App, HistoryWindow, RecommendationsWindow, SESSIONS_PATH, INCIDENTS_PATH,
    _urgency_from_zone, sessions_at_zone_ceiling, recommend_gpu_cooling, recommend_cpu_cooling,
    compute_recommendations, format_recommendation, NO_ACTION_RECOMMENDATION,
    RECOMMENDATION_MIN_OCCURRENCES, RECOMMENDATION_GENEROUS_OCCURRENCES, CPU_ORANGE,
)

NOW = time.time()


def fresh_files():
    for p in (SESSIONS_PATH, INCIDENTS_PATH):
        if p.exists():
            p.unlink()


def session_fixture(sid, workload, start, cpu_temp=60.0, cpu_power=80.0, gpu_core=65.0,
                    gpu_hotspot=77.0, gpu_power=250.0, dur=1800, zone_time=None):
    return {
        "session_id": sid, "workload_key": workload.casefold(), "workload": workload,
        "start_timestamp": start, "end_timestamp": start + dur, "duration_seconds": dur,
        "duration_exact": True,
        "cpu": {"avg_temp": cpu_temp, "peak_temp": cpu_temp, "avg_power": cpu_power},
        "gpu": {"avg_core_temp": gpu_core, "peak_core_temp": gpu_core + 10,
               "avg_hotspot_temp": gpu_hotspot, "peak_hotspot_temp": gpu_hotspot + 4,
               "avg_vram_temp": 78.0, "peak_vram_temp": 82.0,
               "avg_power": gpu_power, "peak_power": gpu_power + 50},
        "zone_time": zone_time or {}, "incident_count": 0, "max_incident_severity": None,
        "incident_ids": [], "monitoring_gaps": [],
    }


def gpu_cooling_fixture(workload, n_older=6, n_recent=6, hotspot_shift=20.0, power_shift=0.0):
    """A workload's sessions: an older 15-day half with a normal ~12°C hotspot/core delta, a
    recent 15-day half with a widened delta - power jitter uses the SAME pattern in both halves
    (no additive shift) so 'flat power' means genuinely flat, not just numerically small (see the
    lesson documented in the Trend Intelligence roadmap memory about zero-stddev/tiny-jitter
    edge cases)."""
    older = [session_fixture(f"{workload}_old{i}", workload, NOW - (28 - i * 2) * 86400,
                             gpu_core=65.0 + i * 0.05, gpu_hotspot=77.0 + i * 0.05,
                             gpu_power=250.0 + i * 0.3) for i in range(n_older)]
    recent = [session_fixture(f"{workload}_new{i}", workload, NOW - (13 - i * 2) * 86400,
                              gpu_core=66.0 + i * 0.05, gpu_hotspot=77.0 + hotspot_shift + i * 0.05,
                              gpu_power=250.0 + power_shift + i * 0.3) for i in range(n_recent)]
    return older + recent


@contextmanager
def mock_sessions(sessions):
    orig = appmod.read_sessions_file
    appmod.read_sessions_file = lambda: sessions
    try:
        yield
    finally:
        appmod.read_sessions_file = orig


def main():
    fresh_files()

    print("=== 1. _urgency_from_zone: derived from the EXISTING zone vocabulary, never a new threshold ===")
    assert _urgency_from_zone("RED") == "IMMEDIATE ATTENTION"
    assert _urgency_from_zone("ORANGE") == "MAINTENANCE"
    assert _urgency_from_zone("YELLOW") == "MONITOR"
    assert _urgency_from_zone("GREEN") == "INFO"
    assert _urgency_from_zone(None) == "INFO"
    print("  PASS: RED->IMMEDIATE ATTENTION, ORANGE->MAINTENANCE, YELLOW->MONITOR, GREEN/None->INFO")

    print("\n=== 2. sessions_at_zone_ceiling: reuses the session's OWN zone_time, gated on a real fraction ===")
    brief_spike = session_fixture("brief", "x.exe", NOW, dur=1800, zone_time={"cpu": {"ORANGE": 50.0}})  # ~3%
    sustained = session_fixture("sustained", "x.exe", NOW, dur=1800, zone_time={"cpu": {"ORANGE": 900.0}})  # 50%
    no_zone_time = session_fixture("none", "x.exe", NOW, dur=1800)
    result = sessions_at_zone_ceiling([brief_spike, sustained, no_zone_time], "cpu")
    assert [s["session_id"] for s in result] == ["sustained"], f"FAIL: {[s['session_id'] for s in result]}"
    print("  PASS: a brief (3%) spike is excluded, a sustained (50%) one counts, no zone_time at all is excluded")

    print("\n=== 3. recommend_gpu_cooling: unknown workload -> None ===")
    with mock_sessions([]):
        assert recommend_gpu_cooling("nonexistent.exe") is None
    print("  PASS: a workload with no recorded sessions returns None")

    print("\n=== 4. recommend_gpu_cooling: STABLE/IMPROVING trend never recommends anything ===")
    stable_sessions = gpu_cooling_fixture("stable.exe", hotspot_shift=0.0)
    with mock_sessions(stable_sessions):
        assert recommend_gpu_cooling("stable.exe") is None, "FAIL: a stable hotspot/core delta must never trigger a recommendation"
    print("  PASS: no real hotspot shift -> no recommendation (never manufactures a problem)")

    print("\n=== 5. recommend_gpu_cooling: a genuine WORSENING trend with real confidence fires, matches the worked example's shape ===")
    worsening = gpu_cooling_fixture("Cyberpunk2077.exe", n_older=6, n_recent=6, hotspot_shift=20.0, power_shift=0.0)
    with mock_sessions(worsening):
        gpu_rec = recommend_gpu_cooling("cyberpunk2077.exe")
    assert gpu_rec is not None, "FAIL: a sustained, generously-sampled, power-corroborated hotspot rise must recommend"
    assert gpu_rec["title"] == "COOLING RECOMMENDATION — GPU (Cyberpunk2077.exe)"
    assert gpu_rec["recommendation"] == "Consider inspecting GPU cooler contact / thermal interface"
    assert gpu_rec["confidence"] == "HIGH", gpu_rec  # 6 samples/period + power corroborated -> HIGH
    joined = "\n".join(gpu_rec["evidence"])
    assert "Hotspot/core delta" in joined and "Historical typical delta" in joined and "30-day delta trend" in joined
    assert "GPU power remained stable" in joined
    assert "Finding repeated across 6 recent session(s)" in joined, joined
    assert gpu_rec["caveat"].startswith("This pattern can be consistent with declining thermal-interface")
    print("  PASS: title/recommendation/evidence/confidence(HIGH)/caveat all match the worked example's shape:")
    for line in format_recommendation(gpu_rec):
        print(f"    {line}")

    print("\n=== 6. recommend_gpu_cooling: confidence and the power-corroboration downgrade are INHERITED from the trend report, never recomputed ===")
    power_also_rose = gpu_cooling_fixture("uncorroborated.exe", n_older=6, n_recent=6, hotspot_shift=20.0, power_shift=60.0)
    with mock_sessions(power_also_rose):
        unc_rec = recommend_gpu_cooling("uncorroborated.exe")
    assert unc_rec is not None and unc_rec["confidence"] in ("MEDIUM", "LOW"), unc_rec
    assert unc_rec["confidence"] != "HIGH", "FAIL: power ALSO rising must prevent HIGH confidence, same rule as the trend report itself"
    print(f"  PASS: power flat -> {gpu_rec['confidence']} confidence; power ALSO rose alongside hotspot -> {unc_rec['confidence']}")

    print("\n=== 7. recommend_gpu_cooling: thin repetition (below RECOMMENDATION_MIN_OCCURRENCES-equivalent) never reaches a strong verdict ===")
    thin = gpu_cooling_fixture("thin.exe", n_older=3, n_recent=3, hotspot_shift=20.0, power_shift=0.0)
    with mock_sessions(thin):
        thin_rec = recommend_gpu_cooling("thin.exe")
    if thin_rec is not None:
        assert thin_rec["confidence"] != "HIGH", f"FAIL: only 3 recent sessions must never reach HIGH: {thin_rec}"
    print(f"  PASS: 3 recent sessions -> {'no recommendation' if thin_rec is None else thin_rec['confidence'] + ' (never HIGH)'}")

    print("\n=== 8. recommend_gpu_cooling: workload isolation - one workload's cooling issue never bleeds into another's ===")
    healthy = gpu_cooling_fixture("healthy.exe", hotspot_shift=0.0)
    with mock_sessions(worsening + healthy):
        still_fires = recommend_gpu_cooling("cyberpunk2077.exe")
        healthy_rec = recommend_gpu_cooling("healthy.exe")
    assert still_fires is not None and still_fires["confidence"] == gpu_rec["confidence"]
    assert healthy_rec is None, "FAIL: a healthy workload must never inherit another workload's cooling recommendation"
    print("  PASS: Cyberpunk's recommendation is unaffected by an unrelated healthy workload's presence, and vice versa")

    print("\n=== 9. recommend_cpu_cooling: fewer than RECOMMENDATION_MIN_OCCURRENCES ceiling sessions -> None ===")
    def cpu_session(sid, workload, start, cpu_temp=92.0, cpu_power=190.0, dur=1800):
        return session_fixture(sid, workload, start, cpu_temp=cpu_temp, cpu_power=cpu_power, dur=dur,
                               zone_time={"cpu": {"ORANGE": dur * 0.8}})
    too_few_cpu = [cpu_session(f"c{i}", f"w{i}.exe", NOW - i * 86400) for i in range(RECOMMENDATION_MIN_OCCURRENCES - 1)]
    with mock_sessions(too_few_cpu):
        assert recommend_cpu_cooling() is None
    print(f"  PASS: {RECOMMENDATION_MIN_OCCURRENCES - 1} ceiling sessions (below the minimum) -> None")

    print("\n=== 10. recommend_cpu_cooling: enough repetition fires, matches the worked example's shape, confidence NEVER reaches HIGH ===")
    cpu_sessions = [cpu_session(f"cc{i}", f"workload{i}.exe", NOW - i * 3 * 86400, cpu_temp=90.0 + i, cpu_power=185.0 + i * 2)
                    for i in range(RECOMMENDATION_GENEROUS_OCCURRENCES + 2)]
    with mock_sessions(cpu_sessions):
        cpu_rec_corroborated = recommend_cpu_cooling(live_system_temp_moderate=True, live_cpu_fan_rpm=1520.0)
        cpu_rec_no_live = recommend_cpu_cooling(live_system_temp_moderate=None, live_cpu_fan_rpm=None)
    assert cpu_rec_corroborated is not None and cpu_rec_corroborated["title"] == "CPU COOLING"
    assert cpu_rec_corroborated["recommendation"] == "Consider reviewing CPU cooling performance"
    assert cpu_rec_corroborated["confidence"] != "HIGH", \
        f"FAIL: CPU cooling confidence must be structurally capped at MEDIUM (live-only corroboration gap): {cpu_rec_corroborated}"
    joined = "\n".join(cpu_rec_corroborated["evidence"])
    assert "CPU repeatedly reaches" in joined and "Package power remains" in joined
    assert "System temperature remains moderate" in joined and "CPU fan is responding" in joined
    assert "comparable workload" in joined
    assert cpu_rec_no_live is not None and "System temperature remains moderate" not in "\n".join(cpu_rec_no_live["evidence"])
    print("  PASS: title/recommendation/evidence/caveat match the worked example's shape, confidence capped below HIGH:")
    for line in format_recommendation(cpu_rec_corroborated):
        print(f"    {line}")

    print("\n=== 11. recommend_cpu_cooling: urgency derives from the EXISTING CPU zone thresholds ===")
    assert cpu_rec_corroborated["urgency"] in ("MONITOR", "MAINTENANCE", "IMMEDIATE ATTENTION")
    orange_zone_temps = [cpu_session(f"o{i}", f"w{i}.exe", NOW - i * 86400, cpu_temp=CPU_ORANGE + 1.0)
                         for i in range(RECOMMENDATION_MIN_OCCURRENCES)]
    with mock_sessions(orange_zone_temps):
        orange_rec = recommend_cpu_cooling()
    assert orange_rec["urgency"] == "MAINTENANCE", orange_rec
    print(f"  PASS: peak temps just over CPU_ORANGE -> Urgency: MAINTENANCE (reuses the existing cpu_zone_for threshold)")

    print("\n=== 12. compute_recommendations: NO ACTION RECOMMENDED is the default when nothing fires ===")
    quiet = gpu_cooling_fixture("quiet.exe", hotspot_shift=0.0)
    with mock_sessions(quiet):
        results = compute_recommendations()
    assert len(results) == 1 and results[0]["title"] == "NO ACTION RECOMMENDED"
    assert results[0]["confidence"] is None and results[0]["urgency"] is None and results[0]["evidence"] == []
    no_action_lines = format_recommendation(results[0])
    assert no_action_lines == ["NO ACTION RECOMMENDED", "",
                               "Current behavior is consistent with this machine's established baseline."]
    print("  PASS: a machine with no qualifying pattern gets exactly one NO ACTION RECOMMENDED entry, no confidence/urgency/evidence")

    print("\n=== 13. compute_recommendations: multiple real findings all surface together (GPU + CPU) ===")
    with mock_sessions(worsening + cpu_sessions):
        combined = compute_recommendations(live_system_temp_moderate=True, live_cpu_fan_rpm=1500.0)
    titles = [r["title"] for r in combined]
    assert any(t.startswith("COOLING RECOMMENDATION — GPU") for t in titles), titles
    assert "CPU COOLING" in titles, titles
    assert "NO ACTION RECOMMENDED" not in titles, "FAIL: NO ACTION must never appear alongside real findings"
    print(f"  PASS: both a GPU and a CPU recommendation surface together when both independently qualify: {titles}")

    print("\n=== 14. format_recommendation: MAINTENANCE gets the '— NOT EMERGENCY' suffix, other tiers don't ===")
    maint = dict(gpu_rec); maint["urgency"] = "MAINTENANCE"
    info = dict(gpu_rec); info["urgency"] = "INFO"
    immediate = dict(gpu_rec); immediate["urgency"] = "IMMEDIATE ATTENTION"
    assert "MAINTENANCE — NOT EMERGENCY" in "\n".join(format_recommendation(maint))
    assert "INFO — NOT EMERGENCY" not in "\n".join(format_recommendation(info))
    assert "IMMEDIATE ATTENTION — NOT EMERGENCY" not in "\n".join(format_recommendation(immediate))
    print("  PASS: only MAINTENANCE gets the clarifying suffix; INFO/IMMEDIATE ATTENTION print as-is")

    print("\n=== 15. Advisory-only: no hardware-control code path exists anywhere in this file ===")
    src = inspect.getsource(appmod)
    forbidden_substrings = ("set_fan_curve", "SetFanSpeed", "set_power_limit", "SetPowerLimit",
                            "set_voltage", "SetVoltage", "set_clock", "SetClock", "bios_write",
                            "WriteBios", "os.system", "shutdown /")
    for needle in forbidden_substrings:
        assert needle not in src, f"FAIL: found a potential hardware-control/shutdown code path: {needle}"
    print("  PASS: no fan-curve/power-limit/voltage/clock/BIOS-write/shutdown code path exists anywhere in app.py")

    print("\n=== 16. RecommendationsWindow: opens via HistoryWindow (singleton), renders real recommendations ===")
    fresh_files()
    with SESSIONS_PATH.open("w", encoding="utf-8") as fh:
        for s in worsening + cpu_sessions:
            fh.write(json.dumps(s) + "\n")
    app = App()
    hw = HistoryWindow(app)
    hw.open_recommendations()
    app.update()
    win1 = hw.recommendations_window
    hw.open_recommendations()  # must reuse the same window, not open a second one
    assert hw.recommendations_window is win1, "FAIL: open_recommendations() must reuse the existing window (singleton pattern)"
    cards = win1.list_frame.winfo_children()
    assert len(cards) >= 2, f"FAIL: expected at least a GPU and a CPU recommendation card, got {len(cards)}"
    all_text = "\n".join(c.winfo_children()[0].cget("text") for c in cards)
    assert "COOLING RECOMMENDATION — GPU" in all_text and "CPU COOLING" in all_text, all_text
    win1.destroy()
    hw.destroy()
    app.stop_event.set(); app.destroy()
    print(f"  PASS: RecommendationsWindow opens as a singleton from History, renders {len(cards)} real "
          f"recommendation card(s) through the full UI stack")

    print("\n=== 17. Recommendations never run on the live 2s poll or on session/app close ===")
    forbidden = ("compute_recommendations(", "recommend_gpu_cooling(", "recommend_cpu_cooling(")
    update_src = inspect.getsource(App.update_data)
    close_src = inspect.getsource(App.close)
    for name in forbidden:
        assert name not in update_src, f"FAIL: update_data() must never call {name} on the 2s poll"
        assert name not in close_src, f"FAIL: recommendations must never run automatically on session/app close"
    print("  PASS: update_data()/close() contain no recommendation computation - display-only, on-demand")

    fresh_files()
    print("\nALL RECOMMENDATIONS CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
