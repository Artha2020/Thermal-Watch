"""Verification for v1.1 Phase 3 - Connection Intelligence.

active_connections() (GetExtendedTcpTable/GetExtendedUdpTable, fully unprivileged - confirmed
during Phase 2 research against real connections on this machine, not inferred from docs) plus
the NETWORK panel's connection-count summary and the new ConnectionsWindow. Metadata only:
owning process, local/remote address:port, TCP state - this suite also structurally confirms
nothing resembling packet-content inspection was introduced, same rule as every other network
layer in this app.
"""
import inspect
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _verify_sandbox  # noqa: E402,F401  - MUST precede `import app`
from app import App, active_connections, ConnectionsWindow, _TCP_STATE_NAMES  # noqa: E402

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
print("1. active_connections() - real hardware, structural sanity")
print("=" * 78)
conns = active_connections()
tcp = [c for c in conns if c["protocol"] == "TCP"]
udp = [c for c in conns if c["protocol"] == "UDP"]
check("returns a list", isinstance(conns, list))
check("finds at least one real TCP connection on this machine (a listening service always exists)",
      len(tcp) > 0, f"found {len(tcp)} TCP")
check("finds at least one real UDP endpoint on this machine", len(udp) > 0, f"found {len(udp)} UDP")
check("every row has all expected fields", all(
    set(c.keys()) == {"protocol", "pid", "name", "local", "remote", "state"} for c in conns))
check("every protocol value is TCP or UDP", all(c["protocol"] in ("TCP", "UDP") for c in conns))
check("every pid is a non-negative int", all(isinstance(c["pid"], int) and c["pid"] >= 0 for c in conns))
check("every name is a non-empty string (real name, 'pid:N' fallback, or 'System')",
      all(isinstance(c["name"], str) and c["name"] for c in conns))
check("every local endpoint looks like ip:port", all(":" in c["local"] for c in conns))
check("UDP rows correctly report no remote endpoint/state (UDP is connectionless - never fabricated)",
      all(c["remote"] == "-" and c["state"] == "-" for c in udp))
check("every TCP state is a real MIB_TCP_STATE value (or the honest UNKNOWN(n) fallback)",
      all(c["state"] in _TCP_STATE_NAMES.values() or c["state"].startswith("UNKNOWN(") for c in tcp))
check("at least one TCP connection is actually ESTABLISHED right now (this test process itself "
      "has open HTTPS connections)", any(c["state"] == "ESTABLISHED" for c in tcp))

print()
print("=" * 78)
print("2. name_cache - correctness and reuse")
print("=" * 78)
cache = {}
conns1 = active_connections(cache)
pids_seen = {c["pid"] for c in conns1 if c["pid"]}
check("cache is populated with every real PID seen", pids_seen.issubset(set(cache.keys())))
cache_before = dict(cache)
conns2 = active_connections(cache)
check("a second call with the same cache doesn't drop or corrupt previously-resolved names",
      all(cache[pid] == cache_before[pid] for pid in cache_before))
own_pid_rows = [c for c in conns2 if c["pid"] and c["name"] not in (None, "") and not c["name"].startswith("pid:")]
check("at least one connection resolves to a real, non-placeholder process name",
      len(own_pid_rows) > 0, f"example: {own_pid_rows[0]['name'] if own_pid_rows else None}")

print()
print("=" * 78)
print("3. structural - no packet-content inspection, ever")
print("=" * 78)
src = inspect.getsource(active_connections) + inspect.getsource(sys.modules["app"]._raw_tcp_connections) \
    + inspect.getsource(sys.modules["app"]._raw_udp_endpoints)
forbidden = ("recv(", "socket.socket", "PacketReceiveProvider", "WinDivert", "npcap", "WSARecv", ".decode(")
check("no packet-capture/content-read call anywhere in the connection-enumeration code",
      not any(tok in src for tok in forbidden), f"scanned {len(src)} chars")

print()
print("=" * 78)
print("4. real App(): NETWORK panel connection summary + ConnectionsWindow, no crash")
print("=" * 78)
app = App()
try:
    for _ in range(2):
        time.sleep(2.1)
        app.after(0, app.poll)
        app.update()
    check("self.last_connections is populated with real data after a live tick",
          len(app.last_connections) > 0)
    conn_text = app.net_detail_labels["connections"]["val"].cget("text")
    check("NETWORK panel's CONNECTIONS cell shows a real 'N TCP · M UDP' summary, not a placeholder",
          "TCP" in conn_text and "UDP" in conn_text and conn_text != "--", f"rendered: {conn_text!r}")

    app.open_connections_window()
    win = app.connections_window
    check("ConnectionsWindow opens without exception", win is not None and win.winfo_exists())
    app.update()
    check("ConnectionsWindow's tree is populated with real rows on open", len(win.tree.get_children()) > 0)
    check("count label reflects real TCP/UDP counts", "TCP" in win.count_label.cget("text"))

    app.open_connections_window()
    check("re-opening reuses the same instance rather than stacking a duplicate window",
          app.connections_window is win)

    win.destroy()
    app.update()
    app.open_connections_window()
    check("opening again after the user closed it creates a fresh instance, not a dead reference",
          app.connections_window is not None and app.connections_window.winfo_exists()
          and app.connections_window is not win)
    app.connections_window.destroy()
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
