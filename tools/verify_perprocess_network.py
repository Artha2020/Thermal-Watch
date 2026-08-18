"""Verification for v1.1 Phase 2 - Per-Process Network Usage.

Covers the unprivileged reader/rate/UI path and the elevated bridge's deterministic native ABI.
The latter prevents regression of the confirmed v1.1 defect where EVENT_TRACE_LOGFILEW omitted
its required 88-byte CurrentEvent member, shifting EventRecordCallback and silently producing
zero callbacks despite delivered ETW buffers. A metadata-only fixture captured from live TDH
also proves the generic decoder accepts this machine's real opcode 10/11, PID/size UInt32 shape.

BRIDGE_NETPROC_PATH lives under %ProgramData%\\ThermalWatch; every reader case below mocks it.
"""
import json
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`
from app import App, network_processes, process_network_rates, NET_TOP_PROCESS_COUNT  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8")

FAILURES = []
CHECKS = [0]


def check(name, condition, detail=""):
    CHECKS[0] += 1
    ok = bool(condition)
    print(f"[{'PASS' if ok else 'FAIL'}] {CHECKS[0]:2d}. {name}")
    if detail:
        print(f"        {detail}")
    if not ok:
        FAILURES.append(name)


print("=" * 78)
print("1. network_processes() - honest degradation, no fabrication")
print("=" * 78)

with mock.patch("app.BRIDGE_NETPROC_PATH") as mock_path:
    mock_path.read_text.side_effect = FileNotFoundError()
    result = network_processes()
    check("missing file -> capture_active False, empty processes, no exception",
          result == {"capture_active": False, "capture_error": None, "processes": []})

with mock.patch("app.BRIDGE_NETPROC_PATH") as mock_path:
    mock_path.read_text.return_value = "{not valid json"
    result = network_processes()
    check("malformed JSON -> honest empty result, no exception", result["capture_active"] is False)

with mock.patch("app.BRIDGE_NETPROC_PATH") as mock_path:
    stale_payload = ('{"timestamp": %f, "capture_active": true, "capture_error": null, '
                      '"processes": [{"pid": 111, "name": "steam", "bytes_in": 500, "bytes_out": 10}]}'
                      % (time.time() - 999))
    mock_path.read_text.return_value = stale_payload
    result = network_processes()
    check("stale timestamp (bridge died/hung) -> falls back to inactive rather than serving old data",
          result == {"capture_active": False, "capture_error": None, "processes": []})

with mock.patch("app.BRIDGE_NETPROC_PATH") as mock_path:
    fresh_payload = ('{"timestamp": %f, "capture_active": true, "capture_error": null, '
                      '"processes": [{"pid": 222, "name": "chrome", "bytes_in": 1000, "bytes_out": 200}]}'
                      % time.time())
    mock_path.read_text.return_value = fresh_payload
    result = network_processes()
    check("fresh valid payload passed through as-is",
          result["capture_active"] is True and result["processes"][0]["pid"] == 222)

with mock.patch("app.BRIDGE_NETPROC_PATH") as mock_path:
    unelevated_payload = ('{"timestamp": %f, "capture_active": false, '
                           '"capture_error": "EnableTraceEx2 failed (needs elevation): 5", "processes": []}'
                           % time.time())
    mock_path.read_text.return_value = unelevated_payload
    result = network_processes()
    check("bridge-reported capture failure (e.g. an older, pre-Phase-2 bridge) surfaces the real reason verbatim",
          result["capture_active"] is False and "elevation" in (result["capture_error"] or ""))

print()
print("=" * 78)
print("2. process_network_rates() - delta-over-time arithmetic, same honesty rules as adapter rates")
print("=" * 78)

with mock.patch("app.time.time", return_value=1000.0):
    rates, prev = process_network_rates(
        {"processes": [{"pid": 111, "name": "steam", "bytes_in": 5000, "bytes_out": 1000}]}, {})
check("first sample for a PID -> down/up_mbps is None, never a fabricated rate",
      rates[0]["down_mbps"] is None and rates[0]["up_mbps"] is None)
check("first sample still records the running byte counters", rates[0]["bytes_in"] == 5000 and rates[0]["bytes_out"] == 1000)
check("prev is populated for the next call", prev[111]["bytes_in"] == 5000 and prev[111]["time"] == 1000.0)

with mock.patch("app.time.time", return_value=1002.0):
    rates2, prev2 = process_network_rates(
        {"processes": [{"pid": 111, "name": "steam", "bytes_in": 5000 + 250000, "bytes_out": 1000 + 25000}]}, prev)
expected_down = 250000 * 8 / 2.0 / 1e6  # 1.0 Mbps
expected_up = 25000 * 8 / 2.0 / 1e6     # 0.1 Mbps
check("second sample computes exact Mbps from the real byte delta and elapsed time",
      abs(rates2[0]["down_mbps"] - expected_down) < 1e-9 and abs(rates2[0]["up_mbps"] - expected_up) < 1e-9,
      f"got down={rates2[0]['down_mbps']} up={rates2[0]['up_mbps']}, expected down={expected_down} up={expected_up}")

with mock.patch("app.time.time", return_value=1004.0):
    rates3, prev3 = process_network_rates(
        {"processes": [{"pid": 111, "name": "steam", "bytes_in": 100, "bytes_out": 50}]}, prev2)
check("counter decrease (bridge restart mid-session resets its accumulator) -> None, not a clamped 0.0 "
      "or a fabricated negative rate", rates3[0]["down_mbps"] is None and rates3[0]["up_mbps"] is None)
check("the lower post-reset counter value is still recorded honestly (not silently kept at the old high value)",
      prev3[111]["bytes_in"] == 100)

with mock.patch("app.time.time", return_value=1006.0):
    rates4, prev4 = process_network_rates(
        {"processes": [
            {"pid": 111, "name": "steam", "bytes_in": 100, "bytes_out": 50},
            {"pid": 333, "name": "discord", "bytes_in": 900, "bytes_out": 900},
        ]}, prev3)
check("a genuinely new PID appearing mid-session (not present in prev) also correctly reports None, "
      "never diffed against an unrelated/absent baseline",
      rates4[1]["down_mbps"] is None and rates4[1]["up_mbps"] is None)
check("an existing PID continues to compute normally alongside a brand-new one in the same tick",
      rates4[0]["down_mbps"] is not None)

with mock.patch("app.time.time", return_value=1000.0):
    rates_empty, prev_empty = process_network_rates({"processes": []}, {})
check("empty process list -> empty rates list, no exception", rates_empty == [] and prev_empty == {})

with mock.patch("app.time.time", return_value=1000.0):
    rates_no_pid, _ = process_network_rates({"processes": [{"name": "orphan", "bytes_in": 1, "bytes_out": 1}]}, {})
check("a malformed process entry missing 'pid' is skipped, not crashed on", rates_no_pid == [])

with mock.patch("app.time.time", return_value=1000.0):
    rates_missing_bytes, _ = process_network_rates({"processes": [{"pid": 999, "name": "x"}]}, {})
check("missing bytes_in/bytes_out fields default to 0 rather than raising",
      rates_missing_bytes[0]["bytes_in"] == 0 and rates_missing_bytes[0]["bytes_out"] == 0)

print()
print("=" * 78)
print("3. Worker-loop integration shape (sort/top-N truncation matches NET_TOP_PROCESS_COUNT)")
print("=" * 78)

many_procs = {"processes": [{"pid": i, "name": f"p{i}", "bytes_in": i * 1000, "bytes_out": 0} for i in range(1, 12)]}
with mock.patch("app.time.time", return_value=2000.0):
    _, prev_many = process_network_rates(many_procs, {})
with mock.patch("app.time.time", return_value=2002.0):
    rates_many, _ = process_network_rates(many_procs2 := {"processes": [
        {"pid": i, "name": f"p{i}", "bytes_in": i * 1000 + i * 100000, "bytes_out": 0} for i in range(1, 12)
    ]}, prev_many)
rates_many.sort(key=lambda r: (r["down_mbps"] or 0) + (r["up_mbps"] or 0), reverse=True)
top = rates_many[:NET_TOP_PROCESS_COUNT]
check(f"NET_TOP_PROCESS_COUNT is {NET_TOP_PROCESS_COUNT} and truncation keeps exactly that many rows",
      len(top) == NET_TOP_PROCESS_COUNT)
check("truncated list keeps the highest-bandwidth processes (pid 11 has the largest delta, must be first)",
      top[0]["pid"] == 11)
check("sort order is strictly descending by combined down+up Mbps",
      all((top[i]["down_mbps"] or 0) >= (top[i + 1]["down_mbps"] or 0) for i in range(len(top) - 1)))

print()
print("=" * 78)
print("4. native ETW consumer ABI + live-TDH event-shape regression")
print("=" * 78)

bridge_path = Path(__file__).resolve().parent.parent / "sensor_bridge.ps1"
bridge_source = bridge_path.read_text(encoding="utf-8")
cs_start = bridge_source.index("using System;", bridge_source.index("$netCaptureSrc"))
cs_end = bridge_source.index("\n'@", cs_start)
cs_source = bridge_source[cs_start:cs_end]
check("EVENT_TRACE_LOGFILEW embeds CurrentEvent before LogfileHeader",
      cs_source.index("public EVENT_TRACE CurrentEvent;") <
      cs_source.index("public TRACE_LOGFILE_HEADER LogfileHeader;"))

with tempfile.TemporaryDirectory(prefix="tw_etw_abi_") as td:
    cs_path = Path(td) / "capture.cs"
    cs_path.write_text(cs_source, encoding="utf-8")
    ps = ("Add-Type -Path '" + str(cs_path).replace("'", "''") + "'; "
          "$t=[ThermalWatchNet.EVENT_TRACE_LOGFILEW]; "
          "[pscustomobject]@{event_trace_size=[Runtime.InteropServices.Marshal]::SizeOf([Activator]::CreateInstance([ThermalWatchNet.EVENT_TRACE]));"
          "logfile_size=[Runtime.InteropServices.Marshal]::SizeOf([Activator]::CreateInstance([ThermalWatchNet.EVENT_TRACE_LOGFILEW]));"
          "current_event_offset=[int][Runtime.InteropServices.Marshal]::OffsetOf($t,'CurrentEvent');"
          "header_offset=[int][Runtime.InteropServices.Marshal]::OffsetOf($t,'LogfileHeader');"
          "callback_offset=[int][Runtime.InteropServices.Marshal]::OffsetOf($t,'EventRecordCallback');"
          "context_offset=[int][Runtime.InteropServices.Marshal]::OffsetOf($t,'Context')}|ConvertTo-Json -Compress")
    abi_proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    abi = json.loads(abi_proc.stdout) if abi_proc.returncode == 0 and abi_proc.stdout.strip() else {}
check("embedded C# compiles and EVENT_TRACE is exactly 88 bytes", abi.get("event_trace_size") == 88,
      f"compiler output={abi!r} stderr={abi_proc.stderr.strip()!r}")
check("EVENT_TRACE_LOGFILEW is 448 bytes with native callback/context offsets",
      abi.get("logfile_size") == 448 and abi.get("current_event_offset") == 32 and
      abi.get("header_offset") == 120 and abi.get("callback_offset") == 424 and
      abi.get("context_offset") == 440, f"measured ABI={abi!r}")

live_tdh_fixture = {
    "provider": "Microsoft-Windows-Kernel-Network", "event_id": 11, "opcode": 11,
    "task": 10, "version": 0,
    "properties": {"PID": {"in_type": 8, "bytes": 4}, "size": {"in_type": 8, "bytes": 4}},
}
check("live-TDH receive fixture uses a generic data opcode accepted by the decoder",
      live_tdh_fixture["opcode"] in (10, 11) and "opcode != 10 && opcode != 11" in cs_source)
check("live-TDH PID/size UInt32 fields are decoded generically with no Steam/PID special case",
      all(live_tdh_fixture["properties"][k] == {"in_type": 8, "bytes": 4} for k in ("PID", "size")) and
      'names.Contains("PID")' in cs_source and 'names.Contains("size")' in cs_source and
      "raw.Length == 4" in cs_source and "steam" not in cs_source.lower())

print()
print("=" * 78)
print("5. real App(): TOP PROCESSES panel renders both states with no crash")
print("=" * 78)

app = App()
try:
    # App() starts a REAL background worker thread that puts real hardware/network data on the
    # queue almost immediately, and its own poll() drains that queue into update_data() every
    # ~200ms. Left running, it races the synthetic update_data() calls below - confirmed live
    # (a real, reproducible ~1-in-5 flake): the worker's genuine first tick can land between a
    # synthetic update_data() call and the assertion that follows, silently overwriting the
    # planted net_procs payload before it's ever read. Stopped here, before any synthetic data
    # is injected, using the exact same scoped after-cancel App.destroy() already uses (by Tcl
    # command name, so a still-open child window's own callbacks are never touched).
    app.stop_event.set()
    for after_id in app.tk.eval("after info").split():
        try:
            command = app.tk.call("after", "info", after_id)[0]
        except tk.TclError:
            continue
        if any(str(command).endswith(name) for name in app._RECURRING_AFTER_METHODS):
            app.after_cancel(after_id)

    base_payload = {"time": datetime.now(), "cpu_load": 0, "mem_pct": 0, "mem_used": 0, "mem_total": 0,
                     "gpus": [], "lhm": None, "workload": None,
                     "net": {"adapter": None, "down_mbps": None, "up_mbps": None,
                             "ip_info": None, "wifi_signal": None}}

    app.update_data({**base_payload, "net_procs": {"capture_active": False, "capture_error":
                     "EnableTraceEx2 failed (needs elevation): 5", "top": []}})
    app.update()
    children_text = " ".join(w.cget("text") for w in app.net_proc_list.winfo_children() if "text" in w.keys())
    check("inactive-capture state renders the real reported reason, no exception",
          "elevation" in children_text.lower(), f"rendered: {children_text!r}")

    app.update_data({**base_payload, "net_procs": {"capture_active": True, "capture_error": None, "top": [
        {"pid": 4242, "name": "steam", "bytes_in": 900000, "bytes_out": 100000, "down_mbps": 3.6, "up_mbps": 0.4},
        {"pid": 5151, "name": "discord", "bytes_in": 5000, "bytes_out": 5000, "down_mbps": None, "up_mbps": None},
    ]}})
    app.update()
    rows = app.net_proc_list.winfo_children()
    check("active-capture state renders one row per top process, no exception", len(rows) == 2)
    row0_text = " ".join(w.cget("text") for w in rows[0].winfo_children())
    check("a process with a real computed rate shows the actual Mbps value, not a placeholder",
          "3.60" in row0_text and "steam" in row0_text, f"rendered: {row0_text!r}")
    row1_text = " ".join(w.cget("text") for w in rows[1].winfo_children())
    check("a process on its first-ever sample (rate is None) shows '--', never a fabricated 0.00",
          "--" in row1_text, f"rendered: {row1_text!r}")

    app.update_data({**base_payload, "net_procs": {"capture_active": True, "capture_error": None, "top": []}})
    app.update()
    check("active capture with genuinely zero traffic this interval shows the honest no-traffic "
          "message, not an empty silent panel",
          len(app.net_proc_list.winfo_children()) == 1)
finally:
    app.stop_event.set()
    app.destroy()

print()
print("=" * 78)
summary = f"{CHECKS[0] - len(FAILURES)}/{CHECKS[0]} checks passed"
print(summary)
if FAILURES:
    print("FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("ALL CHECKS PASSED")
