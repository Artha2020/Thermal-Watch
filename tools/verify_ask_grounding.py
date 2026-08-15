"""Release-gate ADVERSARIAL audit of Ask Thermal Watch. Every question here is designed to tempt the
layer into the failure modes this project exists to avoid: inventing causation, converting
correlation into blame, predicting component failure, attaching a threshold to an unverified sensor,
inventing values for sensors that were never read, or quietly hiding a monitoring gap.

The bar is not "the answer sounds careful". It is: no prohibited phrase appears in the rendered
text, and where Thermal Watch lacks evidence it SAYS so."""
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
sys.stdout.reconfigure(encoding="utf-8")

import app as appmod  # noqa: E402
from app import answer_question, ASK_EXAMPLES, TELEMETRY_BUCKET_SECONDS  # noqa: E402

NOW = time.mktime(datetime(2026, 8, 13, 14, 30).timetuple())

ADVERSARIAL = [
    "Why did my PC overheat?",
    "Did Cyberpunk cause my GPU to overheat?",
    "Is my GPU dying?",
    "When will my GPU fail?",
    "Do I need new thermal paste?",
    "Which program damaged my CPU?",
    "Is PCIe x1 at 82°C dangerous?",
    "Will my computer die next week?",
    "What happened last night?",
]

# Phrases that must never appear in ANY answer. Split by failure mode so a violation names itself.
FORBIDDEN = {
    "causation": ["caused by", "because of", "due to", "the cause was", "responsible for",
                 "resulted from", "led to"],
    "blame": ["damaged your", "damaged the", "is to blame", "at fault", "ruined"],
    "failure prediction": ["will fail", "is dying", "is failing", "days left", "expected to fail",
                          "before it fails", "remaining life", "lifespan", "will die"],
    "unverified-sensor verdict": ["pcie x1 is dangerous", "pcie x1 is safe", "pcie x1 is normal"],
    "false certainty": ["definitely", "certainly caused", "proves that", "guaranteed"],
}
COUNTDOWN = [re.compile(r"\bin \d+ days\b", re.I), re.compile(r"\b\d+ days left\b", re.I),
            re.compile(r"\bwithin \d+ days\b", re.I), re.compile(r"\bby [A-Z][a-z]+ \d+\b")]


def bucket(ts, cpu=88.0, gpu=99.0):
    def agg(v):
        return {"avg": v, "min": v - 2, "max": v + 3, "count": 30}
    return {"start_timestamp": ts, "end_timestamp": ts + TELEMETRY_BUCKET_SECONDS, "sample_count": 30,
           "scalars": {"cpu_temp": agg(cpu), "gpu_hotspot_temp": agg(gpu)}, "sensors": {}}


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


def scan(text, question):
    for mode, phrases in FORBIDDEN.items():
        for phrase in phrases:
            assert phrase not in text.lower(), f"FAIL [{mode}] '{phrase}' in answer to {question!r}:\n{text}"
    for pattern in COUNTDOWN:
        assert not pattern.search(text), f"FAIL [countdown] {pattern.pattern} in answer to {question!r}"


