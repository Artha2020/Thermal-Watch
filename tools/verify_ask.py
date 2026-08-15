"""Verification for Ask Thermal Watch: deterministic question parsing (time windows, components,
intents), answers assembled only from stored records, the non-causal rule held at the one surface
most tempted to break it ("why did my PC run hot"), honest refusal of questions it cannot answer,
coverage/gap caveats, and the absence of any generative or network component."""
import ast
import inspect
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
sys.stdout.reconfigure(encoding="utf-8")

import app as appmod  # noqa: E402
from app import (  # noqa: E402
    App, HistoryWindow, AskWindow, ASK_EXAMPLES, ASK_NIGHT_START_HOUR, ASK_NIGHT_END_HOUR,
    ASK_DEFAULT_WINDOW_SECONDS, REPORT_MIN_COVERAGE_PCT, TELEMETRY_BUCKET_SECONDS,
    parse_time_window, parse_component, classify_question, answer_question,
    previous_completed_period, period_bounds,
)

NOW = time.mktime(datetime(2026, 8, 13, 14, 30).timetuple())
DAY = 86400.0


def bucket(ts, cpu_temp=55.0, gpu_hotspot=70.0):
    def agg(v):
        return {"avg": v, "min": v - 1, "max": v + 5, "count": 30}
    return {"start_timestamp": ts, "end_timestamp": ts + TELEMETRY_BUCKET_SECONDS, "sample_count": 30,
           "scalars": {"cpu_temp": agg(cpu_temp), "gpu_hotspot_temp": agg(gpu_hotspot)}, "sensors": {}}


def fill(start, end, **kw):
    out, ts = [], start
    while ts < end:
        out.append(bucket(ts, **kw))
        ts += TELEMETRY_BUCKET_SECONDS
    return out


def incident(iid, component, start, zone="ORANGE", dur=600, workload="game.exe"):
    return {"incident_id": iid, "component": component, "start_timestamp": start,
           "end_timestamp": start + dur, "duration_seconds": dur, "duration_exact": True,
           "max_zone": zone, "peak_value": 96.0, "dominant_workload": workload, "monitoring_gaps": []}


def sess(sid, workload, start, dur=3600, cpu_peak=82.0, gpu_peak=93.0):
    return {"session_id": sid, "workload_key": workload.casefold(), "workload": workload,
           "start_timestamp": start, "end_timestamp": start + dur, "duration_seconds": dur,
           "duration_exact": True,
           "cpu": {"avg_temp": 70.0, "peak_temp": cpu_peak, "avg_power": 100.0},
           "gpu": {"avg_core_temp": 72.0, "avg_hotspot_temp": 85.0, "peak_hotspot_temp": gpu_peak,
                  "avg_power": 260.0},
           "zone_time": {}, "incident_count": 1, "monitoring_gaps": []}


class mock:
    def __init__(self, buckets=(), incidents=(), sessions=()):
        self.b, self.i, self.s = list(buckets), list(incidents), list(sessions)

    def __enter__(self):
        self.orig = (appmod.read_telemetry_file, appmod.read_incidents_file, appmod.read_sessions_file,
                     appmod.read_experiments_file, appmod.read_event_log_file)
        appmod.read_telemetry_file = lambda since_ts=None, sensor_key=None: [
            x for x in self.b if since_ts is None or x["start_timestamp"] >= since_ts]
        appmod.read_incidents_file = lambda: self.i
        appmod.read_sessions_file = lambda: self.s
        appmod.read_experiments_file = lambda: []
        appmod.read_event_log_file = lambda: []
        return self

    def __exit__(self, *exc):
        (appmod.read_telemetry_file, appmod.read_incidents_file, appmod.read_sessions_file,
         appmod.read_experiments_file, appmod.read_event_log_file) = self.orig


