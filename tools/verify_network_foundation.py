"""Verification for v1.1 Phase 1 - Network Foundation.

Covers the stated gate ("accurate against Windows' own network counters with no memory/resource
growth") plus the honesty rules every other Thermal Watch metric already follows: never fabricate
a value, never invent a rate from mismatched samples, and degrade to "unknown"/None rather than
guessing when hardware/data isn't available.

Cross-checks against REAL, independent Windows oracles wherever one exists (Get-NetRoute for the
active-adapter decision, ipconfig for IP/gateway) rather than only testing this app's own code
against itself.
"""
import ctypes
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`: redirects every store to a temp dir
from app import (  # noqa: E402
    App, network_adapters, default_route_interface_index, adapter_ip_info, wifi_signal_percent,
    active_network_snapshot, fmt_net_bytes, TELEMETRY_SCALAR_KEYS, TELEMETRY_SCALAR_CONTEXT_MAP,
    TELEMETRY_SCALAR_LABELS, scalar_sensor_ref, read_telemetry_file, CREATE_NO_WINDOW,
)

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


def run_ps(cmd, timeout=8):
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True,
                           text=True, timeout=timeout, creationflags=CREATE_NO_WINDOW)
        return (r.stdout or "").strip()
    except (OSError, subprocess.SubprocessError):
        return ""


print("=" * 78)
print("1. network_adapters() - structural sanity, real hardware")
print("=" * 78)
adapters = network_adapters()
check("returns a list", isinstance(adapters, list))
check("finds at least one adapter on this machine", len(adapters) > 0, f"found {len(adapters)}")
check("every adapter has a real, non-empty name", all(a["name"] for a in adapters))
check("every adapter's type is one of the three declared kinds",
      all(a["type"] in ("Ethernet", "Wi-Fi", "Other") for a in adapters))
check("no adapter is loopback or a Teredo/ISATAP/6to4 tunnel pseudo-adapter",
      all("loopback" not in a["name"].lower() and
          not any(t in a["name"].lower() or t in a["description"].lower() for t in ("teredo", "isatap", "6to4"))
          for a in adapters))
check("no NDIS filter-driver or WAN Miniport rows leaked through",
      all(not any(tag in a["description"].lower() for tag in
                  ("lightweight filter", "wfp native mac layer", "wfp 802.3 mac layer",
                   "qos packet scheduler", "virtual wifi filter driver", "native wifi filter driver"))
          and not a["description"].lower().startswith("wan miniport")
          for a in adapters),
      "confirms the description-substring filters are still doing their job")
check("byte counters are non-negative integers", all(a["in_octets"] >= 0 and a["out_octets"] >= 0 for a in adapters))
# 0 is a real, legitimate value here - confirmed on this machine's own disconnected adapters
# (e.g. "Ethernet 2", cable unplugged, genuinely reports 0 bps) - only a negative number or a
# non-numeric value would indicate something was fabricated.
check("link speeds are non-negative ints or None (0 for a disconnected adapter is real, not fabricated)",
      all((a["receive_link_speed_bps"] is None or a["receive_link_speed_bps"] >= 0) and
          (a["transmit_link_speed_bps"] is None or a["transmit_link_speed_bps"] >= 0) for a in adapters))

print()
print("=" * 78)
print("2. default_route_interface_index() - cross-checked against Get-NetRoute (real oracle)")
print("=" * 78)
active_idx = default_route_interface_index()
check("resolves to SOME index on a machine with internet access", active_idx is not None)
active = next((a for a in adapters if a["index"] == active_idx), None) if active_idx is not None else None
check("the resolved index matches a real adapter from network_adapters()", active is not None)

ps_route = run_ps(
    "Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue | "
    "Sort-Object RouteMetric | Select-Object -First 1 -ExpandProperty ifIndex"
)
if ps_route.strip().isdigit():
    check("matches Get-NetRoute's own lowest-metric default-route interface (real Windows oracle)",
          int(ps_route.strip()) == active_idx,
          f"ours={active_idx}  Get-NetRoute={ps_route.strip()}")
else:
    print("        (skipped - Get-NetRoute unavailable in this environment)")

print()
print("=" * 78)
print("3. adapter_ip_info() - cross-checked against ipconfig (real oracle)")
print("=" * 78)
if active is not None:
    info = adapter_ip_info(active_idx)
    check("returns a dict with ipv4/gateway keys", isinstance(info, dict) and "ipv4" in info and "gateway" in info)
    ipconfig_out = run_ps("ipconfig")
    if info.get("ipv4") and ipconfig_out:
        check("the reported IPv4 address appears in real ipconfig output",
              info["ipv4"] in ipconfig_out, f"looked for {info['ipv4']!r}")
    if info.get("gateway") and ipconfig_out:
        check("the reported gateway appears in real ipconfig output",
              info["gateway"] in ipconfig_out, f"looked for {info['gateway']!r}")
else:
    print("        (skipped - no active adapter resolved)")

print()
print("=" * 78)
print("4. wifi_signal_percent() - never crashes, never fabricates")
print("=" * 78)
sig = wifi_signal_percent()
check("returns None or an int in [0, 100] - never anything else", sig is None or (isinstance(sig, int) and 0 <= sig <= 100),
      f"got {sig!r}")

print()
print("=" * 78)
print("5. active_network_snapshot() - rate math, first-sample honesty, adapter-switch honesty")
print("=" * 78)

FAKE_ADAPTER = {"name": "FakeNet", "description": "Fake", "type": "Ethernet", "oper_status": "Up",
                "media_connect_state": True, "receive_link_speed_bps": 1_000_000_000,
                "transmit_link_speed_bps": 1_000_000_000, "in_octets": 1_000_000, "out_octets": 500_000,
                "luid": 1, "index": 42}


def fake_adapters(in_o, out_o, index=42):
    a = dict(FAKE_ADAPTER, in_octets=in_o, out_octets=out_o, index=index)
    return [a]


with mock.patch("app.default_route_interface_index", return_value=42), \
     mock.patch("app.network_adapters", return_value=fake_adapters(1_000_000, 500_000)), \
     mock.patch("app.time.time", return_value=1_000_000.0):
    snap1, prev = active_network_snapshot({})
    check("first-ever sample: down_mbps is None (no prior sample to rate against)", snap1["down_mbps"] is None)
    check("first-ever sample: up_mbps is None", snap1["up_mbps"] is None)
    check("prev is populated after the first sample", prev.get("index") == 42 and prev.get("in_octets") == 1_000_000)

# exact rate arithmetic: 10,000,000 bytes in exactly 2 seconds = 40 Mbps down
with mock.patch("app.default_route_interface_index", return_value=42), \
     mock.patch("app.network_adapters", return_value=fake_adapters(11_000_000, 501_250_000)), \
     mock.patch("app.time.time", return_value=1_000_002.0):
    snap2, prev2 = active_network_snapshot(prev)
    expected_down = (11_000_000 - 1_000_000) * 8 / 2 / 1e6  # = 40.0 Mbps
    expected_up = (501_250_000 - 500_000) * 8 / 2 / 1e6      # = 2004.0 Mbps
    check("down_mbps matches the exact rate formula", abs(snap2["down_mbps"] - expected_down) < 1e-9,
          f"expected {expected_down}, got {snap2['down_mbps']}")
    check("up_mbps matches the exact rate formula", abs(snap2["up_mbps"] - expected_up) < 1e-9,
          f"expected {expected_up}, got {snap2['up_mbps']}")

# adapter switch mid-run (e.g. Wi-Fi -> Ethernet): must NOT compute a rate from two different
# adapters' unrelated counters
with mock.patch("app.default_route_interface_index", return_value=99), \
     mock.patch("app.network_adapters", return_value=fake_adapters(50_000, 20_000, index=99)), \
     mock.patch("app.time.time", return_value=1_000_004.0):
    snap3, prev3 = active_network_snapshot(prev2)
    check("active adapter changing mid-run: down_mbps is None, not a bogus cross-adapter delta",
          snap3["down_mbps"] is None)
    check("active adapter changing mid-run: up_mbps is None", snap3["up_mbps"] is None)

# a counter that appears to go backward (adapter reset) must clamp to 0.0, never go negative
with mock.patch("app.default_route_interface_index", return_value=42), \
     mock.patch("app.network_adapters", return_value=fake_adapters(500, 500)), \
     mock.patch("app.time.time", return_value=1_000_000.0):
    snap4, prev4 = active_network_snapshot({})
with mock.patch("app.default_route_interface_index", return_value=42), \
     mock.patch("app.network_adapters", return_value=fake_adapters(100, 100)), \
     mock.patch("app.time.time", return_value=1_000_002.0):
    snap5, _ = active_network_snapshot(prev4)
    check("a decreasing counter (adapter reset) clamps to 0.0, never negative",
          snap5["down_mbps"] == 0.0 and snap5["up_mbps"] == 0.0,
          f"down={snap5['down_mbps']} up={snap5['up_mbps']}")

print()
print("=" * 78)
print("6. offline / no-adapter honesty - never crashes, never fabricates a reading")
print("=" * 78)
with mock.patch("app.default_route_interface_index", return_value=None), \
     mock.patch("app.network_adapters", return_value=[]):
    snap_off, prev_off = active_network_snapshot({})
    check("no route to the internet: adapter is None", snap_off["adapter"] is None)
    check("no route to the internet: down_mbps is None", snap_off["down_mbps"] is None)
    check("no route to the internet: up_mbps is None", snap_off["up_mbps"] is None)
    check("prev resets cleanly to the empty state", prev_off == {"index": None, "in_octets": None,
                                                                  "out_octets": None, "time": None})

print()
print("=" * 78)
print("7. real App(): full worker -> update_data -> UI panel, no crash, honest empty state")
print("=" * 78)
app = App()
try:
    for _ in range(2):
        time.sleep(2.1)
        app.after(0, app.poll)
        app.update()
    check("self.last_net is populated with a real adapter after two live ticks",
          app.last_net.get("adapter") is not None)
    check("NETWORK panel adapter label reflects the real active adapter",
          app.net_adapter_label.cget("text") != "--")
    check("empty-state label is hidden while a real adapter is active", not app.net_empty_shown)

    # simulate going offline mid-run and confirm the panel degrades honestly, live, with no crash
    with mock.patch("app.default_route_interface_index", return_value=None), \
         mock.patch("app.network_adapters", return_value=[]):
        app.update_data({"time": datetime.now(), "cpu_load": 0, "mem_pct": 0, "mem_used": 0, "mem_total": 0,
                         "gpus": [], "lhm": None, "workload": None,
                         "net": {"adapter": None, "down_mbps": None, "up_mbps": None,
                                 "ip_info": None, "wifi_signal": None}})
    check("panel shows the empty state when offline, no exception raised", app.net_empty_shown)
    check("adapter label reverts to the honest placeholder when offline",
          app.net_adapter_label.cget("text") == "--")
finally:
    app.stop_event.set()
    app.destroy()

print()
print("=" * 78)
print("8. telemetry integration - registered scalars, real bucketed history, drill-down wiring")
print("=" * 78)
for key in ("net_down_mbps", "net_up_mbps", "net_rx_bytes", "net_tx_bytes"):
    check(f"{key} is a registered telemetry scalar", key in TELEMETRY_SCALAR_KEYS)
    check(f"{key} has a context-map entry", key in TELEMETRY_SCALAR_CONTEXT_MAP)
    check(f"{key} has a label/unit entry", key in TELEMETRY_SCALAR_LABELS)
    ref = scalar_sensor_ref(key)
    check(f"{key} produces a valid sensor_ref for SensorHistoryWindow drill-down",
          ref["kind"] == "scalar" and ref["key"] == key)
check("network scalars are NOT wired to any incident component (Phase 1 has no thresholds yet)",
      all(scalar_sensor_ref(k)["component"] is None
          for k in ("net_down_mbps", "net_up_mbps", "net_rx_bytes", "net_tx_bytes")))

app2 = App()
try:
    for _ in range(2):
        time.sleep(2.1)
        app2.after(0, app2.poll)
        app2.update()
    app2._telemetry_finalize_bucket(time.time())
    buckets = read_telemetry_file()
    check("at least one telemetry bucket was persisted", len(buckets) > 0)
    if buckets:
        scalars = buckets[-1].get("scalars", {})
        has_net_down = scalars.get("net_down_mbps") is not None
        check("the persisted bucket carries real net_down_mbps data (not silently dropped)",
              has_net_down, f"scalars keys present: {sorted(scalars.keys())}")
finally:
    app2.stop_event.set()
    app2.destroy()

print()
print("=" * 78)
print("9. fmt_net_bytes() - honest formatting, never fabricates a value for None")
print("=" * 78)
check("None -> 'N/A'", fmt_net_bytes(None) == "N/A")
check("0 bytes -> '0 B'", fmt_net_bytes(0) == "0 B")
check("1536 bytes -> 1.50 KB", fmt_net_bytes(1536) == "1.50 KB")
check("10 MB-ish value formats as MB", fmt_net_bytes(10 * 1024 * 1024) == "10.00 MB")
check("1 GB-ish value formats as GB", fmt_net_bytes(1024 ** 3) == "1.00 GB")

print()
print("=" * 78)
print("10. no memory/resource-handle growth across repeated real polling (the stated gate)")
print("=" * 78)
kernel32 = ctypes.windll.kernel32
own_process = kernel32.GetCurrentProcess()
handle_count = ctypes.c_ulong()
kernel32.GetProcessHandleCount(own_process, ctypes.byref(handle_count))
handles_before = handle_count.value

for _ in range(100):
    network_adapters()
    if active_idx is not None:
        adapter_ip_info(active_idx)
    wifi_signal_percent()

kernel32.GetProcessHandleCount(own_process, ctypes.byref(handle_count))
handles_after = handle_count.value
growth = handles_after - handles_before
check("OS handle count does not grow after 100 real iterations (FreeMibTable/WlanCloseHandle/"
      "WlanFreeMemory are all doing their job)", growth <= 5,
      f"handles before={handles_before} after={handles_after} growth={growth} "
      f"(small positive noise from Python's own GC timing is tolerated, unbounded growth is not)")

print()
print("=" * 78)
print("11. structural: read-only, no packet-content inspection, no traffic sent (item: never spy)")
print("=" * 78)
import inspect  # noqa: E402
import app as appmod  # noqa: E402

src = "".join(inspect.getsource(f) for f in
             (appmod.network_adapters, appmod.default_route_interface_index, appmod.adapter_ip_info,
              appmod.wifi_signal_percent, appmod.active_network_snapshot))
check("no raw socket creation", "socket.socket(" not in src and "socket(AF_INET" not in src)
check("no outbound connect() call - GetBestInterfaceEx only consults the route table",
      ".connect(" not in src and "WSAConnect" not in src)
check("no packet capture APIs (WinPcap/Npcap/raw sockets) referenced", not any(
    tag in src for tag in ("pcap", "SOCK_RAW", "IPPROTO_RAW")))

print()
print("=" * 78)
if FAILURES:
    print(f"{len(FAILURES)} OF {CHECKS[0]} CHECKS FAILED:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
print(f"ALL {CHECKS[0]} NETWORK FOUNDATION CHECKS PASSED, NO TRACEBACK")