def main():
    night_start = time.mktime(datetime(2026, 8, 12, 18).timetuple())
    night_end = time.mktime(datetime(2026, 8, 13, 6).timetuple())
    hot_incidents = [
        {"incident_id": "h1", "component": "gpu_hotspot", "start_timestamp": night_start + 3600,
         "end_timestamp": night_start + 4800, "duration_seconds": 1200, "duration_exact": True,
         "max_zone": "RED", "peak_value": 101.0, "dominant_workload": "Cyberpunk2077.exe",
         "monitoring_gaps": []}]
    hot_sessions = [
        {"session_id": "hs1", "workload": "Cyberpunk2077.exe", "workload_key": "cyberpunk2077.exe",
         "start_timestamp": night_start + 1800, "end_timestamp": night_start + 7200,
         "duration_seconds": 5400, "duration_exact": True,
         "cpu": {"avg_temp": 80.0, "peak_temp": 94.0, "avg_power": 150.0},
         "gpu": {"avg_core_temp": 82.0, "avg_hotspot_temp": 97.0, "peak_hotspot_temp": 101.0,
                "avg_power": 340.0},
         "zone_time": {}, "incident_count": 1, "monitoring_gaps": []}]
    full = [bucket(t) for t in range(int(night_start), int(night_end), TELEMETRY_BUCKET_SECONDS)]

    print("=== 1. Adversarial questions against a genuinely HOT night (the tempting case) ===")
    answers = {}
    with mock(buckets=full, incidents=hot_incidents, sessions=hot_sessions):
        for question in ADVERSARIAL:
            result = answer_question(question, now=NOW)
            text = "\n".join(result["lines"])
            scan(text, question)
            answers[question] = (result, text)
    print(f"  PASS: all {len(ADVERSARIAL)} adversarial questions answered with no causation, blame, "
          f"failure-prediction, unverified-sensor verdict, false-certainty or countdown language")

    print("\n=== 2. 'Did Cyberpunk cause my GPU to overheat?' - correlation is never converted to blame ===")
    result, text = answers["Did Cyberpunk cause my GPU to overheat?"]
    assert "Cyberpunk" in text or result["intent"] is None, text
    assert "cause" not in text.lower().replace("caused by", ""), \
        f"FAIL: the answer asserts a cause:\n{text}"
    print(f"  PASS: routed to intent={result['intent']!r}; it reports the recorded incident without "
          f"ever asserting the game caused it")

    print("\n=== 3. Failure-prediction questions get an honest refusal, not a forecast ===")
    for question in ("Is my GPU dying?", "When will my GPU fail?", "Will my computer die next week?"):
        result, text = answers[question]
        assert result["intent"] is None, f"FAIL: {question!r} should not route to a data intent"
        assert "I don't know how to answer that" in text, text
        assert all(ex in text for ex in ASK_EXAMPLES)
    print("  PASS: 'is my GPU dying', 'when will my GPU fail' and 'will my computer die next week' all "
          "return the explicit capability list - no forecast, no reassurance, no engagement")

    print("\n=== 3b. A question with a FALSE PREMISE is answered with records, never with the premise ===")
    # "Which program damaged my CPU?" routes to the workload intent - it reads as "which program",
    # and answering with the recorded workloads is more useful than refusing. What matters is that
    # the answer supplies EVIDENCE without ever adopting the premise that damage occurred.
    result, text = answers["Which program damaged my CPU?"]
    assert result["intent"] == "workloads", result["intent"]
    for premise in ("damage", "damaged", "harm", "destroyed", "wear"):
        assert premise not in text.lower(), f"FAIL: the answer adopts the damage premise ({premise}):\n{text}"
    assert "Workloads recorded during" in text and "peak CPU" in text
    assert "Based on" in text, "the answer must still cite its records"
    print("  PASS: routed to workloads and returns peak temperatures per workload, without the words "
          "damage/harm/wear appearing anywhere - the premise is neither endorsed nor argued with")

    print("\n=== 4. 'Is PCIe x1 at 82°C dangerous?' - no threshold is attached to an unverified sensor ===")
    result, text = answers["Is PCIe x1 at 82°C dangerous?"]
    assert result["intent"] is None, "FAIL: an unverified-sensor safety question must not be answered"
    for word in ("dangerous", "safe", "normal", "critical", "fine", "82"):
        assert word not in text.lower() or word in "\n".join(ASK_EXAMPLES).lower(), \
            f"FAIL: the answer engages with the unverified sensor's value: {word!r}\n{text}"
    print("  PASS: the question is declined outright - no verdict, no threshold, and the sensor's value "
          "is not echoed back as if it meant something")

    print("\n=== 5. 'Why did my PC overheat?' - evidence yes, cause no ===")
    result, text = answers["Why did my PC overheat?"]
    assert result["intent"] == "explain"
    assert "cannot determine from software telemetry alone what caused it" in text, text
    assert "associated with it, not shown to be the cause of it" in text
    assert "101°C" in text or "Cyberpunk2077.exe" in text, "the real evidence should still be reported"
    print("  PASS: reports the RED incident and the active workload, labels the workload as associated, "
          "and states plainly it cannot determine a cause")

    print("\n=== 6. Poor monitoring coverage is disclosed, never hidden ===")
    sparse = [bucket(t) for t in range(int(night_start), int(night_start) + 900, TELEMETRY_BUCKET_SECONDS)]
    with mock(buckets=sparse, incidents=hot_incidents, sessions=hot_sessions):
        poor = answer_question("What happened last night?", now=NOW)
        poor_text = "\n".join(poor["lines"])
    scan(poor_text, "What happened last night? (poor coverage)")
    assert "only monitored" in poor_text and "may not be the whole picture" in poor_text, poor_text
    assert "Not monitored:" in poor_text
    print("  PASS: a 12-hour question answered from 15 minutes of telemetry states its coverage "
          "percentage and names the unmonitored stretch")

    print("\n=== 7. NO monitoring coverage at all - it says nothing was recorded, and invents nothing ===")
    with mock(buckets=[], incidents=[], sessions=[]):
        blind = answer_question("Why did my PC overheat last night?", now=NOW)
        blind_text = "\n".join(blind["lines"])
    scan(blind_text, "no coverage")
    assert "No thermal incidents were recorded" in blind_text
    assert "No workload sessions were recorded" in blind_text
    assert "Based on 0 incident record(s), 0 session record(s) and 0 minute(s) of telemetry" in blind_text
    assert not re.search(r"\d+°C", blind_text), \
        f"FAIL: a temperature was reported for a period with no telemetry:\n{blind_text}"
    print("  PASS: with zero telemetry it reports nothing recorded, cites zero records, and no "
          "temperature value appears anywhere in the answer")

    print("\n=== 8. Missing sensors are never given invented values ===")
    no_gpu = []
    for t in range(int(night_start), int(night_end), TELEMETRY_BUCKET_SECONDS):
        b = bucket(t)
        del b["scalars"]["gpu_hotspot_temp"]
        no_gpu.append(b)
    with mock(buckets=no_gpu, incidents=[], sessions=[]):
        partial = answer_question("why was my gpu hot last night", now=NOW)
        partial_text = "\n".join(partial["lines"])
    scan(partial_text, "missing gpu sensor")
    assert "GPU Hotspot" not in partial_text, \
        f"FAIL: an unrecorded GPU sensor was reported anyway:\n{partial_text}"
    assert "0°C" not in partial_text
    print("  PASS: a GPU sensor that was never recorded produces no line at all - not 0°C, not an estimate")

    print("\n=== 9. Determinism under adversarial input ===")
    with mock(buckets=full, incidents=hot_incidents, sessions=hot_sessions):
        repeat = {q: "\n".join(answer_question(q, now=NOW)["lines"]) for q in ADVERSARIAL}
    for question in ADVERSARIAL:
        assert repeat[question] == answers[question][1], f"FAIL: {question!r} answered differently"
    print("  PASS: every adversarial question returns a byte-identical answer on repeat")

    print("\nALL ASK GROUNDING AUDIT CHECKS PASSED, NO TRACEBACK")


if __name__ == "__main__":
    main()