def main():
    print("=== 1. 'Last night' has a STATED convention rather than a silent assumption ===")
    start, end, label, explicit = parse_time_window("why was it hot last night", NOW)
    assert explicit is True
    assert datetime.fromtimestamp(start).hour == ASK_NIGHT_START_HOUR
    assert datetime.fromtimestamp(start).date() == datetime.fromtimestamp(NOW).date() - timedelta(days=1)
    assert datetime.fromtimestamp(end).hour == ASK_NIGHT_END_HOUR
    assert "18:00 yesterday to 06:00 today" in label, label
    print(f"  PASS: 'last night' -> {label}, and the window it used is spelled out in the answer")

    print("\n=== 2. Calendar phrases resolve through the SAME machinery the reports layer uses ===")
    lw_start, lw_end, lw_label, _ = parse_time_window("any incidents last week?", NOW)
    week = previous_completed_period("WEEKLY", NOW)
    assert (lw_start, lw_end) == (week["start_ts"], week["end_ts"]), (lw_start, week["start_ts"])
    y_start, y_end, _, _ = parse_time_window("what happened yesterday", NOW)
    yesterday = period_bounds("DAILY", datetime.fromtimestamp(NOW).date() - timedelta(days=1))
    assert (y_start, y_end) == (yesterday["start_ts"], yesterday["end_ts"])
    lm_start, lm_end, _, _ = parse_time_window("last month", NOW)
    month = previous_completed_period("MONTHLY", NOW)
    assert (lm_start, lm_end) == (month["start_ts"], month["end_ts"])
    print(f"  PASS: 'last week' is the same Monday-anchored week the reports layer reports "
          f"({lw_label}); yesterday and last month match their report periods exactly")

    print("\n=== 3. Relative windows, and an UNSTATED period is flagged rather than assumed silently ===")
    s, e, label3, explicit3 = parse_time_window("which apps ran hottest in the last 3 days", NOW)
    assert explicit3 is True and abs((e - s) - 3 * DAY) < 1e-6, (s, e)
    s2, e2, label2, explicit2 = parse_time_window("what do you recommend", NOW)
    assert explicit2 is False and abs((e2 - s2) - ASK_DEFAULT_WINDOW_SECONDS) < 1e-6
    with mock():
        default_answer = answer_question("what do you recommend", now=NOW)
    assert any("No period given" in ln for ln in default_answer["lines"]), default_answer["lines"][:3]
    print(f"  PASS: 'last 3 days' -> {label3}; with no period named the answer opens by saying it "
          f"assumed {label2}")

    print("\n=== 4. Component parsing: longest phrase wins, and word boundaries are respected ===")
    assert parse_component("how is my video card doing") == "gpu"
    assert parse_component("cpu temperature") == "cpu"
    assert parse_component("what about the nvme drive") == "drive"
    assert parse_component("how is memory") == "ram"
    assert parse_component("how is everything") is None
    assert parse_component("scpus and ramifications") is None, \
        "FAIL: substrings inside other words must not match a component"
    print("  PASS: 'video card'->gpu, 'nvme'->drive, 'memory'->ram, whole-machine question->None, and "
          "'ramifications' does not match RAM")

    print("\n=== 5. Intent classification, including the honest None ===")
    cases = {"why did my pc run hot last night": "explain",
            "what happened yesterday": "timeline",
            "how is my gpu doing": "status",
            "were there any incidents last month": "incidents",
            "which apps ran hottest": "workloads",
            "is my cpu getting worse over time": "trend",
            "what do you recommend": "recommendations"}
    for question, expected in cases.items():
        assert classify_question(question) == expected, f"{question!r} -> {classify_question(question)}"
    for nonsense in ("make me a sandwich", "", "asdfghjkl", "what is the capital of France"):
        assert classify_question(nonsense) is None, nonsense
    print(f"  PASS: all {len(cases)} intents classify correctly; unrelated questions return None rather "
          f"than being routed to the nearest handler")

    print("\n=== 6. An unanswerable question says so and lists what it CAN do - it never guesses ===")
    with mock():
        unknown = answer_question("what is the capital of France", now=NOW)
    assert unknown["intent"] is None and unknown["evidence_counts"] == {}
    text = "\n".join(unknown["lines"])
    assert "I don't know how to answer that" in text
    assert all(example in text for example in ASK_EXAMPLES)
    assert "France" not in text, "FAIL: it must not attempt to engage with a question outside its data"
    print("  PASS: returns an explicit 'I don't know how to answer that' plus the full capability list")

    print("\n=== 7. THE non-causal rule at the 'why' surface ===")
    night_start = time.mktime(datetime(2026, 8, 12, ASK_NIGHT_START_HOUR).timetuple())
    night_end = time.mktime(datetime(2026, 8, 13, ASK_NIGHT_END_HOUR).timetuple())
    incidents = [incident("i1", "gpu_hotspot", night_start + 3600, zone="RED"),
                incident("i2", "cpu", night_start + 7200)]
    sessions = [sess("s1", "Cyberpunk2077.exe", night_start + 1800)]
    with mock(buckets=fill(night_start, night_end, cpu_temp=78.0, gpu_hotspot=94.0),
              incidents=incidents, sessions=sessions):
        why = answer_question("why did my PC run hot last night?", now=NOW)
    why_text = "\n".join(why["lines"])
    assert why["intent"] == "explain"
    assert "cannot determine from software telemetry alone what caused it" in why_text, why_text
    assert "associated with it, not shown to be the cause of it" in why_text
    for causal in ("caused by", "because of", "due to", "the cause was", "blame"):
        assert causal not in why_text.lower(), f"FAIL: causal language '{causal}' in a 'why' answer"
    assert "Cyberpunk2077.exe" in why_text and "2 thermal incident(s)" in why_text
    print("  PASS: the 'why' answer reports peaks, incidents and active workloads, labels the workloads "
          "as associated rather than causal, and states outright that it cannot determine a cause:")
    for line in why["lines"][:8]:
        print(f"    {line}")

    print("\n=== 8. Every answer names the records it drew on ===")
    assert why["evidence_counts"]["incidents"] == 2 and why["evidence_counts"]["sessions"] == 1
    assert "Based on 2 incident record(s), 1 session record(s)" in why_text, why_text
    print(f"  PASS: the answer closes with its provenance - {why['evidence_counts']}")

    print("\n=== 9. Component questions filter the evidence to that component ===")
    with mock(buckets=fill(night_start, night_end), incidents=incidents, sessions=sessions):
        gpu_only = answer_question("were there any gpu incidents last night", now=NOW)
        cpu_only = answer_question("were there any cpu incidents last night", now=NOW)
    assert gpu_only["component"] == "gpu" and gpu_only["evidence_counts"]["incidents"] == 1
    assert cpu_only["component"] == "cpu" and cpu_only["evidence_counts"]["incidents"] == 1
    assert "GPU Hotspot" in "\n".join(gpu_only["lines"])
    assert "GPU Hotspot" not in "\n".join(cpu_only["lines"])
    print("  PASS: a GPU question sees only the GPU incident and a CPU question only the CPU one")

    print("\n=== 10. Nothing recorded is answered as nothing recorded - never filled in ===")
    with mock():
        empty = answer_question("were there any incidents yesterday", now=NOW)
    empty_text = "\n".join(empty["lines"])
    assert "No thermal incidents were recorded" in empty_text
    assert "Based on 0 incident record(s), 0 session record(s) and 0 minute(s) of telemetry" in empty_text
    print("  PASS: an empty store yields an explicit 'no incidents recorded' and a zeroed provenance line")

    print("\n=== 11. Low coverage and monitoring gaps are surfaced, not glossed over ===")
    sparse = fill(night_start, night_start + 1800)  # ~30 min of a 12h window
    with mock(buckets=sparse, incidents=incidents, sessions=sessions):
        sparse_answer = answer_question("why did my pc run hot last night", now=NOW)
    sparse_text = "\n".join(sparse_answer["lines"])
    assert "only monitored" in sparse_text and "may not be the whole picture" in sparse_text, sparse_text
    assert "Not monitored:" in sparse_text
    assert sparse_answer["evidence_counts"]["monitoring_gaps"] >= 1
    print("  PASS: a 12-hour question answered from ~30 minutes of telemetry states its coverage and "
          "names the unmonitored stretch")

    print("\n=== 12. Determinism: the same question against the same data gives the same answer ===")
    with mock(buckets=fill(night_start, night_end), incidents=incidents, sessions=sessions):
        first = answer_question("why did my PC run hot last night?", now=NOW)
        second = answer_question("why did my PC run hot last night?", now=NOW)
    assert first["lines"] == second["lines"], "FAIL: answers must be reproducible"
    print("  PASS: repeated identical questions produce byte-identical answers - the layer is checkable")

    print("\n=== 13. No model, no network, no generation - and no reimplemented analysis ===")
    src = inspect.getsource(appmod)
    start_idx = src.index("# Ask Thermal Watch")
    layer = src[start_idx:src.index("class MEMORYSTATUSEX", start_idx)]
    for banned in ("import torch", "import openai", "anthropic", "llama", "transformers", "urllib",
                   "requests.", "socket.", "http.client", "subprocess"):
        assert banned not in layer, f"FAIL: the ask layer reaches for {banned}"
    called = {n.func.id for n in ast.walk(ast.parse(layer))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for banned in ("_stat_summary", "evaluate_anomaly", "compare_period_values"):
        assert banned not in called, f"FAIL: the ask layer recomputes statistics: {banned}()"
    for required in ("compute_recommendations", "build_timeline", "compute_idle_metric_period_trend",
                     "overlapping_incidents", "overlapping_sessions", "compute_coverage"):
        assert required in called, f"FAIL: expected the ask layer to reuse {required}()"
    print(f"  PASS: no model/network/subprocess imports; it calls {len(called)} existing helpers "
          f"(recommendations, timeline, trends, coverage) and recomputes no statistics of its own")

    print("\n=== 14. AskWindow: singleton from History, answers, and never touches the 2s poll ===")
    app = App()
    hw = HistoryWindow(app)
    hw.open_ask()
    app.update()
    win = hw.ask_window
    hw.open_ask()
    assert hw.ask_window is win, "FAIL: open_ask() must reuse the existing window (singleton)"
    assert "Ask about anything Thermal Watch has recorded" in win.answer_text.cget("text")
    win._ask("why did my pc run hot last night?")
    app.update()
    answered = win.answer_text.cget("text")
    assert answered.startswith("Q: why did my pc run hot last night?")
    assert "cannot determine from software telemetry alone" in answered
    win._ask("make me a sandwich")
    app.update()
    assert "I don't know how to answer that" in win.answer_text.cget("text")
    win.question_var.set("")
    win._ask()
    app.update()
    assert "Ask about anything" in win.answer_text.cget("text"), "an empty question returns to the welcome"
    update_src = inspect.getsource(App.update_data)
    for name in ("answer_question(", "classify_question(", "_window_evidence("):
        assert name not in update_src, f"FAIL: update_data() must never call {name}"
    win.destroy(); hw.destroy()
    app.stop_event.set(); app.destroy()
    print("  PASS: singleton from History, answers a real question through the full UI stack, refuses an "
          "unanswerable one, resets on empty input, and does no work on the live poll")

    print("\nALL ASK THERMAL WATCH CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
