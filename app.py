"""Thermal Watch - dependency-free Windows CPU/GPU monitor."""
from __future__ import annotations

import ctypes
import csv
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from ctypes import wintypes
from datetime import datetime, date, timedelta
from pathlib import Path
from tkinter import filedialog, ttk

# Phase 16 - AI Integration Settings. ai/ has zero dependency on app.py (and must stay that way -
# see ai/ai_settings.py's own docstring on why its DATA_DIR resolution is independent rather than
# importing data_path() from here); this is the one direction the dependency is allowed to run.
from ai import ai_settings
from ai.provider_contract import ProviderContractError, ProviderResponse
from ai.provider_registry import UniversalAIAdapter

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------
BG = "#0b0c0e"
PANEL = "#111317"
BORDER = "#22252b"
BORDER2 = "#1a1d22"
TEXT = "#e8eaed"
MUTED = "#8b929c"
DIM = "#5c636e"
ORANGE = "#ff5a2e"
ORANGE2 = "#ff7847"
GREEN = "#3ddc84"
BLUE = "#5aa9ff"
AMBER = "#ffb224"
RED = "#ff3b3b"
ALERT_BG = "#170e0a"
ALERT_BORDER = "#59261a"
ALERT_STRIP_BG = "#1c110c"

MONO = "Consolas"
SANS = "Segoe UI"

CREATE_NO_WINDOW = 0x08000000

# Thresholds (mirrors the design's alert bands).
THRESH_MEM = 90.0
TJMAX = 105.0
GPU_TMAX = 95.0

# CPU temperature zones. This is Thermal Watch's own monitoring/UI alert
# ceiling, not AMD's hardware shutdown limit (Tjmax above) - nothing here
# talks to the CPU or tells it to throttle/shut down.
CPU_YELLOW = 80.0
CPU_ORANGE = 90.0
CPU_RED = 100.0
CPU_ALERT_DEBOUNCE_S = 3.0

# (floor, key, color, banner/log label, short card-badge label), checked high to low.
CPU_ZONES = [
    (CPU_RED, "RED", RED, "DANGER — SHUT DOWN", "DANGER"),
    (CPU_ORANGE, "ORANGE", ORANGE, "CRITICAL — NEAR THERMAL LIMIT", "CRITICAL"),
    (CPU_YELLOW, "YELLOW", AMBER, "HIGH TEMP", "HIGH TEMP"),
    (0.0, "GREEN", GREEN, "NOMINAL", "NOMINAL"),
]
CPU_ZONE_SEVERITY = {"GREEN": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}


def cpu_zone_for(value):
    """Classify a CPU temperature into one of the four alert zones, or None if unavailable."""
    if value is None:
        return None
    for floor, key, color, label, short in CPU_ZONES:
        if value >= floor:
            return {"key": key, "color": color, "label": label, "short": short}
    return None  # unreachable, floor=0.0 always matches


# NVMe/SSD Composite Temperature zones - deliberately separate from CPU_ZONES: drives run far
# cooler than a CPU die, so the CPU's 80/90/100 bands would never trip for storage.
DRIVE_YELLOW = 60.0
DRIVE_ORANGE = 70.0
DRIVE_RED = 80.0
DRIVE_ALERT_DEBOUNCE_S = 3.0

DRIVE_ZONES = [
    (DRIVE_RED, "RED", RED, "CRITICAL"),
    (DRIVE_ORANGE, "ORANGE", ORANGE, "HOT"),
    (DRIVE_YELLOW, "YELLOW", AMBER, "WARM"),
    (0.0, "GREEN", GREEN, "NOMINAL"),
]
DRIVE_ZONE_SEVERITY = {"GREEN": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}


def drive_zone_for(value):
    """Classify a drive's live Composite Temperature into a zone, or None if unavailable."""
    if value is None:
        return None
    for floor, key, color, label in DRIVE_ZONES:
        if value >= floor:
            return {"key": key, "color": color, "label": label}
    return None  # unreachable, floor=0.0 always matches


# --- New component classes (GPU sub-sensors, RAM). Each gets its own table, per the task's
# "keep constants separate so we can tune them later" - deliberately NOT reusing CPU/drive
# tables even where numbers happen to be similar, and never applied to a different component.
GPU_CORE_ZONES = [
    (90.0, "RED", RED, "CRITICAL"),
    (83.0, "ORANGE", ORANGE, "HOT"),
    (75.0, "YELLOW", AMBER, "WARM"),
    (0.0, "GREEN", GREEN, "NOMINAL"),
]
GPU_HOTSPOT_ZONES = [
    (105.0, "RED", RED, "CRITICAL"),
    (95.0, "ORANGE", ORANGE, "HOT"),
    (85.0, "YELLOW", AMBER, "WARM"),
    (0.0, "GREEN", GREEN, "NOMINAL"),
]
GPU_VRAM_ZONES = [
    (105.0, "RED", RED, "CRITICAL"),
    (100.0, "ORANGE", ORANGE, "HOT"),
    (90.0, "YELLOW", AMBER, "WARM"),
    (0.0, "GREEN", GREEN, "NOMINAL"),
]
RAM_ZONES = [
    (75.0, "RED", RED, "CRITICAL"),
    (65.0, "ORANGE", ORANGE, "HOT"),
    (55.0, "YELLOW", AMBER, "WARM"),
    (0.0, "GREEN", GREEN, "NOMINAL"),
]
ZONE_SEVERITY = {"GREEN": 0, "YELLOW": 1, "ORANGE": 2, "RED": 3}  # generic; used only by zone_for() below
ALERT_DEBOUNCE_S = 3.0  # generic; used only by the new per-sensor engine below


def zone_for(value, table):
    """Generic (floor, key, color, label) classifier for the new sensor classes above.
    cpu_zone_for/drive_zone_for are intentionally separate, untouched functions."""
    if value is None:
        return None
    for floor, key, color, label in table:
        if value >= floor:
            # "short" duplicates "label" - present so this drop-in matches cpu_zone_for's shape
            # (MetricCard.update_value reads zone["short"] for the card's compact status badge).
            return {"key": key, "color": color, "label": label, "short": label}
    return None


# ATX rail nominals or the +-5% ATX spec, keyed by EXACT (case-insensitive) sensor name - not a
# substring match - so an ambiguous/unlabeled rail (e.g. "Voltage #1") is never guessed at.
ATX_NOMINAL = {
    "+12v": (12.00, 11.40, 12.60),
    "12v": (12.00, 11.40, 12.60),
    "+5v": (5.00, 4.75, 5.25),
    "5v": (5.00, 4.75, 5.25),
    "avcc3": (3.30, 3.135, 3.465),  # Nuvoton's standard name for the +3.3V analog rail
}


# Per-sensor label metadata for a raw reading whose PHYSICAL MEANING was investigated and could
# NOT be confidently verified (Classification B: real, responds to real thermal load, but the
# LHM-supplied name/location can't be trusted - see the PCIe x1 investigation). This is NOT a
# floating/bogus reading, so it must keep displaying its live value with no threshold, no zone,
# and no alert - only the label/status shown next to it changes. Keyed via sensor_identity()
# (see below): the production Identifier when the bridge provides one, or the exact (Parent,
# Name, SensorType) fallback tuple otherwise (Tier 2/3, or an older bridge without Identifier) -
# so this only ever applies to the specific investigated sensor on this specific board, never
# broadened to other motherboard sensors just because a reading looks high. To flag another
# sensor the same way later, add its identity here; the rendering code stays untouched (no
# scattered per-sensor string checks).
_PCIE_X1_UNVERIFIED_META = {
    "suffix": "*",
    "status": "UNVERIFIED",
    "color": DIM,  # neutral - this is not a thermal-health (NOMINAL/WARM/HOT/CRITICAL) color
    "note": "* Sensor label unverified · raw reading only",
}
UNVERIFIED_SENSOR_LABELS = {
    "/lpc/nct6687d/0/temperature/5": _PCIE_X1_UNVERIFIED_META,  # preferred: production Identifier
    ("SuperIO Nuvoton NCT6687D", "PCIe x1", "Temperature"): _PCIE_X1_UNVERIFIED_META,  # fallback: no Identifier available
}


def sensor_identity(sensor):
    """Canonical identity for a raw sensor dict from lhm_sensors(): prefers LibreHardwareMonitor's
    own stable Identifier field when present and non-empty (survives Name changes across BIOS/LHM
    versions); falls back to a (Parent, Name, SensorType) tuple otherwise. ALL identity decisions
    in this file (row-cache keys, sensor-label metadata lookups) flow through this one helper
    rather than each call site choosing its own ad hoc key."""
    ident = sensor.get("Identifier")
    if ident:
        return ident
    return (sensor.get("Parent", ""), sensor.get("Name", ""), sensor.get("SensorType", ""))


HISTORY_SECONDS = 24 * 3600
POLL_SECONDS = 2
MAX_SAMPLES = HISTORY_SECONDS // POLL_SECONDS
# IP/gateway/Wi-Fi signal refresh cadence, in POLL_SECONDS ticks - 15 ticks = 30s. These change
# far less often than the 2s live-Mbps rate needs (a lease renewal, not a per-second event), so
# paying GetAdaptersAddresses' buffer allocation and a WLAN handle open/close every single tick
# would be pure overhead; see worker()'s net_slow.
NET_SLOW_REFRESH_TICKS = 15

RANGES = [("15M", 15 * 60), ("1H", 3600), ("6H", 6 * 3600), ("24H", 24 * 3600)]

# ---------------------------------------------------------------------------
# Where persistent state lives. EVERY store this app writes is derived from DATA_DIR and from
# DATA_DIR alone - never from Path(__file__) directly. Two distinct reasons:
#
#   1. DATA is not CODE, even when they default to the same folder. Code resources
#      (LibreHardwareMonitorLib.dll, sensor_bridge.ps1) stay anchored to app.py's own directory
#      below and must NOT follow this setting anywhere.
#   2. THERMAL_WATCH_DATA_DIR lets a process redirect every store elsewhere BEFORE this module is
#      imported. The verification suite uses exactly that to run wholly inside a temp directory,
#      which is what makes it structurally incapable of touching real history: a verify script no
#      longer promises not to delete the production event log - it cannot NAME it. That guarantee
#      is not theoretical. A verify script's fixture-setup helper unlinked the real
#      thermal_watch_events.log on 2026-08-12 and destroyed ~166 KB of irreplaceable history; the
#      environment-variable indirection here, plus tools/verify_isolation.py's byte-for-byte hash
#      gate over the whole production directory, is the structural fix for that class of accident.
#
# The default is unchanged - next to app.py - so a normal launch behaves exactly as it always has.
# Anything that legitimately needs to REWRITE a store in place (a schema migration, a maintenance
# compaction) must go through backup_store() below first; that is a separate concern from this
# redirection, and it applies in production, where redirection deliberately does not.
# ---------------------------------------------------------------------------
# The base directory CODE resources (and, by default, DATA) resolve against. Path(__file__)
# alone is wrong once this app is packaged: a frozen PyInstaller build's __file__ points inside
# the bundle's internal extraction folder, never the real folder the .exe and its sibling
# resources (LibreHardwareMonitor/, sensor_bridge.ps1) actually live in. sys.frozen/sys.executable
# are the standard way to detect and correct for that. For a normal `python app.py` launch
# (sys.frozen unset), this is exactly Path(__file__).parent - unchanged from before.
_APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
DATA_DIR = Path(os.environ.get("THERMAL_WATCH_DATA_DIR") or _APP_DIR).resolve()


def data_path(filename):
    """The one place a persistent store's location is decided. Everything below uses this rather
    than Path(__file__).with_name(), so redirecting DATA_DIR can never miss a store by oversight."""
    return DATA_DIR / filename


def backup_store(path, tag="backup"):
    """Copy a persistent store aside before an operation that will legitimately modify it in place
    (a schema migration, a compaction, a corruption repair) - the recovery half of the safety
    story, and deliberately NOT something tests rely on: a test must never reach a production store
    at all, so this exists for real maintenance running against real data.

    Returns the backup path, or None if there was nothing to back up (a store that doesn't exist
    yet needs no protection). A failure to WRITE the backup returns None too, and the caller is
    expected to treat that as "do not proceed with the destructive step" rather than pressing on -
    an unbacked-up migration is exactly the trade this project should never make silently."""
    path = Path(path)
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    target = path.with_name(f"{path.name}.{tag}-{stamp}")
    try:
        shutil.copy2(path, target)
    except OSError:
        return None
    return target


def atomic_write_lines(path, lines):
    """Rewrite a whole JSONL-style store SAFELY: write a sibling .tmp, then Path.replace() it over
    the original (an atomic same-volume rename - the same pattern the active-incident/session
    snapshots already use). The three retention passes that run at every startup - events,
    incidents, sessions - each rewrite their entire store to drop expired records, and each
    previously did it with a plain open("w"), which TRUNCATES FIRST: a crash, power loss or kill
    between the truncate and the final write left the store empty or half-written, with the
    original already gone. A transaction is the right protection there rather than a backup - a
    routine startup prune must not litter the directory with a copy on every launch; backup_store()
    above is for the rarer one-way operations. Returns False if the store couldn't be replaced,
    in which case the ORIGINAL file is still intact on disk."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False
    return True


LOG_RETENTION_DAYS = 30
EVENT_LOG_PATH = data_path("thermal_watch_events.log")

# Incident history: a *separate* store from the Event Log above. An event is a point-in-time
# transition/message; an incident is the complete episode (start -> peak/escalation ->
# recovery) that a real, debounced alert transition represents. Stored next to app.py, same as
# EVENT_LOG_PATH - not C:\ProgramData (which is the ELEVATED bridge's directory) - so writing
# it never depends on elevation, consistent with how the event log already works.
INCIDENT_RETENTION_DAYS = 30
INCIDENTS_PATH = data_path("thermal_watch_incidents.jsonl")
INCIDENT_MAX_SAMPLES = 120  # ~4-8 min of an incident's own temp samples before decimating

# Active-incident durability: a *separate* file from INCIDENTS_PATH (which only ever holds
# fully CLOSED incidents). This one holds a live snapshot of whatever is in incidents_active
# (plus anything still awaiting post-restart reconciliation), so an incident survives a clean
# close, a crash, or a restart instead of just vanishing. Same directory/atomicity pattern as
# everything else here: write to .tmp, then Path.replace() (atomic same-volume rename).
ACTIVE_INCIDENTS_PATH = data_path("thermal_watch_active_incidents.json")
# How long to wait after startup before deciding what to do with a restored incident - long
# enough for the (untouched) debounce engine to get at least one real chance to reconfirm a
# still-hot sensor (ALERT_DEBOUNCE_S/CPU_ALERT_DEBOUNCE_S/DRIVE_ALERT_DEBOUNCE_S are all 3.0s)
# plus a couple of poll cycles so LHM data has actually refreshed.
RECONCILE_DELAY_MS = 8000
ACTIVE_INCIDENTS_FLUSH_INTERVAL_MS = 10000  # periodic throttle for non-escalation changes

# Which workload list (CPU-heavy vs GPU-heavy processes) is the more meaningful signal for each
# incident component - the exact same bias choices _log_alert_with_workload()/
# _bias_for_sensor_key() already use for live alert text, reused here for consistency. Drives
# get None: no confident per-process disk-I/O attribution source exists (see the drive alert
# code) - never invented for incidents either.
# "network" (v1.1 Phase 6): no per-process CPU/GPU workload bias makes sense for a connectivity
# incident (unlike a thermal one, there's no established reason network loss correlates with
# whichever process happens to be CPU/GPU-heaviest) - None, same as "drive".
INCIDENT_BIAS = {"cpu": "cpu", "gpu_core": "gpu", "gpu_hotspot": "gpu", "gpu_vram": "gpu", "ram": "cpu",
                 "drive": None, "network": None}
COMPONENT_LABELS = {
    "cpu": "CPU Package", "gpu_core": "GPU Core", "gpu_hotspot": "GPU Hotspot",
    "gpu_vram": "GPU Memory Junction", "ram": "RAM", "drive": "Drive", "network": "Network Connectivity",
}


# ---------------------------------------------------------------------------
# Workload session tracking - a layer independent from both the thermal alert/incident engine
# and the sensor bridge. Sessions observe the SAME 2s workload snapshot (self.last_cpu_top/
# self.last_gpu_top/self.last_context, already produced by the untouched worker thread) that
# incident workload-attribution already reads; nothing here polls a process list, queries PDH,
# or touches a sensor a second time. Sessions and incidents are linked only by read-only
# correlation (_session_link_incidents) - neither system drives the other.
#
# Threshold/debounce/grace values below are deliberately separate constants from every thermal
# threshold (CPU_YELLOW, GPU_HOTSPOT_ZONES, etc.) - a "meaningful workload" and a "meaningful
# temperature" are unrelated concepts and must never share a knob.
#
#   SESSION_CPU_ACTIVE_PCT / SESSION_GPU_ACTIVE_PCT: idle background apps (Explorer, Discord,
#   RGB/control utilities, a browser sitting on a static page) observed on this machine sit
#   under ~5% CPU and under ~2% GPU almost all the time, with only occasional brief spikes
#   (a browser compositing a scroll, Discord decoding a notification sound). 12%/10% sit clearly
#   above that noise floor while remaining far below any real game/render/inference workload
#   (which routinely sits at 40-99% on at least one of CPU or GPU). Either dimension alone can
#   qualify - a CPU-bound workload (Blender CPU render) may barely touch the GPU, and a
#   GPU-bound one (a game) may only lightly use the CPU.
#   SESSION_START_DEBOUNCE_SAMPLES: 3 consecutive qualifying 2s samples (~6s sustained) - long
#   enough that a single-tick spike (a compile step, a page load) can never open a session, per
#   the explicit "GPU 83% for 6 seconds -> session starts" example.
#   SESSION_IDLE_GRACE_S: 90s - the middle of the suggested 60-120s range. Long enough to cover
#   a game's loading screen/menu/cutscene or an AI workload pausing between generations without
#   the 2s poll cadence needing to guess further; short enough that two genuinely separate
#   short sessions (e.g. quick unrelated app launches) aren't merged into one.
#   SESSION_GAP_THRESHOLD_S: a real-time gap this large between two consecutive session-engine
#   ticks (system sleep, a debugger pause, the process stalling) is treated as unmonitored time -
#   no zone-time or foreground-time is ever attributed across it, matching the incident engine's
#   own monitoring-gap philosophy.
SESSION_CPU_ACTIVE_PCT = 12.0
SESSION_GPU_ACTIVE_PCT = 10.0
SESSION_START_DEBOUNCE_SAMPLES = 3
SESSION_IDLE_GRACE_S = 90.0
SESSION_GAP_THRESHOLD_S = 10.0

SESSION_RETENTION_DAYS = INCIDENT_RETENTION_DAYS
SESSIONS_PATH = data_path("thermal_watch_sessions.jsonl")
ACTIVE_SESSIONS_PATH = data_path("thermal_watch_active_sessions.json")
# Long enough for a genuinely-still-active workload to reconfirm SESSION_START_DEBOUNCE_SAMPLES
# (~6s) worth of real post-restart ticks with margin; short enough the user isn't left wondering
# for long whether a session survived restart.
SESSION_RECONCILE_DELAY_MS = 10000
SESSION_ACTIVE_FLUSH_INTERVAL_MS = ACTIVE_INCIDENTS_FLUSH_INTERVAL_MS

# Thermal-zone time (item 10) is tracked only for components with a single, unambiguous
# workload-relevant reading and an existing zone table - CPU package and the three GPU
# sub-sensors. Drive and per-DIMM RAM temperatures are shared/multi-instance and not a
# workload's own thermal signature, so they're deliberately excluded (no new threshold
# invented to force a single number out of them either).
SESSION_ZONE_TABLES = {
    "cpu": None,  # None => use cpu_zone_for() directly, same as everywhere else in this file
    "gpu_core": GPU_CORE_ZONES,
    "gpu_hotspot": GPU_HOTSPOT_ZONES,
    "gpu_vram": GPU_VRAM_ZONES,
}
SESSION_ZONE_CONTEXT_KEY = {"cpu": "cpu_temp", "gpu_core": "gpu_core_temp",
                           "gpu_hotspot": "gpu_hotspot_temp", "gpu_vram": "gpu_vram_temp"}

# Streaming per-session metric keys - each accumulated as {count, sum, max} only (item 7: no
# unlimited raw sample list kept just to compute an average). net_down_mbps/net_up_mbps (v1.1
# Phase 5) are the ACTIVE ADAPTER's whole-machine rate during this session's window - the same
# "reading observed while this workload was active" semantic cpu_temp/gpu_temp already use, not
# an attribution claim that this workload caused that traffic (no per-process network data is
# session-scoped this app - Phase 2's per-process bytes are a separate, live-only view).
SESSION_METRIC_KEYS = ("cpu_temp", "cpu_util", "cpu_power", "gpu_core_temp", "gpu_hotspot_temp",
                       "gpu_vram_temp", "gpu_util", "gpu_power", "mem_pct", "proc_cpu_pct", "proc_gpu_pct",
                       "net_down_mbps", "net_up_mbps")


def _agg_new():
    return {"count": 0, "sum": 0.0, "max": None}


def _agg_add(agg, value):
    """Adds one real sample. A missing (None) value contributes nothing - never treated as 0,
    so a session's average is always over samples that actually measured something (item 6)."""
    if value is None:
        return
    agg["count"] += 1
    agg["sum"] += value
    agg["max"] = value if agg["max"] is None else max(agg["max"], value)


def _agg_result(agg):
    """{'avg', 'peak', 'count'}, or None if this metric was never once observed - callers must
    never substitute 0 for a metric that simply wasn't available this session."""
    if not agg or agg["count"] == 0:
        return None
    return {"avg": agg["sum"] / agg["count"], "peak": agg["max"], "count": agg["count"]}


def _new_session_record(key, display_name, pid, start_ts):
    """The single working representation of a workload session, used identically whether it's
    still an unconfirmed candidate (item 2's start debounce) or a fully confirmed session -
    only the 'confirmed' flag and presence of a real session_id differ, so accumulation code
    never has to special-case one or the other. start_timestamp is set here, at the FIRST
    qualifying sample, and never moved later - so a session confirmed on its 3rd sample still
    reports the true start of the sustained window, not the confirmation moment (item 2)."""
    return {
        "session_id": None, "confirmed": False, "_consecutive": 0,
        "workload_key": key, "workload": display_name, "process_name": display_name,
        "starting_pid": pid, "observed_pids": [pid] if pid is not None else [],
        "start_timestamp": start_ts, "end_timestamp": None,
        "duration_seconds": None, "duration_exact": True,
        "last_active_timestamp": start_ts, "last_observed_timestamp": start_ts,
        "foreground_seconds": 0.0,
        "agg": {k: _agg_new() for k in SESSION_METRIC_KEYS},
        "zone_time": {c: {"GREEN": 0.0, "YELLOW": 0.0, "ORANGE": 0.0, "RED": 0.0} for c in SESSION_ZONE_TABLES},
        "incident_ids": [], "max_incident_severity": None,
        "monitoring_gaps": [], "close_reason": None,
    }


# ---------------------------------------------------------------------------
# Long-term telemetry history - a low-frequency AGGREGATE layer, entirely separate from the
# incident/session engines and from the live dashboard. Reuses the exact same 2s snapshot
# (self.last_context plus the already-filtered drive/DIMM/motherboard sensor lists update_data()
# computes for rendering) - no new hardware poll, no new process/PDH call. Samples are folded
# into a fixed-size streaming {count,sum,min,max} accumulator per metric and never kept as raw
# points, so memory use is bounded regardless of how long Thermal Watch has been running.
#
#   TELEMETRY_BUCKET_SECONDS = 60: fine enough to see real thermal movement (a game launching,
#   a fan ramping) without persisting a row for every single 2s poll (30x fewer writes/rows).
#   TELEMETRY_RETENTION_DAYS = 30: matches INCIDENT_RETENTION_DAYS/SESSION_RETENTION_DAYS - one
#   consistent "how far back does Thermal Watch remember" policy across every store.
#   TELEMETRY_GAP_BUCKETS = 3: on the chart, a run of 3+ consecutive MISSING minute-buckets
#   (>=3 min with no telemetry - the app was closed, asleep, or crashed) draws as a visible break
#   rather than a smooth interpolated line, matching the incident/session monitoring-gap
#   philosophy - never implying data existed when it didn't. A single occasionally-missing
#   bucket (a slow poll tick, a bridge hiccup) is common/harmless and doesn't need its own gap
#   marker; only a sustained absence does.
# ---------------------------------------------------------------------------
TELEMETRY_BUCKET_SECONDS = 60
TELEMETRY_RETENTION_DAYS = 30
# JSONL was Storage v1 (see the prior task). Storage v2 moves to SQLite for indexed
# timestamp/sensor range queries - TELEMETRY_JSONL_PATH now names only the legacy file a
# fresh SQLite store migrates from once, on first startup after upgrading; nothing live ever
# reads or writes it again afterward.
TELEMETRY_JSONL_PATH = data_path("thermal_watch_telemetry.jsonl")
TELEMETRY_DB_PATH = data_path("thermal_watch_telemetry.db")
TELEMETRY_GAP_BUCKETS = 3

# Single-instance ("scalar") metrics, one aggregate per key per bucket - sourced directly from
# self.last_context (already built once per tick for incident context-peak tracking) plus
# additive keys (gpu_vram_used_mb, then cpu_fan_rpm/gpu_fan_pct for Cooling/Fan Intelligence)
# appended to that same dict for this purpose. cpu_fan_rpm/gpu_fan_pct were ALREADY being
# collected into last_context for Cross-Sensor Diagnostics' live-only checks - this is the first
# phase that actually PERSISTS them, which is what lets fan-speed-vs-temperature be correlated
# from real accumulated history instead of the current live moment alone.
TELEMETRY_SCALAR_KEYS = ("cpu_temp", "cpu_util", "cpu_power", "gpu_core_temp", "gpu_hotspot_temp",
                        "gpu_vram_temp", "gpu_util", "gpu_power", "gpu_vram_used_mb", "mem_pct",
                        "cpu_fan_rpm", "gpu_fan_pct", "net_down_mbps", "net_up_mbps",
                        "net_rx_bytes", "net_tx_bytes")
TELEMETRY_SCALAR_CONTEXT_MAP = {
    "cpu_temp": "cpu_temp", "cpu_util": "cpu_load", "cpu_power": "cpu_power",
    "gpu_core_temp": "gpu_core_temp", "gpu_hotspot_temp": "gpu_hotspot_temp", "gpu_vram_temp": "gpu_vram_temp",
    "gpu_util": "gpu_load", "gpu_power": "gpu_power", "gpu_vram_used_mb": "gpu_vram_used_mb", "mem_pct": "mem_pct",
    "cpu_fan_rpm": "cpu_fan_rpm", "gpu_fan_pct": "gpu_fan_pct",
    "net_down_mbps": "net_down_mbps", "net_up_mbps": "net_up_mbps",
    "net_rx_bytes": "net_rx_bytes", "net_tx_bytes": "net_tx_bytes",
}
# (display name, unit, is_temperature) - is_temperature gates the incident-overlay component
# lookup below (only temperature sensors have a matching thermal-incident component) and the
# avg/max/min chart-line semantics (item 8: "for temperature sensors, allow average/maximum").
TELEMETRY_SCALAR_LABELS = {
    "cpu_temp": ("CPU Package", "°C", True), "cpu_util": ("CPU Utilization", "%", False),
    "cpu_power": ("CPU Power", "W", False), "gpu_core_temp": ("GPU Core", "°C", True),
    "gpu_hotspot_temp": ("GPU Hotspot", "°C", True), "gpu_vram_temp": ("GPU Memory Junction", "°C", True),
    "gpu_util": ("GPU Utilization", "%", False), "gpu_power": ("GPU Power", "W", False),
    "gpu_vram_used_mb": ("GPU VRAM Used", "MB", False), "mem_pct": ("System RAM Usage", "%", False),
    "cpu_fan_rpm": ("CPU Fan", "RPM", False),
    # nvidia-smi's fan.speed is a PERCENTAGE, not RPM - most discrete GPUs don't expose fan RPM
    # the way LHM exposes "CPU Fan" (see Cross-Sensor Diagnostics' own scope note); tracked here
    # as-is rather than converted/estimated into a fabricated RPM figure.
    "gpu_fan_pct": ("GPU Fan", "%", False),
    # Unit carries a leading space (unlike every other unit here) because every consumer of
    # TELEMETRY_SCALAR_LABELS' unit concatenates it directly onto a formatted number
    # ({value:.0f}{unit}, no space in the template) - correct for "60" + "°C" = "60°C",
    # but would render "45" + "Mbps" = "45Mbps" with no space at all. v1.1 Phase 8 found this
    # pre-existing Phase 1 gap while wiring up network idle baselines through SensorHistoryWindow.
    "net_down_mbps": ("Network Download", " Mbps", False), "net_up_mbps": ("Network Upload", " Mbps", False),
    # Monotonic counters (bytes since the adapter last came up) - a bucket's "avg" is not
    # meaningful for these the way it is for a temperature, but min/max ARE: since the counter
    # only ever increases, a bucket's min is its first observed value and max is its last, which
    # is exactly "how much had this adapter moved by the end of this minute". Tracked through the
    # exact same bucket/avg/min/max machinery as every other scalar rather than inventing a
    # second aggregation scheme just for counters.
    "net_rx_bytes": ("Network RX (cumulative)", "B", False),
    "net_tx_bytes": ("Network TX (cumulative)", "B", False),
}
# Which existing incident `component` a scalar/per-sensor metric corresponds to, for overlay
# matching (item 9) - drive/RAM per-sensor entries map via their own component string directly
# (drive -> "drive", dimm -> "ram"), so only the scalar keys need an explicit table here.
TELEMETRY_SCALAR_INCIDENT_COMPONENT = {
    "cpu_temp": "cpu", "gpu_core_temp": "gpu_core", "gpu_hotspot_temp": "gpu_hotspot", "gpu_vram_temp": "gpu_vram",
}


def scalar_sensor_ref(key):
    """A TELEMETRY_SCALAR_KEYS entry -> the sensor_ref dict SensorHistoryWindow/drill-down
    clicks expect, built from the one canonical TELEMETRY_SCALAR_LABELS table so a scalar's
    label/unit/is_temp/component is never hand-duplicated at each click-binding call site."""
    label, unit, is_temp = TELEMETRY_SCALAR_LABELS[key]
    return {"kind": "scalar", "key": key, "label": label, "unit": unit, "is_temp": is_temp,
           "component": TELEMETRY_SCALAR_INCIDENT_COMPONENT.get(key)}

# Display-only downsampling targets (item 15) - the underlying per-minute buckets on disk are
# never altered; only what a chart draws is grouped further for long ranges. Values are the
# number of native 60s buckets folded into one displayed point.
TELEMETRY_DOWNSAMPLE_GROUPING = {
    "1h": 1, "6h": 2, "24h": 5, "7d": 30, "30d": 120,  # 120 x 60s = 2h groups
}
TELEMETRY_RANGE_SECONDS = {"1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def _bucket_agg_new():
    return {"count": 0, "sum": 0.0, "min": None, "max": None}


def _bucket_agg_add(agg, value):
    """Adds one real sample. A missing (None) value contributes nothing - never treated as 0
    (item 2: "missing values remain missing")."""
    if value is None:
        return
    agg["count"] += 1
    agg["sum"] += value
    agg["min"] = value if agg["min"] is None else min(agg["min"], value)
    agg["max"] = value if agg["max"] is None else max(agg["max"], value)


def _bucket_agg_result(agg):
    """{'avg','min','max','count'}, or None if this metric was never once observed this
    bucket - never substituted with 0."""
    if not agg or agg["count"] == 0:
        return None
    return {"avg": agg["sum"] / agg["count"], "min": agg["min"], "max": agg["max"], "count": agg["count"]}


def _sensor_bucket_key(identity):
    """sensor_identity() returns either a string (LHM Identifier) or a (Parent, Name,
    SensorType) fallback tuple - JSON object keys must be strings, so the tuple form is joined
    into one deterministic string ONLY for use as a dict key. The real identity fields
    (identifier/parent/name/sensor_type) are still stored verbatim inside the record itself
    (item 16), so nothing about the true identity is lost or invented in this join."""
    return identity if isinstance(identity, str) else "|".join(identity)


def _new_telemetry_bucket(start_ts):
    return {
        "start_timestamp": start_ts, "end_timestamp": None, "sample_count": 0,
        "scalars": {k: _bucket_agg_new() for k in TELEMETRY_SCALAR_KEYS},
        "sensors": {},  # _sensor_bucket_key(identity) -> {"identifier","parent","name","sensor_type","component","unverified","agg"}
    }


# ---------------------------------------------------------------------------
# Storage v2: SQLite-backed telemetry store. Replaces Storage v1 (JSONL, see
# TELEMETRY_JSONL_PATH above) with two indexed tables so a range query costs one indexed scan
# instead of a linear text parse - the previous implementation's `since_ts` reverse-chunk reader
# is gone entirely; SQLite's own B-tree index on start_timestamp does that job properly. Every
# function below returns/accepts the EXACT SAME bucket dict shape Storage v1 always used
# ({'start_timestamp','end_timestamp','sample_count','scalars':{...},'sensors':{...}}), so
# SensorHistoryWindow, TelemetryChart, normalize_bucket_series, downsample_series,
# compute_coverage, and every overlay function are completely unaware the backend changed.
# ---------------------------------------------------------------------------
_TELEMETRY_SCHEMA_STATEMENTS = [
    # scalars are stored as ONE JSON text column, not one column per metric-stat: they're always
    # read/written as a single unit (never filtered/sorted by an individual scalar's value), and
    # measurement showed a 40-column wide row costs MORE to unmarshal through Python's sqlite3
    # row protocol than one json.loads() call per row does - see measure_telemetry_overhead.py's
    # v1(JSONL)-vs-v2(SQLite) comparison, which is exactly what caught this.
    "CREATE TABLE IF NOT EXISTS buckets (\n  start_timestamp REAL PRIMARY KEY,\n  end_timestamp REAL,\n"
    "  sample_count INTEGER NOT NULL,\n  scalars_json TEXT\n)",
    "CREATE INDEX IF NOT EXISTS idx_buckets_end ON buckets(end_timestamp)",
    "CREATE TABLE IF NOT EXISTS sensor_readings (\n"
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  start_timestamp REAL NOT NULL,\n  sensor_key TEXT NOT NULL,\n"
    "  identifier TEXT, parent TEXT, name TEXT, sensor_type TEXT, component TEXT, unverified INTEGER,\n"
    "  avg REAL, min REAL, max REAL, count INTEGER,\n"
    "  UNIQUE(start_timestamp, sensor_key)\n)",
    "CREATE INDEX IF NOT EXISTS idx_sensor_readings_key_time ON sensor_readings(sensor_key, start_timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_sensor_readings_time ON sensor_readings(start_timestamp)",
    "CREATE TABLE IF NOT EXISTS telemetry_meta (key TEXT PRIMARY KEY, value TEXT)",
]


def _telemetry_db_connect(path):
    """A short-lived connection with the pragmas this store needs for crash-safety: WAL
    journaling (survives an app crash or power loss mid-write without corrupting the main file -
    the write-ahead log is replayed or truncated cleanly the next time anything opens the
    database) and NORMAL synchronous (durable across an application crash; the standard,
    widely-recommended pairing with WAL). isolation_level=None puts the connection in autocommit
    mode so every write site below uses an explicit BEGIN/COMMIT/ROLLBACK it fully controls,
    rather than sqlite3's own implicit-transaction guessing."""
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.DatabaseError:
        # A corrupt file fails right here, before this function ever returns - the caller's own
        # `conn = _telemetry_db_connect(path)` assignment never completes, so it can't close
        # this connection itself. Close it here instead: on Windows, an unclosed handle blocks
        # the caller's very next step (renaming the corrupt file aside).
        conn.close()
        raise
    return conn


def _telemetry_db_init(conn):
    conn.execute("BEGIN")
    try:
        for stmt in _TELEMETRY_SCHEMA_STATEMENTS:
            conn.execute(stmt)
        conn.commit()
    except sqlite3.DatabaseError:
        conn.rollback()
        raise


def open_telemetry_db(path=None):
    """Opens (creating and/or schema-initializing as needed) the telemetry SQLite store. If the
    file exists but isn't a valid SQLite database (corruption), the corrupt file is moved aside
    with a timestamped suffix and a fresh empty store is created in its place - Thermal Watch
    must still launch on damaged telemetry storage, exactly like the JSONL era's tolerance for a
    malformed line (item 19), just at the file level instead of the line level. Returns None
    only if even a fresh database can't be created (e.g. an unwritable directory) - callers must
    treat that as 'no telemetry available this session', never crash the caller."""
    path = path or TELEMETRY_DB_PATH
    conn = None
    try:
        conn = _telemetry_db_connect(path)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")  # forces a real page read, surfaces corruption now
        _telemetry_db_init(conn)
        return conn
    except sqlite3.DatabaseError:
        # The failed connection above still holds an open handle on `path` - on Windows a file
        # with an open handle can't be renamed (unlike POSIX), so it must be closed before the
        # corrupt file can be moved aside.
        if conn is not None:
            conn.close()
        try:
            if path.exists():
                path.rename(path.with_name(f"{path.stem}.corrupt-{int(time.time())}{path.suffix}"))
            conn = _telemetry_db_connect(path)
            _telemetry_db_init(conn)
            return conn
        except OSError:
            return None
    except OSError:
        return None


def read_telemetry_file(since_ts=None, sensor_key=None):
    """All persisted telemetry buckets, oldest first - Storage v2: one indexed range query for
    bucket scalars, plus (only when sensor_key is given) a SECOND indexed query for that ONE
    sensor's readings specifically. sensor_key is opt-in rather than "fetch every sensor for
    every bucket" because normalize_bucket_series() - the only real consumer of a bucket's
    'sensors' dict - only ever looks up ONE key per call anyway (whichever sensor_ref it was
    given); fetching all of them for a purely-scalar chart (the common case - CPU/GPU
    temperature, power, utilization) was pure waste, and measurement showed it was the single
    biggest cost in a 24h/7d/30d query. Callers see the exact same nested dict shape Storage v1
    always returned; 'sensors' is simply empty ({}) unless sensor_key was requested. A corrupt/
    unreadable store yields an empty list rather than crashing the caller - see
    open_telemetry_db()."""
    conn = open_telemetry_db()
    if conn is None:
        return []
    try:
        cols = "start_timestamp, end_timestamp, sample_count, scalars_json"
        if since_ts is None:
            rows = conn.execute(f"SELECT {cols} FROM buckets ORDER BY start_timestamp").fetchall()
        else:
            rows = conn.execute(f"SELECT {cols} FROM buckets WHERE start_timestamp >= ? ORDER BY start_timestamp",
                               (since_ts,)).fetchall()
        buckets, order = {}, []
        for start_ts, end_ts, sample_count, scalars_json in rows:
            try:
                scalars = json.loads(scalars_json) if scalars_json else {}
            except ValueError:
                scalars = {}
            buckets[start_ts] = {"start_timestamp": start_ts, "end_timestamp": end_ts, "sample_count": sample_count,
                                 "scalars": scalars, "sensors": {}}
            order.append(start_ts)
        if buckets and sensor_key is not None:
            where = "sensor_key = ? AND start_timestamp >= ?" if since_ts is not None else "sensor_key = ?"
            params = (sensor_key, order[0]) if since_ts is not None else (sensor_key,)
            sensor_rows = conn.execute(
                "SELECT start_timestamp, identifier, parent, name, sensor_type, component, "
                f"unverified, avg, min, max, count FROM sensor_readings WHERE {where} "
                "ORDER BY start_timestamp", params).fetchall()
            for sr in sensor_rows:
                b = buckets.get(sr[0])
                if b is None:
                    continue
                b["sensors"][sensor_key] = {"identifier": sr[1], "parent": sr[2], "name": sr[3], "sensor_type": sr[4],
                                            "component": sr[5], "unverified": bool(sr[6]),
                                            "avg": sr[7], "min": sr[8], "max": sr[9], "count": sr[10]}
        return [buckets[ts] for ts in order]
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


def read_sensor_summaries(start_ts, end_ts):
    """Per-sensor aggregates for EVERY per-sensor identity (drives, DIMMs, motherboard) recorded in
    a window, in ONE grouped query. read_telemetry_file()'s sensor_key argument deliberately fetches
    a single sensor at a time - right for a drill-down chart, hopeless for a report that needs every
    drive and DIMM at once, which would otherwise be one query per sensor.

    The average is SUM(avg*count)/SUM(count) - the same count-weighted convention downsample_series
    already uses, not a new statistic - and `unverified` is carried through untouched so a report
    can render an unverified sensor's observed range without ever attaching a health conclusion to
    it (the PCIe x1 rule). Returns [] on a corrupt/unreadable store, like every other reader here."""
    conn = open_telemetry_db()
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT sensor_key, MAX(name), MAX(parent), MAX(sensor_type), MAX(component), MAX(unverified), "
            "SUM(avg * count) / NULLIF(SUM(count), 0), MIN(min), MAX(max), SUM(count) "
            "FROM sensor_readings WHERE start_timestamp >= ? AND start_timestamp < ? "
            "GROUP BY sensor_key ORDER BY sensor_key", (start_ts, end_ts)).fetchall()
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()
    return [{"sensor_key": r[0], "name": r[1], "parent": r[2], "sensor_type": r[3], "component": r[4],
            "unverified": bool(r[5]), "avg": r[6], "min": r[7], "max": r[8], "count": r[9]}
           for r in rows if r[9]]


def _read_jsonl_buckets_for_migration(path):
    """Tolerant one-time reader for the LEGACY Storage v1 JSONL format, used only by the
    migration path below - forward order, skips any malformed/blank line rather than aborting,
    mirroring the resilience Storage v1's own reader always had (item 19)."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("start_timestamp") is not None:
            out.append(rec)
    return out


def migrate_telemetry_jsonl_to_sqlite(conn, jsonl_path=None):
    """One-time, idempotent migration of legacy JSONL buckets into the SQLite store. Runs inside
    ONE transaction, so a crash mid-migration leaves either the fully-migrated state or the
    untouched pre-migration state, never a half-written one ('crash-safe transactions').
    Idempotent two ways: a 'jsonl_migration_done' marker in telemetry_meta skips the (possibly
    large) file entirely on every startup after the first, and even within a single run each
    bucket is INSERT OR IGNOREd keyed on start_timestamp (buckets) / (start_timestamp,
    sensor_key) (sensor_readings), so re-running against a partially-migrated database - the
    exact state a crash mid-migration would leave if the marker write itself didn't survive -
    can never duplicate or corrupt a row. Returns the number of NEW buckets migrated (0 on a
    no-op run, which is the normal case after the first successful migration)."""
    jsonl_path = jsonl_path or TELEMETRY_JSONL_PATH
    row = conn.execute("SELECT value FROM telemetry_meta WHERE key = 'jsonl_migration_done'").fetchone()
    if row and row[0] == "1":
        return 0
    conn.execute("BEGIN")
    migrated = 0
    try:
        if jsonl_path.exists():
            for bucket in _read_jsonl_buckets_for_migration(jsonl_path):
                scalars = bucket.get("scalars") or {}
                cur = conn.execute(
                    "INSERT OR IGNORE INTO buckets (start_timestamp, end_timestamp, sample_count, scalars_json) "
                    "VALUES (?, ?, ?, ?)",
                    (bucket["start_timestamp"], bucket.get("end_timestamp"), bucket.get("sample_count", 0),
                     json.dumps(scalars)))
                if cur.rowcount:
                    migrated += 1
                for sensor_key, s in (bucket.get("sensors") or {}).items():
                    conn.execute(
                        "INSERT OR IGNORE INTO sensor_readings (start_timestamp, sensor_key, identifier, parent, "
                        "name, sensor_type, component, unverified, avg, min, max, count) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (bucket["start_timestamp"], sensor_key, s.get("identifier"), s.get("parent"), s.get("name"),
                         s.get("sensor_type"), s.get("component"), int(bool(s.get("unverified"))),
                         s.get("avg"), s.get("min"), s.get("max"), s.get("count")))
        conn.execute("INSERT OR REPLACE INTO telemetry_meta (key, value) VALUES ('jsonl_migration_done', '1')")
        conn.commit()
    except sqlite3.DatabaseError:
        conn.rollback()
        raise
    return migrated


def build_telemetry_json_export(buckets, sensor_ref=None, filters=None):
    """Pure function: a portable JSON snapshot of telemetry buckets, in the same
    metadata-enveloped shape build_json_export() already uses for incidents - so telemetry
    history stays independently exportable even though the primary store is now SQLite rather
    than a human-readable file (the roadmap's 'preserve JSON export'). Never mutates buckets."""
    payload = {"export_timestamp": time.time(), "schema_version": EXPORT_SCHEMA_VERSION,
              "app_version": APP_VERSION, "count": len(buckets), "filters": dict(filters) if filters else {},
              "buckets": list(buckets)}
    if sensor_ref is not None:
        payload["sensor"] = {"kind": sensor_ref["kind"], "key": sensor_ref["key"], "label": sensor_ref["label"]}
    return payload


def prune_telemetry_buckets(buckets, retention_days=TELEMETRY_RETENTION_DAYS, now=None):
    """Pure function: drops buckets whose end_timestamp is older than the retention window.
    A bucket with no end_timestamp (shouldn't happen for a persisted/completed one, but never
    trust blindly) is kept rather than guessed away."""
    now = now if now is not None else time.time()
    cutoff = now - retention_days * 86400
    return [b for b in buckets if b.get("end_timestamp") is None or b["end_timestamp"] >= cutoff]


def filter_buckets_by_range(buckets, window_seconds, now=None):
    """Mirrors filter_incidents_by_range()'s semantics exactly (end_timestamp-based cutoff,
    None = no filter), applied to telemetry buckets instead of incidents."""
    if window_seconds is None:
        return list(buckets)
    now = now if now is not None else time.time()
    return [b for b in buckets if b.get("end_timestamp") is not None and (now - b["end_timestamp"]) <= window_seconds]


def compute_coverage(buckets, window_seconds, bucket_seconds=TELEMETRY_BUCKET_SECONDS):
    """(valid_bucket_count, expected_bucket_count, coverage_pct) for a time window - item 14.
    "Valid" means the bucket actually has at least one sample (sample_count > 0); a bucket that
    exists on disk but somehow recorded zero samples doesn't count as coverage either. Expected
    count is purely time-based (window_seconds / bucket_seconds), never inflated or reduced by
    what actually happened to exist - that's the whole point of a coverage metric."""
    valid = sum(1 for b in buckets if b.get("sample_count", 0) > 0)
    expected = max(1, round(window_seconds / bucket_seconds))
    pct = min(100.0, valid / expected * 100)
    return valid, expected, pct


def extract_bucket_metric(bucket, sensor_ref):
    """One bucket -> that sensor's {'avg','min','max','count'} for this bucket, or None if this
    sensor had no data this bucket. sensor_ref is {'kind': 'scalar'|'sensor', 'key': ...} -
    'scalar' looks up TELEMETRY_SCALAR_KEYS directly; 'sensor' looks up a specific drive/DIMM/
    motherboard identity's _sensor_bucket_key() inside the bucket's per-sensor breakdown."""
    if sensor_ref["kind"] == "scalar":
        return bucket.get("scalars", {}).get(sensor_ref["key"])
    return bucket.get("sensors", {}).get(sensor_ref["key"])


def normalize_bucket_series(buckets, sensor_ref):
    """Persisted buckets -> a flat point list for ONE sensor: [{'start_timestamp',
    'end_timestamp', 'metric': {...} or None}, ...]. A bucket that exists but never captured
    this particular sensor (e.g. a drive not detected that minute) yields metric=None - a real
    hole in that sensor's own series, distinct from a bucket that doesn't exist at all."""
    return [{"start_timestamp": b["start_timestamp"], "end_timestamp": b.get("end_timestamp"),
            "metric": extract_bucket_metric(b, sensor_ref)} for b in buckets]


def downsample_series(points, group_size, range_start):
    """Pure function: a normalize_bucket_series() point list -> display-resolution groups
    (item 15), preserving true min/max across the WHOLE group (never averaging away a short
    spike) while still averaging the average, weighted by each point's own sample count.
    Groups are aligned to fixed TELEMETRY_BUCKET_SECONDS*group_size time slots anchored at
    range_start - NOT grouped by list position - so a short run of points separated from the
    next run by a real gap is never silently merged across that gap just because few points
    exist near it. gap_before=True on a point means the elapsed real time since the PREVIOUS
    output point exceeds TELEMETRY_GAP_BUCKETS worth of buckets - the chart must break its line
    there rather than draw a continuous segment across unmonitored time (item 6)."""
    slot_seconds = max(1, group_size) * TELEMETRY_BUCKET_SECONDS
    slots = {}
    for p in points:
        slot_idx = int((p["start_timestamp"] - range_start) // slot_seconds)
        slots.setdefault(slot_idx, []).append(p)
    out = []
    prev_end = None
    for key in sorted(slots):
        chunk = slots[key]
        vals = [p["metric"] for p in chunk if p["metric"]]
        if vals:
            total = sum(v["count"] for v in vals)
            metric = {"avg": sum(v["avg"] * v["count"] for v in vals) / total if total else None,
                      "min": min(v["min"] for v in vals), "max": max(v["max"] for v in vals), "count": total}
        else:
            metric = None
        start_ts = min(p["start_timestamp"] for p in chunk)
        end_ts = max(p.get("end_timestamp") or p["start_timestamp"] for p in chunk)
        gap_before = prev_end is not None and (start_ts - prev_end) > TELEMETRY_GAP_BUCKETS * TELEMETRY_BUCKET_SECONDS
        out.append({"start_timestamp": start_ts, "end_timestamp": end_ts, "metric": metric, "gap_before": gap_before})
        prev_end = end_ts
    return out


def overlapping_incidents(incidents, start_ts, end_ts, component=None):
    """Incidents whose [start_timestamp, end_timestamp] overlaps [start_ts, end_ts] - item 9.
    An incident still active (end_timestamp is None) is treated as ongoing through "now" for
    overlap purposes. component=None means "any component" (used for a multi-metric chart)."""
    out = []
    for inc in incidents:
        if component is not None and inc.get("component") != component:
            continue
        inc_start = inc.get("start_timestamp")
        if inc_start is None:
            continue
        inc_end = inc.get("end_timestamp")
        if inc_end is None:
            inc_end = time.time()
        if inc_start <= end_ts and inc_end >= start_ts:
            out.append(inc)
    return out


def overlapping_sessions(sessions, start_ts, end_ts, workload_key=None):
    """Workload sessions whose [start_timestamp, end_timestamp] overlaps [start_ts, end_ts] -
    item 10. Never invents a session from raw process samples - only ever looks at ALREADY
    completed/persisted session records."""
    out = []
    for s in sessions:
        if workload_key is not None and s.get("workload_key") != workload_key:
            continue
        s_start = s.get("start_timestamp")
        s_end = s.get("end_timestamp")
        if s_start is None or s_end is None:
            continue
        if s_start <= end_ts and s_end >= start_ts:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Incident export/reporting - a pure, standalone layer around the (untouched) incident
# lifecycle. Nothing here reads live sensor state, mutates an incident record, or has any
# opinion about when an incident opens/escalates/closes; it only ever turns an already-recorded
# incident dict into CSV/JSON/plain-text output. Kept deliberately separate from the monitoring
# engine (item 12) so it's testable with plain dicts and no Tk/file-dialog involved.
# ---------------------------------------------------------------------------
EXPORT_SCHEMA_VERSION = "1.0"
APP_VERSION = "1.1.0"  # single source of truth for the app's release version - the header label
                        # derives its displayed "vX.Y.Z" from this constant rather than a second
                        # hardcoded literal, so the two can never silently drift apart again

# ---------------------------------------------------------------------------
# Evidence API (v1.1 Phase 10) - "Thermal Watch remains the evidence engine; Nox, or any other
# AI, can query it." File-based, not a network service: Thermal Watch periodically writes a
# structured snapshot to a known local path (same atomic tmp-then-replace pattern as every other
# store here), and any process that can read a file - Nox, a script, a human - can consume it.
# No listening socket, no new attack surface, no dependency on an AI being configured or even
# running; Thermal Watch writes this whether or not anything ever reads it.
#
# Every section is assembled from ALREADY-COMPUTED state or an already-existing read function -
# no new aggregation logic, no new causal language. This is the same "AI owns explanation,
# Thermal Watch owns the facts" split the whole v1.1 roadmap is built around: what's written here
# is observed readings and Thermal Watch's own deterministic, evidence-qualified findings
# (incidents, sessions, diagnostics with their existing confidence tiers) - never a fabricated
# summary, never a value invented to fill a gap. A None/missing field in the JSON means exactly
# what it means everywhere else in this app: not currently known, not zero.
# ---------------------------------------------------------------------------
EVIDENCE_SCHEMA_VERSION = "1.0"
EVIDENCE_SNAPSHOT_PATH = data_path("thermal_watch_evidence.json")
# Same cadence as the active-incident/session flush timers - live enough to be useful to an AI
# polling it, not so frequent that a modest JSON write competes with the 2s sensor poll.
EVIDENCE_SNAPSHOT_INTERVAL_MS = ACTIVE_INCIDENTS_FLUSH_INTERVAL_MS
EVIDENCE_RECENT_WINDOW_S = 24 * 3600  # "recent" incidents/sessions included verbatim = last 24h

CSV_DIRECT_FIELDS = [
    "incident_id", "start_timestamp", "end_timestamp", "duration_seconds", "duration_exact",
    "component", "sensor_name", "sensor_identifier", "starting_zone", "max_zone",
    "start_value", "peak_value", "recovery_value", "dominant_workload",
    "foreground_process", "foreground_title", "recovery_during_monitoring_gap",
    "monitoring_gap_seconds", "close_reason",
]
# context_peak's internal key -> CSV column name (context_peak keys match self.last_context's
# names, e.g. "gpu_vram_temp" for GPU Memory Junction - see the context snapshot in update_data).
CONTEXT_PEAK_TO_CSV = {
    "cpu_temp": "peak_cpu_temp", "gpu_core_temp": "peak_gpu_core_temp",
    "gpu_hotspot_temp": "peak_gpu_hotspot_temp", "gpu_vram_temp": "peak_gpu_memory_temp",
    "cpu_power": "peak_cpu_power", "gpu_power": "peak_gpu_power",
    "cpu_load": "peak_cpu_load", "gpu_load": "peak_gpu_load", "mem_pct": "peak_memory_usage",
    # v1.1 Phase 9 - same generic context_peak capture as every key above, just never exported
    # before. _csv_cell()'s fixed 2-decimal float formatting already gives Mbps real precision -
    # no unit-conditional formatting needed here the way the display views required.
    "net_down_mbps": "peak_network_download_mbps", "net_up_mbps": "peak_network_upload_mbps",
}
CSV_NESTED_FIELDS = ["top_cpu_processes", "top_gpu_processes", "monitoring_gaps"]
CSV_COLUMNS = CSV_DIRECT_FIELDS + list(CONTEXT_PEAK_TO_CSV.values()) + CSV_NESTED_FIELDS


def sanitize_filename_part(text):
    """Strips anything that isn't filesystem-safe from a component/sensor name before it goes
    into a suggested filename (item 8)."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", text or "").strip("_")
    return cleaned or "Incident"


def _csv_cell(value):
    """None/missing -> a blank cell, never a fabricated placeholder like 'N/A' or 0 (item 3:
    "Do not invent missing values. Leave unavailable CSV fields blank."). csv.DictWriter takes
    care of quoting commas/quotes/unicode correctly once the value is a plain string."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _format_process_list(procs):
    """[(name, pid, pct), ...] -> "Name:91%; Other:4%" (item 4's example format). Blank for a
    missing/empty list - never invents a process that wasn't actually captured."""
    if not procs:
        return ""
    parts = []
    for p in procs:
        try:
            name, pct = p[0], p[2]
        except (IndexError, TypeError):
            continue
        parts.append(f"{name}:{pct:.0f}%")
    return "; ".join(parts)


def _format_monitoring_gaps(gaps):
    """Each recorded gap -> a compact "6m 47s (23:41:10 -> 23:47:57)" cell; blank if none."""
    if not gaps:
        return ""
    parts = []
    for g in gaps:
        secs = g.get("gap_seconds")
        if secs is None:
            continue
        before, after = g.get("last_sample_before"), g.get("first_sample_after")
        span = ""
        if before is not None and after is not None:
            span = (f" ({datetime.fromtimestamp(before).strftime('%H:%M:%S')} → "
                   f"{datetime.fromtimestamp(after).strftime('%H:%M:%S')})")
        parts.append(f"{fmt_dur(secs)}{span}")
    return "; ".join(parts)


def incident_to_csv_row(inc):
    """Pure function: one persisted incident dict -> {CSV column name: cell string}. Never
    mutates `inc`. An older/minimal incident missing newer fields (monitoring gaps, workload,
    context_peak, ...) just produces blank cells for those columns rather than raising (item 13)."""
    row = {col: _csv_cell(inc.get(col)) for col in CSV_DIRECT_FIELDS}
    ctx = inc.get("context_peak") or {}
    for ctx_key, col in CONTEXT_PEAK_TO_CSV.items():
        row[col] = _csv_cell(ctx.get(ctx_key))
    row["top_cpu_processes"] = _format_process_list(inc.get("top_cpu_processes"))
    row["top_gpu_processes"] = _format_process_list(inc.get("top_gpu_processes"))
    row["monitoring_gaps"] = _format_monitoring_gaps(inc.get("monitoring_gaps"))
    return row


def build_json_export(incidents, filters=None):
    """Pure function - never mutates the incidents passed in. Each incident is embedded as its
    full structured record (item 5: "preserve the complete incident data rather than
    flattening it"), inside a metadata envelope describing the export itself."""
    return {
        "export_timestamp": time.time(),
        "schema_version": EXPORT_SCHEMA_VERSION,
        "app_version": APP_VERSION,
        "count": len(incidents),
        "filters": dict(filters) if filters else {},
        "incidents": list(incidents),
    }


def build_incident_summary(inc):
    """Pure function: one incident dict -> a short plain-text diagnostic summary (item 10),
    using only fields the incident actually has - never fabricates a value to fill a line, and
    simply omits a context-peak line if that reading wasn't captured."""
    comp_label = COMPONENT_LABELS.get(inc.get("component"), str(inc.get("component", "?")).upper())
    start = (datetime.fromtimestamp(inc["start_timestamp"]).strftime("%b %d, %Y %I:%M:%S %p")
            if inc.get("start_timestamp") else "N/A")
    dur = fmt_dur(inc["duration_seconds"]) if inc.get("duration_seconds") is not None else "N/A"
    peak = f"{inc['peak_value']:.0f}°C" if inc.get("peak_value") is not None else "N/A"
    fg = inc.get("foreground_process") or "N/A"
    fg_title = inc.get("foreground_title")

    lines = [
        "THERMAL WATCH INCIDENT",
        "",
        f"Component: {comp_label}",
        f"Start: {start}",
        f"Duration: {dur}",
        f"Peak: {peak}",
        f"Maximum severity: {inc.get('max_zone') or 'N/A'}",
        f"Dominant workload: {inc.get('dominant_workload') or 'Not identified'}",
        f"Foreground application: {fg}" + (f" — {fg_title}" if fg_title else ""),
    ]
    ctx = inc.get("context_peak") or {}
    for ctx_key, label, unit in (
        ("gpu_core_temp", "GPU Core peak", "°C"), ("gpu_hotspot_temp", "GPU Hotspot peak", "°C"),
        ("gpu_vram_temp", "GPU Memory peak", "°C"), ("gpu_power", "GPU Power peak", "W"),
        ("cpu_temp", "CPU peak", "°C"), ("cpu_power", "CPU Power peak", "W"),
    ):
        v = ctx.get(ctx_key)
        if v is not None:
            lines.append(f"{label}: {v:.0f}{unit}")

    gaps = inc.get("monitoring_gaps") or []
    if gaps:
        total_gap = inc.get("monitoring_gap_seconds")
        if total_gap is None:
            total_gap = sum(g.get("gap_seconds", 0) for g in gaps)
        lines.append("")
        lines.append(f"Monitoring gap: {fmt_dur(total_gap)}")
        if inc.get("duration_exact") is False:
            lines.append("Duration contains an unmonitored interval and is not exact.")
        if inc.get("recovery_during_monitoring_gap"):
            lines.append("Recovery occurred while monitoring was offline - exact recovery time/value unknown.")
        elif inc.get("close_reason") == "sensor_unavailable":
            lines.append("Sensor was no longer available when monitoring resumed.")
    return "\n".join(lines)


def read_incidents_file():
    """All persisted incidents, newest first - used by the History view, which always wants
    the complete on-disk record (already pruned to INCIDENT_RETENTION_DAYS by load_incidents())
    rather than App's in-memory incidents_recent, which is capped independently for light
    in-app bookkeeping."""
    if not INCIDENTS_PATH.exists():
        return []
    out = []
    try:
        for line in INCIDENTS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue  # valid JSON but not a record - a non-dict would crash every consumer
            out.append(rec)
    except OSError:
        return []
    out.reverse()  # file is oldest-first; UI wants newest-first
    return out


def read_sessions_file():
    """All persisted completed workload sessions, newest first - mirrors read_incidents_file()
    exactly, but reads the entirely separate SESSIONS_PATH store (item 11)."""
    if not SESSIONS_PATH.exists():
        return []
    out = []
    try:
        for line in SESSIONS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue  # valid JSON but not a record - a non-dict would crash every consumer
            out.append(rec)
    except OSError:
        return []
    out.reverse()
    return out


def _next_evidence_id(existing_records, prefix, day_str, legacy_match=None):
    """1-based, zero-padded rank for a NEW record sharing (prefix, day_str) with whatever is
    ALREADY persisted, formatted as PREFIX-YYYYMMDD-NNNN (Phase 14 - Evidence IDs). Deliberately
    requires no new mutable counter state: the id is a pure function of the existing store,
    recomputed fresh every time a record is closed/finalized, then frozen into that record and
    never recomputed again (mirrors incident_id/session_id themselves).

    Counts each existing record once: if it already carries its own frozen `evidence_id`, that id
    is trusted directly (prefix/day match on the string itself); an OLDER record that predates
    this field has no `evidence_id` yet, so `legacy_match(rec)` decides whether it belongs to the
    same (prefix, day_str) group by inspecting its other fields (start_timestamp/component) - so
    pre-Phase-14 history is never silently skipped and can never collide with a new id."""
    needle = f"{prefix}-{day_str}-"
    count = 0
    for rec in existing_records:
        eid = rec.get("evidence_id")
        if eid:
            if eid.startswith(needle):
                count += 1
        elif legacy_match is not None and legacy_match(rec):
            count += 1
    return f"{prefix}-{day_str}-{count + 1:04d}"


def assign_incident_evidence_id(inc):
    """Freezes `evidence_id` into a completed incident dict, in place, just once, immediately
    before its first persist - INC for component != 'network', NET for component == 'network'
    (network incidents are real incidents through the same engine - see _update_network_incident -
    but keep the existing NET/INC semantic split). Must run AFTER inc['start_timestamp'] and
    inc['component'] are final and BEFORE _persist_incident() so the rank it computes from
    read_incidents_file() never counts this record against itself."""
    prefix = "NET" if inc.get("component") == "network" else "INC"
    day_str = local_day_str(inc["start_timestamp"])

    def legacy_match(rec):
        ts = rec.get("start_timestamp")
        if ts is None:
            return False
        return local_day_str(ts) == day_str and (rec.get("component") == "network") == (prefix == "NET")

    inc["evidence_id"] = _next_evidence_id(read_incidents_file(), prefix, day_str, legacy_match)
    return inc["evidence_id"]


def assign_session_evidence_id(completed):
    """Freezes `evidence_id` (prefix always SES - sessions have no component split) into a
    completed session dict, in place, just once, immediately before its first persist. Same
    contract as assign_incident_evidence_id()."""
    day_str = local_day_str(completed["start_timestamp"])

    def legacy_match(rec):
        ts = rec.get("start_timestamp")
        return ts is not None and local_day_str(ts) == day_str

    completed["evidence_id"] = _next_evidence_id(read_sessions_file(), "SES", day_str, legacy_match)
    return completed["evidence_id"]


def _normalize_workload_name(raw):
    """(canonical_key, display_name) for a raw process/workload name string. Normalization is
    deliberately limited to trimming whitespace and case-folding - it never fuzzy-matches or
    guesses that two different executables are related (item 5: python.exe never becomes a
    specific AI app just because it looks similar to something). Shared by both incident
    workload analytics (canonical_workload_name, below) and the session engine, so the two
    systems can never disagree about what counts as "the same workload"."""
    raw = (raw or "").strip()
    if not raw or raw.casefold() == NOT_IDENTIFIED_KEY:
        return NOT_IDENTIFIED_KEY, NOT_IDENTIFIED_DISPLAY
    return raw.casefold(), raw


def group_sessions_by_workload(sessions):
    """{canonical_key: {"display_name": ..., "sessions": [...]}} - mirrors
    group_incidents_by_workload() exactly, re-normalizing each session's own `workload` field
    rather than trusting its stored workload_key blindly (cheap, and robust against a
    hand-edited or future-schema record)."""
    groups = {}
    for s in sessions:
        key, display = _normalize_workload_name(s.get("workload"))
        group = groups.setdefault(key, {"display_name": display, "sessions": []})
        group["sessions"].append(s)
    return groups


# ---------------------------------------------------------------------------
# Baseline learning - a pure, read-only analytical layer over ALREADY-PERSISTED completed
# sessions and telemetry buckets. Like Application Analytics, nothing here touches live sensor
# state, the incident/session lifecycle, or the thermal alert engine; it only ever reads
# already-recorded data and computes statistics from it, on demand (view open/refresh), never on
# the 2s poll. Two kinds of baseline:
#   - a per-WORKLOAD baseline, built from that workload's own completed session records (each
#     session already carries its own avg/peak per-component stats - the baseline is simply
#     count/mean/min/max/stddev of those per-session numbers across multiple sessions).
#   - an IDLE baseline for one sensor, built from telemetry buckets that fall OUTSIDE every
#     session's time span (i.e., no workload was meaningfully active) - "what is normal for this
#     machine at rest".
# Both report an explicit `established` flag rather than ever presenting a baseline computed
# from too few samples as if it were reliable - matching the project's standing "missing/
# insufficient stays visibly so" rule. Sample (n-1) standard deviation is used throughout since
# a baseline is built from a SAMPLE of sessions/buckets, not the full population of all possible
# ones - the standard, transparent, non-gamified choice, in the spirit of the roadmap's own
# "derived transparently from measured behavior" requirement for later phases.
# ---------------------------------------------------------------------------
BASELINE_MIN_SESSIONS = 3   # fewer real sessions than this: a real number, just not yet reliable
BASELINE_MIN_IDLE_BUCKETS = 30  # 30 x 60s = 30 minutes of idle telemetry

# (component block, field, display label, unit) - the per-session metrics a workload baseline is
# built from. Deliberately a curated subset of session.py's full stat set: the ones a user would
# actually recognize from the roadmap's own examples (thermals + power), not every single field.
BASELINE_SESSION_METRICS = [
    ("cpu", "avg_temp", "CPU Package (session avg)", "°C"),
    ("cpu", "peak_temp", "CPU Package (session peak)", "°C"),
    ("cpu", "avg_power", "CPU Power (session avg)", "W"),
    ("gpu", "avg_core_temp", "GPU Core (session avg)", "°C"),
    ("gpu", "peak_core_temp", "GPU Core (session peak)", "°C"),
    ("gpu", "avg_hotspot_temp", "GPU Hotspot (session avg)", "°C"),
    ("gpu", "peak_hotspot_temp", "GPU Hotspot (session peak)", "°C"),
    ("gpu", "avg_vram_temp", "GPU Memory Junction (session avg)", "°C"),
    ("gpu", "peak_vram_temp", "GPU Memory Junction (session peak)", "°C"),
    ("gpu", "avg_power", "GPU Power (session avg)", "W"),
    ("gpu", "peak_power", "GPU Power (session peak)", "W"),
    # v1.1 Phase 7 - Network Analytics. Reuses this exact list: adding these four entries is
    # the whole implementation - compute_workload_baseline()/evaluate_session_anomalies()/the
    # Analytics detail renderer all already iterate this list generically, so per-workload
    # network baselines and session-level network anomaly flagging both come from this addition
    # alone, no new architecture. Same "whole-active-adapter rate" semantic as the session block
    # itself (see Phase 5) - an observed correlation with this workload's active window, never a
    # causal usage claim.
    ("network", "avg_down_mbps", "Download (session avg)", " Mbps"),
    ("network", "peak_down_mbps", "Download (session peak)", " Mbps"),
    ("network", "avg_up_mbps", "Upload (session avg)", " Mbps"),
    ("network", "peak_up_mbps", "Upload (session peak)", " Mbps"),
]


def _stat_summary(values, min_established=1):
    """count/mean/min/max/(sample)stddev for a list that may contain None - missing entries are
    dropped, never treated as 0. Returns None if there isn't even one real value (never
    fabricates a baseline from nothing). stddev is None for a single sample (undefined, not 0).
    `established` is True only once at least min_established REAL samples were seen."""
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    n = len(vals)
    mean = sum(vals) / n
    if n >= 2:
        variance = sum((v - mean) ** 2 for v in vals) / (n - 1)
        stddev = variance ** 0.5
    else:
        stddev = None
    return {"count": n, "mean": mean, "min": min(vals), "max": max(vals), "stddev": stddev,
           "established": n >= min_established}


def compute_workload_baseline(sessions):
    """Pure function: one workload's completed session records -> {metric_key: {'label','unit',
    'stats'}} - 'stats' is a _stat_summary() (or None if that metric was never once captured
    across any of these sessions) built from each session's OWN avg/peak for that metric, so the
    baseline reflects session-level behavior, not a re-derivation from raw telemetry."""
    out = {}
    for block, field, label, unit in BASELINE_SESSION_METRICS:
        values = [(s.get(block) or {}).get(field) for s in sessions]
        out[f"{block}.{field}"] = {"label": label, "unit": unit,
                                   "stats": _stat_summary(values, BASELINE_MIN_SESSIONS)}
    return out


def filter_idle_buckets(buckets, sessions):
    """Telemetry buckets whose [start,end] window does NOT overlap any session's [start,end] -
    i.e. no workload was meaningfully active. A session missing end_timestamp (shouldn't happen
    for a persisted/completed one, but never trust blindly) is treated as covering [start, now]
    so an unexpectedly-still-open-looking record is conservatively excluded from 'idle' rather
    than risking counting active time as idle."""
    now = time.time()
    spans = [(s["start_timestamp"], s.get("end_timestamp") or now) for s in sessions
            if s.get("start_timestamp") is not None]
    out = []
    for b in buckets:
        b_start = b.get("start_timestamp")
        b_end = b.get("end_timestamp") or b_start
        if b_start is None:
            continue
        if any(b_start < s_end and b_end > s_start for s_start, s_end in spans):
            continue
        out.append(b)
    return out


def compute_idle_baseline(idle_buckets, sensor_ref):
    """Pure function: idle-covered telemetry buckets -> a _stat_summary() for ONE sensor (scalar
    or per-sensor identity, via the same extract_bucket_metric() the historical chart already
    uses) built from each bucket's own AVERAGE for that sensor - the raw per-tick samples were
    never kept (item 1's streaming-aggregate design), so a bucket's average is the finest-grained
    real number available. None if this sensor was never once observed during idle time."""
    values = []
    for b in idle_buckets:
        m = extract_bucket_metric(b, sensor_ref)
        values.append(m["avg"] if m else None)
    return _stat_summary(values, BASELINE_MIN_IDLE_BUCKETS)


# ---------------------------------------------------------------------------
# Anomaly detection - a pure, read-only layer directly on top of baseline learning above:
# "is this one measurement unusual compared with the baseline we already computed for it?"
# Never claims WHY (this project's standing non-causal-language rule - "Associated"/"Unusual",
# never "caused by"/"problem is"), never runs automatically (no new event-log entries, no
# notifications - display-only, exactly like baseline learning itself), and only ever evaluates
# a metric against a baseline that's already `established` (item: never guess from too little
# history). Two thresholds, both transparent and standard rather than tuned/gamified:
#   ANOMALY_Z_THRESHOLD = 2.0 standard deviations from the baseline mean - the common statistical
#   rule of thumb for "notably outside ordinary variation" (~95% of a roughly normal
#   distribution's mass sits within +-2 sigma), conservative enough that ordinary session-to-
#   session variance isn't constantly flagged.
#   ANOMALY_MIN_ABS_DELTA - fallback used only when the baseline's own stddev is ~0 (a
#   suspiciously perfectly uniform history, where a z-score would divide by ~zero and flag any
#   tiny difference) - a minimum absolute deviation before even considering a flag, so floating-
#   point noise on an unusually consistent baseline can never trigger one.
# ---------------------------------------------------------------------------
ANOMALY_Z_THRESHOLD = 2.0
ANOMALY_MIN_ABS_DELTA = {"°C": 3.0, "W": 15.0, " Mbps": 5.0}


def evaluate_anomaly(current_value, baseline_stats, unit):
    """Pure function: is `current_value` unusual relative to an established baseline? None if
    there's nothing real to compare (the current value or the baseline is missing/not
    established) - never guesses. Otherwise {'delta','z_score'(or None when stddev is
    unavailable),'unusual','baseline_mean'}."""
    if current_value is None or not baseline_stats or not baseline_stats.get("established"):
        return None
    delta = current_value - baseline_stats["mean"]
    stddev = baseline_stats.get("stddev")
    if stddev and stddev > 1e-9:
        z = delta / stddev
        unusual = abs(z) >= ANOMALY_Z_THRESHOLD
    else:
        z = None
        unusual = abs(delta) >= ANOMALY_MIN_ABS_DELTA.get(unit, float("inf"))
    return {"delta": delta, "z_score": z, "unusual": unusual, "baseline_mean": baseline_stats["mean"],
           "baseline_stddev": stddev}


def evaluate_session_anomalies(session, baseline):
    """Pure function: ONE session's own per-metric values vs a baseline (built by the caller,
    typically from every OTHER completed session of the same workload - never from the session
    being evaluated itself) -> {metric_key: {'label','unit','current','anomaly'}} for every
    metric this session actually recorded AND that has an established baseline to compare
    against. Metrics missing from either side are simply absent from the result, never
    fabricated."""
    out = {}
    for block, field, label, unit in BASELINE_SESSION_METRICS:
        current = (session.get(block) or {}).get(field)
        entry = baseline.get(f"{block}.{field}")
        anomaly = evaluate_anomaly(current, entry["stats"] if entry else None, unit)
        if current is not None and anomaly is not None:
            out[f"{block}.{field}"] = {"label": label, "unit": unit, "current": current, "anomaly": anomaly}
    return out


def count_anomalous_sessions(sessions):
    """Pure function: of these completed sessions (all the same workload), how many were
    'unusual' against a leave-one-out baseline built from the OTHER sessions in the same list -
    never a session compared against a baseline that includes itself. Returns None when there
    are too few sessions for ANY leave-one-out baseline to ever reach `established`, rather than
    a misleading '0 anomalous' that would actually just mean 'couldn't tell'. O(n^2) in session
    count - fine for an on-demand, button-triggered Analytics view (never the poll), and a
    single workload's realistic session count is tens, not thousands."""
    if len(sessions) < BASELINE_MIN_SESSIONS + 1:
        return None
    count = 0
    for i, s in enumerate(sessions):
        others = sessions[:i] + sessions[i + 1:]
        baseline = compute_workload_baseline(others)
        anomalies = evaluate_session_anomalies(s, baseline)
        if any(v["anomaly"]["unusual"] for v in anomalies.values()):
            count += 1
    return count


# ---------------------------------------------------------------------------
# Cross-sensor diagnostics - one layer up from anomaly detection. Anomaly detection says "this
# ONE metric is unusual"; this layer asks "does the PATTERN across two-or-more sensors together
# resemble a recognizable thermal-behavior signature" (cooling efficiency, case airflow,
# workload-intensity-vs-temperature mismatch). Same standing rules as every layer below it: pure
# functions over already-persisted/already-live data, on-demand only (never the poll, never an
# event-log entry), and strictly non-causal, evidence-qualified language - "Possible X", "pattern
# is more consistent with Y than Z", never "your X is bad"/"caused by". Every finding carries its
# own evidence lines so the user can see exactly what produced it, plus a Confidence: HIGH/MEDIUM
# tier (HIGH only when the primary signal is a large deviation AND a second, independent signal
# corroborates the same interpretation; MEDIUM when only the primary signal cleared the anomaly
# threshold). A pattern with no unusual primary signal returns None - never a padded "nothing
# here" entry; the caller shows "Sensor pattern inconclusive" only when NONE of the patterns it
# ran found anything.
#
# Scope note on fan RPM: workload sessions and telemetry history never recorded fan RPM (the
# Long-term History phase scoped telemetry to temperature/power/utilization only). Rather than a
# schema migration to persist it retroactively, the two fan-involving patterns
# (CPU/GPU-temp<->fan RPM) use ONLY the current LIVE reading (last_context, extended with
# cpu_fan_rpm/gpu_fan_pct above) and are therefore live-only - they cannot be evaluated against a
# historical session, only against what the machine is doing right now. Fan RPM is shown as
# supporting evidence, never as a pass/fail threshold: there's no confidently-established "normal"
# fan RPM to compare against, so the deciding signal is the EXISTING CPU/GPU thermal zone
# (already-established thresholds, untouched) corroborated by the System sensor's OWN idle
# baseline - never a newly-invented absolute number, and never a threshold applied to a
# motherboard sensor whose physical meaning wasn't independently verified.
# ---------------------------------------------------------------------------


def _diagnostic_confidence(z_score, corroborated):
    """HIGH only when the primary signal is a large deviation (>=3 sigma - one full standard
    deviation past ANOMALY_Z_THRESHOLD's 2.0 anomaly floor) AND a second, independent signal
    corroborates the same interpretation; MEDIUM otherwise. Matches the two confidence tiers
    actually used in the worked examples this phase was specced from - no LOW tier: a pattern
    that doesn't clear the anomaly threshold produces no finding at all rather than a low-
    confidence guess."""
    if z_score is not None and abs(z_score) >= 3.0 and corroborated:
        return "HIGH"
    return "MEDIUM"


def compute_session_delta_baseline(sessions, block, field_hi, field_lo):
    """Pure function: a baseline for a DERIVED per-session delta (field_hi - field_lo within the
    same component block, e.g. GPU hotspot-minus-core) - each session contributes ONE delta value
    (mean-of-differences, not the less honest difference-of-means, which would hide session-to-
    session variance), then _stat_summary()'d exactly like any other baseline metric. Separate
    from compute_workload_baseline() because BASELINE_SESSION_METRICS only pulls fields that
    already exist verbatim on a session record; a delta here is computed, not stored."""
    values = []
    for s in sessions:
        blk = s.get(block) or {}
        hi, lo = blk.get(field_hi), blk.get(field_lo)
        values.append(hi - lo if hi is not None and lo is not None else None)
    return _stat_summary(values, BASELINE_MIN_SESSIONS)


def diagnose_gpu_cooling_pattern(session, other_sessions, workload_display):
    """GPU Core<->Hotspot delta vs this workload's own history. `other_sessions` must already
    exclude `session` itself (leave-one-out - same convention as evaluate_session_anomalies'
    caller elsewhere). Flags only when the delta is unusually WIDE (a narrower-than-usual delta
    is not a cooling concern) - and separates two cases: if GPU power was normal for the
    workload, a wider delta with unchanged power argues against "GPU is just working harder" and
    toward a localized cooling-contact/case-airflow explanation; if power was ALSO elevated (or
    has no established baseline), increased workload intensity alone hasn't been ruled out, so
    the finding is reported with an explicitly weaker interpretation."""
    gpu = session.get("gpu") or {}
    core, hotspot, power = gpu.get("avg_core_temp"), gpu.get("avg_hotspot_temp"), gpu.get("avg_power")
    if core is None or hotspot is None:
        return None
    delta = hotspot - core
    delta_baseline = compute_session_delta_baseline(other_sessions, "gpu", "avg_hotspot_temp", "avg_core_temp")
    delta_anomaly = evaluate_anomaly(delta, delta_baseline, "°C")
    if delta_anomaly is None or not delta_anomaly["unusual"] or delta_anomaly["delta"] <= 0:
        return None

    power_entry = compute_workload_baseline(other_sessions).get("gpu.avg_power")
    power_anomaly = evaluate_anomaly(power, power_entry["stats"] if power_entry else None, "W")
    corroborated = power_anomaly is not None and not power_anomaly["unusual"]

    evidence = [f"GPU Core: {core:.0f}°C", f"GPU Hotspot: {hotspot:.0f}°C", f"Delta: {delta:.0f}°C"]
    if delta_baseline["stddev"] is not None:
        lo, hi = delta_baseline["mean"] - delta_baseline["stddev"], delta_baseline["mean"] + delta_baseline["stddev"]
        evidence.append(f"Typical {workload_display} delta: {lo:.0f}–{hi:.0f}°C")
    if power_anomaly is not None:
        evidence.append(f"GPU power: {'normal for this workload' if not power_anomaly['unusual'] else 'also elevated for this workload'}")
    elif power is not None:
        evidence.append(f"GPU power: {power:.0f}W (not enough history for this workload yet)")

    if corroborated:
        interpretation = ("Hotspot is disproportionately high relative to core temperature while GPU power draw "
                          "is normal for this workload. Possible cooling efficiency degradation - a pattern more "
                          "consistent with localized cooling contact or case airflow than workload intensity.")
    else:
        interpretation = ("Hotspot/core delta is unusually high, but GPU power draw is also elevated (or has no "
                          "established baseline yet) for this workload, so higher workload intensity alone cannot "
                          "be ruled out as the explanation. Sensor pattern only partially conclusive.")
    return {"title": "GPU COOLING PATTERN — UNUSUAL", "evidence": evidence, "interpretation": interpretation,
           "confidence": _diagnostic_confidence(delta_anomaly["z_score"], corroborated)}


def diagnose_temp_vs_power_pattern(title, block, temp_field, temp_label, power_field, power_label,
                                   session, other_sessions, workload_display):
    """ONE component's temperature vs its OWN power draw, both compared against this workload's
    history (leave-one-out via `other_sessions`, same convention as above). Only produces a
    finding when temperature is unusually high AND power draw is NOT also unusually high -
    temperature rising without power rising is the signature this pattern looks for ("temperature
    elevated despite normal power"); temperature and power both elevated together is fully
    explained by increased workload intensity and correctly produces no finding here (that case
    is what evaluate_session_anomalies already reports, unqualified, in the VS BASELINE section)."""
    blk = session.get(block) or {}
    temp, power = blk.get(temp_field), blk.get(power_field)
    if temp is None:
        return None
    baseline = compute_workload_baseline(other_sessions)
    temp_entry = baseline.get(f"{block}.{temp_field}")
    temp_anomaly = evaluate_anomaly(temp, temp_entry["stats"] if temp_entry else None, "°C")
    if temp_anomaly is None or not temp_anomaly["unusual"] or temp_anomaly["delta"] <= 0:
        return None
    power_entry = baseline.get(f"{block}.{power_field}")
    power_anomaly = evaluate_anomaly(power, power_entry["stats"] if power_entry else None, "W") if power is not None else None
    if power_anomaly is None or power_anomaly["unusual"]:
        return None  # power itself explains the rise, or can't be ruled out - not a distinguishing pattern

    evidence = [f"{temp_label}: {temp:.0f}°C"]
    if temp_entry and temp_entry["stats"] and temp_entry["stats"]["established"]:
        evidence.append(f"Typical {workload_display} {temp_label.lower()}: {temp_entry['stats']['mean']:.0f}°C")
    evidence.append(f"{power_label}: {power:.0f}W (normal for this workload)")
    interpretation = (f"{temp_label} is elevated relative to this workload's own baseline while {power_label.lower()} "
                      "remains normal - temperature rose without a corresponding rise in power draw. Possible "
                      "cooling efficiency degradation; more consistent with reduced cooling effectiveness than "
                      "increased workload demand.")
    return {"title": f"{title} — UNUSUAL", "evidence": evidence, "interpretation": interpretation,
           "confidence": _diagnostic_confidence(temp_anomaly["z_score"], True)}


def diagnose_cpu_cooling_ceiling(cpu_temp, cpu_power, cpu_fan_rpm, system_temp, system_idle_baseline):
    """Live-only pattern (see the fan-RPM scope note above `_diagnostic_confidence`). Reuses the
    SAME cpu_zone_for() thermal-ceiling classification already shown on the live dashboard -
    never a newly-invented threshold: is the CPU currently in the ORANGE/RED zone while the
    motherboard's own "System" sensor is NOT itself elevated relative to ITS established idle
    baseline? That combination argues the heat is localized to the CPU rather than general case
    airflow. CPU fan RPM is shown as supporting evidence only, so the user can see the fan isn't
    stalled - deliberately never used as a pass/fail threshold."""
    zone = cpu_zone_for(cpu_temp)
    if zone is None or zone["key"] not in ("ORANGE", "RED"):
        return None
    system_anomaly = evaluate_anomaly(system_temp, system_idle_baseline, "°C") if system_temp is not None else None
    if system_anomaly is not None and system_anomaly["unusual"] and system_anomaly["delta"] > 0:
        return None  # case/system temp is ALSO elevated - can't isolate a CPU-specific pattern

    evidence = [f"CPU: {cpu_temp:.0f}°C"]
    if cpu_power is not None:
        evidence.append(f"Package power: {cpu_power:.0f}W")
    if cpu_fan_rpm is not None:
        evidence.append(f"CPU fan: {cpu_fan_rpm:,.0f} RPM")
    if system_temp is not None:
        evidence.append(f"System temp: {system_temp:.0f}°C")

    interpretation = (f"CPU is reaching its thermal ceiling (currently in the {zone['short']} zone) while "
                      "surrounding case/system temperature remains moderate. This pattern is more consistent "
                      "with CPU cooler capacity or contact than overall case heat.")
    corroborated = system_anomaly is not None and not system_anomaly["unusual"]
    confidence = "HIGH" if zone["key"] == "RED" and corroborated else "MEDIUM"
    return {"title": "CPU COOLING PATTERN", "evidence": evidence, "interpretation": interpretation,
           "confidence": confidence}


def diagnose_gpu_cooling_ceiling(gpu_hotspot_temp, gpu_power, gpu_fan_pct, system_temp, system_idle_baseline):
    """GPU-side counterpart to diagnose_cpu_cooling_ceiling - same logic, reusing the existing
    GPU_HOTSPOT_ZONES thresholds instead of cpu_zone_for. gpu_fan_pct is a PERCENTAGE
    (nvidia-smi's fan.speed), not RPM - most discrete GPUs don't expose fan RPM the way LHM
    exposes "CPU Fan", so it's shown as-is rather than converted/estimated."""
    zone = zone_for(gpu_hotspot_temp, GPU_HOTSPOT_ZONES) if gpu_hotspot_temp is not None else None
    if zone is None or zone["key"] not in ("ORANGE", "RED"):
        return None
    system_anomaly = evaluate_anomaly(system_temp, system_idle_baseline, "°C") if system_temp is not None else None
    if system_anomaly is not None and system_anomaly["unusual"] and system_anomaly["delta"] > 0:
        return None

    evidence = [f"GPU Hotspot: {gpu_hotspot_temp:.0f}°C"]
    if gpu_power is not None:
        evidence.append(f"GPU power: {gpu_power:.0f}W")
    if gpu_fan_pct is not None:
        evidence.append(f"GPU fan: {gpu_fan_pct:.0f}%")
    if system_temp is not None:
        evidence.append(f"System temp: {system_temp:.0f}°C")

    interpretation = (f"GPU hotspot is reaching its thermal ceiling (currently in the {zone['short']} zone) "
                      "while surrounding case/system temperature remains moderate. This pattern is more "
                      "consistent with GPU cooler capacity or contact than overall case heat.")
    corroborated = system_anomaly is not None and not system_anomaly["unusual"]
    confidence = "HIGH" if zone["key"] == "RED" and corroborated else "MEDIUM"
    return {"title": "GPU THERMAL CEILING", "evidence": evidence, "interpretation": interpretation,
           "confidence": confidence}


def diagnose_session_trend(sessions_for_workload, block, field, label, unit, workload_display, min_each_half=3):
    """Pure function: split a workload's completed sessions (sorted by start_timestamp) into an
    older half and a more-recent half; treat the older half as the baseline and ask whether the
    recent half's own MEAN is unusual against it (evaluate_anomaly, same z-score math as
    everywhere else in this file) - a genuine trend needs the recent sessions to be consistently
    different as a GROUP, not just one outlier (that's what per-session anomaly detection already
    catches). Needs at least `min_each_half` real sessions on EACH side or returns None - never
    guesses a trend from a handful of sessions."""
    ordered = sorted((s for s in sessions_for_workload if s.get("start_timestamp") is not None),
                     key=lambda s: s["start_timestamp"])
    values = [v for v in (((s.get(block) or {}).get(field)) for s in ordered) if v is not None]
    if len(values) < min_each_half * 2:
        return None
    mid = len(values) // 2
    older, recent = values[:mid], values[mid:]
    if len(older) < min_each_half or len(recent) < min_each_half:
        return None
    older_baseline = _stat_summary(older, min_each_half)
    recent_mean = sum(recent) / len(recent)
    anomaly = evaluate_anomaly(recent_mean, older_baseline, unit)
    if anomaly is None or not anomaly["unusual"]:
        return None

    direction = "higher" if anomaly["delta"] > 0 else "lower"
    evidence = [f"Older {len(older)} sessions' {label.lower()}: {older_baseline['mean']:.1f}{unit}",
               f"Most recent {len(recent)} sessions' {label.lower()}: {recent_mean:.1f}{unit}",
               f"Change: {anomaly['delta']:+.1f}{unit}"]
    interpretation = (f"{label} for {workload_display} has trended {direction} across recent sessions compared "
                      "with earlier ones. Possible session-to-session degradation - worth watching over further "
                      "sessions before treating it as confirmed.")
    return {"title": f"SESSION TREND — {label.upper()}", "evidence": evidence, "interpretation": interpretation,
           "confidence": _diagnostic_confidence(anomaly["z_score"], False)}


# Metrics session-to-session trend is checked for - a small, recognizable subset (not every
# BASELINE_SESSION_METRICS entry), to avoid a wall of low-signal trend findings.
SESSION_TREND_METRICS = [("gpu", "avg_hotspot_temp", "GPU Hotspot", "°C"), ("cpu", "avg_temp", "CPU Package", "°C")]


def run_session_diagnostics(session, other_sessions, workload_display):
    """All session-baseline-driven cross-sensor patterns for ONE completed session, evaluated
    against a leave-one-out baseline built from `other_sessions` (must already exclude `session`
    itself). Returns only the patterns that actually found something unusual."""
    findings = [f for f in (
        diagnose_gpu_cooling_pattern(session, other_sessions, workload_display),
        diagnose_temp_vs_power_pattern("CPU THERMAL PATTERN", "cpu", "avg_temp", "CPU Package",
                                       "avg_power", "CPU Power", session, other_sessions, workload_display),
        diagnose_temp_vs_power_pattern("GPU THERMAL PATTERN", "gpu", "avg_core_temp", "GPU Core",
                                       "avg_power", "GPU Power", session, other_sessions, workload_display),
    ) if f is not None]
    return findings


def run_session_trend_diagnostics(sessions_for_workload, workload_display):
    """Session-to-session degradation trend for the curated SESSION_TREND_METRICS list."""
    findings = [f for f in (
        diagnose_session_trend(sessions_for_workload, block, field, label, unit, workload_display)
        for block, field, label, unit in SESSION_TREND_METRICS
    ) if f is not None]
    return findings


def run_live_cooling_ceiling_diagnostics(cpu_temp, cpu_power, cpu_fan_rpm, gpu_hotspot_temp, gpu_power,
                                         gpu_fan_pct, system_temp, system_idle_baseline):
    """Live-only cooling-ceiling patterns for both CPU and GPU - see diagnose_cpu_cooling_ceiling/
    diagnose_gpu_cooling_ceiling."""
    findings = [f for f in (
        diagnose_cpu_cooling_ceiling(cpu_temp, cpu_power, cpu_fan_rpm, system_temp, system_idle_baseline),
        diagnose_gpu_cooling_ceiling(gpu_hotspot_temp, gpu_power, gpu_fan_pct, system_temp, system_idle_baseline),
    ) if f is not None]
    return findings


def format_diagnostic_finding(finding):
    """One finding dict -> the display block lines, matching the worked-example format this
    phase was specced from: title, evidence lines, a blank line, Interpretation, Confidence."""
    lines = [finding["title"]]
    lines.extend(finding["evidence"])
    lines.append(f"Interpretation: {finding['interpretation']}")
    lines.append(f"Confidence: {finding['confidence']}")
    return lines


# ---------------------------------------------------------------------------
# Transparent health scoring - one more layer up: a single 0-100 number that summarizes a
# session's already-measured signals (zone time, incidents, anomaly detection, cross-sensor
# diagnostics - everything computed by the layers above) into one glanceable verdict. The user's
# own words for this phase: "derived from measured behavior, never gamified/random". That means:
# every point lost is a real, already-computed signal with a FIXED, documented weight - never an
# opaque/tuned/ML formula - and the score is NEVER shown without its full breakdown alongside it
# (a bare score with no breakdown is never displayed anywhere in this file). A session with no
# measured issues at all scores 100 - this function needs no history/baseline to produce a
# meaningful score; an anomaly-count input of None (no established baseline yet) contributes 0,
# never a penalty for missing data.
# ---------------------------------------------------------------------------
HEALTH_SCORE_MAX = 100.0
# Weight = points lost if a component spent the ENTIRE session in that zone (scaled by the actual
# fraction of the session spent there). Ordering mirrors the existing CPU_ZONE_SEVERITY/
# ZONE_SEVERITY tables (YELLOW < ORANGE < RED); values are round, human-picked numbers - not
# fitted/tuned to any dataset - chosen so a single component fully RED for a whole session (an
# extreme, rare case) lands the score around 65 ("FAIR" - clearly notable, not a worthless 0),
# while several simultaneous zone/incident/anomaly/diagnostic issues can still legitimately drive
# a genuinely bad session toward 0.
HEALTH_ZONE_WEIGHTS = {"YELLOW": 8.0, "ORANGE": 20.0, "RED": 35.0}
HEALTH_INCIDENT_WEIGHTS = {"YELLOW": 2.0, "ORANGE": 5.0, "RED": 10.0}
HEALTH_ANOMALY_POINTS = 3.0
HEALTH_DIAGNOSTIC_WEIGHTS = {"HIGH": 8.0, "MEDIUM": 4.0}
HEALTH_SCORE_BANDS = [(90.0, "EXCELLENT"), (75.0, "GOOD"), (55.0, "FAIR"), (30.0, "POOR"), (0.0, "CRITICAL")]
HEALTH_ZONE_TITLES = {"cpu": "CPU", "gpu_core": "GPU Core", "gpu_hotspot": "GPU Hotspot", "gpu_vram": "GPU Memory"}


def health_score_label(score):
    """A score -> its human-readable band (EXCELLENT/GOOD/FAIR/POOR/CRITICAL) - purely
    descriptive labeling of the already-computed transparent number, not a separate score."""
    for floor, label in HEALTH_SCORE_BANDS:
        if score >= floor:
            return label
    return HEALTH_SCORE_BANDS[-1][1]  # unreachable, floor=0.0 always matches


def compute_session_health_score(session, anomalies=None, diagnostic_findings=None):
    """Pure function: one completed session -> {'score', 'label', 'deductions'} - 'deductions' is
    the full, ORDERED breakdown of every point lost, each {'reason', 'points'} - never hidden,
    never summarized away. `anomalies` is evaluate_session_anomalies()'s result for this session
    against a leave-one-out baseline (or None if no baseline exists yet - contributes 0, not a
    penalty). `diagnostic_findings` is run_session_diagnostics()'s result for THIS session only -
    session-to-session TREND findings (run_session_trend_diagnostics) are deliberately excluded:
    a trend is a property of the workload's history, not this one session's own behavior, so it
    never counts against an individual session's score."""
    deductions = []
    duration = session.get("duration_seconds") or 0
    if duration > 0:
        zone_time = session.get("zone_time") or {}
        for comp, title in HEALTH_ZONE_TITLES.items():
            times = zone_time.get(comp) or {}
            for zone_key, weight in HEALTH_ZONE_WEIGHTS.items():
                secs = times.get(zone_key, 0.0)
                if secs > 0:
                    pct = secs / duration * 100
                    deductions.append({"reason": f"{title} spent {pct:.0f}% of the session in {zone_key.title()}",
                                       "points": (secs / duration) * weight})

    incident_count = session.get("incident_count", 0)
    max_sev = session.get("max_incident_severity")
    if incident_count and max_sev in HEALTH_INCIDENT_WEIGHTS:
        deductions.append({"reason": f"{incident_count} associated incident(s), highest severity {max_sev.title()}",
                           "points": HEALTH_INCIDENT_WEIGHTS[max_sev] * incident_count})

    if anomalies:
        # Network anomalies (v1.1 Phase 7) are deliberately excluded from scoring: unusual
        # bandwidth doesn't indicate anything wrong with the system's thermal/operational health
        # the way an unusual temperature does. Still shown informationally in VS BASELINE - just
        # never a health deduction.
        unusual = [v for k, v in anomalies.items() if v["anomaly"]["unusual"] and not k.startswith("network.")]
        if unusual:
            deductions.append({"reason": f"{len(unusual)} metric(s) deviated notably from this workload's baseline",
                               "points": HEALTH_ANOMALY_POINTS * len(unusual)})

    for f in diagnostic_findings or []:
        weight = HEALTH_DIAGNOSTIC_WEIGHTS.get(f["confidence"], 0.0)
        if weight:
            deductions.append({"reason": f"{f['title']} ({f['confidence']} confidence)", "points": weight})

    score = max(0.0, min(HEALTH_SCORE_MAX, HEALTH_SCORE_MAX - sum(d["points"] for d in deductions)))
    return {"score": score, "label": health_score_label(score), "deductions": deductions}


def compute_workload_session_health_scores(sessions):
    """Pure function: a workload's completed sessions -> a list of each session's own health
    score, each built against a leave-one-out baseline from the OTHER sessions in the list - same
    convention as count_anomalous_sessions(). Unlike anomaly detection, a score is well-defined
    even for a workload's very first session or two (its anomaly/diagnostic inputs just
    contribute 0 rather than needing BASELINE_MIN_SESSIONS to be meaningful at all) - only the
    anomaly/diagnostic DEDUCTIONS inside each score depend on an established baseline, the zone-
    time/incident deductions never do. O(n^2) in session count, same cost class as
    count_anomalous_sessions - fine for an on-demand Analytics view."""
    scores = []
    for i, s in enumerate(sessions):
        others = sessions[:i] + sessions[i + 1:]
        baseline = compute_workload_baseline(others)
        anomalies = evaluate_session_anomalies(s, baseline)
        session_findings = run_session_diagnostics(s, others, s.get("workload", "?"))
        scores.append(compute_session_health_score(s, anomalies, session_findings)["score"])
    return scores


def compute_workload_health_average(session_scores):
    """Pure function: a list of already-computed per-session score floats -> a simple mean +
    label, or None if there are no scores at all. No separate/decayed weighting - just the mean
    of the same transparent per-session numbers already shown in SessionsWindow; nothing new is
    invented at the workload level."""
    if not session_scores:
        return None
    avg = sum(session_scores) / len(session_scores)
    return {"score": avg, "label": health_score_label(avg)}


def format_health_score(result):
    """A compute_session_health_score()/compute_workload_health_average()-shaped result -> its
    display lines - the score is ALWAYS shown with its full breakdown, per this phase's own
    'transparent, never gamified' requirement."""
    lines = [f"HEALTH SCORE: {result['score']:.0f}/100 — {result['label']}"]
    deductions = result.get("deductions")
    if deductions:
        for d in sorted(deductions, key=lambda x: -x["points"]):
            lines.append(f"  -{d['points']:.0f}  {d['reason']}")
    elif deductions is not None:
        lines.append("  No deductions - no measured thermal issues this session")
    return lines


# ---------------------------------------------------------------------------
# Trend Intelligence - one more layer up from Cross-Sensor Diagnostics and Health Scoring. The
# existing SESSION TREND (inside Cross-Sensor Diagnostics) answers "do recent sessions differ
# from older ones" - session-COUNT based, so a burst of 6 sessions in one afternoon could
# masquerade as a meaningful trend. Trend Intelligence instead answers "how did this metric
# change between two actual CALENDAR periods" (this week vs last week; the first half vs second
# half of the last 30 days) - same underlying comparison primitive (compare_period_values, built
# on the SAME _stat_summary/evaluate_anomaly machinery as every other layer in this file),
# applied to calendar-bounded groups instead of count-bounded ones.
#
# Anti-hype guard (the user's own words - "three random samples" must never produce "YOUR GPU IS
# DETERIORATING!!!"): TREND_MIN_SAMPLES gates whether a trend can be reported AT ALL (below it -
# "not enough data yet", exactly like baseline learning's `established` flag). Confidence is a
# genuine two-factor rubric, not just a z-score: TREND_GENEROUS_SAMPLES caps confidence at MEDIUM
# even for a huge (>=3 sigma) shift when either period is thin (3-5 samples) - only a large shift
# backed by generous data on BOTH sides reaches HIGH. Unlike Cross-Sensor Diagnostics (which
# reports NOTHING when a pattern isn't unusual), a comparison that clears the minimum-data bar but
# shows no meaningful shift is reported as STABLE, not silently dropped - "how is the machine
# changing" deserves an answer even when the answer is "not much". Still no recommendations here
# (the user's own words: "what is changing and how confident we are" - the next roadmap phase is
# what does something with that evidence).
# ---------------------------------------------------------------------------
TREND_MIN_SAMPLES = 3
TREND_GENEROUS_SAMPLES = 6
TREND_WOW_LOOKBACK_DAYS = 14     # -> two 7-day halves: "this week vs last week"
TREND_MONTH_LOOKBACK_DAYS = 30   # -> two 15-day halves: "30-day trajectory"
TREND_MIN_COVERAGE_PCT = 40.0    # minimum telemetry coverage required in EACH period before an
                                  # incident-frequency comparison is trusted - a quiet period with
                                  # almost no monitoring coverage isn't evidence of "fewer
                                  # incidents", it's evidence of "wasn't being watched".


def calendar_window_halves(total_days, now=None):
    """A `total_days`-long lookback, split into two equal ADJACENT calendar halves ending now:
    (older_start, older_end, recent_start, recent_end). total_days=14 -> two 7-day halves (week
    over week); total_days=30 -> two 15-day halves (30-day trajectory) - ONE shape serves both,
    parameterized purely by lookback length. Calendar-based, not session-count-based, unlike the
    existing per-workload SESSION TREND - a burst of sessions in one afternoon can never
    masquerade as a week's worth of change."""
    now = now if now is not None else time.time()
    half_seconds = (total_days * 86400) / 2
    return now - total_days * 86400, now - half_seconds, now - half_seconds, now


def _trend_confidence(z_score, n_older, n_recent):
    """LOW/MEDIUM/HIGH - the direct answer to "three random samples shouldn't shout
    DETERIORATING": thin data (either period under TREND_GENEROUS_SAMPLES) reaches MEDIUM at
    best, never HIGH, no matter how large the observed shift looks. Only a large (>=3 sigma)
    shift backed by generous samples on BOTH sides reaches HIGH."""
    thin = min(n_older, n_recent) < TREND_GENEROUS_SAMPLES
    strong = z_score is not None and abs(z_score) >= 3.0
    if strong:
        return "MEDIUM" if thin else "HIGH"
    return "LOW" if thin else "MEDIUM"


def compare_period_values(older_values, recent_values, unit, higher_is_worse=True, min_samples=TREND_MIN_SAMPLES):
    """Pure function: two lists of real numbers (an older calendar period, a more recent calendar
    period) -> a trend verdict, or None if either period has fewer than `min_samples` real values
    - never guesses a trend from a handful of samples. Reuses _stat_summary/evaluate_anomaly
    exactly like every other comparison in this file: the older period IS the baseline, the
    recent period's own mean is what gets judged against it. `higher_is_worse` controls only the
    WORSENING/IMPROVING wording (health score is higher-is-BETTER; temperatures are higher-is-
    worse) - the underlying numbers are identical either way. A real difference that doesn't
    clear the anomaly threshold is 'STABLE', never silently omitted."""
    older = [v for v in older_values if v is not None]
    recent = [v for v in recent_values if v is not None]
    if len(older) < min_samples or len(recent) < min_samples:
        return None
    baseline = _stat_summary(older, min_samples)
    recent_mean = sum(recent) / len(recent)
    anomaly = evaluate_anomaly(recent_mean, baseline, unit)
    if anomaly is None:
        return None
    direction = "STABLE"
    if anomaly["unusual"]:
        worse = (anomaly["delta"] > 0) == higher_is_worse
        direction = "WORSENING" if worse else "IMPROVING"
    return {"older_mean": baseline["mean"], "recent_mean": recent_mean, "delta": anomaly["delta"],
           "z_score": anomaly["z_score"], "direction": direction,
           "confidence": _trend_confidence(anomaly["z_score"], len(older), len(recent)),
           "n_older": len(older), "n_recent": len(recent),
           "older_stats": baseline}  # full _stat_summary (stddev/count) - e.g. for a "typical
                                      # historical range" display Recommendations needs beyond
                                      # just the bare mean; existing callers unaffected (additive)


def session_metric_values(sessions, block, field):
    """Pure extractor: a list of sessions -> the list of one metric's raw values (None entries
    included - compare_period_values() is what drops them, keeping this a faithful 1:1 mapping
    callers can also use for other purposes)."""
    return [(s.get(block) or {}).get(field) for s in sessions]


def compute_workload_period_trend(workload_sessions, block, field, unit, total_days, higher_is_worse=True, now=None):
    """One session metric (e.g. gpu.avg_hotspot_temp) for ONE workload's own sessions, compared
    across a calendar-split lookback window - the "workload-matched" trend the user asked for:
    sessions of OTHER workloads never enter this comparison. Uses overlapping_sessions() (not a
    hand-rolled window filter) so a session straddling the boundary is handled exactly the same
    way every other window-filtered view in this file already does."""
    older_start, older_end, recent_start, recent_end = calendar_window_halves(total_days, now)
    older = overlapping_sessions(workload_sessions, older_start, older_end)
    recent = overlapping_sessions(workload_sessions, recent_start, recent_end)
    return compare_period_values(session_metric_values(older, block, field),
                                 session_metric_values(recent, block, field), unit, higher_is_worse)


def compute_hotspot_core_delta_period_trend(workload_sessions, total_days, now=None):
    """GPU Hotspot-minus-Core delta (mean-of-per-session-differences, same convention as Cross-
    Sensor Diagnostics' compute_session_delta_baseline), compared across a calendar-split lookback
    window - the TIME-based counterpart to that function, which instead compares one session
    against a leave-one-out baseline of its peers regardless of when those peers happened."""
    older_start, older_end, recent_start, recent_end = calendar_window_halves(total_days, now)
    older = overlapping_sessions(workload_sessions, older_start, older_end)
    recent = overlapping_sessions(workload_sessions, recent_start, recent_end)

    def deltas(sessions):
        out = []
        for s in sessions:
            gpu = s.get("gpu") or {}
            hi, lo = gpu.get("avg_hotspot_temp"), gpu.get("avg_core_temp")
            out.append(hi - lo if hi is not None and lo is not None else None)
        return out

    return compare_period_values(deltas(older), deltas(recent), "°C", higher_is_worse=True)


# Which temperature each component block's efficiency ratio is built from. The two blocks do NOT
# use the same field name: a session's cpu block stores `avg_temp`, while its gpu block stores
# `avg_core_temp`/`avg_hotspot_temp` and has no `avg_temp` at all. Hotspot is the GPU entry because
# it is this project's primary GPU cooling signal everywhere else (the GPU COOLING PATTERN
# diagnostic, the cooling trend report, the experiments layer) - core temperature understates the
# cooling path a degrading thermal interface shows up in first.
SESSION_EFFICIENCY_TEMP_FIELD = {"cpu": "avg_temp", "gpu": "avg_hotspot_temp"}


def session_thermal_efficiency(session, component_block, temp_field=None):
    """°C-per-Watt for one component in one session: that component's average temperature over its
    average power, or None if either is missing or power is negligible (never divides by zero,
    never fabricates an efficiency number for a component that wasn't meaningfully drawing power).

    The temperature field is resolved per block via SESSION_EFFICIENCY_TEMP_FIELD rather than
    assumed. This function previously hard-coded `avg_temp`, a field ONLY the cpu block has - so
    every GPU efficiency value it was ever asked for came back None, silently, and
    compute_thermal_efficiency_period_trend was effectively CPU-only despite accepting a
    component_block argument. Nothing raised, and no report showed a wrong number; the GPU line
    just never appeared. `temp_field` can still be passed explicitly for a block outside the table."""
    blk = session.get(component_block) or {}
    field = temp_field or SESSION_EFFICIENCY_TEMP_FIELD.get(component_block, "avg_temp")
    temp, power = blk.get(field), blk.get("avg_power")
    if temp is None or not power or power < 1.0:
        return None
    return temp / power


def compute_thermal_efficiency_period_trend(workload_sessions, component_block, total_days, now=None):
    """Thermal-efficiency (°C/W) trend for one component, one workload, across a calendar-split
    lookback - rising °C/W means the SAME power draw is producing MORE heat over time (a cooling-
    capacity signal independent of whether the workload itself got more demanding this week);
    falling means the opposite. "°C/W" has no entry in ANOMALY_MIN_ABS_DELTA, so the absolute-
    delta fallback (used only when the older period's ratio is perfectly uniform - stddev=0, an
    unlikely edge case for a real derived ratio) resolves to "never flag" rather than an invented
    threshold - conservative, not a bug."""
    older_start, older_end, recent_start, recent_end = calendar_window_halves(total_days, now)
    older = overlapping_sessions(workload_sessions, older_start, older_end)
    recent = overlapping_sessions(workload_sessions, recent_start, recent_end)
    older_vals = [session_thermal_efficiency(s, component_block) for s in older]
    recent_vals = [session_thermal_efficiency(s, component_block) for s in recent]
    return compare_period_values(older_vals, recent_vals, "°C/W", higher_is_worse=True)


def compute_idle_metric_period_trend(sensor_ref, total_days, now=None):
    """Idle-time trend for one scalar/sensor metric (e.g. cpu_temp) - the machine's OWN resting
    temperature, calendar-split and compared, reusing filter_idle_buckets/extract_bucket_metric
    exactly like SensorHistoryWindow's idle baseline. ONE telemetry query covers the whole
    lookback window (not two separate ones); buckets are then split into the two calendar halves
    in Python. Gated on BASELINE_MIN_IDLE_BUCKETS (not the smaller TREND_MIN_SAMPLES) - buckets
    are 60s each, so the SAME "is this idle-time sample trustworthy" bar used everywhere else in
    this file applies here too, not a looser one."""
    now = now if now is not None else time.time()
    older_start, older_end, recent_start, recent_end = calendar_window_halves(total_days, now)
    query_sensor_key = sensor_ref["key"] if sensor_ref["kind"] == "sensor" else None
    buckets = read_telemetry_file(since_ts=older_start, sensor_key=query_sensor_key)
    sessions = overlapping_sessions(read_sessions_file(), older_start, now)
    idle_buckets = filter_idle_buckets(buckets, sessions)
    older_buckets = [b for b in idle_buckets if older_start <= b["start_timestamp"] < older_end]
    recent_buckets = [b for b in idle_buckets if recent_start <= b["start_timestamp"] < recent_end]

    def bucket_values(bs):
        return [m["avg"] if (m := extract_bucket_metric(b, sensor_ref)) else None for b in bs]

    return compare_period_values(bucket_values(older_buckets), bucket_values(recent_buckets),
                                 sensor_ref["unit"], higher_is_worse=True, min_samples=BASELINE_MIN_IDLE_BUCKETS)


def compute_incident_frequency_trend(total_days, max_zone=None, now=None):
    """Count-based trend (RED/critical incidents by default via max_zone='RED', or every incident
    if max_zone=None) - period A count vs period B count, machine-wide. Unlike the mean-based
    comparisons above there's only ONE number per period, so TREND_MIN_SAMPLES doesn't apply the
    same way; instead this gates on MONITORING COVERAGE in each period (compute_coverage(), the
    same "was this period even watched enough to draw a conclusion" check Long-term History
    already uses) - a quiet period with almost no telemetry coverage isn't evidence of "fewer
    incidents", it's evidence the machine (or Thermal Watch) wasn't running."""
    now = now if now is not None else time.time()
    older_start, older_end, recent_start, recent_end = calendar_window_halves(total_days, now)
    incidents = read_incidents_file()
    if max_zone is not None:
        incidents = [i for i in incidents if i.get("max_zone") == max_zone]
    older_count = len(overlapping_incidents(incidents, older_start, older_end))
    recent_count = len(overlapping_incidents(incidents, recent_start, recent_end))

    buckets = read_telemetry_file(since_ts=older_start)
    older_buckets = [b for b in buckets if older_start <= b["start_timestamp"] < older_end]
    recent_buckets = [b for b in buckets if recent_start <= b["start_timestamp"] < recent_end]
    _, _, older_cov = compute_coverage(older_buckets, older_end - older_start)
    _, _, recent_cov = compute_coverage(recent_buckets, recent_end - recent_start)
    if older_cov < TREND_MIN_COVERAGE_PCT or recent_cov < TREND_MIN_COVERAGE_PCT:
        return None
    delta = recent_count - older_count
    direction = "STABLE" if delta == 0 else ("WORSENING" if delta > 0 else "IMPROVING")
    return {"older_count": older_count, "recent_count": recent_count, "delta": delta, "direction": direction,
           "older_coverage_pct": older_cov, "recent_coverage_pct": recent_cov}


def _session_health_scores_by_id(sessions):
    """{session_id: score} for a mixed-workload session list, each session scored against ITS OWN
    workload's peers (group_sessions_by_workload first, then compute_workload_session_health_
    scores per group) - never mixing one workload's typical values into another's baseline. The
    shared plumbing behind compute_health_score_period_trend, whether it's called machine-wide
    (every workload) or pre-filtered to one workload's own sessions."""
    groups = group_sessions_by_workload(sessions)
    out = {}
    for group in groups.values():
        group_sessions = group["sessions"]
        for s, score in zip(group_sessions, compute_workload_session_health_scores(group_sessions)):
            out[s.get("session_id")] = score
    return out


def compute_health_score_period_trend(sessions, total_days, now=None):
    """Health-score (Phase 9) trend across a calendar-split lookback window - pass every
    completed session for the machine-wide "Average health score" line (WEEK OVER WEEK), or one
    workload's own sessions for a per-workload trend report. Every session's score is computed
    against its own workload's peers regardless of which half it falls in (never two separately-
    normalized halves), so a genuine trend in the resulting numbers reflects real behavioral
    change, not a baseline-selection artifact."""
    older_start, older_end, recent_start, recent_end = calendar_window_halves(total_days, now)
    combined = overlapping_sessions(sessions, older_start, recent_end)
    if not combined:
        return None
    score_by_id = _session_health_scores_by_id(combined)
    older_ids = {s.get("session_id") for s in overlapping_sessions(combined, older_start, older_end)}
    recent_ids = {s.get("session_id") for s in overlapping_sessions(combined, recent_start, recent_end)}
    older_scores = [score_by_id[i] for i in older_ids if i in score_by_id]
    recent_scores = [score_by_id[i] for i in recent_ids if i in score_by_id]
    return compare_period_values(older_scores, recent_scores, "pts", higher_is_worse=False)


def compute_week_over_week_report(top_n=2, now=None):
    """The WEEK OVER WEEK digest (7d vs previous 7d) across several data sources: the machine's
    top-N most recently-active workloads' own CPU/GPU-hotspot temperature trends (workload-
    matched, never mixed across workloads), the machine-wide idle-CPU trend, critical-incident
    frequency (coverage-gated), and the machine-wide average health-score trend. Every entry is
    either a real compare_period_values()-shaped result or explicitly None ("not enough data
    yet") - this function never guesses to fill a gap; the formatter decides how to render a
    None."""
    now = now if now is not None else time.time()
    _, _, recent_start, recent_end = calendar_window_halves(TREND_WOW_LOOKBACK_DAYS, now)
    all_sessions = read_sessions_file()
    groups = group_sessions_by_workload(all_sessions)

    def recent_session_count(group):
        return len(overlapping_sessions(group["sessions"], recent_start, recent_end))

    top_workloads = sorted(groups.items(), key=lambda kv: -recent_session_count(kv[1]))[:top_n]
    workload_trends = []
    for key, group in top_workloads:
        if recent_session_count(group) == 0:
            continue
        cpu_trend = compute_workload_period_trend(group["sessions"], "cpu", "avg_temp", "°C",
                                                   TREND_WOW_LOOKBACK_DAYS, now=now)
        gpu_trend = compute_workload_period_trend(group["sessions"], "gpu", "avg_hotspot_temp", "°C",
                                                   TREND_WOW_LOOKBACK_DAYS, now=now)
        workload_trends.append({"workload": group["display_name"], "cpu_temp": cpu_trend, "gpu_hotspot": gpu_trend})

    return {
        "window_days": TREND_WOW_LOOKBACK_DAYS,
        "workload_trends": workload_trends,
        "idle_cpu": compute_idle_metric_period_trend(scalar_sensor_ref("cpu_temp"), TREND_WOW_LOOKBACK_DAYS, now),
        "critical_incidents": compute_incident_frequency_trend(TREND_WOW_LOOKBACK_DAYS, max_zone="RED", now=now),
        "health_score": compute_health_score_period_trend(all_sessions, TREND_WOW_LOOKBACK_DAYS, now),
    }


def compute_workload_cooling_trend_report(workload_key, total_days=TREND_MONTH_LOOKBACK_DAYS, now=None):
    """The GPU COOLING — N DAY TREND report for ONE workload: hotspot temp, GPU power (shown as a
    % change), core temp, hotspot/core delta, and health score, all compared across the SAME
    calendar-split lookback window - plus an overall Trend/Confidence verdict driven by the
    PRIMARY signal (hotspot temp), corroborated against GPU power exactly like Cross-Sensor
    Diagnostics' own GPU COOLING PATTERN: a hotspot rise with power that DIDN'T also rise is a
    stronger, more confident cooling-degradation signal than one where power rose too (in the
    latter case, increased workload intensity hasn't been ruled out, so confidence is capped one
    tier lower than the raw statistics alone would suggest). Returns None if this workload has no
    recorded sessions at all."""
    now = now if now is not None else time.time()
    group = group_sessions_by_workload(read_sessions_file()).get(workload_key)
    if not group:
        return None
    sessions = group["sessions"]

    hotspot = compute_workload_period_trend(sessions, "gpu", "avg_hotspot_temp", "°C", total_days, now=now)
    core = compute_workload_period_trend(sessions, "gpu", "avg_core_temp", "°C", total_days, now=now)
    power = compute_workload_period_trend(sessions, "gpu", "avg_power", "W", total_days, now=now)
    delta = compute_hotspot_core_delta_period_trend(sessions, total_days, now=now)
    health = compute_health_score_period_trend(sessions, total_days, now)

    power_pct = None
    if power is not None and power["older_mean"]:
        power_pct = power["delta"] / power["older_mean"] * 100

    direction, confidence = "STABLE", None
    if hotspot is not None:
        direction = hotspot["direction"]
        if hotspot["direction"] != "STABLE":
            power_also_rose = power is not None and power["direction"] == "WORSENING"
            confidence = hotspot["confidence"] if not power_also_rose else \
                {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}[hotspot["confidence"]]

    return {"workload": group["display_name"], "window_days": total_days, "hotspot": hotspot, "core": core,
           "power": power, "power_pct": power_pct, "hotspot_core_delta": delta, "health_score": health,
           "direction": direction, "confidence": confidence}


def _fmt_signed(value, decimals, unit):
    sign = "+" if value >= 0 else ""
    return f"{sign}{value:.{decimals}f}{unit}"


def format_period_delta(trend, unit, decimals=0):
    """'72°C → 73°C (+1°C)' style formatting for one compare_period_values() result - the WEEK
    OVER WEEK worked example's exact shape. None in, None out (the caller decides how to render a
    missing line)."""
    if trend is None:
        return None
    return (f"{trend['older_mean']:.{decimals}f}{unit} → {trend['recent_mean']:.{decimals}f}{unit} "
           f"({_fmt_signed(trend['delta'], decimals, unit)})")


def format_week_over_week_report(report):
    """A compute_week_over_week_report() result -> its display lines, matching the WEEK OVER WEEK
    worked example's format exactly. Any single missing/not-established comparison is simply
    skipped (never a fabricated line); if EVERY comparison came back empty, says so explicitly
    rather than rendering a near-blank, confusing report."""
    lines = ["WEEK OVER WEEK"]
    shown = False
    for wt in report["workload_trends"]:
        if wt["cpu_temp"] is not None:
            lines.append(f"CPU average under {wt['workload']}: {format_period_delta(wt['cpu_temp'], '°C')}")
            shown = True
        if wt["gpu_hotspot"] is not None:
            lines.append(f"GPU Hotspot under {wt['workload']}: {format_period_delta(wt['gpu_hotspot'], '°C')}")
            shown = True
    if report["idle_cpu"] is not None:
        lines.append(f"Idle CPU: {format_period_delta(report['idle_cpu'], '°C')}")
        shown = True
    ci = report["critical_incidents"]
    if ci is not None:
        lines.append(f"Critical incidents: {ci['older_count']} → {ci['recent_count']}")
        shown = True
    hs = report["health_score"]
    if hs is not None:
        lines.append(f"Average health score: {hs['older_mean']:.0f} → {hs['recent_mean']:.0f}")
        shown = True
    if not shown:
        lines.append("Not enough data yet for a week-over-week comparison")
    return lines


def format_workload_cooling_trend_report(report):
    """A compute_workload_cooling_trend_report() result -> its display lines, matching the GPU
    COOLING — N DAY TREND worked example's format exactly (1-decimal signed deltas, unlike WEEK
    OVER WEEK's whole-number before/after pairs - matching the two examples' own distinct
    formats)."""
    if report is None:
        return ["No recorded sessions for this workload yet"]
    lines = [f"GPU COOLING — {report['window_days']} DAY TREND"]
    if report["hotspot"] is not None:
        lines.append(f"Hotspot under comparable {report['workload']} sessions: "
                     f"{_fmt_signed(report['hotspot']['delta'], 1, '°C')}")
    if report["power_pct"] is not None:
        lines.append(f"GPU power: {_fmt_signed(report['power_pct'], 1, '%')}")
    if report["core"] is not None:
        lines.append(f"Core temperature: {_fmt_signed(report['core']['delta'], 1, '°C')}")
    if report["hotspot_core_delta"] is not None:
        lines.append(f"Hotspot/Core delta: {_fmt_signed(report['hotspot_core_delta']['delta'], 1, '°C')}")
    if report["health_score"] is not None:
        hs = report["health_score"]
        lines.append(f"Health score: {hs['older_mean']:.0f} → {hs['recent_mean']:.0f}")
    if report["hotspot"] is None and report["core"] is None and report["health_score"] is None:
        lines.append("Not enough data yet for this workload over this window")
        return lines
    lines.append(f"Trend: {report['direction']}")
    if report["confidence"] is not None:
        lines.append(f"Confidence: {report['confidence']}")
    return lines


# ---------------------------------------------------------------------------
# Recommendations - the top layer: Telemetry -> Baselines -> Anomalies -> Cross-Sensor
# Diagnostics -> Health Scores -> Trends -> Recommendation (the user's own framing). Deterministic
# and evidence-backed, NEVER AI-generated: every recommendation is produced by a fixed rule
# consuming ONLY already-computed signals from the layers below it - no new sensor reads, no new
# thresholds invented here that don't already exist somewhere else in this file. ADVISORY ONLY -
# this file contains no hardware-control code path at all (no fan curve, power limit, voltage,
# clock, or BIOS write, and never shuts anything down); every recommendation ends with the same
# honest caveat that Thermal Watch can describe a SENSOR PATTERN, not determine a physical cause
# from software telemetry alone.
#
# Two axes, deliberately independent (the user's own words: "A HIGH-confidence observation
# doesn't automatically mean something is dangerous"):
#   Confidence (LOW/MEDIUM/HIGH) - how sure Thermal Watch is the PATTERN is real: repetition
#   (RECOMMENDATION_MIN_OCCURRENCES/RECOMMENDATION_GENEROUS_OCCURRENCES, the same two-factor
#   spirit as Trend Intelligence's own rubric) plus corroboration (a second independent signal
#   supporting the same interpretation, same idea as Cross-Sensor Diagnostics).
#   Urgency (INFO/MONITOR/MAINTENANCE/IMMEDIATE ATTENTION) - how close to actual thermal danger
#   the observed behavior gets, derived from the EXISTING zone thresholds (cpu_zone_for/
#   GPU_HOTSPOT_ZONES) - never a new number invented for this phase.
#
# The default, and by far the most common, output is NO ACTION RECOMMENDED - a recommendation
# only exists when a rule's own evidence bar is actually cleared; nothing in this file ever lowers
# that bar just to have something to say (the user's own explicit design goal: "so the system
# doesn't become a machine for manufacturing problems").
# ---------------------------------------------------------------------------
RECOMMENDATION_MIN_OCCURRENCES = 3       # fewer repeats than this = not a pattern yet, stay silent
RECOMMENDATION_GENEROUS_OCCURRENCES = 4  # at/above this, confidence can reach its rule's own ceiling
RECOMMENDATION_POWER_STABLE_PCT = 5.0    # +/- this many % counts as "power remained stable" -
                                          # a plain, documented round number (no existing
                                          # percentage-based power convention to tie it to, unlike
                                          # the °C/W absolute-delta table)


def _urgency_from_zone(zone_key):
    """INFO/MONITOR/MAINTENANCE/IMMEDIATE ATTENTION, derived from an EXISTING thermal zone
    classification (never a new threshold) - urgency answers "how close to real thermal danger",
    a question entirely separate from confidence's "how sure are we this pattern is real"."""
    return {"RED": "IMMEDIATE ATTENTION", "ORANGE": "MAINTENANCE", "YELLOW": "MONITOR"}.get(zone_key, "INFO")


def sessions_at_zone_ceiling(sessions, component, min_fraction=0.1):
    """Sessions (regardless of workload) where `component`'s OWN zone_time (already recorded per
    session - the exact numbers Health Scoring's own zone-time deduction already reads) shows
    ORANGE or RED for at least `min_fraction` of that session's own duration - never a new
    threshold, reuses the EXISTING per-session zone_time breakdown. min_fraction=0.1 filters out
    one brief spike from counting as "this session reached its thermal ceiling"."""
    out = []
    for s in sessions:
        duration = s.get("duration_seconds") or 0
        if duration <= 0:
            continue
        times = (s.get("zone_time") or {}).get(component) or {}
        hot_seconds = times.get("ORANGE", 0.0) + times.get("RED", 0.0)
        if hot_seconds / duration >= min_fraction:
            out.append(s)
    return out


def recommend_gpu_cooling(workload_key, now=None):
    """GPU cooler-contact/thermal-interface recommendation for ONE workload - fires only when
    that workload's OWN 30-day GPU COOLING trend (compute_workload_cooling_trend_report, the SAME
    calendar-split two-group comparison Trend Intelligence already uses) shows a genuine
    WORSENING hotspot trend at MEDIUM-or-better confidence. Deliberately NOT a per-session leave-
    one-out occurrence count: the more sessions share a new pattern, the more they'd pollute each
    other's own leave-one-out baseline (each "new" session's peers would increasingly include
    OTHER "new" sessions, widening the comparison's spread and masking the very shift being
    looked for) - a calendar-split two-group comparison doesn't have that problem, since the
    older/recent groups are fixed by TIME, not by how many other sessions look similar.
    Confidence and the power-corroboration downgrade are inherited directly from that trend
    report, never recomputed here, so this recommendation and Trend Intelligence can never
    quietly disagree about the same evidence."""
    now = now if now is not None else time.time()
    report = compute_workload_cooling_trend_report(workload_key, TREND_MONTH_LOOKBACK_DAYS, now)
    if report is None or report["hotspot"] is None or report["direction"] != "WORSENING":
        return None
    if report["confidence"] in (None, "LOW"):
        return None  # a single-tier-above-nothing signal isn't enough to recommend a physical check

    hotspot, delta = report["hotspot"], report["hotspot_core_delta"]
    evidence = [f"Hotspot/core delta: {delta['recent_mean']:.0f}°C"] if delta is not None else []
    if delta is not None and delta["older_stats"]["established"] and delta["older_stats"]["stddev"] is not None:
        stats = delta["older_stats"]
        lo, hi = stats["mean"] - stats["stddev"], stats["mean"] + stats["stddev"]
        evidence.append(f"Historical typical delta: {lo:.0f}–{hi:.0f}°C")
    if delta is not None:
        evidence.append(f"30-day delta trend: {_fmt_signed(delta['delta'], 1, '°C')}")
    if report["power_pct"] is not None:
        power_stable = abs(report["power_pct"]) < RECOMMENDATION_POWER_STABLE_PCT
        evidence.append(f"GPU power remained {'stable' if power_stable else 'elevated'}: "
                        f"{_fmt_signed(report['power_pct'], 1, '%')}")
    if report["core"] is not None:
        core_delta = report["core"]["delta"]
        qualifier = "only " if abs(core_delta) < ANOMALY_MIN_ABS_DELTA["°C"] else ""
        evidence.append(f"GPU core temperature changed {qualifier}{_fmt_signed(core_delta, 1, '°C')}")
    evidence.append(f"Finding repeated across {hotspot['n_recent']} recent session(s)")

    zone = zone_for(hotspot["recent_mean"], GPU_HOTSPOT_ZONES)

    return {
        "title": f"COOLING RECOMMENDATION — GPU ({report['workload']})",
        "recommendation": "Consider inspecting GPU cooler contact / thermal interface",
        "evidence": evidence, "confidence": report["confidence"],
        "urgency": _urgency_from_zone(zone["key"] if zone else None),
        "caveat": ("This pattern can be consistent with declining thermal-interface/contact performance, "
                  "but Thermal Watch cannot determine the physical cause from software telemetry alone."),
    }


def recommend_cpu_cooling(live_system_temp_moderate=None, live_cpu_fan_rpm=None, now=None):
    """CPU cooler-capacity/contact recommendation, MACHINE-WIDE (not workload-scoped like GPU
    cooling above) - a CPU that repeatedly reaches its ORANGE/RED ceiling is a property of the
    whole system's cooling, not any one workload's own behavior. Historical evidence (repeat
    count, peak temp/power ranges, distinct-workload count) is fully reconstructable from stored
    session records; the System-sensor/fan corroboration Cross-Sensor Diagnostics' own live-only
    CPU COOLING PATTERN uses is only ever knowable for the CURRENT moment (see that pattern's own
    scope note on the fan-RPM gap) - so confidence here is deliberately capped at MEDIUM even with
    generous repetition and live corroboration; repetition alone (without live corroboration
    available right now) still supports a LOW-confidence recommendation rather than staying
    silent. `live_system_temp_moderate`/`live_cpu_fan_rpm` are optional - the caller supplies
    them from the SAME live values SensorHistoryWindow's own CPU COOLING PATTERN check already
    gathers (this function never reads live state itself, staying a pure function like every
    other rule in this file)."""
    now = now if now is not None else time.time()
    sessions = read_sessions_file()
    hot_sessions = sessions_at_zone_ceiling(sessions, "cpu")
    if len(hot_sessions) < RECOMMENDATION_MIN_OCCURRENCES:
        return None

    workload_keys = {s.get("workload_key") for s in hot_sessions if s.get("workload_key")}
    temps = [t for t in ((s.get("cpu") or {}).get("peak_temp") for s in hot_sessions) if t is not None]
    powers = [p for p in ((s.get("cpu") or {}).get("avg_power") for s in hot_sessions) if p is not None]

    evidence = []
    if temps:
        evidence.append(f"CPU repeatedly reaches {min(temps):.0f}–{max(temps):.0f}°C")
    if powers:
        evidence.append(f"Package power remains ~{min(powers):.0f}–{max(powers):.0f}W")
    corroborated = False
    if live_system_temp_moderate:
        evidence.append("System temperature remains moderate")
        corroborated = True
    if live_cpu_fan_rpm is not None and live_cpu_fan_rpm > 0:
        evidence.append(f"CPU fan is responding ({live_cpu_fan_rpm:,.0f} RPM)")
    evidence.append(f"Pattern occurred in {len(workload_keys)} comparable workload(s)")

    generous = len(hot_sessions) >= RECOMMENDATION_GENEROUS_OCCURRENCES
    confidence = "MEDIUM" if (generous or corroborated) else "LOW"  # never HIGH - see docstring

    zone = cpu_zone_for(max(temps)) if temps else None
    return {
        "title": "CPU COOLING",
        "recommendation": "Consider reviewing CPU cooling performance",
        "evidence": evidence, "confidence": confidence, "urgency": _urgency_from_zone(zone["key"] if zone else None),
        "caveat": ("Possible explanations include cooler capacity, mounting/contact, thermal interface, "
                  "or intentionally aggressive CPU boost behavior."),
    }


NO_ACTION_RECOMMENDATION = {
    "title": "NO ACTION RECOMMENDED",
    "recommendation": "Current behavior is consistent with this machine's established baseline.",
    "evidence": [], "confidence": None, "urgency": None, "caveat": None,
}


def compute_recommendations(live_system_temp_moderate=None, live_cpu_fan_rpm=None, now=None):
    """Runs every recommendation rule and returns the list of ones that actually fired - or a
    single explicit NO_ACTION_RECOMMENDATION entry if none did. Never partially recommends: a
    rule either clears its own evidence bar or contributes nothing at all - this function itself
    never lowers any rule's bar just to have something to say."""
    now = now if now is not None else time.time()
    groups = group_sessions_by_workload(read_sessions_file())
    results = []
    for key in groups:
        r = recommend_gpu_cooling(key, now)
        if r is not None:
            results.append(r)
    cpu_rec = recommend_cpu_cooling(live_system_temp_moderate, live_cpu_fan_rpm, now)
    if cpu_rec is not None:
        results.append(cpu_rec)
    return results or [dict(NO_ACTION_RECOMMENDATION)]


def format_recommendation(rec):
    """One recommendation -> its display lines, matching the worked examples' shape: title,
    the recommendation itself, an evidence section headed "Why Thermal Watch is suggesting this",
    Confidence, Urgency, and a closing caveat - or, for NO_ACTION_RECOMMENDATION, just the title
    and the one-line explanation (confidence/urgency/evidence/caveat are all None - there's
    nothing to hedge or corroborate when nothing fired)."""
    lines = [rec["title"], "", rec["recommendation"]]
    if rec["confidence"] is None:
        return lines
    lines.append("")
    lines.append("Why Thermal Watch is suggesting this")
    lines.extend(f"  {e}" for e in rec["evidence"])
    lines.append("")
    lines.append(f"Confidence: {rec['confidence']}")
    urgency_suffix = " — NOT EMERGENCY" if rec["urgency"] == "MAINTENANCE" else ""
    lines.append(f"Urgency: {rec['urgency']}{urgency_suffix}")
    lines.append("")
    lines.append(rec["caveat"])
    return lines


# ---------------------------------------------------------------------------
# Cooling/Fan Intelligence - recommendations-adjacent, but answering a narrower, more mechanical
# question than Recommendations does: "is additional fan speed actually buying meaningful
# cooling, at COMPARABLE load/power, or has it plateaued?" RECOMMENDATIONS ONLY, same as every
# other layer - this file never writes a fan curve, never touches fan control of any kind; it
# only ever reads already-persisted telemetry and describes what it observed.
#
# The user's own explicit requirement: never correlate from a live-only snapshot and call it a
# learned curve. Concretely, that means every comparison here requires REAL accumulated telemetry
# history - multiple genuinely distinct fan-speed levels observed at comparable load, each with
# enough samples AND spanning multiple distinct calendar days (FAN_RESPONSE_MIN_DISTINCT_DAYS) -
# never satisfied by one sitting, however long. Below that bar: None, "not enough data yet",
# exactly like every other minimum-data gate in this file (BASELINE_MIN_SESSIONS,
# BASELINE_MIN_IDLE_BUCKETS, TREND_MIN_SAMPLES, RECOMMENDATION_MIN_OCCURRENCES).
#
# Prerequisite this phase adds: cpu_fan_rpm/gpu_fan_pct are now PERSISTED telemetry scalars (see
# TELEMETRY_SCALAR_KEYS above) - they were already being read into last_context for Cross-Sensor
# Diagnostics' live-only checks, but never stored. On a machine with no history yet (including
# THIS one, until real usage accumulates from this point forward), every function below correctly
# returns "not enough data yet" - there is no way to backfill history that was never recorded.
#
# Scope note (documented, not silent): this pass covers CPU Fan and GPU Fan (nvidia-smi's
# fan.speed, a PERCENTAGE - see TELEMETRY_SCALAR_LABELS's own note on why GPU fan isn't RPM here)
# only. Individual case/System fan headers (System Fan #1-6, Pump Fan) are NOT yet tracked in
# telemetry history - they're multiple, per-system, often-unpopulated sensors that belong in the
# EXISTING per-sensor EAV mechanism (sensor_readings, the same one drives/DIMMs/motherboard
# temps already use), not the scalar table used here. Wiring that in, and the broader "does more
# case airflow actually change CPU/GPU/System temperature" question and a genuine multi-load-band
# fan-CURVE-SHAPE suggestion (as opposed to this pass's two-point response/diminishing-returns
# check), are natural extensions of this same machinery, deliberately deferred - this phase
# implements exactly the two worked examples given, not the full "eventually" wishlist.
# ---------------------------------------------------------------------------
FAN_RESPONSE_MIN_BUCKETS_PER_BIN = 30    # >=30 minutes of comparable-load, comparable-fan-speed
                                          # telemetry time before a fan-speed BIN counts at all
FAN_RESPONSE_MIN_BINS = 2                # need at least 2 distinct, qualifying fan-speed levels
                                          # at the SAME comparable load to compare anything
FAN_RESPONSE_MIN_DISTINCT_DAYS = 2       # a qualifying bin's samples must span >=2 calendar days -
                                          # the direct fix for "don't learn a curve from one sitting"
FAN_RESPONSE_DIMINISHING_RATIO = 0.3     # the LAST leg's cooling-per-fan-unit rate is "diminishing"
                                          # when it's under 30% of the FIRST leg's rate - a plain,
                                          # documented round number (no existing convention to tie
                                          # a marginal-cooling-rate ratio to)
GPU_LOAD_BAND_WIDTH = 10.0    # % - group GPU utilization into 10%-wide comparability bands
CPU_LOAD_BAND_WIDTH = 10.0    # % - group CPU utilization into 10%-wide comparability bands
GPU_FAN_BIN_WIDTH = 10.0      # % - group GPU fan speed into 10%-wide bins
CPU_FAN_BIN_WIDTH = 200.0     # RPM - group CPU fan speed into 200-RPM-wide bins


def group_buckets_by_comparable_load_and_fan(buckets, load_ref, fan_ref, temp_ref, load_band_width,
                                             fan_bin_width, power_ref=None):
    """Telemetry buckets -> {(load_band, fan_bin): [{'temp', 'power', 'start_timestamp'}, ...]}.
    A bucket missing ANY of load/fan/temp that tick is excluded outright - never a fabricated
    pairing; 'power' is carried along for display context only and is None on a bucket that
    happened to miss it, never excluding the entry over that alone. load_band/fan_bin are each
    metric's own bucket-average rounded to the nearest band/bin width, so buckets from genuinely
    different moments in time land in the same cell purely because their real load and real fan
    speed happened to be close - not because of any artificial grouping by session or time
    window."""
    cells = {}
    for b in buckets:
        load, fan, temp = extract_bucket_metric(b, load_ref), extract_bucket_metric(b, fan_ref), extract_bucket_metric(b, temp_ref)
        if not load or not fan or not temp:
            continue
        power = extract_bucket_metric(b, power_ref) if power_ref is not None else None
        load_band = round(load["avg"] / load_band_width) * load_band_width
        fan_bin = round(fan["avg"] / fan_bin_width) * fan_bin_width
        cells.setdefault((load_band, fan_bin), []).append({"temp": temp["avg"], "power": power["avg"] if power else None,
                                                            "start_timestamp": b["start_timestamp"]})
    return cells


def _distinct_days(entries):
    """Real LOCAL calendar-day diversity - NOT timestamp // 86400 (a UTC epoch-day count), which
    silently miscounts for anyone not at UTC+0: a run spanning less than an hour can straddle a
    UTC-midnight boundary while staying entirely within one local day (e.g. every day around
    5pm Pacific, UTC-7 - confirmed by this exact bug reproducing then, not something guessed at).
    Matches the same local-calendar-date discipline Scheduled Health Reports already established
    for exactly this class of "a day is not 86400 seconds" bug."""
    return len({datetime.fromtimestamp(e["start_timestamp"]).date() for e in entries})


def compute_fan_cooling_response(component, since_ts=None, now=None):
    """Does higher fan speed measurably lower temperature AT COMPARABLE load, learned ONLY from
    real accumulated telemetry history? component is 'cpu' or 'gpu'. Groups telemetry buckets
    into (load_band, fan_bin) cells; picks whichever load band has the most fan-bin diversity
    that actually clears the minimum-data bar (FAN_RESPONSE_MIN_BUCKETS_PER_BIN samples spanning
    >=FAN_RESPONSE_MIN_DISTINCT_DAYS distinct calendar days per bin); within that band, reports
    the response between the LOWEST and HIGHEST qualifying fan bins, plus a pairwise breakdown
    between every adjacent qualifying bin (the raw material both worked examples' numbers come
    from) and a diminishing-returns verdict comparing the marginal cooling rate of the last leg
    to the first. Returns None - explicitly "not enough data yet" - if no load band has at least
    FAN_RESPONSE_MIN_BINS qualifying bins; this is the expected, correct answer on any machine
    (including this one) until real fan-speed-varying telemetry has actually accumulated."""
    now = now if now is not None else time.time()
    since_ts = since_ts if since_ts is not None else now - TELEMETRY_RANGE_SECONDS["30d"]
    if component == "gpu":
        load_ref = scalar_sensor_ref("gpu_util")
        fan_ref = scalar_sensor_ref("gpu_fan_pct")
        temp_ref = scalar_sensor_ref("gpu_hotspot_temp")
        power_ref = scalar_sensor_ref("gpu_power")
        load_band_width, fan_bin_width, fan_unit = GPU_LOAD_BAND_WIDTH, GPU_FAN_BIN_WIDTH, "%"
    elif component == "cpu":
        load_ref = scalar_sensor_ref("cpu_util")
        fan_ref = scalar_sensor_ref("cpu_fan_rpm")
        temp_ref = scalar_sensor_ref("cpu_temp")
        power_ref = scalar_sensor_ref("cpu_power")
        load_band_width, fan_bin_width, fan_unit = CPU_LOAD_BAND_WIDTH, CPU_FAN_BIN_WIDTH, "RPM"
    else:
        raise ValueError(f"unknown component: {component!r}")

    buckets = read_telemetry_file(since_ts=since_ts)
    cells = group_buckets_by_comparable_load_and_fan(buckets, load_ref, fan_ref, temp_ref, load_band_width,
                                                     fan_bin_width, power_ref=power_ref)

    by_load_band = {}
    for (load_band, fan_bin), entries in cells.items():
        by_load_band.setdefault(load_band, {})[fan_bin] = entries

    best_band, best_bins = None, None
    for load_band, fan_bins in by_load_band.items():
        qualifying = {fb: entries for fb, entries in fan_bins.items()
                     if len(entries) >= FAN_RESPONSE_MIN_BUCKETS_PER_BIN
                     and _distinct_days(entries) >= FAN_RESPONSE_MIN_DISTINCT_DAYS}
        if len(qualifying) >= FAN_RESPONSE_MIN_BINS and (best_bins is None or len(qualifying) > len(best_bins)):
            best_band, best_bins = load_band, qualifying
    if best_bins is None:
        return None

    sorted_bins = sorted(best_bins.items())  # [(fan_bin, entries), ...] ascending fan speed
    bin_summaries = [{"fan": fb, "avg_temp": sum(e["temp"] for e in entries) / len(entries), "n": len(entries)}
                     for fb, entries in sorted_bins]
    lowest, highest = bin_summaries[0], bin_summaries[-1]

    pairwise = []
    for a, b in zip(bin_summaries, bin_summaries[1:]):
        fan_delta = b["fan"] - a["fan"]
        temp_delta = b["avg_temp"] - a["avg_temp"]
        pairwise.append({"fan_from": a["fan"], "fan_to": b["fan"], "temp_from": a["avg_temp"], "temp_to": b["avg_temp"],
                         "fan_delta": fan_delta, "temp_delta": temp_delta,
                         "rate": (temp_delta / fan_delta) if fan_delta else None})
    diminishing = None
    if len(pairwise) >= 2:
        first_rate, last_rate = pairwise[0]["rate"], pairwise[-1]["rate"]
        if first_rate is not None and last_rate is not None and first_rate < 0:
            diminishing = abs(last_rate) < abs(first_rate) * FAN_RESPONSE_DIMINISHING_RATIO

    all_entries = [e for entries in best_bins.values() for e in entries]
    power_values = [e["power"] for e in all_entries if e["power"] is not None]

    return {"component": component, "fan_unit": fan_unit, "load_band": best_band,
           "avg_power": (sum(power_values) / len(power_values)) if power_values else None,
           "bins": bin_summaries, "lowest": lowest, "highest": highest,
           "response_delta": highest["avg_temp"] - lowest["avg_temp"],
           "pairwise": pairwise, "diminishing_returns": diminishing}


def format_fan_cooling_response(report, component_label):
    """A compute_fan_cooling_response() result -> its display lines, matching the two worked
    examples' shapes: the overall lowest-vs-highest response (example 1's shape) always shown
    when a report exists, with a DIMINISHING RETURNS section (example 2's shape, using the LAST
    pairwise leg specifically) appended when the marginal cooling rate has genuinely dropped off."""
    if report is None:
        return [f"{component_label} COOLING RESPONSE", "", "Not enough data yet - needs multiple fan-speed "
                f"levels observed at comparable load, on at least {FAN_RESPONSE_MIN_DISTINCT_DAYS} different days."]
    lo, hi, unit = report["lowest"], report["highest"], report["fan_unit"]
    fan_fmt = (lambda v: f"{v:,.0f}") if unit == "RPM" else (lambda v: f"{v:.0f}")
    lines = [f"{component_label} COOLING RESPONSE", "", f"{component_label} load: ~{report['load_band']:.0f}%"]
    if report["avg_power"] is not None:
        lines.append(f"{component_label} power: ~{report['avg_power']:.0f}W")
    lines.append(f"{component_label} fan{'s' if component_label == 'GPU' else ''}: "
                f"{fan_fmt(lo['fan'])} → {fan_fmt(hi['fan'])} {unit}")
    lines.append(f"Hotspot: {lo['avg_temp']:.0f} → {hi['avg_temp']:.0f}°C" if component_label == "GPU"
                else f"CPU: {lo['avg_temp']:.0f} → {hi['avg_temp']:.0f}°C")
    lines.append(f"Cooling response: {_fmt_signed(report['response_delta'], 0, '°C')}")
    lines.append("")
    if report["response_delta"] <= -3.0:
        lines.append("Higher fan speed produced a meaningful temperature reduction at comparable load/power.")
    elif report["response_delta"] >= -1.0:
        lines.append("Higher fan speed produced little measurable temperature reduction at comparable load/power.")
    else:
        lines.append("Higher fan speed produced a modest temperature reduction at comparable load/power.")

    if report["diminishing_returns"] and report["pairwise"]:
        last = report["pairwise"][-1]
        fan_pct_change = (last["fan_delta"] / last["fan_from"] * 100) if last["fan_from"] else None
        lines += ["", "DIMINISHING RETURNS"]
        pct_text = f" ({_fmt_signed(fan_pct_change, 0, '%')})" if fan_pct_change is not None else ""
        lines.append(f"{component_label} fan{'s' if component_label == 'GPU' else ''}: "
                    f"{fan_fmt(last['fan_from'])} → {fan_fmt(last['fan_to'])} {unit}{pct_text}")
        lines.append((f"Hotspot: {last['temp_from']:.0f} → {last['temp_to']:.0f}°C" if component_label == "GPU"
                     else f"CPU: {last['temp_from']:.0f} → {last['temp_to']:.0f}°C")
                    + f" ({_fmt_signed(last['temp_delta'], 0, '°C')})")
        lines.append("")
        lines.append("Additional fan speed produced little additional cooling.")
    return lines


# ---------------------------------------------------------------------------
# Hardware-change Experiments - "I changed something; did it actually help?" The user marks a real
# physical change (new fan, repasted GPU, added case fans, cleaned dust) with the date it
# happened, and Thermal Watch compares the machine's OWN measured behavior before and after that
# marker. Nothing statistical is invented here: the marker simply replaces Trend Intelligence's
# calendar-MIDPOINT split with a USER-CHOSEN split point, and every comparison then runs through
# the same compare_period_values() primitive (the before period IS the baseline; the after
# period's mean is what gets judged against it), the same TREND_MIN_SAMPLES bar, and the same
# two-factor _trend_confidence rubric. No new thresholds beyond the two window rules below, both
# of which exist to keep the comparison FAIR rather than to judge the result.
#
# This is also the first layer that stores something the USER authored rather than something the
# machine measured, which drives four deliberate design decisions:
#
#   1. Experiment markers are NEVER auto-pruned, unlike incidents/sessions/telemetry (all capped
#      at 30 days). Silently deleting a user's own annotation of their own hardware is not this
#      app's call, and the records are tiny and text-only. What DOES expire is the measured data
#      around a marker - handled honestly by clamping the comparison windows to what history
#      actually still exists (below), never by discarding the marker itself.
#   2. EQUAL-DURATION windows, always. The "after" window is however much time has genuinely
#      elapsed since the change (capped at EXPERIMENT_WINDOW_DAYS), and the "before" window is
#      made exactly that same length. A 14-day "before" compared against a 2-day "after" would let
#      the longer side accumulate more samples across more varied conditions purely because it was
#      longer - a sampling artifact, not a hardware effect.
#   3. EXPERIMENT_MIN_ELAPSED_DAYS before any verdict exists at all. "I repasted the GPU an hour
#      ago, is it better?" has no honest answer from an hour of data, however many sessions were
#      squeezed into it (the same spirit as Cooling/Fan Intelligence's own multi-distinct-day rule:
#      one sitting is not history).
#   4. EVIDENCE, NEVER ATTRIBUTION - the project's standing non-causal-language rule, and the one
#      that matters most here because a marker is so tempting to read as a cause. The report says
#      what changed in the measurements ACROSS the marker; it never says the marked change PRODUCED
#      that difference. Drivers, ambient room temperature, dust, and workload mix all move between
#      two calendar periods and software telemetry cannot separate them, so that caveat is part of
#      every report - and a SECOND marked change falling inside either window is reported as an
#      explicit confound that caps confidence at LOW outright (naming the overlapping change),
#      since attribution between two simultaneous changes isn't recoverable from this data at all.
#
# Corroboration here is deliberately ASYMMETRIC, and the distinction is the whole point:
#   - POWER moving the same direction as temperature is an ALTERNATIVE EXPLANATION, so it
#     DOWNGRADES confidence one tier (cooler GPU that also drew 40W less may simply have been
#     working less hard). This generalizes compute_workload_cooling_trend_report's existing
#     power-corroboration rule to the improving direction as well as the worsening one.
#   - A SECOND INDEPENDENT TEMPERATURE moving the same direction is genuine SUPPORT, so its
#     absence is what downgrades (used for chassis/airflow-class changes, where the idle resting
#     temperature of two different components moving together is the real signal).
# ---------------------------------------------------------------------------
EXPERIMENTS_PATH = data_path("thermal_watch_experiments.jsonl")

EXPERIMENT_WINDOW_DAYS = 14      # max length of EACH side of the comparison. 14+14 = 28 days of
                                  # span, deliberately inside the existing 30-day SESSION_RETENTION_
                                  # DAYS/TELEMETRY_RETENTION_DAYS window - a longer default would
                                  # promise a comparison the stored data can never supply.
EXPERIMENT_MIN_ELAPSED_DAYS = 1  # no verdict at all until a full day has passed since the change
EXPERIMENT_MAX_HISTORY_DAYS = SESSION_RETENTION_DAYS  # not a new number - the retention window that
                                                       # already governs what "before" data survives

# Which measured signal answers "did this change help?", per class of change. `block`/`temp_field`
# name a completed session's own already-recorded stats (workload-matched, load-comparable - the
# preferred evidence); `idle_scalar` names the telemetry scalar used when no workload has enough
# sessions on both sides, and is the PRIMARY signal outright for a chassis/airflow-class change,
# where resting temperature is the cleanest measure and no single workload owns the effect.
EXPERIMENT_COMPONENTS = {
    "gpu": {"label": "GPU", "block": "gpu", "temp_field": "avg_hotspot_temp", "temp_label": "GPU Hotspot",
           "power_field": "avg_power", "power_label": "GPU power", "idle_scalar": "gpu_hotspot_temp"},
    "cpu": {"label": "CPU", "block": "cpu", "temp_field": "avg_temp", "temp_label": "CPU Package",
           "power_field": "avg_power", "power_label": "CPU power", "idle_scalar": "cpu_temp"},
    "system": {"label": "System", "block": None, "temp_field": None, "temp_label": None,
              "power_field": None, "power_label": None, "idle_scalar": "cpu_temp"},
}
EXPERIMENT_COMPONENT_ORDER = ("gpu", "cpu", "system")
EXPERIMENT_COMPONENT_LABELS = {"gpu": "GPU", "cpu": "CPU", "system": "System / airflow"}
EXPERIMENT_IDLE_SCALARS = ("cpu_temp", "gpu_hotspot_temp")  # the two resting temperatures reported
                                                             # on every experiment, whatever its class
EXPERIMENT_DIRECTION_LABELS = {"IMPROVING": "IMPROVED", "WORSENING": "WORSE",
                              "STABLE": "NO MEASURED CHANGE"}


def _downgrade_confidence(confidence):
    """One tier down, LOW-floored - the same mapping compute_workload_cooling_trend_report applies
    inline for its own power corroboration. Kept as a separate named helper used only by this
    phase rather than refactoring that already-verified call site (same precedent as
    _live_system_temp_moderate vs SensorHistoryWindow's own inline idle-baseline lookup)."""
    return {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}.get(confidence, confidence)


def parse_experiment_timestamp(text, now=None):
    """'2026-08-04' or '2026-08-04 19:40' (local time) -> epoch seconds, or None if it isn't a
    real date/time in one of those two shapes, or is in the FUTURE. A date-only value means
    midnight local, i.e. the whole of that day counts as "after". Rejecting future timestamps is
    honest validation, not politeness: no measured data can exist after a change that hasn't
    happened yet, so such a marker could only ever produce an empty comparison."""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            ts = time.mktime(time.strptime(text, fmt))
        except (ValueError, OverflowError):
            continue
        return None if ts > (now if now is not None else time.time()) else ts
    return None


def new_experiment_record(description, change_timestamp, component, now=None, existing_ids=()):
    """Pure builder for one experiment marker. `component` must be a key of EXPERIMENT_COMPONENTS
    (it selects which measured signal answers this experiment, not merely a label), and
    `description` must be real text - an unlabelled marker is useless months later when the whole
    point is remembering what was changed."""
    description = (description or "").strip()
    if not description:
        raise ValueError("an experiment needs a description of what was changed")
    if component not in EXPERIMENT_COMPONENTS:
        raise ValueError(f"unknown experiment component: {component!r}")
    if change_timestamp is None:
        raise ValueError("an experiment needs the date the change was made")
    created = now if now is not None else time.time()
    experiment_id = f"exp-{int(change_timestamp)}"
    suffix = 1
    while experiment_id in set(existing_ids):
        suffix += 1
        experiment_id = f"exp-{int(change_timestamp)}-{suffix}"
    return {"experiment_id": experiment_id, "created_timestamp": created,
           "change_timestamp": float(change_timestamp), "description": description,
           "component": component}


def read_experiments_file():
    """All persisted experiment markers, newest CHANGE first - mirrors read_incidents_file()/
    read_sessions_file(), but sorts explicitly by change_timestamp rather than reversing file
    order: unlike incidents and sessions (appended as they happen, so file order IS time order), a
    user can mark a change they made last month at any time, so append order says nothing about
    when the change actually occurred."""
    if not EXPERIMENTS_PATH.exists():
        return []
    out = []
    try:
        for line in EXPERIMENTS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue  # valid JSON but not a record (e.g. a garbled write leaving `null`/`[]`)
            if rec.get("change_timestamp") is not None:
                out.append(rec)
    except OSError:
        return []
    out.sort(key=lambda r: r["change_timestamp"], reverse=True)
    return out


def append_experiment(record):
    """Append one marker (same plain-JSONL append as _persist_session/_persist_incident). Returns
    True only if it actually reached disk - the caller surfaces a failure rather than showing a
    marker in the UI that isn't really stored."""
    try:
        with EXPERIMENTS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        return False
    return True


def delete_experiment(experiment_id):
    """Remove one marker by id, rewriting the file via a temp file + atomic replace (the same
    durability pattern _save_active_incidents uses) so an interrupted delete can never leave a
    half-written store. Returns True if a record was actually removed."""
    records = read_experiments_file()
    kept = [r for r in records if r.get("experiment_id") != experiment_id]
    if len(kept) == len(records):
        return False
    kept.sort(key=lambda r: r["change_timestamp"])  # store stays oldest-first like the other JSONLs
    try:
        tmp = EXPERIMENTS_PATH.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")
        tmp.replace(EXPERIMENTS_PATH)
    except OSError:
        return False
    return True


def experiment_window_bounds(change_ts, now=None, window_days=EXPERIMENT_WINDOW_DAYS,
                             max_history_days=EXPERIMENT_MAX_HISTORY_DAYS):
    """The two EQUAL-LENGTH comparison windows around one marker, or None with the reason why
    there aren't any yet. The shared duration is the smallest of: time actually elapsed since the
    change, how much history still exists before it (retention), and window_days - so the two
    sides always span the same amount of calendar time, and never reach past data that has already
    been pruned. Returns {'before_start','before_end','after_start','after_end','duration_days'};
    the two windows are adjacent and share only the instant of the change itself. A session that
    STRADDLES the marker therefore overlaps both and is counted on both sides - deliberately the
    same convention calendar_window_halves-based trends already use (overlapping_sessions(), not a
    hand-rolled cut), rather than a new boundary rule invented for this phase."""
    now = now if now is not None else time.time()
    retention_start = now - max_history_days * 86400
    elapsed = now - change_ts
    available_before = change_ts - retention_start
    min_seconds = EXPERIMENT_MIN_ELAPSED_DAYS * 86400
    if elapsed < min_seconds:
        return None, (f"only {elapsed / 86400:.1f} days since the change - needs at least "
                     f"{EXPERIMENT_MIN_ELAPSED_DAYS} day before a before/after comparison means anything")
    if available_before < min_seconds:
        return None, (f"less than {EXPERIMENT_MIN_ELAPSED_DAYS} day of stored history exists before this "
                     f"change (history is kept for {max_history_days} days)")
    duration = min(elapsed, available_before, window_days * 86400)
    return {"before_start": change_ts - duration, "before_end": change_ts,
           "after_start": change_ts, "after_end": change_ts + duration,
           "duration_days": duration / 86400}, None


def overlapping_experiments(experiments, start_ts, end_ts, exclude_id=None):
    """Other markers whose change falls inside [start_ts, end_ts] - the confound check. A change
    made DURING one of an experiment's own comparison windows means the two effects are mixed
    together in the same measurements, which no amount of statistics can separate."""
    return [e for e in experiments
           if e.get("experiment_id") != exclude_id
           and e.get("change_timestamp") is not None
           and start_ts <= e["change_timestamp"] <= end_ts]


def compute_experiment_period_trend(sessions, block, field, unit, bounds, higher_is_worse=True):
    """One session metric compared across an experiment's before/after windows. Identical in shape
    to compute_workload_period_trend, differing only in where the split comes from (a user-marked
    change instead of calendar_window_halves) - same overlapping_sessions() windowing, same
    compare_period_values() judgement, so a marker-split trend and a midpoint-split trend can never
    mean different things by the same words."""
    before = overlapping_sessions(sessions, bounds["before_start"], bounds["before_end"])
    after = overlapping_sessions(sessions, bounds["after_start"], bounds["after_end"])
    return compare_period_values(session_metric_values(before, block, field),
                                 session_metric_values(after, block, field), unit, higher_is_worse)


def compute_experiment_session_counts(sessions, bounds):
    return (len(overlapping_sessions(sessions, bounds["before_start"], bounds["before_end"])),
           len(overlapping_sessions(sessions, bounds["after_start"], bounds["after_end"])))


def compute_experiment_idle_trend(sensor_ref, bounds, sessions=None, buckets=None):
    """The machine's own RESTING temperature for one metric, before vs after the marker - the
    workload-independent signal, and the primary one for a chassis/airflow change. Mirrors
    compute_idle_metric_period_trend exactly (same filter_idle_buckets, same
    BASELINE_MIN_IDLE_BUCKETS gate rather than a looser one), but accepts already-fetched
    buckets/sessions so a report covering several metrics pays for ONE telemetry read, not one per
    metric."""
    if buckets is None:
        query_key = sensor_ref["key"] if sensor_ref["kind"] == "sensor" else None
        buckets = read_telemetry_file(since_ts=bounds["before_start"], sensor_key=query_key)
    if sessions is None:
        sessions = read_sessions_file()
    idle = filter_idle_buckets(buckets, overlapping_sessions(sessions, bounds["before_start"],
                                                             bounds["after_end"]))
    before = [b for b in idle if bounds["before_start"] <= b["start_timestamp"] < bounds["before_end"]]
    after = [b for b in idle if bounds["after_start"] <= b["start_timestamp"] < bounds["after_end"]]

    def values(bs):
        return [m["avg"] if (m := extract_bucket_metric(b, sensor_ref)) else None for b in bs]

    return compare_period_values(values(before), values(after), sensor_ref["unit"],
                                 higher_is_worse=True, min_samples=BASELINE_MIN_IDLE_BUCKETS)


def compute_experiment_health_trend(sessions, bounds):
    """Health-score (Phase 9) before vs after the marker, machine-wide. Every session is scored
    against ITS OWN workload's peers across BOTH windows via _session_health_scores_by_id - never
    two separately-normalized halves - so a shift in these numbers reflects real behavioral change
    rather than a baseline-selection artifact of the split itself."""
    combined = overlapping_sessions(sessions, bounds["before_start"], bounds["after_end"])
    if not combined:
        return None
    score_by_id = _session_health_scores_by_id(combined)
    before_ids = {s.get("session_id") for s in overlapping_sessions(combined, bounds["before_start"],
                                                                    bounds["before_end"])}
    after_ids = {s.get("session_id") for s in overlapping_sessions(combined, bounds["after_start"],
                                                                   bounds["after_end"])}
    before = [score_by_id[i] for i in before_ids if i in score_by_id]
    after = [score_by_id[i] for i in after_ids if i in score_by_id]
    return compare_period_values(before, after, "pts", higher_is_worse=False)


def _experiment_verdict(spec, workload_trends, idle_trends):
    """Which single comparison answers this experiment, and how much to trust it. Preference order
    is deliberate: a WORKLOAD-MATCHED temperature trend first (the same workload on both sides
    means comparable load, the strongest evidence available), falling back to the resting/idle
    temperature only when no workload cleared TREND_MIN_SAMPLES on both sides. For a chassis/
    airflow-class change there is no workload block at all, so idle IS the primary signal by
    construction. Returns None when nothing cleared the bar - "not enough data yet", never a
    verdict assembled from whatever happened to be available."""
    candidates = [wt for wt in workload_trends if wt["temp"] is not None]
    if candidates:
        best = max(candidates, key=lambda wt: wt["n_before"] + wt["n_after"])
        primary = best["temp"]
        power = best["power"]
        # Power moving the SAME way as temperature is an alternative explanation for the shift
        # (less work done, not better cooling), so it costs a tier - it never adds one.
        corroborated = not (power is not None and power["direction"] == primary["direction"]
                            and primary["direction"] != "STABLE")
        return {"primary": primary, "source": f"{spec['temp_label']} under {best['workload']}",
               "corroborated": corroborated}
    primary = idle_trends.get(spec["idle_scalar"])
    if primary is None:
        return None
    # For a resting-temperature primary the corroborating signal is the OTHER component's resting
    # temperature: two independent sensors drifting the same way is real support for a machine-wide
    # change, and its absence (or a contrary move) is what costs a tier here.
    others = [t for key, t in idle_trends.items() if key != spec["idle_scalar"] and t is not None]
    corroborated = any(t["direction"] == primary["direction"] for t in others)
    return {"primary": primary, "source": f"Idle {scalar_sensor_ref(spec['idle_scalar'])['label']}",
           "corroborated": corroborated}


def compute_experiment_report(experiment, now=None, sessions=None, buckets=None, experiments=None):
    """One marker -> the full before/after report. Every number comes from an existing layer
    (session stats, idle baselines, health scores), compared across the marker by the shared
    compare_period_values primitive. `direction`/`confidence` are None whenever nothing cleared
    the minimum-data bar; `insufficient_reason` then says plainly why, in the same "here is the
    real state of the evidence" spirit as every other minimum-data gate in this file.

    sessions/buckets/experiments may be supplied already-fetched so a view showing SEVERAL markers
    pays for one read of each store rather than one per marker (the telemetry read in particular is
    the expensive one - the same "reuse what's already been fetched" discipline the idle baseline
    in SensorHistoryWindow follows). Passed-in buckets must cover the oldest marker's before-window;
    each report filters the span it actually needs out of them by timestamp regardless."""
    now = now if now is not None else time.time()
    spec = EXPERIMENT_COMPONENTS.get(experiment.get("component")) or EXPERIMENT_COMPONENTS["system"]
    base = {"experiment": experiment, "component_label": spec["label"], "bounds": None,
           "insufficient_reason": None, "workload_trends": [], "idle": {}, "health_score": None,
           "confounds": [], "direction": None, "confidence": None, "primary_source": None,
           "primary": None}

    bounds, reason = experiment_window_bounds(experiment["change_timestamp"], now=now)
    if bounds is None:
        base["insufficient_reason"] = reason
        return base
    base["bounds"] = bounds

    sessions = read_sessions_file() if sessions is None else sessions
    if spec["block"] is not None:
        for key, group in group_sessions_by_workload(sessions).items():
            n_before, n_after = compute_experiment_session_counts(group["sessions"], bounds)
            temp = compute_experiment_period_trend(group["sessions"], spec["block"], spec["temp_field"],
                                                   "°C", bounds)
            power = compute_experiment_period_trend(group["sessions"], spec["block"], spec["power_field"],
                                                    "W", bounds)
            before = overlapping_sessions(group["sessions"], bounds["before_start"], bounds["before_end"])
            after = overlapping_sessions(group["sessions"], bounds["after_start"], bounds["after_end"])
            def efficiency_values(group_of_sessions):
                # The shared session_thermal_efficiency() now resolves the right temperature field
                # per block itself; this passes the experiment's own spec explicitly anyway, so a
                # future component class with an unusual field still works without a second
                # implementation of °C/W to keep in step.
                return [session_thermal_efficiency(s, spec["block"], temp_field=spec["temp_field"])
                       for s in group_of_sessions]

            efficiency = compare_period_values(efficiency_values(before), efficiency_values(after),
                                               "°C/W", higher_is_worse=True)
            if temp is None and power is None and efficiency is None:
                continue  # this workload simply wasn't run enough on both sides - not a row of blanks
            base["workload_trends"].append({"workload": group["display_name"], "workload_key": key,
                                           "temp": temp, "power": power, "efficiency": efficiency,
                                           "n_before": n_before, "n_after": n_after})
        base["workload_trends"].sort(key=lambda wt: -(wt["n_before"] + wt["n_after"]))

    # ONE telemetry read covers every idle metric in the report (buckets carry all scalars).
    buckets = read_telemetry_file(since_ts=bounds["before_start"]) if buckets is None else buckets
    for scalar in EXPERIMENT_IDLE_SCALARS:
        base["idle"][scalar] = compute_experiment_idle_trend(scalar_sensor_ref(scalar), bounds,
                                                             sessions=sessions, buckets=buckets)
    base["health_score"] = compute_experiment_health_trend(sessions, bounds)
    experiments = read_experiments_file() if experiments is None else experiments
    base["confounds"] = overlapping_experiments(experiments, bounds["before_start"],
                                                bounds["after_end"], exclude_id=experiment.get("experiment_id"))

    verdict = _experiment_verdict(spec, base["workload_trends"], base["idle"])
    if verdict is None:
        base["insufficient_reason"] = (f"no workload or idle measurement has at least {TREND_MIN_SAMPLES} "
                                      "comparable samples on both sides of the change yet")
        return base
    primary = verdict["primary"]
    base["primary_source"] = verdict["source"]
    base["primary"] = primary
    base["direction"] = EXPERIMENT_DIRECTION_LABELS[primary["direction"]]
    confidence = primary["confidence"]
    if not verdict["corroborated"]:
        confidence = _downgrade_confidence(confidence)
    if base["confounds"]:
        confidence = "LOW"  # not a downgrade - two changes mixed in one window can't be attributed
    base["confidence"] = confidence
    return base


def format_experiment_timestamp(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


EXPERIMENT_CAVEAT = ("Measured before and after the marked change. Other factors (drivers, ambient "
                    "room temperature, dust, a different mix of workloads) also move these numbers - "
                    "Thermal Watch reports what changed, not what caused it.")


def format_experiment_report(report):
    """A compute_experiment_report() result -> its display lines. The caveat is appended to EVERY
    report that shows any measurement at all, including a NO MEASURED CHANGE one - the whole risk
    of this feature is a marker being read as proof of cause, and that risk doesn't go away when
    the result is favourable."""
    exp = report["experiment"]
    lines = [f"EXPERIMENT — {exp['description']}",
            f"Marked: {format_experiment_timestamp(exp['change_timestamp'])} "
            f"({EXPERIMENT_COMPONENT_LABELS.get(exp.get('component'), exp.get('component'))})"]
    bounds = report["bounds"]
    if bounds is None:
        return lines + ["", f"Not enough data yet - {report['insufficient_reason']}."]
    lines.append(f"Compared: {bounds['duration_days']:.1f} days before vs {bounds['duration_days']:.1f} days after")

    for wt in report["workload_trends"]:
        lines += ["", f"{wt['workload']}  ({wt['n_before']} sessions before, {wt['n_after']} after)"]
        if wt["temp"] is not None:
            lines.append(f"  {report['component_label']} temperature: {format_period_delta(wt['temp'], '°C')}")
        if wt["power"] is not None:
            lines.append(f"  {report['component_label']} power: {format_period_delta(wt['power'], 'W')}")
        if wt["efficiency"] is not None:
            eff = wt["efficiency"]
            lines.append(f"  Thermal efficiency: {eff['older_mean']:.3f} → {eff['recent_mean']:.3f} °C/W "
                        f"({_fmt_signed(eff['delta'], 3, '')})")

    idle_lines = [f"Idle {scalar_sensor_ref(key)['label']}: {format_period_delta(trend, '°C')}"
                 for key, trend in report["idle"].items() if trend is not None]
    if idle_lines:
        lines += [""] + idle_lines
    if report["health_score"] is not None:
        hs = report["health_score"]
        lines.append(f"Average health score: {hs['older_mean']:.0f} → {hs['recent_mean']:.0f}")

    if report["direction"] is None:
        return lines + ["", f"No result yet - {report['insufficient_reason']}.", "", EXPERIMENT_CAVEAT]

    lines += ["", f"Result: {report['direction']}  ({report['primary_source']})",
             f"Confidence: {report['confidence']}"]
    for other in report["confounds"]:
        lines.append(f"Confounded by another marked change inside this window: \"{other['description']}\" "
                    f"({format_experiment_timestamp(other['change_timestamp'])}) - confidence capped at LOW, "
                    "because two changes measured together cannot be told apart.")
    return lines + ["", EXPERIMENT_CAVEAT]


# ---------------------------------------------------------------------------
# Unified Flight Recorder Timeline - the merge layer. Every store this app keeps records a
# different KIND of thing on its own separate timeline: thermal incidents, workload sessions,
# hardware-change markers, and the point-in-time event log. Answering "what was actually going on
# at 21:14 last night" currently means opening four views and correlating timestamps by eye. This
# layer puts them on ONE ordered axis, and adds nothing to any of them - no new store, no new
# thresholds, no re-derived numbers; every entry is a faithful projection of a record that already
# exists elsewhere, carrying its own source id so the detail always traces back.
#
# The one thing this layer contributes that no other view has is UNMONITORED TIME AS A FIRST-CLASS
# ENTRY. That is the whole point of calling it a flight recorder: a period Thermal Watch wasn't
# running looks, on any chart or list built from stored records alone, exactly like a quiet period
# where nothing happened. On a timeline that inverts the meaning of silence, so a gap is emitted as
# its own event, with its own duration, saying plainly that nothing was recorded and that no
# inference about that window is available. The gap threshold is the EXISTING TELEMETRY_GAP_BUCKETS
# (the same run-of-missing-minutes the historical chart already refuses to draw a line across), not
# a new number invented here.
#
# Scope decision (documented, not silent): this pass is the merged event axis, not a new waveform
# view. Plotting temperature under the timeline would mean a second chart implementation alongside
# TelemetryChart, and SensorHistoryWindow already owns per-sensor drill-down with incident/session
# overlays over the same buckets. The timeline reports COVERAGE (via the existing compute_coverage)
# so the honesty question - "how much of this window was actually watched" - is answered numerically
# here, and the waveform stays where it already works.
#
# INFO-level log entries are excluded by default (TIMELINE_LOG_KINDS): the event log's INFO stream
# is mostly lifecycle chatter ("Polling interval set to 2000ms") that would bury the real content
# of a 30-day timeline. WARN/CRIT entries - the ones that describe something that actually happened
# to the hardware - are included. Nothing is deleted; this is a display filter over an untouched
# store, and TIMELINE_LOG_KINDS is one edit away from including INFO for anyone who wants it.
# ---------------------------------------------------------------------------
TIMELINE_RANGE_SECONDS = {"6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
TIMELINE_MIN_GAP_SECONDS = TELEMETRY_GAP_BUCKETS * TELEMETRY_BUCKET_SECONDS  # reused, not a new number
TIMELINE_LOG_KINDS = ("WARN", "CRIT", "NETWORK")
# v1.1 Phase 4 - reused as the sentinel for "no observation yet" in App._detect_network_flight_events,
# distinct from a real observed absence (None = genuinely no active adapter right now).
_NET_STATE_UNSET = object()

# A wall-clock jump this large between two consecutive live polls is unmonitored time, not
# scheduling jitter: the process was suspended (S3 sleep/hibernate) or stalled so severely it is
# indistinguishable from a suspend. Until this existed, EVERY gap path in the app was reachable
# only through RESTART reconciliation (_reconcile_restored_incidents/_reconcile_restored_sessions),
# which a suspend never triggers because the process never dies - so a sleep left incidents marked
# duration_exact=True and let one 60s telemetry bucket stretch across the whole outage.
#
# Derived from TIMELINE_MIN_GAP_SECONDS rather than picked: that is the SAME threshold
# timeline_gap_events() uses to decide a stretch of missing telemetry counts as unmonitored. Using
# one number for both means the live engines can never record a discontinuity the timeline then
# declines to draw, which is exactly the invariant summarize_timeline() promises ("the percentage
# and the gap entries can never tell different stories"). At 180s it is 90x the 2s POLL_SECONDS
# cadence, so ordinary jitter cannot reach it.
MONITORING_DISCONTINUITY_S = TIMELINE_MIN_GAP_SECONDS
# Deterministic tie-break for entries sharing a timestamp, so the same stores always render in the
# same order: the wider context first (a gap or session frames whatever happened inside it), then
# the specific thing that happened.
TIMELINE_KIND_ORDER = {"gap": 0, "experiment": 1, "session": 2, "incident": 3, "log": 4}
TIMELINE_KIND_LABELS = {"gap": "NOT MONITORED", "experiment": "HARDWARE CHANGE", "session": "WORKLOAD",
                       "incident": "INCIDENT", "log": "EVENT"}


def read_event_log_file():
    """All persisted event-log entries, oldest first - the read-only counterpart to App.load_events
    (which also PRUNES and rewrites the file as a startup side effect). This one only ever reads,
    so a view can consult the log without mutating it, exactly like read_incidents_file()/
    read_sessions_file() do for their own stores."""
    if not EVENT_LOG_PATH.exists():
        return []
    out = []
    try:
        for line in EVENT_LOG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue  # valid JSON but not a record (e.g. a garbled write leaving `null`/`[]`)
            if rec.get("ts") is not None:
                out.append(rec)
    except OSError:
        return []
    return out


def fmt_timeline_span(seconds):
    """Duration formatting for timeline-scale spans. The shared fmt_dur() tops out at minutes,
    which is right for an incident (they last minutes) but unreadable for the spans this view
    produces - a 12-hour unmonitored stretch renders as '720m 00s' there, and a 20-day one as
    '28800m 00s'. Anything under an hour is DELEGATED to fmt_dur() unchanged, so short durations
    stay byte-identical to every other view in this app; only the longer spans no existing view can
    even produce get the extra units."""
    seconds = max(0, int(seconds))
    if seconds >= 86400:
        days, rest = divmod(seconds, 86400)
        return f"{days}d {rest // 3600:02d}h"
    if seconds >= 3600:
        hours, rest = divmod(seconds, 3600)
        return f"{hours}h {rest // 60:02d}m"
    return fmt_dur(seconds)


def _timeline_event(timestamp, kind, title, detail, end_timestamp=None, severity=None, source_id=None):
    """One entry in the merged axis. `detail` is a list of already-formatted lines rather than a
    raw record, so the whole timeline is plain testable data and the window never has to know how
    to render four different record shapes."""
    return {"timestamp": timestamp, "end_timestamp": end_timestamp, "kind": kind, "severity": severity,
           "title": title, "detail": list(detail), "source_id": source_id}


def timeline_incident_events(incidents, start_ts, end_ts):
    """Thermal incidents overlapping the window, projected onto the timeline. Uses
    overlapping_incidents() rather than a hand-rolled filter, so an incident still open (no
    end_timestamp) is treated as ongoing through now exactly as every other view treats it."""
    out = []
    for inc in overlapping_incidents(incidents, start_ts, end_ts):
        label = COMPONENT_LABELS.get(inc.get("component"), str(inc.get("component", "?")).upper())
        zone = inc.get("max_zone") or "N/A"
        detail = [f"Peak: {inc['peak_value']:.0f}°C" if inc.get("peak_value") is not None else "Peak: N/A",
                 f"Duration: {fmt_dur(inc['duration_seconds'])}" if inc.get("duration_seconds") is not None
                 else "Duration: N/A",
                 f"Dominant workload: {inc.get('dominant_workload') or 'Not identified'}"]
        if inc.get("duration_exact") is False:
            detail.append("Duration contains an unmonitored interval and is not exact.")
        out.append(_timeline_event(inc["start_timestamp"], "incident", f"{label} — {zone}", detail,
                                   end_timestamp=inc.get("end_timestamp"), severity=zone,
                                   source_id=inc.get("incident_id")))
    return out


def timeline_session_events(sessions, start_ts, end_ts):
    """Completed workload sessions overlapping the window. Reports only what the session record
    already stores - never recomputes a session's stats from telemetry, so the timeline and
    SessionsWindow can never disagree about the same session."""
    out = []
    for s in overlapping_sessions(sessions, start_ts, end_ts):
        cpu, gpu = s.get("cpu") or {}, s.get("gpu") or {}
        detail = [f"Duration: {fmt_dur(s['duration_seconds'])}" if s.get("duration_seconds") is not None
                 else "Duration: N/A"]
        if cpu.get("peak_temp") is not None:
            detail.append(f"CPU peak: {cpu['peak_temp']:.0f}°C")
        if gpu.get("peak_hotspot_temp") is not None:
            detail.append(f"GPU Hotspot peak: {gpu['peak_hotspot_temp']:.0f}°C")
        detail.append(f"Incidents during this session: {s.get('incident_count', 0)}")
        out.append(_timeline_event(s["start_timestamp"], "session",
                                   f"{s.get('workload') or NOT_IDENTIFIED_DISPLAY} — workload session",
                                   detail, end_timestamp=s.get("end_timestamp"),
                                   source_id=s.get("session_id")))
    return out


def timeline_experiment_events(experiments, start_ts, end_ts):
    """Hardware-change markers whose change falls inside the window - a point in time, never a
    span: the marker records WHEN something was changed, and the before/after evidence for it lives
    in ExperimentsWindow rather than being re-derived here."""
    return [_timeline_event(e["change_timestamp"], "experiment", f"Hardware change — {e['description']}",
                           [f"Affects: {EXPERIMENT_COMPONENT_LABELS.get(e.get('component'), e.get('component'))}",
                            "Before/after evidence for this change is in the Experiments view."],
                           source_id=e.get("experiment_id"))
           for e in experiments
           if e.get("change_timestamp") is not None and start_ts <= e["change_timestamp"] <= end_ts]


def _gap_spans(buckets, start_ts, end_ts):
    """Raw (from_ts, to_ts) pairs for unmonitored stretches inside [start_ts, end_ts], derived
    purely from telemetry bucket boundaries that already exist - every stretch between buckets
    longer than TIMELINE_MIN_GAP_SECONDS, plus any stretch before the first bucket or after the
    last. Factored out of timeline_gap_events() (Phase 14 - Evidence IDs) so both the
    window-scoped display view and the day-scoped evidence-id identity function
    (coverage_gap_events_for_day) walk buckets with exactly the same rule and can never quietly
    disagree about where a gap starts or ends."""
    spans = sorted((b["start_timestamp"], b.get("end_timestamp") or b["start_timestamp"] + TELEMETRY_BUCKET_SECONDS)
                  for b in buckets if b.get("start_timestamp") is not None)
    out, cursor = [], start_ts
    for b_start, b_end in spans:
        if b_start - cursor >= TIMELINE_MIN_GAP_SECONDS:
            out.append((cursor, b_start))
        cursor = max(cursor, b_end)
    if end_ts - cursor >= TIMELINE_MIN_GAP_SECONDS:
        out.append((cursor, end_ts))
    return out


def coverage_gap_events_for_day(day, buckets=None):
    """Every monitoring-gap event whose start falls on local calendar date `day`, each carrying a
    stable `source_id`/`evidence_id` (COV-YYYYMMDD-NNNN, 1-based, chronological by gap start
    timestamp) - a PURE function of already-recorded, immutable telemetry bucket boundaries
    (Phase 14 - Evidence IDs). No new storage: the id is recomputed fresh from persisted telemetry
    every call (or from a caller-supplied `buckets` list, e.g. a test fixture) and is always
    scoped to that day's own [local midnight, next local midnight) bounds, regardless of whatever
    window a caller happens to be displaying - so the same underlying gap gets the same id whether
    it is looked up via 'today', 'this week', or a direct per-day query."""
    day_start = local_midnight_ts(day)
    day_end = local_midnight_ts(day + timedelta(days=1))
    if buckets is None:
        buckets = read_telemetry_file(since_ts=day_start)
    buckets = [b for b in buckets
              if b.get("start_timestamp") is not None and day_start <= b["start_timestamp"] < day_end]
    day_str = day.strftime("%Y%m%d")
    out = []
    for idx, (from_ts, to_ts) in enumerate(_gap_spans(buckets, day_start, day_end), start=1):
        out.append(_timeline_event(
            from_ts, "gap", "Monitoring gap — nothing was recorded",
            [f"Duration: {fmt_timeline_span(to_ts - from_ts)}",
             "No telemetry exists for this period. Thermal Watch makes no claim about what the "
             "hardware was doing here."],
            end_timestamp=to_ts, severity="GAP", source_id=f"COV-{day_str}-{idx:04d}"))
    return out


def timeline_gap_events(buckets, start_ts, end_ts):
    """Runs of UNMONITORED time inside the window - the entries that only exist because this is a
    timeline. Derived by walking the telemetry buckets that DO exist and emitting an event for
    every stretch between them longer than TIMELINE_MIN_GAP_SECONDS (plus any stretch before the
    first bucket or after the last). A leading gap covering most of the window is the correct,
    honest answer on a machine that simply wasn't running Thermal Watch then - never suppressed for
    looking dramatic.

    Each gap's `source_id` (Phase 14 - Evidence IDs) is looked up from
    coverage_gap_events_for_day() - the canonical, day-scoped, pure identity function - rather
    than invented here, so a gap shown in this window-scoped view always carries the exact same
    evidence_id it would get from a direct day query. A window-scoped gap is matched to its
    day-scoped canonical span by containment (the day-scoped span, computed over the FULL day
    regardless of this window, can only be equal to or wider than the window-clipped one)."""
    day_cache = {}
    out = []
    for from_ts, to_ts in _gap_spans(buckets, start_ts, end_ts):
        day = datetime.fromtimestamp(from_ts).date()
        if day not in day_cache:
            day_cache[day] = coverage_gap_events_for_day(day)
        source_id = None
        for g in day_cache[day]:
            if g["timestamp"] <= from_ts and (g["end_timestamp"] or to_ts) >= to_ts:
                source_id = g["source_id"]
                break
        out.append(_timeline_event(from_ts, "gap", "Monitoring gap — nothing was recorded",
                                   [f"Duration: {fmt_timeline_span(to_ts - from_ts)}",
                                    "No telemetry exists for this period. Thermal Watch makes no claim "
                                    "about what the hardware was doing here."],
                                   end_timestamp=to_ts, severity="GAP", source_id=source_id))
    return out


def timeline_log_events(records, start_ts, end_ts, kinds=TIMELINE_LOG_KINDS):
    """WARN/CRIT event-log entries inside the window (see TIMELINE_LOG_KINDS above for why INFO is
    filtered out by default). A display filter only - the log file itself is never touched."""
    return [_timeline_event(r["ts"], "log", r.get("text", ""), [], severity=r.get("kind"))
           for r in records
           if start_ts <= r["ts"] <= end_ts and r.get("kind") in kinds]


def build_timeline(start_ts, end_ts, incidents=None, sessions=None, experiments=None, buckets=None,
                   log_records=None, kinds=None):
    """Every store merged onto one axis for [start_ts, end_ts], NEWEST FIRST (the same order every
    other list view in this app uses). Entries sharing a timestamp are ordered by
    TIMELINE_KIND_ORDER then title, so the same data always renders in the same order rather than
    depending on which store happened to be read first. Each store may be passed already-fetched
    (one read serves both the timeline and its summary); `kinds` optionally restricts which kinds
    are built at all - a filter applied BEFORE the work, not after."""
    incidents = read_incidents_file() if incidents is None else incidents
    sessions = read_sessions_file() if sessions is None else sessions
    experiments = read_experiments_file() if experiments is None else experiments
    buckets = read_telemetry_file(since_ts=start_ts) if buckets is None else buckets
    log_records = read_event_log_file() if log_records is None else log_records

    builders = {"incident": lambda: timeline_incident_events(incidents, start_ts, end_ts),
               "session": lambda: timeline_session_events(sessions, start_ts, end_ts),
               "experiment": lambda: timeline_experiment_events(experiments, start_ts, end_ts),
               "gap": lambda: timeline_gap_events(buckets, start_ts, end_ts),
               "log": lambda: timeline_log_events(log_records, start_ts, end_ts)}
    events = []
    for kind, build in builders.items():
        if kinds is None or kind in kinds:
            events += build()
    events.sort(key=lambda e: (-e["timestamp"], TIMELINE_KIND_ORDER.get(e["kind"], 99), e["title"]))
    return events


def summarize_timeline(events, buckets, start_ts, end_ts):
    """The timeline's header: how much of this window was actually watched, and what was found.
    Coverage comes from the EXISTING compute_coverage() over the same buckets the gap events were
    derived from, so the percentage and the gap entries can never tell different stories."""
    counts = {}
    for e in events:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    window_buckets = [b for b in buckets if b.get("start_timestamp") is not None
                     and start_ts <= b["start_timestamp"] <= end_ts]
    _, _, coverage_pct = compute_coverage(window_buckets, end_ts - start_ts)
    gap_seconds = sum((e["end_timestamp"] or e["timestamp"]) - e["timestamp"]
                     for e in events if e["kind"] == "gap")
    return {"counts": counts, "coverage_pct": coverage_pct, "gap_seconds": gap_seconds,
           "window_seconds": end_ts - start_ts, "total": len(events)}


def format_timeline_summary(summary):
    parts = [f"Monitoring coverage: {summary['coverage_pct']:.0f}% of the window"]
    if summary["gap_seconds"] > 0:
        parts.append(f"unmonitored: {fmt_timeline_span(summary['gap_seconds'])}")
    for kind in ("incident", "session", "experiment", "gap", "log"):
        if summary["counts"].get(kind):
            parts.append(f"{TIMELINE_KIND_LABELS[kind].lower()}: {summary['counts'][kind]}")
    return "   ".join(parts)


def format_timeline_event(event):
    """One merged entry -> its detail lines. Never invents a field: an entry whose source record
    simply didn't capture something shows nothing for it rather than a placeholder value."""
    when = datetime.fromtimestamp(event["timestamp"]).strftime("%b %d, %Y %I:%M:%S %p")
    lines = [f"{TIMELINE_KIND_LABELS.get(event['kind'], event['kind'].upper())} — {event['title']}", "",
            f"Start: {when}"]
    if event["end_timestamp"] is not None:
        lines.append(f"End: {datetime.fromtimestamp(event['end_timestamp']).strftime('%b %d, %Y %I:%M:%S %p')}")
    if event["severity"] is not None:
        lines.append(f"Severity: {event['severity']}")
    return lines + ([""] + event["detail"] if event["detail"] else [])


# ---------------------------------------------------------------------------
# Scheduled Health Reports - a deterministic REPORTING layer over evidence every earlier phase
# already produces. It collects no new sensor data, defines no new health rule, invents no new
# score, and writes nothing to hardware; every number in a report is either read from a store or
# produced by calling an EXISTING analysis helper. That constraint is what makes a report
# trustworthy: if Trend Intelligence says STABLE, the report says STABLE, because the report asked
# Trend Intelligence rather than recomputing anything of its own.
#
# CALENDAR CORRECTNESS is the part that needs real care. A "day" is not 86400 seconds - on a DST
# transition it is 23 or 25 hours - so every boundary here is built from local CALENDAR dates
# (datetime -> time.mktime, which resolves local time including DST) and a report's IDENTITY is its
# local start/end DATE, never an epoch offset. That means a DST change can neither duplicate a
# logical report nor silently skip one.
#
# Only COMPLETED periods are ever generated on a schedule: today, this week and this month are
# still accumulating, and a report that described them would be answering "how has this PC been
# doing" with a partial sample while looking exactly like a full one.
#
#   DAILY   - the previous completed calendar day
#   WEEKLY  - the previous completed Monday 00:00 -> next Monday 00:00 week (ISO convention:
#             Monday starts the week; documented here because "last week" is otherwise ambiguous)
#   MONTHLY - the previous completed calendar month (correct across February, leap years and the
#             year boundary because month arithmetic is done on dates, never by adding 30 days)
#
# COVERAGE is mandatory and load-bearing rather than decorative. compute_coverage() - the existing
# one, not a competing calculation - decides how much of the period was actually observed, and
# below REPORT_MIN_COVERAGE_PCT the report's overall status becomes INSUFFICIENT COVERAGE outright.
# Individual observed facts still appear in that case (a maximum that really was recorded is a real
# measurement), but they are labelled as observed during available telemetry rather than presented
# as representative of the period. A 2-hour sample must never read like a 24-hour one.
# ---------------------------------------------------------------------------
REPORT_TYPES = ("DAILY", "WEEKLY", "MONTHLY")
REPORT_SCHEMA_VERSION = "1.0"
REPORTS_DB_PATH = data_path("thermal_watch_reports.db")
# Not a new threshold: the SAME bar Trend Intelligence already uses to decide whether a period was
# watched enough to draw a conclusion from. Using one number for "is this period trustworthy"
# keeps a report and a trend from ever disagreeing about the same window.
REPORT_MIN_COVERAGE_PCT = TREND_MIN_COVERAGE_PCT
REPORT_TOP_WORKLOADS = 5
# How often a RUNNING app re-checks whether a completed period is missing its report. Calendar
# boundaries move slowly; this is deliberately far away from the 2s telemetry poll, and the check
# itself is a cheap key lookup that only reaches real analytics when a report is genuinely absent.
REPORT_DUE_CHECK_INTERVAL_MS = 15 * 60 * 1000
REPORT_STARTUP_DELAY_MS = 12000  # let the bridge/restore settle before any catch-up generation


def local_midnight_ts(d):
    """Epoch seconds for 00:00 local time on calendar date `d`. time.mktime interprets a naive
    local timetuple WITH the platform's DST rules (tm_isdst=-1), so this is correct on both DST
    transition days - which is exactly why boundaries are computed this way rather than by
    multiplying days by 86400."""
    return time.mktime(datetime(d.year, d.month, d.day).timetuple())


def local_day_str(ts):
    """'YYYYMMDD' for the local calendar date containing epoch seconds `ts` - same local-date
    discipline as local_midnight_ts()/period_bounds() (Phase 14 - Evidence IDs), deliberately NOT
    `int(ts) // 86400` (that UTC-epoch-day bug class was already found and fixed once, in Cooling/
    Fan Intelligence's _distinct_days(), and broke at UTC midnight = 5pm Pacific)."""
    return datetime.fromtimestamp(ts).strftime("%Y%m%d")


def add_month(d, months=1):
    """Calendar month arithmetic on a date, clamped to the 1st - the only shape this file needs.
    Correct across the year boundary by construction, and month LENGTH never enters into it, so
    February and leap years need no special case."""
    total = (d.year * 12 + (d.month - 1)) + months
    return date(total // 12, total % 12 + 1, 1)


def period_bounds(report_type, start_date):
    """The half-open [start, end) bounds of ONE report period beginning on `start_date`, as both
    local dates and epoch seconds. end_date is EXCLUSIVE (the first day NOT in the period), so a
    period never overlaps its neighbour by a day and a report's identity is unambiguous."""
    if report_type == "DAILY":
        end_date = start_date + timedelta(days=1)
    elif report_type == "WEEKLY":
        end_date = start_date + timedelta(days=7)
    elif report_type == "MONTHLY":
        end_date = add_month(start_date, 1)
    else:
        raise ValueError(f"unknown report type: {report_type!r}")
    return {"report_type": report_type, "start_date": start_date, "end_date": end_date,
           "start_ts": local_midnight_ts(start_date), "end_ts": local_midnight_ts(end_date),
           "label": period_label(report_type, start_date, end_date),
           "report_id": f"{report_type}:{start_date.isoformat()}"}


def period_label(report_type, start_date, end_date):
    """Human label for a period. The WEEKLY/MONTHLY forms show the INCLUSIVE last day, because
    'Aug 10-17' for a week that stops at Aug 16 would read as eight days."""
    last_day = end_date - timedelta(days=1)
    if report_type == "DAILY":
        return start_date.strftime("%b %d, %Y")
    if report_type == "MONTHLY":
        return start_date.strftime("%B %Y")
    if start_date.year != last_day.year:
        return f"{start_date.strftime('%b %d, %Y')} – {last_day.strftime('%b %d, %Y')}"
    if start_date.month != last_day.month:
        return f"{start_date.strftime('%b %d')} – {last_day.strftime('%b %d, %Y')}"
    return f"{start_date.strftime('%b %d')}–{last_day.strftime('%d, %Y')}"


def previous_completed_period(report_type, now=None):
    """The most recent period of this type that has FULLY ELAPSED. Never the one in progress: today,
    this week and this month are still accumulating evidence, and reporting them on a schedule would
    present a partial sample in the same shape as a complete one."""
    now = now if now is not None else time.time()
    today = datetime.fromtimestamp(now).date()
    if report_type == "DAILY":
        return period_bounds("DAILY", today - timedelta(days=1))
    if report_type == "WEEKLY":
        this_week_monday = today - timedelta(days=today.weekday())  # weekday(): Monday == 0
        return period_bounds("WEEKLY", this_week_monday - timedelta(days=7))
    if report_type == "MONTHLY":
        return period_bounds("MONTHLY", add_month(date(today.year, today.month, 1), -1))
    raise ValueError(f"unknown report type: {report_type!r}")


def period_metric_stats(buckets, sensor_ref, bounds):
    """avg/min/max/count for ONE scalar across a whole report period. Implemented by calling
    downsample_series() with a group large enough that the entire period collapses into a single
    group - deliberately, rather than writing a fresh aggregation loop: that function already
    defines this project's count-weighted average and true-min/max-across-the-group semantics, and
    a second implementation of the same arithmetic is exactly the kind of drift this phase is
    forbidden from introducing. None when the sensor was never observed in the period; a missing
    reading stays missing and never becomes 0."""
    points = normalize_bucket_series(buckets, sensor_ref)
    if not points:
        return None
    span = max(1, int((bounds["end_ts"] - bounds["start_ts"]) / TELEMETRY_BUCKET_SECONDS) + 2)
    groups = downsample_series(points, span, bounds["start_ts"])
    metrics = [g["metric"] for g in groups if g["metric"]]
    if not metrics:
        return None
    total = sum(m["count"] for m in metrics)
    return {"avg": sum(m["avg"] * m["count"] for m in metrics) / total if total else None,
           "min": min(m["min"] for m in metrics), "max": max(m["max"] for m in metrics), "count": total}


def _report_metric_block(buckets, bounds, keys):
    """{scalar_key: period_metric_stats(...)} for a set of scalars, keeping only the ones actually
    observed. A scalar absent from the result means "not recorded", which the formatter renders as
    nothing at all rather than as a zero."""
    out = {}
    for key in keys:
        stats = period_metric_stats(buckets, scalar_sensor_ref(key), bounds)
        if stats is not None:
            out[key] = stats
    return out


def _incident_rollup(incidents, components):
    """Counts and worst severity for one component family inside an already-windowed incident list.
    Reuses ZONE_SEVERITY for "which zone is worse" rather than ordering severity strings ad hoc."""
    subset = [i for i in incidents if i.get("component") in components]
    worst = None
    for inc in subset:
        zone = inc.get("max_zone")
        if zone and (worst is None or ZONE_SEVERITY.get(zone, 0) > ZONE_SEVERITY.get(worst, 0)):
            worst = zone
    return {"count": len(subset), "max_severity": worst,
           "with_monitoring_gaps": sum(1 for i in subset if i.get("monitoring_gaps"))}


def build_report_payload(bounds, now=None):
    """Assemble ONE period's structured report. Every figure is either read straight from a store or
    produced by an existing helper called with this period's own window - no analysis is reimplemented
    here, and nothing is inferred beyond what those helpers already report. The result is a plain
    dict: it is what gets persisted, and the renderers below are pure functions of it, so a stored
    report can be re-rendered later without recomputing anything."""
    now = now if now is not None else time.time()
    start_ts, end_ts = bounds["start_ts"], bounds["end_ts"]
    period_days = max(1.0, (end_ts - start_ts) / 86400.0)

    buckets = [b for b in read_telemetry_file(since_ts=start_ts)
              if b.get("start_timestamp") is not None and start_ts <= b["start_timestamp"] < end_ts]
    incidents = overlapping_incidents(read_incidents_file(), start_ts, end_ts)
    sessions = overlapping_sessions(read_sessions_file(), start_ts, end_ts)
    valid_buckets, expected_buckets, coverage_pct = compute_coverage(buckets, end_ts - start_ts)
    sufficient = coverage_pct >= REPORT_MIN_COVERAGE_PCT

    # --- overview: counts and existing scores only; no new 0-100 system score is invented here.
    scores_by_id = _session_health_scores_by_id(sessions) if sessions else {}
    scores = [s for s in scores_by_id.values() if s is not None]
    avg_health = sum(scores) / len(scores) if scores else None
    critical = [i for i in incidents if i.get("max_zone") == "RED"]
    uncertain_sessions = [s for s in sessions if s.get("duration_exact") is False]
    overview = {
        "coverage_pct": coverage_pct, "telemetry_buckets": valid_buckets,
        "expected_buckets": expected_buckets, "sessions": len(sessions),
        "uncertain_sessions": len(uncertain_sessions),
        "incidents": len(incidents), "critical_incidents": len(critical),
        "incidents_with_monitoring_gaps": sum(1 for i in incidents if i.get("monitoring_gaps")),
        "avg_health_score": avg_health,
        "worst_health_score": min(scores) if scores else None,
        "health_label": health_score_label(avg_health) if avg_health is not None else None,
    }
    # Status is derived transparently from evidence already on the page: too little coverage to
    # judge, or the existing health-score band over the existing per-session scores. Nothing else.
    if not sufficient:
        overview["status"] = "INSUFFICIENT COVERAGE"
    elif avg_health is not None:
        overview["status"] = overview["health_label"]
    else:
        overview["status"] = "NO WORKLOAD SESSIONS RECORDED"

    groups = group_sessions_by_workload(sessions)

    def top_workload_sessions():
        ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]["sessions"]))
        return ranked[0][1] if ranked else None

    top = top_workload_sessions()

    # --- CPU / GPU: observed statistics from telemetry, incident rollups from the incident store,
    # and TRENDS delegated wholesale to Trend Intelligence with this period as its own window.
    cpu = {"metrics": _report_metric_block(buckets, bounds, ("cpu_temp", "cpu_power", "cpu_util")),
          "incidents": _incident_rollup(incidents, ("cpu",)),
          "idle_trend": compute_idle_metric_period_trend(scalar_sensor_ref("cpu_temp"), period_days, now=end_ts),
          "efficiency_trend": (compute_thermal_efficiency_period_trend(top["sessions"], "cpu", period_days,
                                                                       now=end_ts) if top else None),
          "trend_workload": top["display_name"] if top else None}
    gpu = {"metrics": _report_metric_block(buckets, bounds, ("gpu_core_temp", "gpu_hotspot_temp",
                                                             "gpu_vram_temp", "gpu_power", "gpu_util")),
          "incidents": _incident_rollup(incidents, ("gpu_core", "gpu_hotspot", "gpu_vram")),
          "idle_trend": compute_idle_metric_period_trend(scalar_sensor_ref("gpu_hotspot_temp"), period_days,
                                                          now=end_ts),
          "efficiency_trend": (compute_thermal_efficiency_period_trend(top["sessions"], "gpu", period_days,
                                                                       now=end_ts) if top else None),
          "hotspot_core_delta_trend": (compute_hotspot_core_delta_period_trend(top["sessions"], period_days,
                                                                               now=end_ts) if top else None),
          "cooling_trend": None, "trend_workload": top["display_name"] if top else None}
    if top is not None:
        key = next((k for k, g in groups.items() if g is top), None)
        if key is not None:
            gpu["cooling_trend"] = compute_workload_cooling_trend_report(key, total_days=period_days, now=end_ts)

    # --- per-sensor families. `unverified` rides along untouched: an unverified sensor contributes
    # an OBSERVED RANGE and never a health conclusion or a threshold comparison (the PCIe x1 rule).
    summaries = read_sensor_summaries(start_ts, end_ts)
    storage = [s for s in summaries if s.get("component") == "drive"]
    memory = [s for s in summaries if s.get("component") == "ram"]
    motherboard = [s for s in summaries if s.get("component") not in ("drive", "ram")]
    sensors = {
        "storage": {"sensors": storage, "incidents": _incident_rollup(incidents, ("drive",))},
        "memory": {"sensors": memory, "incidents": _incident_rollup(incidents, ("ram",)),
                  "usage": _report_metric_block(buckets, bounds, ("mem_pct",))},
        "motherboard": {"sensors": motherboard,
                       "unverified_count": sum(1 for s in motherboard if s.get("unverified"))},
    }

    # --- workloads: ranked from the session records themselves, using each workload's own already
    # computed per-session health scores. "Associated incidents", never "caused".
    workloads = []
    for key, group in groups.items():
        group_sessions = group["sessions"]
        group_scores = [scores_by_id.get(s.get("session_id")) for s in group_sessions]
        group_scores = [s for s in group_scores if s is not None]
        peak_cpu = [(s.get("cpu") or {}).get("peak_temp") for s in group_sessions]
        peak_gpu = [(s.get("gpu") or {}).get("peak_hotspot_temp") for s in group_sessions]
        peak_cpu = [v for v in peak_cpu if v is not None]
        peak_gpu = [v for v in peak_gpu if v is not None]
        workloads.append({
            "workload": group["display_name"], "workload_key": key, "sessions": len(group_sessions),
            "total_seconds": sum(s.get("duration_seconds") or 0 for s in group_sessions),
            "uncertain_sessions": sum(1 for s in group_sessions if s.get("duration_exact") is False),
            "avg_health_score": (sum(group_scores) / len(group_scores)) if group_scores else None,
            "peak_cpu_temp": max(peak_cpu) if peak_cpu else None,
            "peak_gpu_hotspot": max(peak_gpu) if peak_gpu else None,
            "associated_incidents": sum(s.get("incident_count", 0) for s in group_sessions),
            # int, or None when there are too few sessions for any leave-one-out baseline to be
            # established - None means "couldn't tell", never "none were unusual".
            "anomalous_sessions": count_anomalous_sessions(group_sessions),
        })
    workloads.sort(key=lambda w: (-w["sessions"], -(w["total_seconds"] or 0)))
    workloads = workloads[:REPORT_TOP_WORKLOADS]

    # --- experiments whose comparison window overlaps this period, reusing Phase 13's own result.
    experiments = []
    for exp in read_experiments_file():
        report = compute_experiment_report(exp, now=end_ts)
        exp_bounds = report.get("bounds")
        if exp_bounds is None or exp_bounds["after_end"] < start_ts or exp_bounds["before_start"] > end_ts:
            continue
        experiments.append({"description": exp["description"], "change_timestamp": exp["change_timestamp"],
                           "component": exp.get("component"), "direction": report["direction"],
                           "confidence": report["confidence"], "primary_source": report["primary_source"],
                           "insufficient_reason": report["insufficient_reason"],
                           "primary": report.get("primary")})

    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_type": bounds["report_type"], "report_id": bounds["report_id"],
        "period_label": bounds["label"], "period_start_date": bounds["start_date"].isoformat(),
        "period_end_date": bounds["end_date"].isoformat(),
        "period_start_ts": start_ts, "period_end_ts": end_ts,
        "generated_timestamp": now, "sufficient_coverage": sufficient,
        "min_coverage_pct": REPORT_MIN_COVERAGE_PCT,
        "overview": overview, "cpu": cpu, "gpu": gpu, "sensors": sensors, "workloads": workloads,
        "experiments": experiments,
    }
    payload["findings"] = build_report_findings(payload, incidents, groups, scores_by_id)
    payload["recommendations"] = [{"title": r["title"], "recommendation": r.get("recommendation"),
                                  "confidence": r.get("confidence"), "urgency": r.get("urgency"),
                                  "evidence": list(r.get("evidence") or []), "caveat": r.get("caveat")}
                                 for r in compute_recommendations(now=end_ts)]
    return payload


def build_report_findings(payload, incidents, groups, scores_by_id):
    """NOTABLE FINDINGS, built deterministically from evidence already in the payload or already in
    a store - no prose generation, no new judgement. Every entry names the source it came from, so a
    reader can go and look at the underlying record. An empty result is reported as "no significant
    findings", never padded."""
    findings = []
    overview = payload["overview"]
    if not payload["sufficient_coverage"]:
        findings.append({"kind": "coverage", "source": "compute_coverage",
                        "text": f"Limited monitoring coverage ({overview['coverage_pct']:.1f}%) - conclusions "
                                f"may not represent the full period."})
    if overview["critical_incidents"]:
        findings.append({"kind": "critical", "source": "incidents",
                        "text": f"{overview['critical_incidents']} critical (RED) thermal incident(s) occurred."})
    elif overview["incidents"] == 0 and payload["sufficient_coverage"]:
        findings.append({"kind": "quiet", "source": "incidents",
                        "text": "No thermal incidents were recorded during this period."})
    if overview["incidents_with_monitoring_gaps"]:
        findings.append({"kind": "gap", "source": "incidents",
                        "text": f"{overview['incidents_with_monitoring_gaps']} incident(s) span an interval where "
                                f"monitoring was offline - their durations are not exact."})
    for wl in payload["workloads"]:
        anomalous = wl.get("anomalous_sessions")
        if anomalous:  # None ("couldn't tell") and 0 both stay silent - only a real count speaks
            findings.append({"kind": "anomaly", "source": "count_anomalous_sessions",
                            "text": f"{anomalous} of {wl['sessions']} {wl['workload']} session(s) behaved "
                                    f"unusually against that workload's own baseline."})
    for group in groups.values():
        for finding in run_session_trend_diagnostics(group["sessions"], group["display_name"]):
            findings.append({"kind": "diagnostic", "source": "run_session_trend_diagnostics",
                            "text": " ".join(format_diagnostic_finding(finding))})
    for section, label in (("cpu", "CPU"), ("gpu", "GPU Hotspot")):
        trend = payload[section].get("idle_trend")
        if trend is not None and trend["direction"] != "STABLE":
            findings.append({"kind": "trend", "source": "compute_idle_metric_period_trend",
                            "text": f"Idle {label} temperature is {trend['direction'].lower()} across this period "
                                    f"({format_period_delta(trend, '°C')}), confidence {trend['confidence']}."})
    for exp in payload["experiments"]:
        if exp["direction"] is not None:
            findings.append({"kind": "experiment", "source": "compute_experiment_report",
                            "text": f"Hardware change \"{exp['description']}\": {exp['direction']} "
                                    f"(confidence {exp['confidence']})."})
    if not findings:
        findings.append({"kind": "none", "source": None, "text": "No significant findings."})
    return findings


_REPORTS_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS reports (
        report_id TEXT PRIMARY KEY,
        report_type TEXT NOT NULL,
        period_start_date TEXT NOT NULL,
        period_end_date TEXT NOT NULL,
        period_start_ts REAL NOT NULL,
        period_end_ts REAL NOT NULL,
        generated_timestamp REAL NOT NULL,
        coverage_pct REAL,
        status TEXT,
        schema_version TEXT NOT NULL,
        payload_json TEXT NOT NULL)""",
    # The uniqueness rule the spec asks for, enforced by the STORE rather than by caller discipline:
    # one logical report per (type, period). report_id is itself derived from those, so the primary
    # key already implies this, but the explicit index states the invariant a future reader relies on.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_period "
    "ON reports (report_type, period_start_date, period_end_date)",
    "CREATE INDEX IF NOT EXISTS idx_reports_start ON reports (period_start_ts)",
]


def open_reports_db(path=None):
    """The reports store, kept in its OWN database rather than as a table in the telemetry file:
    reports are derived, human-facing artefacts with a totally different lifetime from raw
    telemetry (which is pruned at 30 days), and a corrupt telemetry file must not take the report
    history with it - nor the reverse. Same durability posture as the telemetry store (WAL, corrupt
    file renamed aside rather than deleted) and the same contract: returns None instead of raising,
    so a damaged or unwritable report store can never stop Thermal Watch from starting."""
    path = path or REPORTS_DB_PATH
    conn = None
    try:
        conn = _telemetry_db_connect(path)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        for statement in _REPORTS_SCHEMA:
            conn.execute(statement)
        return conn
    except sqlite3.DatabaseError:
        if conn is not None:
            conn.close()
        try:
            if path.exists():
                path.rename(path.with_name(f"{path.stem}.corrupt-{int(time.time())}{path.suffix}"))
            conn = _telemetry_db_connect(path)
            for statement in _REPORTS_SCHEMA:
                conn.execute(statement)
            return conn
        except (OSError, sqlite3.DatabaseError):
            return None
    except OSError:
        return None


def _report_row_to_dict(row):
    try:
        payload = json.loads(row[9])
    except (ValueError, TypeError):
        payload = None
    return {"report_id": row[0], "report_type": row[1], "period_start_date": row[2],
           "period_end_date": row[3], "period_start_ts": row[4], "period_end_ts": row[5],
           "generated_timestamp": row[6], "coverage_pct": row[7], "status": row[8],
           "payload": payload}


_REPORT_COLUMNS = ("report_id, report_type, period_start_date, period_end_date, period_start_ts, "
                  "period_end_ts, generated_timestamp, coverage_pct, status, payload_json")


def save_report(payload, replace=False):
    """Persist one report. IDEMPOTENCY POLICY, chosen and enforced here: generation is
    INSERT-OR-IGNORE keyed on the logical report id, so generating "Weekly Aug 10-16" twice leaves
    exactly one report and the FIRST one's conclusions stand - a report records what Thermal Watch
    concluded from the data available when it was generated, and merely looking at it again must
    never quietly rewrite that. Only an explicit regeneration (replace=True) overwrites the stored
    payload, keeping the same report_id and period while recording a new generated_timestamp.
    Returns True if the store now holds this report."""
    conn = open_reports_db()
    if conn is None:
        return False
    verb = "INSERT OR REPLACE" if replace else "INSERT OR IGNORE"
    values = (payload["report_id"], payload["report_type"], payload["period_start_date"],
             payload["period_end_date"], payload["period_start_ts"], payload["period_end_ts"],
             payload["generated_timestamp"], payload["overview"]["coverage_pct"],
             payload["overview"]["status"], json.dumps(payload), REPORT_SCHEMA_VERSION)
    try:
        conn.execute("BEGIN")
        conn.execute(f"{verb} INTO reports ({_REPORT_COLUMNS}, schema_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
        conn.execute("COMMIT")
        return True
    except sqlite3.DatabaseError:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass
        return False
    finally:
        conn.close()


def read_reports(report_type=None):
    """Stored reports, newest period first. A corrupt/unreadable store yields [] rather than
    raising - the Reports view shows nothing, and the app still runs."""
    conn = open_reports_db()
    if conn is None:
        return []
    try:
        sql = f"SELECT {_REPORT_COLUMNS} FROM reports"
        params = ()
        if report_type:
            sql += " WHERE report_type = ?"
            params = (report_type,)
        sql += " ORDER BY period_start_ts DESC, report_type"
        return [_report_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


def report_exists(report_id):
    conn = open_reports_db()
    if conn is None:
        return False
    try:
        return conn.execute("SELECT 1 FROM reports WHERE report_id = ?", (report_id,)).fetchone() is not None
    except sqlite3.DatabaseError:
        return False
    finally:
        conn.close()


def due_report_periods(now=None):
    """Which completed periods currently have NO report. Deliberately cheap: three calendar
    computations and up to three primary-key lookups, with no analytics touched unless something is
    actually missing - this runs on a timer in a live app, so the common answer (nothing due) must
    cost almost nothing."""
    return [bounds for bounds in (previous_completed_period(t, now) for t in REPORT_TYPES)
           if not report_exists(bounds["report_id"])]


def generate_due_reports(now=None):
    """Catch-up generation: build and store a report for every completed period missing one. This is
    what makes "the PC was off on Sunday night" work - the next time Thermal Watch runs, last week's
    report is simply generated then. Returns the report ids actually created."""
    created = []
    for bounds in due_report_periods(now):
        payload = build_report_payload(bounds, now=now)
        if save_report(payload):
            created.append(bounds["report_id"])
    return created


def regenerate_report(report_id, now=None):
    """EXPLICIT regeneration of one stored report from currently-retained source data, keeping its
    logical identity and recording a new generation timestamp. Honest about retention: telemetry,
    incidents and sessions are pruned at 30 days, so a period that has since fallen outside those
    windows can no longer be fully reconstructed. The rebuilt payload records that in
    `reconstruction` rather than silently presenting a thinner report as if it were the original."""
    now = now if now is not None else time.time()
    existing = next((r for r in read_reports() if r["report_id"] == report_id), None)
    if existing is None:
        return None
    start_date = date.fromisoformat(existing["period_start_date"])
    bounds = period_bounds(existing["report_type"], start_date)
    payload = build_report_payload(bounds, now=now)
    retention_start = now - TELEMETRY_RETENTION_DAYS * 86400
    expired = bounds["start_ts"] < retention_start
    previous_coverage = (existing.get("payload") or {}).get("overview", {}).get("coverage_pct")
    payload["reconstruction"] = {
        "regenerated": True,
        "previous_generated_timestamp": existing["generated_timestamp"],
        "previous_coverage_pct": previous_coverage,
        "source_data_expired": expired,
        "note": (f"Source telemetry for this period is older than the {TELEMETRY_RETENTION_DAYS}-day retention "
                 "window and has been pruned - this report could not be fully reconstructed."
                 if expired else None),
    }
    if not save_report(payload, replace=True):
        return None
    return payload


def _fmt_metric_line(label, stats, unit, decimals=0):
    """'Average observed temperature: 54°C' style lines, only for a metric that was actually
    observed. Returns [] for a missing metric so a caller can concatenate without ever emitting a
    zero for something that was never recorded."""
    if not stats or stats.get("avg") is None:
        return []
    return [f"{label}: {stats['avg']:.{decimals}f}{unit}",
           f"{label.replace('Average', 'Maximum').replace('average', 'maximum')}: {stats['max']:.{decimals}f}{unit}"]


def _fmt_trend(prefix, trend, unit="°C"):
    if trend is None:
        return []
    line = f"{prefix}: {trend['direction']}"
    delta = format_period_delta(trend, unit, 1)
    if delta:
        line += f" ({delta}, confidence {trend['confidence']})"
    return [line]


def format_report_text(payload):
    """The plain-text rendering - a pure function of the STORED payload, so a report re-read years
    later renders from its own recorded evidence rather than from whatever the analytics would say
    today. Readable enough to paste into a message or a support ticket."""
    o = payload["overview"]
    lines = ["THERMAL WATCH", "SYSTEM HEALTH REPORT", "",
            f"Period: {payload['period_label']}  ({payload['report_type'].title()})",
            f"Monitoring coverage: {o['coverage_pct']:.1f}%"]
    if not payload["sufficient_coverage"]:
        lines.append(f"Limited monitoring coverage - conclusions may not represent the full period. "
                    f"Figures below are observed during available telemetry, not representative of "
                    f"the whole period.")
    lines += ["", "OVERVIEW"]
    if payload["sufficient_coverage"]:
        lines.append(f"Overall health: {o['status']}")
    else:
        lines += ["SYSTEM HEALTH", "INSUFFICIENT COVERAGE"]
    if o["avg_health_score"] is not None:
        lines.append(f"Average session health score: {o['avg_health_score']:.0f}/100")
        lines.append(f"Worst session health score: {o['worst_health_score']:.0f}/100")
    lines += [f"Thermal incidents: {o['incidents']}",
             f"Critical incidents: {o['critical_incidents']}",
             f"Recorded workload sessions: {o['sessions']}",
             f"Telemetry buckets recorded: {o['telemetry_buckets']} of {o['expected_buckets']} expected"]
    if o["uncertain_sessions"]:
        lines.append(f"Sessions with uncertain duration: {o['uncertain_sessions']}")
    if o["incidents_with_monitoring_gaps"]:
        lines.append(f"Incidents spanning a monitoring gap: {o['incidents_with_monitoring_gaps']}")

    cpu, gpu = payload["cpu"], payload["gpu"]
    lines += ["", "CPU"]
    lines += _fmt_metric_line("Average observed temperature", cpu["metrics"].get("cpu_temp"), "°C")
    lines += _fmt_metric_line("Average package power", cpu["metrics"].get("cpu_power"), "W")
    lines += _fmt_metric_line("Average utilization", cpu["metrics"].get("cpu_util"), "%")
    lines.append(f"Thermal incidents: {cpu['incidents']['count']}"
                + (f" (max severity {cpu['incidents']['max_severity']})" if cpu["incidents"]["max_severity"] else ""))
    lines += _fmt_trend("Idle temperature trend", cpu["idle_trend"])
    lines += _fmt_trend(f"Thermal efficiency trend ({cpu['trend_workload']})", cpu["efficiency_trend"], "°C/W")

    lines += ["", "GPU"]
    lines += _fmt_metric_line("Average core temperature", gpu["metrics"].get("gpu_core_temp"), "°C")
    lines += _fmt_metric_line("Average hotspot temperature", gpu["metrics"].get("gpu_hotspot_temp"), "°C")
    lines += _fmt_metric_line("Average memory junction temperature", gpu["metrics"].get("gpu_vram_temp"), "°C")
    lines += _fmt_metric_line("Average power", gpu["metrics"].get("gpu_power"), "W")
    lines += _fmt_metric_line("Average utilization", gpu["metrics"].get("gpu_util"), "%")
    lines.append(f"Thermal incidents: {gpu['incidents']['count']}"
                + (f" (max severity {gpu['incidents']['max_severity']})" if gpu["incidents"]["max_severity"] else ""))
    lines += _fmt_trend("Hotspot/core delta trend", gpu["hotspot_core_delta_trend"])
    lines += _fmt_trend("Idle hotspot trend", gpu["idle_trend"])
    lines += _fmt_trend(f"Thermal efficiency trend ({gpu['trend_workload']})", gpu["efficiency_trend"], "°C/W")
    cooling = gpu.get("cooling_trend")
    if cooling and cooling.get("direction"):
        lines.append(f"Cooling trend ({cooling['workload']}): {cooling['direction']}"
                    + (f", confidence {cooling['confidence']}" if cooling.get("confidence") else ""))

    sensors = payload["sensors"]
    if sensors["storage"]["sensors"] or sensors["storage"]["incidents"]["count"]:
        lines += ["", "STORAGE"]
        for s in sensors["storage"]["sensors"]:
            lines.append(f"{s['name']}: observed {s['min']:.0f}–{s['max']:.0f}°C (avg {s['avg']:.0f}°C)")
        lines.append(f"Thermal incidents: {sensors['storage']['incidents']['count']}")
    if sensors["memory"]["sensors"] or sensors["memory"]["usage"]:
        lines += ["", "MEMORY"]
        lines += _fmt_metric_line("Average system memory usage", sensors["memory"]["usage"].get("mem_pct"), "%")
        for s in sensors["memory"]["sensors"]:
            lines.append(f"{s['name']}: observed {s['min']:.0f}–{s['max']:.0f}°C (avg {s['avg']:.0f}°C)")
        lines.append(f"Thermal incidents: {sensors['memory']['incidents']['count']}")
    if sensors["motherboard"]["sensors"]:
        lines += ["", "MOTHERBOARD"]
        for s in sensors["motherboard"]["sensors"]:
            # An unverified sensor contributes its OBSERVED RANGE and nothing else - no threshold
            # comparison, no zone, no health wording. Appearing in a report grants it no trust.
            suffix = "  (unverified sensor - observed values only, no health conclusion)" if s["unverified"] else ""
            lines.append(f"{s['name']}: observed {s['min']:.0f}–{s['max']:.0f}°C (avg {s['avg']:.0f}°C){suffix}")

    if payload["workloads"]:
        lines += ["", "WORKLOADS"]
        for wl in payload["workloads"]:
            lines.append(f"{wl['workload']}")
            lines.append(f"  Sessions: {wl['sessions']}"
                        + (f" ({wl['uncertain_sessions']} with uncertain duration)"
                           if wl["uncertain_sessions"] else ""))
            if wl["avg_health_score"] is not None:
                lines.append(f"  Average health score: {wl['avg_health_score']:.0f}")
            if wl["peak_cpu_temp"] is not None:
                lines.append(f"  Peak CPU: {wl['peak_cpu_temp']:.0f}°C")
            if wl["peak_gpu_hotspot"] is not None:
                lines.append(f"  Peak GPU Hotspot: {wl['peak_gpu_hotspot']:.0f}°C")
            lines.append(f"  Associated incidents: {wl['associated_incidents']}")

    if payload["experiments"]:
        lines += ["", "HARDWARE CHANGE"]
        for exp in payload["experiments"]:
            when = datetime.fromtimestamp(exp["change_timestamp"]).strftime("%b %d")
            lines.append(f"{exp['description']} — {when}")
            if exp["direction"] is None:
                lines.append(f"  Not enough data yet - {exp['insufficient_reason']}.")
                continue
            lines.append(f"  Result: {exp['direction']} ({exp['primary_source']})")
            lines.append(f"  Confidence: {exp['confidence']}")
            lines.append("  Evidence shows this change across the marked date. It does not prove the "
                        "hardware change was the cause.")

    lines += ["", "NOTABLE FINDINGS"]
    lines += [f"• {f['text']}" for f in payload["findings"]]

    lines += ["", "RECOMMENDATIONS"]
    for rec in payload["recommendations"]:
        lines.append(f"• {rec['title']}")
        if rec.get("recommendation"):
            lines.append(f"  {rec['recommendation']}")
        if rec.get("confidence"):
            lines.append(f"  Confidence: {rec['confidence']}   Urgency: {rec['urgency']}")
        for ev in rec.get("evidence") or []:
            lines.append(f"  - {ev}")
        if rec.get("caveat"):
            lines.append(f"  {rec['caveat']}")

    recon = payload.get("reconstruction")
    if recon and recon.get("note"):
        lines += ["", "REGENERATION", recon["note"]]
    lines += ["", f"Generated: {datetime.fromtimestamp(payload['generated_timestamp']):%Y-%m-%d %H:%M:%S}",
             f"Report schema: {payload['schema_version']}"]
    return lines


REPORT_CSV_COLUMNS = ["section", "item", "metric", "value", "unit", "note"]


def build_report_csv_rows(payload):
    """The tabular slice of a report - the parts that genuinely ARE rows. Prose sections (findings,
    recommendation caveats) go out as a `note` on their own row rather than being forced into
    columns they don't have. Values are emitted as plain numbers/strings; csv.writer handles
    quoting, commas and newlines, and the file is written UTF-8 so non-ASCII workload names
    survive."""
    rows = [["overview", "", "monitoring_coverage_pct", f"{payload['overview']['coverage_pct']:.2f}", "%", ""]]
    o = payload["overview"]
    for metric in ("telemetry_buckets", "expected_buckets", "sessions", "uncertain_sessions", "incidents",
                   "critical_incidents", "incidents_with_monitoring_gaps"):
        rows.append(["overview", "", metric, o[metric], "", ""])
    for metric in ("avg_health_score", "worst_health_score"):
        if o[metric] is not None:
            rows.append(["overview", "", metric, f"{o[metric]:.1f}", "pts", ""])
    rows.append(["overview", "", "status", o["status"], "", ""])
    for section in ("cpu", "gpu"):
        for key, stats in payload[section]["metrics"].items():
            unit = scalar_sensor_ref(key)["unit"]
            for stat in ("avg", "min", "max"):
                if stats.get(stat) is not None:
                    rows.append([section, key, stat, f"{stats[stat]:.2f}", unit, ""])
        rows.append([section, "", "incidents", payload[section]["incidents"]["count"], "", ""])
        if payload[section]["incidents"]["max_severity"]:
            rows.append([section, "", "max_severity", payload[section]["incidents"]["max_severity"], "", ""])
    for family in ("storage", "memory", "motherboard"):
        for s in payload["sensors"][family]["sensors"]:
            note = "unverified sensor - observed values only" if s["unverified"] else ""
            for stat in ("avg", "min", "max"):
                if s.get(stat) is not None:
                    rows.append([family, s["name"], stat, f"{s[stat]:.2f}", "°C", note])
    for wl in payload["workloads"]:
        for metric in ("sessions", "total_seconds", "uncertain_sessions", "associated_incidents"):
            rows.append(["workload", wl["workload"], metric, wl[metric], "", ""])
        for metric, unit in (("avg_health_score", "pts"), ("peak_cpu_temp", "°C"), ("peak_gpu_hotspot", "°C")):
            if wl[metric] is not None:
                rows.append(["workload", wl["workload"], metric, f"{wl[metric]:.1f}", unit, ""])
    for f in payload["findings"]:
        rows.append(["finding", f["kind"], "", "", "", f["text"]])
    for rec in payload["recommendations"]:
        rows.append(["recommendation", rec["title"], "confidence", rec.get("confidence") or "", "",
                    rec.get("recommendation") or ""])
    return rows


# ---------------------------------------------------------------------------
# Predictive Maintenance Outlook - the last analytical layer, and the one with the highest risk of
# becoming exactly what this project has spent every phase avoiding. The user's own framing:
# evidence-based, explicitly NOT fake "AI says 17 days left". Nothing here learns, guesses, or
# extrapolates a curve it cannot justify. A projection is arithmetic on a trend some OTHER layer
# already established, aimed at a threshold that ALREADY exists, and stated conditionally.
#
# Four rules make that concrete, and each one exists to refuse rather than to predict:
#
#   1. NO PROJECTION WITHOUT AN ESTABLISHED TREND. The rate comes from Trend Intelligence's own
#      compare_period_values result - it is never recomputed here - and only a WORSENING direction
#      at PREDICTIVE_MIN_CONFIDENCE or better is eligible. Trend Intelligence's two-factor rubric
#      (thin data caps at MEDIUM however dramatic the shift) therefore gates this layer too, for
#      free. A STABLE or IMPROVING metric produces nothing at all.
#   2. NEVER A PRECISE COUNTDOWN. The output is a RANGE derived from the trend's own dispersion
#      (the older period's sample stddev bounds an optimistic and a pessimistic rate), rounded to
#      coarse buckets - "roughly 3-6 weeks", never "17 days". A single number would imply a
#      precision the underlying two-period comparison simply does not have.
#   3. HORIZON CAPPING. A projection may not reach further than PREDICTIVE_MAX_HORIZON_MULTIPLE
#      times the window it was measured over. Two weeks of data cannot honestly say anything about
#      next year, and a rate small enough to imply that is indistinguishable from noise; those
#      cases are reported as "no meaningful horizon", not as a comfortable large number.
#   4. THRESHOLDS ARE THE EXISTING ONES. A projection aims at the next zone boundary above the
#      current value, read from the SAME CPU_ZONES/GPU_HOTSPOT_ZONES tables the alert engine uses.
#      No new limit is invented, and an UNVERIFIED sensor is never projected at all - it has no
#      trustworthy threshold to aim at (the standing PCIe x1 rule).
#
# Everything is phrased as a conditional about the OBSERVED TREND, never a prediction about the
# hardware: "if this trend continued unchanged". Thermal cooling degradation is not linear, ambient
# conditions vary, and a workload mix can change tomorrow - so the projection describes what the
# measurements have been doing, and says plainly that continuing is an assumption, not a forecast.
# ---------------------------------------------------------------------------
PREDICTIVE_MIN_CONFIDENCE = "MEDIUM"      # LOW-confidence trends never produce a projection at all
PREDICTIVE_MAX_HORIZON_MULTIPLE = 3.0     # never project beyond 3x the observation window
PREDICTIVE_MIN_RATE_PER_DAY = 0.01        # °C/day below which a "rise" is noise, not a trajectory
_CONFIDENCE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def next_zone_threshold(value, table):
    """The next zone FLOOR strictly above `value` in an existing zone table, with its key - the
    boundary a rising metric would cross next. None when the value is already in the top zone:
    there is nothing further to project toward, and inventing a limit beyond RED is exactly the
    kind of new threshold this layer is forbidden to create."""
    if value is None:
        return None
    for floor, key, *_ in sorted(table, key=lambda row: row[0]):
        if floor > value:
            return {"threshold": floor, "zone": key}
    return None


def _horizon_bucket(days):
    """Coarse, honest phrasing for a horizon. Deliberately lossy: the underlying evidence is a
    comparison of two period means, which cannot support day-level precision, so the wording never
    implies it."""
    if days < 14:
        return "within about 2 weeks"
    if days < 35:
        return "roughly 2-5 weeks"
    if days < 70:
        return "roughly 1-2 months"
    if days < 140:
        return "roughly 2-4 months"
    return "several months or more"


def project_threshold_horizon(current_value, trend, window_days, table):
    """Project ONE established worsening trend toward the next existing zone boundary.

    `trend` is a compare_period_values() result and is never recomputed here. Its `delta` is the
    shift between two adjacent half-window means, so the elapsed time between those two means is
    HALF the window - that is the denominator for the per-day rate, not the full window. The
    older period's own sample stddev bounds a faster and a slower rate, which is what turns a
    single number into an honest range. Returns None - "no projection" - whenever any rule refuses,
    which is the common and correct outcome."""
    if trend is None or trend.get("direction") != "WORSENING":
        return None
    if _CONFIDENCE_RANK.get(trend.get("confidence"), 0) < _CONFIDENCE_RANK[PREDICTIVE_MIN_CONFIDENCE]:
        return None
    target = next_zone_threshold(current_value, table)
    if target is None:
        return None

    half_window_days = max(1e-9, window_days / 2.0)
    rate = trend["delta"] / half_window_days
    if rate < PREDICTIVE_MIN_RATE_PER_DAY:
        return None
    spread = (trend.get("older_stats") or {}).get("stddev") or 0.0
    fast = (trend["delta"] + spread) / half_window_days
    slow = (trend["delta"] - spread) / half_window_days
    gap = target["threshold"] - current_value
    if gap <= 0:
        return None

    soonest = gap / fast if fast > PREDICTIVE_MIN_RATE_PER_DAY else None
    latest = gap / slow if slow > PREDICTIVE_MIN_RATE_PER_DAY else None
    central = gap / rate
    max_horizon = window_days * PREDICTIVE_MAX_HORIZON_MULTIPLE
    if central > max_horizon:
        # Beyond what this much observation can support. Reported as an explicit refusal rather
        # than as a reassuringly distant date, because the two are not the same statement.
        return {"projected": False, "reason": "beyond_horizon", "zone": target["zone"],
               "threshold": target["threshold"], "current": current_value, "rate_per_day": rate,
               "window_days": window_days, "max_horizon_days": max_horizon,
               "confidence": trend["confidence"]}
    return {"projected": True, "zone": target["zone"], "threshold": target["threshold"],
           "current": current_value, "rate_per_day": rate, "window_days": window_days,
           "days_soonest": soonest, "days_central": central, "days_latest": latest,
           "bucket": _horizon_bucket(soonest if soonest is not None else central),
           "confidence": trend["confidence"]}


PREDICTIVE_METRICS = [
    # (key, label, scalar for the current value + idle trend, zone table). Idle/resting temperature
    # is the projected signal because it is workload-independent: a rising floor is a property of
    # the machine's cooling, whereas a rising loaded temperature may only mean a heavier game.
    ("cpu_idle", "CPU Package (idle)", "cpu_temp", CPU_ZONES),
    ("gpu_hotspot_idle", "GPU Hotspot (idle)", "gpu_hotspot_temp", GPU_HOTSPOT_ZONES),
]


def compute_maintenance_outlook(window_days=TREND_MONTH_LOOKBACK_DAYS, now=None):
    """The whole outlook: one entry per projectable metric, each either a projection, an explicit
    refusal with its reason, or absent because no established worsening trend exists. Reuses
    compute_idle_metric_period_trend for the trend and the existing idle baseline for the current
    value - no new statistics, no new sensor reads."""
    now = now if now is not None else time.time()
    entries = []
    for key, label, scalar, table in PREDICTIVE_METRICS:
        ref = scalar_sensor_ref(scalar)
        trend = compute_idle_metric_period_trend(ref, window_days, now=now)
        if trend is None:
            entries.append({"key": key, "label": label, "unit": ref["unit"], "trend": None,
                           "projection": None, "reason": "insufficient_data"})
            continue
        # The recent half's own mean IS the current resting level for this metric - the same number
        # the trend already reported, never a separate live reading that could disagree with it.
        projection = project_threshold_horizon(trend["recent_mean"], trend, window_days, table)
        entries.append({"key": key, "label": label, "unit": ref["unit"], "trend": trend,
                       "projection": projection,
                       "reason": None if projection else "no_established_worsening_trend"})
    return {"window_days": window_days, "generated_timestamp": now, "entries": entries}


MAINTENANCE_CAVEAT = ("These are projections of an OBSERVED TREND, not predictions about the "
                     "hardware. They assume the trend continues unchanged, which it may not: "
                     "cooling behaviour is not linear, ambient temperature varies, and a change in "
                     "workload or a clean-out can reverse it. Thermal Watch reports what the "
                     "measurements have been doing - it does not forecast failure.")


def format_maintenance_outlook(outlook):
    """Display lines. A refusal is always rendered as a refusal WITH ITS REASON - "no projection"
    carries real information (the metric is not trending toward a limit) and is never left blank
    to make the view look emptier or more alarming than the evidence supports."""
    lines = [f"MAINTENANCE OUTLOOK — {outlook['window_days']:.0f} DAY WINDOW", ""]
    projected_any = False
    for entry in outlook["entries"]:
        lines.append(entry["label"])
        trend, proj = entry["trend"], entry["projection"]
        if trend is None:
            lines += ["  Not enough idle telemetry yet to establish a trend.", ""]
            continue
        lines.append(f"  Trend: {trend['direction']} ({format_period_delta(trend, entry['unit'], 1)}), "
                    f"confidence {trend['confidence']}")
        if proj is None:
            lines += ["  No projection: this metric is not on an established worsening trajectory.", ""]
            continue
        if not proj["projected"]:
            lines += [f"  No meaningful horizon: at {proj['rate_per_day']:.3f}{entry['unit']}/day the "
                      f"{proj['zone']} threshold ({proj['threshold']:.0f}{entry['unit']}) is further away "
                      f"than {outlook['window_days']:.0f} days of data can support.", ""]
            continue
        projected_any = True
        lines.append(f"  Currently {proj['current']:.1f}{entry['unit']}; {proj['zone']} threshold is "
                    f"{proj['threshold']:.0f}{entry['unit']}")
        lines.append(f"  IF this trend continued unchanged, that threshold would be reached "
                    f"{proj['bucket']}")
        lines.append(f"  Confidence in the underlying trend: {proj['confidence']}")
        lines.append("")
    if not projected_any:
        lines.append("Nothing is currently projected to reach a thermal threshold.")
        lines.append("")
    lines.append(MAINTENANCE_CAVEAT)
    return lines


# ---------------------------------------------------------------------------
# Ask Thermal Watch - the natural-language QUERY layer, and deliberately the last one built. The
# roadmap's own words: a layer that "queries Thermal Watch's own structured data to answer questions
# like 'why did my PC run hot last night' - never the first layer". Every phase below it exists so
# this one has real evidence to retrieve; without them it would have nothing to say and would have
# to invent something, which is exactly the failure mode this project is built against.
#
# WHAT THIS IS: a deterministic parser and dispatcher. A question resolves into (intent, time
# window, component), each routing to helpers that ALREADY exist, and the answer is assembled from
# the records those helpers return. Every answer ends by naming how many records it drew on.
#
# WHAT THIS IS NOT: a language model. No text here is generated - it is selected and formatted from
# retrieved evidence. That is a deliberate architectural decision, not a shortcut. (1) Phase 15
# forbade AI-generated conclusions in reports; a model answering "why did my PC run hot" would be
# the one component in this app capable of asserting something no record supports, in the most
# convincing register available. (2) The app is dependency-free by design; a local model means a
# runtime, a multi-gigabyte download and a hard dependency for a feature whose evidence is already
# structured. (3) Determinism is what makes answers checkable - the same question against the same
# data returns the same answer, and a verify script can assert its content. If a local model is
# ever added, its correct shape is a PHRASING layer over answer_question()'s already-retrieved
# evidence - never the source of a fact.
#
# THE NON-CAUSAL RULE APPLIES HARDEST HERE. Someone asking "why" is asking for a cause, and this
# layer cannot supply one: it reports what was running, what was measured and what coincided, in
# those terms. "Associated with", never "caused by" - the rule Cross-Sensor Diagnostics established,
# at the surface most tempted to break it. An unparseable question gets an honest "I don't know how
# to answer that" plus what it CAN answer, never a guess at what was meant.
# ---------------------------------------------------------------------------
ASK_DEFAULT_WINDOW_SECONDS = 24 * 3600
# "Last night" has no clock definition, so this file picks one and STATES it in the answer rather
# than quietly assuming: 18:00 the previous day through 06:00 today.
ASK_NIGHT_START_HOUR = 18
ASK_NIGHT_END_HOUR = 6
ASK_MAX_EVIDENCE_ROWS = 6

ASK_COMPONENT_WORDS = {
    "cpu": "cpu", "processor": "cpu", "package": "cpu",
    "gpu": "gpu", "graphics": "gpu", "video card": "gpu", "hotspot": "gpu", "vram": "gpu",
    "drive": "drive", "ssd": "drive", "nvme": "drive", "disk": "drive", "storage": "drive",
    "ram": "ram", "memory": "ram", "dimm": "ram",
}
ASK_COMPONENT_INCIDENT_KEYS = {"cpu": ("cpu",), "gpu": ("gpu_core", "gpu_hotspot", "gpu_vram"),
                              "drive": ("drive",), "ram": ("ram",)}
ASK_COMPONENT_SCALARS = {"cpu": ("cpu_temp", "cpu_power", "cpu_util"),
                        "gpu": ("gpu_hotspot_temp", "gpu_core_temp", "gpu_power", "gpu_util"),
                        "drive": (), "ram": ("mem_pct",)}

# (intent, keyword groups). A match needs ALL words of at least one group, so "why ... hot" reaches
# `explain` while a bare "hot" cannot hijack an unrelated intent. Order matters: the first match
# wins, so narrower intents are listed before broader ones.
ASK_INTENT_PATTERNS = [
    ("recommendations", [("recommend",), ("should", "i"), ("what", "do", "about")]),
    ("trend", [("trend",), ("getting", "worse"), ("getting", "better"), ("over", "time"), ("degrad",)]),
    ("explain", [("why",), ("what", "caused"), ("reason",)]),
    ("workloads", [("which", "app"), ("which", "program"), ("what", "was", "running"),
                  ("workload",), ("which", "game")]),
    ("incidents", [("incident",), ("overheat",), ("too", "hot"), ("alert",), ("throttl",)]),
    ("status", [("how", "is"), ("how", "has"), ("health",), ("doing",), ("status",), ("summary",)]),
    ("timeline", [("what", "happened"), ("timeline",), ("log",)]),
]

ASK_EXAMPLES = [
    "Why did my PC run hot last night?",
    "What happened yesterday?",
    "How has my GPU been doing this week?",
    "Which apps ran hottest in the last 3 days?",
    "Were there any incidents last month?",
    "Is my CPU getting worse over time?",
    "What do you recommend?",
]


def _hour_ts(d, hour):
    return time.mktime(datetime(d.year, d.month, d.day, hour).timetuple())


def parse_time_window(text, now=None):
    """A question -> (start_ts, end_ts, label, explicit). Calendar phrases resolve through the same
    local-date machinery the reports layer uses, so "last week" means the same Monday-anchored week
    everywhere in this app. `explicit` is False when nothing named a period and the default 24h
    window was assumed - the answer says so rather than letting the reader supply an assumption."""
    now = now if now is not None else time.time()
    today = datetime.fromtimestamp(now).date()
    lowered = (text or "").lower()

    match = re.search(r"\b(?:last|past|previous)\s+(\d+)\s*(hour|hr|day|week|month)s?\b", lowered)
    if match:
        count, unit = int(match.group(1)), match.group(2)
        seconds = {"hour": 3600, "hr": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400}[unit]
        return now - count * seconds, now, f"the last {count} {unit}{'s' if count != 1 else ''}", True
    if "last night" in lowered or "overnight" in lowered:
        start = _hour_ts(today - timedelta(days=1), ASK_NIGHT_START_HOUR)
        return start, min(_hour_ts(today, ASK_NIGHT_END_HOUR), now), (
            f"last night ({ASK_NIGHT_START_HOUR}:00 yesterday to {ASK_NIGHT_END_HOUR:02d}:00 today)"), True
    if "this morning" in lowered:
        return _hour_ts(today, ASK_NIGHT_END_HOUR), min(_hour_ts(today, 12), now), "this morning", True
    if "yesterday" in lowered:
        b = period_bounds("DAILY", today - timedelta(days=1))
        return b["start_ts"], b["end_ts"], f"yesterday ({b['label']})", True
    if "today" in lowered:
        return local_midnight_ts(today), now, "today so far", True
    if "last week" in lowered:
        b = previous_completed_period("WEEKLY", now)
        return b["start_ts"], b["end_ts"], f"last week ({b['label']})", True
    if "this week" in lowered:
        monday = today - timedelta(days=today.weekday())
        return local_midnight_ts(monday), now, "this week so far", True
    if "last month" in lowered:
        b = previous_completed_period("MONTHLY", now)
        return b["start_ts"], b["end_ts"], f"last month ({b['label']})", True
    if "this month" in lowered:
        return local_midnight_ts(date(today.year, today.month, 1)), now, "this month so far", True
    return now - ASK_DEFAULT_WINDOW_SECONDS, now, "the last 24 hours", False


def parse_component(text):
    """The component a question is about, or None for a whole-machine question. Longest phrase wins,
    so "video card" is not shadowed by a shorter word inside it."""
    lowered = (text or "").lower()
    for phrase in sorted(ASK_COMPONENT_WORDS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            return ASK_COMPONENT_WORDS[phrase]
    return None


def classify_question(text):
    """Intent, or None when nothing matches. None is a real answer: the layer says what it cannot do
    rather than routing an unrecognised question to whichever handler happens to be closest."""
    lowered = (text or "").lower()
    for intent, groups in ASK_INTENT_PATTERNS:
        for group in groups:
            if all(word in lowered for word in group):
                return intent
    return None


def _window_evidence(start_ts, end_ts, component=None):
    """Everything already recorded about one window, fetched ONCE and shared by every answer builder
    so no two of them read the same store twice."""
    buckets = [b for b in read_telemetry_file(since_ts=start_ts)
              if b.get("start_timestamp") is not None and start_ts <= b["start_timestamp"] < end_ts]
    incidents = overlapping_incidents(read_incidents_file(), start_ts, end_ts)
    if component:
        keys = ASK_COMPONENT_INCIDENT_KEYS.get(component, ())
        incidents = [i for i in incidents if i.get("component") in keys]
    sessions = overlapping_sessions(read_sessions_file(), start_ts, end_ts)
    _, _, coverage = compute_coverage(buckets, end_ts - start_ts)
    return {"buckets": buckets, "incidents": incidents, "sessions": sessions,
           "coverage_pct": coverage, "gaps": timeline_gap_events(buckets, start_ts, end_ts)}


def _coverage_caveat(evidence, lines):
    """State coverage whenever the window was poorly observed - the reports layer's honesty rule
    applied to conversational answers, where it matters more because a fluent sentence reads as
    complete even when the data behind it is not."""
    if evidence["coverage_pct"] < REPORT_MIN_COVERAGE_PCT:
        lines.append(f"Note: Thermal Watch only monitored {evidence['coverage_pct']:.0f}% of this period, "
                    f"so this may not be the whole picture.")
    for gap in evidence["gaps"][:2]:
        lines.append(f"  Not monitored: {fmt_timeline_span(gap['end_timestamp'] - gap['timestamp'])} "
                    f"from {datetime.fromtimestamp(gap['timestamp']):%b %d %H:%M}.")


def _peak_lines(evidence, component, bounds_like):
    out = []
    scalars = ASK_COMPONENT_SCALARS.get(component) if component else ("cpu_temp", "gpu_hotspot_temp")
    for key in scalars or ():
        stats = period_metric_stats(evidence["buckets"], scalar_sensor_ref(key), bounds_like)
        if stats and stats.get("max") is not None:
            ref = scalar_sensor_ref(key)
            out.append(f"  {ref['label']}: peaked at {stats['max']:.0f}{ref['unit']} "
                      f"(average {stats['avg']:.0f}{ref['unit']}).")
    return out


def answer_question(text, now=None):
    """A question -> a structured answer: the parsed interpretation, the evidence counts it drew on,
    and the display lines. intent=None with a capability list when the question isn't understood."""
    now = now if now is not None else time.time()
    intent = classify_question(text)
    start_ts, end_ts, window_label, explicit = parse_time_window(text, now)
    component = parse_component(text)
    answer = {"question": text, "intent": intent, "component": component,
             "window_label": window_label, "window_explicit": explicit,
             "start_ts": start_ts, "end_ts": end_ts, "lines": [], "evidence_counts": {}}

    if intent is None:
        answer["lines"] = ["I don't know how to answer that from the data I keep.", "",
                          "Things I can answer:"] + [f"  • {q}" for q in ASK_EXAMPLES]
        return answer

    evidence = _window_evidence(start_ts, end_ts, component)
    bounds_like = {"start_ts": start_ts, "end_ts": end_ts}
    answer["evidence_counts"] = {"incidents": len(evidence["incidents"]),
                                "sessions": len(evidence["sessions"]),
                                "telemetry_buckets": len(evidence["buckets"]),
                                "monitoring_gaps": len(evidence["gaps"])}
    label = COMPONENT_LABELS.get(component, component.upper()) if component else "the machine"
    lines = []
    if not explicit:
        lines.append(f"(No period given, so I looked at {window_label}.)")

    if intent == "explain":
        # "Why" is answered as "here is what was happening and what coincided with it". This layer
        # cannot establish a cause and says so, rather than offering the most plausible-sounding one.
        lines.append(f"Here is what was recorded for {label} during {window_label}:")
        lines += _peak_lines(evidence, component, bounds_like)
        if evidence["incidents"]:
            lines.append(f"  {len(evidence['incidents'])} thermal incident(s) were recorded:")
            for inc in evidence["incidents"][:ASK_MAX_EVIDENCE_ROWS]:
                comp = COMPONENT_LABELS.get(inc.get("component"), inc.get("component"))
                when = datetime.fromtimestamp(inc["start_timestamp"]).strftime("%b %d %H:%M")
                peak = f"{inc['peak_value']:.0f}°C" if inc.get("peak_value") is not None else "unknown peak"
                lines.append(f"    {when} - {comp} reached {inc.get('max_zone', '?')} ({peak}), "
                            f"lasting {fmt_dur(inc.get('duration_seconds') or 0)}.")
        else:
            lines.append("  No thermal incidents were recorded in this period.")
        if evidence["sessions"]:
            names = {}
            for s in evidence["sessions"]:
                name = s.get("workload") or NOT_IDENTIFIED_DISPLAY
                names[name] = names.get(name, 0) + 1
            top = sorted(names.items(), key=lambda kv: -kv[1])[:ASK_MAX_EVIDENCE_ROWS]
            lines.append("  Workloads active during this period (associated with it, not shown to be the "
                        "cause of it): " + ", ".join(f"{n} ({c} session{'s' if c != 1 else ''})"
                                                     for n, c in top))
        else:
            lines.append("  No workload sessions were recorded in this period.")
        lines += ["", "Thermal Watch can tell you what was measured and what coincided with it. It cannot "
                     "determine from software telemetry alone what caused it."]

    elif intent == "incidents":
        if evidence["incidents"]:
            lines.append(f"{len(evidence['incidents'])} incident(s) for {label} during {window_label}:")
            for inc in evidence["incidents"][:ASK_MAX_EVIDENCE_ROWS]:
                comp = COMPONENT_LABELS.get(inc.get("component"), inc.get("component"))
                when = datetime.fromtimestamp(inc["start_timestamp"]).strftime("%b %d %H:%M")
                lines.append(f"  {when} - {comp}, {inc.get('max_zone', '?')}, "
                            f"{fmt_dur(inc.get('duration_seconds') or 0)}"
                            + (f", during {inc['dominant_workload']}" if inc.get("dominant_workload") else ""))
        else:
            lines.append(f"No thermal incidents were recorded for {label} during {window_label}.")

    elif intent == "workloads":
        stats = []
        for group in group_sessions_by_workload(evidence["sessions"]).values():
            gpu_peaks = [(s.get("gpu") or {}).get("peak_hotspot_temp") for s in group["sessions"]]
            cpu_peaks = [(s.get("cpu") or {}).get("peak_temp") for s in group["sessions"]]
            stats.append({"name": group["display_name"], "sessions": len(group["sessions"]),
                         "gpu": max([p for p in gpu_peaks if p is not None], default=None),
                         "cpu": max([p for p in cpu_peaks if p is not None], default=None)})
        stats.sort(key=lambda s: -(s["gpu"] or s["cpu"] or 0))
        if stats:
            lines.append(f"Workloads recorded during {window_label}, hottest first:")
            for s in stats[:ASK_MAX_EVIDENCE_ROWS]:
                parts = [f"{s['sessions']} session{'s' if s['sessions'] != 1 else ''}"]
                if s["cpu"] is not None:
                    parts.append(f"peak CPU {s['cpu']:.0f}°C")
                if s["gpu"] is not None:
                    parts.append(f"peak GPU hotspot {s['gpu']:.0f}°C")
                lines.append(f"  {s['name']}: " + ", ".join(parts))
        else:
            lines.append(f"No workload sessions were recorded during {window_label}.")

    elif intent == "status":
        lines.append(f"How {label} has been during {window_label}:")
        lines += _peak_lines(evidence, component, bounds_like)
        scores = [s for s in _session_health_scores_by_id(evidence["sessions"]).values() if s is not None]
        if scores:
            avg = sum(scores) / len(scores)
            lines.append(f"  Average session health score: {avg:.0f}/100 ({health_score_label(avg)}), "
                        f"from {len(scores)} session(s).")
        lines.append(f"  {len(evidence['incidents'])} thermal incident(s), "
                    f"{sum(1 for i in evidence['incidents'] if i.get('max_zone') == 'RED')} critical.")

    elif intent == "trend":
        window_days = max(1.0, (end_ts - start_ts) / 86400.0)
        scalar = "cpu_temp" if component == "cpu" else "gpu_hotspot_temp"
        trend = compute_idle_metric_period_trend(scalar_sensor_ref(scalar), window_days, now=end_ts)
        if trend is None:
            lines.append(f"I don't have enough idle telemetry across {window_label} to say whether "
                        f"{label} is trending in any direction.")
        else:
            lines.append(f"Idle {scalar_sensor_ref(scalar)['label']} across {window_label}: "
                        f"{trend['direction']} ({format_period_delta(trend, '°C', 1)}), "
                        f"confidence {trend['confidence']}.")
            lines.append("  This compares two halves of the period - a measured shift, not a forecast.")

    elif intent == "recommendations":
        recs = compute_recommendations(now=end_ts)
        answer["evidence_counts"]["recommendations"] = len(recs)
        lines.append("Current recommendations:")
        for rec in recs:
            lines.append(f"  • {rec['title']}")
            if rec.get("recommendation"):
                lines.append(f"    {rec['recommendation']}")
            if rec.get("confidence"):
                lines.append(f"    Confidence: {rec['confidence']}   Urgency: {rec['urgency']}")

    elif intent == "timeline":
        events = build_timeline(start_ts, end_ts, incidents=read_incidents_file(),
                               sessions=read_sessions_file(), experiments=read_experiments_file(),
                               buckets=evidence["buckets"], log_records=read_event_log_file())
        answer["evidence_counts"]["timeline_events"] = len(events)
        if events:
            lines.append(f"{len(events)} thing(s) recorded during {window_label}, most recent first:")
            for ev in events[:ASK_MAX_EVIDENCE_ROWS]:
                when = datetime.fromtimestamp(ev["timestamp"]).strftime("%b %d %H:%M")
                lines.append(f"  {when} - {TIMELINE_KIND_LABELS.get(ev['kind'], ev['kind'])}: {ev['title']}")
            if len(events) > ASK_MAX_EVIDENCE_ROWS:
                lines.append(f"  ... and {len(events) - ASK_MAX_EVIDENCE_ROWS} more (see the Timeline view).")
        else:
            lines.append(f"Nothing was recorded during {window_label}.")

    _coverage_caveat(evidence, lines)
    counts = answer["evidence_counts"]
    lines += ["", f"Based on {counts.get('incidents', 0)} incident record(s), "
                 f"{counts.get('sessions', 0)} session record(s) and "
                 f"{counts.get('telemetry_buckets', 0)} minute(s) of telemetry."]
    answer["lines"] = lines
    return answer


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def run_hidden(args, timeout=4):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def cpu_times():
    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    val = lambda x: (x.dwHighDateTime << 32) | x.dwLowDateTime
    return val(idle), val(kernel) + val(user)


def memory():
    m = MEMORYSTATUSEX(); m.dwLength = ctypes.sizeof(m)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    used = m.ullTotalPhys - m.ullAvailPhys
    return m.dwMemoryLoad, used / 2**30, m.ullTotalPhys / 2**30


# ---------------------------------------------------------------------------
# Workload attribution: "what was the PC doing when this alert fired". Pure ctypes against
# kernel32/user32/psapi/pdh - no subprocess spawns, no external dependency (psutil etc), so
# this can run every worker tick without meaningfully adding to Thermal Watch's own footprint.
# Local-only: nothing here touches window/document CONTENTS, keystrokes, or the network - only
# the same process-name/PID/window-title/utilization metadata Task Manager itself shows.
# ---------------------------------------------------------------------------
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
LOGICAL_CPU_COUNT = os.cpu_count() or 1


def _enum_pids():
    size = 1024
    while True:
        arr = (wintypes.DWORD * size)()
        needed = wintypes.DWORD()
        if not ctypes.windll.psapi.EnumProcesses(arr, ctypes.sizeof(arr), ctypes.byref(needed)):
            return []
        count = needed.value // ctypes.sizeof(wintypes.DWORD)
        if count < size:
            return [p for p in arr[:count] if p]
        size *= 2  # buffer was too small (this many processes); retry larger


def _process_image_name(handle):
    buf = ctypes.create_unicode_buffer(260)
    size = wintypes.DWORD(260)
    if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return os.path.basename(buf.value)
    return None


def foreground_process():
    """Current foreground window's {name, pid, title}, or None if it can't be determined.
    Only the window TITLE is read (same as Alt-Tab shows) - never window/document contents."""
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    title = None
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    if length > 0:
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value or None
    name = None
    h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if h:
        try:
            name = _process_image_name(h)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    return {"name": name or f"pid:{pid.value}", "pid": pid.value, "title": title}


def _sample_process_cpu_times():
    """{pid: (name, kernel+user 100ns total)} for every process we can open/query."""
    out = {}
    for pid in _enum_pids():
        h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            continue
        try:
            name = _process_image_name(h)
            if not name:
                continue
            creation, exit_t = wintypes.FILETIME(), wintypes.FILETIME()
            kernel, user = wintypes.FILETIME(), wintypes.FILETIME()
            if ctypes.windll.kernel32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_t),
                                                       ctypes.byref(kernel), ctypes.byref(user)):
                val = lambda t: (t.dwHighDateTime << 32) | t.dwLowDateTime
                out[pid] = (name, val(kernel) + val(user))
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    return out


def cpu_top_processes(prev_times, curr_times, dt_seconds, top_n=5):
    """Delta-based CPU% per process over the sampling interval, normalized against
    LOGICAL_CPU_COUNT (Task-Manager-style: a single core pegged at 100% shows ~1/32 = ~3% on
    this 32-thread 9950X, never 3200%). Returns [(name, pid, pct), ...] sorted descending,
    dropping anything below a noise floor."""
    if dt_seconds <= 0:
        return []
    out = []
    for pid, (name, cpu_now) in curr_times.items():
        prev = prev_times.get(pid)
        if not prev:
            continue
        delta_100ns = cpu_now - prev[1]
        if delta_100ns <= 0:
            continue
        pct = (delta_100ns / 1e7) / dt_seconds / LOGICAL_CPU_COUNT * 100
        if pct >= 0.5:  # noise floor - don't list processes using a fraction of a percent
            out.append((name, pid, pct))
    out.sort(key=lambda x: -x[2])
    return out[:top_n]


class PDH_FMT_VALUE(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_ulong), ("_pad", ctypes.c_ulong), ("doubleValue", ctypes.c_double)]


class GpuProcessSampler:
    """Long-lived PDH query against \\GPU Engine(*)\\Utilization Percentage - no subprocess,
    no external dependency. A process using several GPU engines concurrently (3D + video
    decode, say) is reported at the MAX of its engines' utilization rather than their sum:
    engines run in parallel on independent hardware pipelines, so summing them can legitimately
    exceed 100% and would misrepresent "how loaded is the GPU because of this process" - max is
    the conservative, never-impossible reading. Re-expands the counter set periodically so
    processes that start/stop using the GPU are picked up without a full restart."""

    PDH_FMT_DOUBLE = 0x00000200
    REEXPAND_EVERY_N_SAMPLES = 5

    def __init__(self):
        self.ok = False
        self.query = ctypes.c_void_p()
        self.counter_pid = {}  # counter handle (as int) -> pid
        self._samples_since_expand = 0
        try:
            if ctypes.windll.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)) == 0:
                self._expand()
                self.ok = True
        except OSError:
            self.ok = False

    def _expand(self):
        # Rebuild the query from scratch, rather than re-adding the wildcard set to the existing
        # one. PDH keeps EVERY counter ever added to a query alive until the query itself is
        # closed - dropping the Python-side counter_pid dict released nothing, so each refresh
        # (~every 10s) orphaned the whole ~600-counter set inside PDH. Measured: 36k counters
        # added and 0 released over 10 minutes, 4.8 MB/min of native heap growth in a standalone
        # ctypes reproduction, and 5.8 -> 0.03 MB/min in the app when this sampler was disabled.
        # Closing the query frees all of them in one call, and cannot leak a counter that the
        # dict lost track of the way a per-counter PdhRemoveCounter loop could.
        try:
            if self.query:
                ctypes.windll.pdh.PdhCloseQuery(self.query)
        except OSError:
            pass
        self.query = ctypes.c_void_p()
        self.counter_pid = {}
        if ctypes.windll.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)) != 0:
            # Couldn't reopen: leave the query null and _samples_since_expand past its threshold
            # so the next sample() retries. sample()'s PdhCollectQueryData on a null query fails
            # and returns {} - "not obtainable this poll", the same contract as before, and never
            # a fabricated value.
            self.query = ctypes.c_void_p()
            return
        path = r"\GPU Engine(*)\Utilization Percentage"
        size = ctypes.c_ulong(0)
        ctypes.windll.pdh.PdhExpandWildCardPathW(None, path, None, ctypes.byref(size), 0)
        if size.value == 0:
            return
        buf = ctypes.create_unicode_buffer(size.value)
        if ctypes.windll.pdh.PdhExpandWildCardPathW(None, path, buf, ctypes.byref(size), 0) != 0:
            return
        raw = ctypes.wstring_at(buf, size.value)
        for counter_path in [p for p in raw.split("\x00") if p]:
            m = re.search(r"pid_(\d+)_", counter_path)
            if not m:
                continue
            handle = ctypes.c_void_p()
            if ctypes.windll.pdh.PdhAddCounterW(self.query, counter_path, 0, ctypes.byref(handle)) == 0:
                self.counter_pid[handle.value] = int(m.group(1))
        # Prime the rebuilt query. \GPU Engine(*)\Utilization Percentage is a RATE counter: it
        # needs two collections before it can yield a value, so the first sample() after every
        # rebuild returned {} - measured at 100% of post-rebuild polls, i.e. one poll in five
        # lost. This priming collect is the pairing sample, so the next real sample() has a
        # valid interval to compute against. (A PdhRemoveCounter-based rebuild would need this
        # too - freshly ADDED counters have the same two-collection requirement.)
        ctypes.windll.pdh.PdhCollectQueryData(self.query)
        self._samples_since_expand = 0

    def sample(self):
        """{pid: max_engine_utilization_percent}, or {} if unavailable this poll (never
        invents a value - callers must treat an empty result as 'not obtainable', per spec)."""
        if not self.ok:
            return {}
        try:
            if self._samples_since_expand >= self.REEXPAND_EVERY_N_SAMPLES:
                self._expand()
            if ctypes.windll.pdh.PdhCollectQueryData(self.query) != 0:
                return {}
            self._samples_since_expand += 1
            result = {}
            for handle_val, pid in self.counter_pid.items():
                fmt = PDH_FMT_VALUE()
                status = ctypes.windll.pdh.PdhGetFormattedCounterValue(
                    ctypes.c_void_p(handle_val), self.PDH_FMT_DOUBLE, None, ctypes.byref(fmt))
                if status == 0 and fmt.CStatus == 0 and fmt.doubleValue > 0:
                    result[pid] = max(result.get(pid, 0.0), fmt.doubleValue)
            return result
        except OSError:
            return {}


def gpu_top_processes(pid_util, cpu_names_by_pid, top_n=5):
    """Joins the PDH {pid: pct} result with process names (from the same-tick CPU sample, so
    no extra OpenProcess calls are needed) into [(name, pid, pct), ...] sorted descending."""
    out = []
    for pid, pct in pid_util.items():
        if pct < 0.5:
            continue
        name = cpu_names_by_pid.get(pid)
        if not name:
            continue
        out.append((name, pid, min(100.0, pct)))
    out.sort(key=lambda x: -x[2])
    return out[:top_n]


def hardware_info():
    ps = "$c=Get-CimInstance Win32_Processor|Select -First 1 Name,MaxClockSpeed,NumberOfCores,NumberOfLogicalProcessors;$o=[pscustomobject]@{cpu=$c.Name;max=$c.MaxClockSpeed;cores=$c.NumberOfCores;threads=$c.NumberOfLogicalProcessors};$o|ConvertTo-Json -Compress"
    try:
        return json.loads(run_hidden(["powershell", "-NoProfile", "-Command", ps], 8))
    except Exception:
        return {"cpu": os.environ.get("PROCESSOR_IDENTIFIER", "CPU"), "max": 0,
                "cores": os.cpu_count() or 0, "threads": os.cpu_count() or 0}


def nvidia_stats():
    fields = "name,temperature.gpu,utilization.gpu,memory.used,memory.total,clocks.current.graphics,power.draw,power.limit,fan.speed"
    out = run_hidden(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"])
    if not out: return []
    result = []
    for row in csv.reader(out.splitlines()):
        if len(row) != 9: continue
        def num(x):
            try: return float(x.strip())
            except ValueError: return None
        result.append({"name": row[0].strip(), "temp": num(row[1]), "load": num(row[2]),
                       "mem_used": num(row[3]), "mem_total": num(row[4]), "clock": num(row[5]),
                       "power": num(row[6]), "power_limit": num(row[7]), "fan": num(row[8])})
    return result


# ---------------------------------------------------------------------------
# Network telemetry (v1.1 Phase 1 - Network Foundation). Same dependency-free, raw-ctypes-
# against-Windows-DLLs style as nvidia_stats()/memory()/lhm_sensors() above - no psutil, no
# wmi, no third-party package of any kind.
#
# APIs used, and why:
#   - GetIfTable2 (iphlpapi.dll): per-adapter identity/state/counters. The modern, Vista+,
#     IPv6-capable replacement for GetIfTable/GetIfEntry - MIB_IF_ROW2 carries 64-bit link
#     speeds and octet counters plus an explicit MediaConnectState (cable-plugged/associated vs
#     not) that the legacy MIB_IFROW does not.
#   - GetBestInterfaceEx (iphlpapi.dll): "which adapter is my real internet connection right
#     now" - the same technique GetBestRoute2 uses (consulting the live route table) with less
#     struct surface to get wrong, since it only needs a destination sockaddr, not a NET_LUID
#     plus a source SOCKADDR_INET binding.
#   - GetAdaptersAddresses (iphlpapi.dll): unicast IPv4 + default gateway for one adapter.
#   - GetIpForwardTable2 (iphlpapi.dll): gateway fallback. Measured on real hardware:
#     IP_ADAPTER_ADDRESSES.FirstGatewayAddress came back NULL for an adapter that unquestionably
#     has a gateway (ipconfig/Get-NetIPConfiguration/Get-NetRoute all agreed on it) - those
#     cmdlets read the gateway from the live route table instead, so that is the fallback here
#     rather than trusting the "textbook" field alone.
#   - wlanapi.dll: Wi-Fi signal quality. The one piece with a real "not applicable" case on most
#     machines (wired-only, or WLAN AutoConfig service disabled), so wifi_signal_percent() is
#     wrapped end-to-end and returns None on any failure - never a fabricated signal reading.
#
# Every function below: explicit argtypes/restype on every DLL call (this file's own lesson,
# paid for once already with the sleep/resume title-bar bug - skipping this can silently
# misinterpret a 64-bit value), defensive None/[] returns, never a fabricated adapter/value,
# never an uncaught exception escaping to a caller. READ-ONLY: nothing here sends traffic,
# opens a raw socket, or changes any adapter/network setting - it only queries counters and
# state the OS/driver already publish.
# ---------------------------------------------------------------------------
_iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
try:
    _wlanapi = ctypes.WinDLL("wlanapi", use_last_error=True)
except OSError:
    _wlanapi = None  # not present/loadable on some locked-down or server builds
_AF_INET = 2


class _NET_SOCKADDR_IN(ctypes.Structure):
    _fields_ = [("sin_family", ctypes.c_short), ("sin_port", ctypes.c_ushort),
                ("sin_addr", ctypes.c_ubyte * 4), ("sin_zero", ctypes.c_char * 8)]


class _NET_SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]


def _net_ipv4_str(sockaddr_ptr):
    """Best-effort dotted-quad from a SOCKET_ADDRESS.lpSockaddr, or None if it isn't AF_INET or
    the pointer is null (GetAdaptersAddresses can still hand back non-IPv4 entries even when
    the query is Family-restricted, in edge cases, so the family is re-checked here)."""
    if not sockaddr_ptr:
        return None
    sa = ctypes.cast(sockaddr_ptr, ctypes.POINTER(_NET_SOCKADDR_IN)).contents
    if sa.sin_family != _AF_INET:
        return None
    return "%d.%d.%d.%d" % tuple(sa.sin_addr)


class _NET_GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
               ("Data4", ctypes.c_ubyte * 8)]


# -- GetIfTable2 / MIB_IF_ROW2: adapter identity, state, counters --------------------------
_IF_MAX_STRING_SIZE = 256
_IF_MAX_PHYS_ADDRESS_LENGTH = 32
IF_TYPE_ETHERNET_CSMACD = 6
IF_TYPE_IEEE80211 = 71
IF_TYPE_SOFTWARE_LOOPBACK = 24
IF_TYPE_TUNNEL = 131
_IF_OPER_STATUS_NAMES = {1: "Up", 2: "Down", 3: "Testing", 4: "Unknown", 5: "Dormant",
                         6: "NotPresent", 7: "LowerLayerDown"}
_NET_IF_ADMIN_STATUS_UP = 1
_MEDIA_CONNECT_STATE = {0: None, 1: True, 2: False}  # Unknown / Connected / Disconnected
_ULONG64_UNKNOWN = 0xFFFFFFFFFFFFFFFF  # sentinel GetIfTable2 uses for "link speed not known"


class _IF_OPER_STATUS_FLAGS(ctypes.Structure):
    """MIB_IF_ROW2.InterfaceAndOperStatusFlags: 8 single-bit BOOLEAN fields packed by MSVC into
    one byte. Nothing here is read - it exists purely so the ULONG fields that follow
    (OperStatus etc.) land at the offsets ctypes would otherwise misplace by skipping the
    compiler's automatic 3-byte pad to the next 4-byte boundary."""
    _fields_ = [
        ("HardwareInterface", ctypes.c_ubyte, 1), ("FilterInterface", ctypes.c_ubyte, 1),
        ("ConnectorPresent", ctypes.c_ubyte, 1), ("NotAuthenticated", ctypes.c_ubyte, 1),
        ("NotMediaConnected", ctypes.c_ubyte, 1), ("Paused", ctypes.c_ubyte, 1),
        ("LowPower", ctypes.c_ubyte, 1), ("EndPointInterface", ctypes.c_ubyte, 1),
    ]


class _MIB_IF_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64), ("InterfaceIndex", ctypes.c_ulong),
        ("InterfaceGuid", _NET_GUID),
        ("Alias", ctypes.c_wchar * (_IF_MAX_STRING_SIZE + 1)),
        ("Description", ctypes.c_wchar * (_IF_MAX_STRING_SIZE + 1)),
        ("PhysicalAddressLength", ctypes.c_ulong),
        ("PhysicalAddress", ctypes.c_ubyte * _IF_MAX_PHYS_ADDRESS_LENGTH),
        ("PermanentPhysicalAddress", ctypes.c_ubyte * _IF_MAX_PHYS_ADDRESS_LENGTH),
        ("Mtu", ctypes.c_ulong), ("Type", ctypes.c_ulong), ("TunnelType", ctypes.c_uint),
        ("MediaType", ctypes.c_uint), ("PhysicalMediumType", ctypes.c_uint),
        ("AccessType", ctypes.c_uint), ("DirectionType", ctypes.c_uint),
        ("InterfaceAndOperStatusFlags", _IF_OPER_STATUS_FLAGS),
        ("OperStatus", ctypes.c_uint), ("AdminStatus", ctypes.c_uint),
        ("MediaConnectState", ctypes.c_uint), ("NetworkGuid", _NET_GUID),
        ("ConnectionType", ctypes.c_uint),
        ("TransmitLinkSpeed", ctypes.c_uint64), ("ReceiveLinkSpeed", ctypes.c_uint64),
        ("InOctets", ctypes.c_uint64), ("InUcastPkts", ctypes.c_uint64),
        ("InNUcastPkts", ctypes.c_uint64), ("InDiscards", ctypes.c_uint64),
        ("InErrors", ctypes.c_uint64), ("InUnknownProtos", ctypes.c_uint64),
        ("InUcastOctets", ctypes.c_uint64), ("InMulticastOctets", ctypes.c_uint64),
        ("InBroadcastOctets", ctypes.c_uint64), ("OutOctets", ctypes.c_uint64),
        ("OutUcastPkts", ctypes.c_uint64), ("OutNUcastPkts", ctypes.c_uint64),
        ("OutDiscards", ctypes.c_uint64), ("OutErrors", ctypes.c_uint64),
        ("OutUcastOctets", ctypes.c_uint64), ("OutMulticastOctets", ctypes.c_uint64),
        ("OutBroadcastOctets", ctypes.c_uint64), ("OutQLen", ctypes.c_uint64),
    ]


class _MIB_IF_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", ctypes.c_ulong), ("Table", _MIB_IF_ROW2 * 1)]  # variable-length


_iphlpapi.GetIfTable2.argtypes = [ctypes.POINTER(ctypes.POINTER(_MIB_IF_TABLE2))]
_iphlpapi.GetIfTable2.restype = wintypes.DWORD
_iphlpapi.FreeMibTable.argtypes = [ctypes.c_void_p]
_iphlpapi.FreeMibTable.restype = None


def network_adapters():
    """One dict per real network adapter (excludes loopback, tunnel/VPN pseudo-interfaces such
    as Teredo/ISATAP/6to4, and adapters administratively disabled in Device Manager). [] on any
    failure - never a fabricated adapter.

    GetIfTable2 also surfaces two categories of non-adapter rows that IF_TYPE alone cannot
    distinguish from a real NIC (confirmed on real hardware: left un-filtered, these turned 8
    real adapters into ~55 rows): every NDIS filter driver bound to a real miniport (WFP
    callout layers, the QoS Packet Scheduler, the Wi-Fi virtual/native filter drivers) gets its
    own row with the real adapter's description plus a filter-layer suffix - these are bind
    points in the same driver stack, not separate hardware, and the real adapter is already
    reported separately without the suffix; and "WAN Miniport (...)" rows are the
    always-present RAS/PPP framework pseudo-devices Windows creates whether or not any
    dial-up/VPN/PPPoE connection exists, per Microsoft's own guidance to filter them by name."""
    table_ptr = ctypes.POINTER(_MIB_IF_TABLE2)()
    try:
        if _iphlpapi.GetIfTable2(ctypes.byref(table_ptr)) != 0:
            return []
        try:
            n = table_ptr.contents.NumEntries
            # MIB_IF_TABLE2.Table is a C99-style flexible array member; the struct above only
            # declares a 1-element placeholder so ctypes can compute Table's offset. The real
            # row count comes back in NumEntries, so re-view that same memory as an n-element
            # array (same address, no copy) rather than trusting the placeholder length.
            rows = (_MIB_IF_ROW2 * n).from_address(ctypes.addressof(table_ptr.contents.Table))
            out = []
            for row in rows:
                if row.Type in (IF_TYPE_SOFTWARE_LOOPBACK, IF_TYPE_TUNNEL):
                    continue
                name = row.Alias or ""
                lname = name.lower()
                desc = row.Description or ""
                ldesc = desc.lower()
                if any(tag in lname or tag in ldesc for tag in ("teredo", "isatap", "6to4")):
                    continue
                if any(tag in ldesc for tag in (
                        "lightweight filter", "wfp native mac layer", "wfp 802.3 mac layer",
                        "qos packet scheduler", "virtual wifi filter driver", "native wifi filter driver")):
                    continue
                if ldesc.startswith("wan miniport"):
                    continue
                if row.AdminStatus != _NET_IF_ADMIN_STATUS_UP:
                    continue
                if row.Type == IF_TYPE_ETHERNET_CSMACD:
                    kind = "Ethernet"
                elif row.Type == IF_TYPE_IEEE80211:
                    kind = "Wi-Fi"
                else:
                    kind = "Other"
                rx, tx = row.ReceiveLinkSpeed, row.TransmitLinkSpeed
                out.append({
                    "name": name, "description": desc, "type": kind,
                    "oper_status": _IF_OPER_STATUS_NAMES.get(row.OperStatus, "Unknown"),
                    "media_connect_state": _MEDIA_CONNECT_STATE.get(row.MediaConnectState),
                    "receive_link_speed_bps": None if rx == _ULONG64_UNKNOWN else int(rx),
                    "transmit_link_speed_bps": None if tx == _ULONG64_UNKNOWN else int(tx),
                    "in_octets": int(row.InOctets), "out_octets": int(row.OutOctets),
                    "luid": int(row.InterfaceLuid), "index": int(row.InterfaceIndex),
                })
            return out
        finally:
            _iphlpapi.FreeMibTable(table_ptr)
    except OSError:
        return []


# -- GetBestInterfaceEx: "which adapter is my real internet connection right now" -----------
_iphlpapi.GetBestInterfaceEx.argtypes = [ctypes.POINTER(_NET_SOCKADDR_IN), ctypes.POINTER(wintypes.DWORD)]
_iphlpapi.GetBestInterfaceEx.restype = wintypes.DWORD


def default_route_interface_index():
    """Interface index Windows' routing table would pick right now to reach a public IP (probes
    8.8.8.8; no packet is actually sent - GetBestInterfaceEx only consults the route table).
    None on failure (e.g. no route to the internet at all). This is how Thermal Watch decides
    which of possibly many adapters (Ethernet + Wi-Fi + VPN + virtual) is "the" one to show on
    the live dashboard - the one actually carrying real internet traffic, not just any adapter
    that happens to be up."""
    try:
        dest = _NET_SOCKADDR_IN()
        dest.sin_family = _AF_INET
        dest.sin_port = 0
        dest.sin_addr[:] = (8, 8, 8, 8)
        idx = wintypes.DWORD()
        if _iphlpapi.GetBestInterfaceEx(ctypes.byref(dest), ctypes.byref(idx)) != 0:
            return None
        return int(idx.value)
    except OSError:
        return None


# -- GetAdaptersAddresses: unicast IPv4 + default gateway for one adapter -------------------
_GAA_FLAG_SKIP_ANYCAST = 0x2
_GAA_FLAG_SKIP_MULTICAST = 0x4
_GAA_FLAG_SKIP_DNS_SERVER = 0x8
_ERROR_BUFFER_OVERFLOW = 111
_MAX_ADAPTER_ADDRESS_LENGTH = 8
_MAX_DHCPV6_DUID_LENGTH = 130


class _IP_ADAPTER_UNICAST_ADDRESS(ctypes.Structure):
    pass


_IP_ADAPTER_UNICAST_ADDRESS._fields_ = [
    ("Length", ctypes.c_ulong), ("Flags", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("Address", _NET_SOCKET_ADDRESS),
    ("PrefixOrigin", ctypes.c_uint), ("SuffixOrigin", ctypes.c_uint), ("DadState", ctypes.c_uint),
    ("ValidLifetime", ctypes.c_ulong), ("PreferredLifetime", ctypes.c_ulong),
    ("LeaseLifetime", ctypes.c_ulong), ("OnLinkPrefixLength", ctypes.c_ubyte),
]


class _IP_ADAPTER_GATEWAY_ADDRESS(ctypes.Structure):
    pass


_IP_ADAPTER_GATEWAY_ADDRESS._fields_ = [
    ("Length", ctypes.c_ulong), ("Reserved", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_IP_ADAPTER_GATEWAY_ADDRESS)),
    ("Address", _NET_SOCKET_ADDRESS),
]


class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    pass


_IP_ADAPTER_ADDRESSES._fields_ = [
    ("Length", ctypes.c_ulong), ("IfIndex", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_IP_ADAPTER_ADDRESSES)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_IP_ADAPTER_UNICAST_ADDRESS)),
    ("FirstAnycastAddress", ctypes.c_void_p), ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p), ("Description", ctypes.c_wchar_p), ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_ubyte * _MAX_ADAPTER_ADDRESS_LENGTH),
    ("PhysicalAddressLength", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong), ("Mtu", ctypes.c_ulong), ("IfType", ctypes.c_ulong),
    ("OperStatus", ctypes.c_uint), ("Ipv6IfIndex", ctypes.c_ulong),
    ("ZoneIndices", ctypes.c_ulong * 16), ("FirstPrefix", ctypes.c_void_p),
    ("TransmitLinkSpeed", ctypes.c_uint64), ("ReceiveLinkSpeed", ctypes.c_uint64),
    ("FirstWinsServerAddress", ctypes.c_void_p),
    ("FirstGatewayAddress", ctypes.POINTER(_IP_ADAPTER_GATEWAY_ADDRESS)),
    ("Ipv4Metric", ctypes.c_ulong), ("Ipv6Metric", ctypes.c_ulong), ("Luid", ctypes.c_uint64),
    ("Dhcpv4Server", _NET_SOCKET_ADDRESS), ("CompartmentId", ctypes.c_ulong),
    ("NetworkGuid", _NET_GUID), ("ConnectionType", ctypes.c_uint), ("TunnelType", ctypes.c_uint),
    ("Dhcpv6Server", _NET_SOCKET_ADDRESS),
    ("Dhcpv6ClientDuid", ctypes.c_ubyte * _MAX_DHCPV6_DUID_LENGTH),
    ("Dhcpv6ClientDuidLength", ctypes.c_ulong), ("Dhcpv6Iaid", ctypes.c_ulong),
    ("FirstDnsSuffix", ctypes.c_void_p),
]

_iphlpapi.GetAdaptersAddresses.argtypes = [
    wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p,
    ctypes.POINTER(_IP_ADAPTER_ADDRESSES), ctypes.POINTER(wintypes.ULONG),
]
_iphlpapi.GetAdaptersAddresses.restype = wintypes.ULONG


class _SOCKADDR_INET(ctypes.Union):
    """Modeled as a raw-bytes union (rather than nesting full sockaddr_in/sockaddr_in6 variants)
    since only the AF_INET arm is ever read here; the c_ulong member forces 4-byte alignment to
    match the real SOCKADDR_INET so later MIB_IPFORWARD_ROW2 fields land at correct offsets."""
    _fields_ = [("family", ctypes.c_ushort), ("raw", ctypes.c_ubyte * 28), ("_force_align", ctypes.c_ulong)]


class _IP_ADDRESS_PREFIX(ctypes.Structure):
    _fields_ = [("Prefix", _SOCKADDR_INET), ("PrefixLength", ctypes.c_ubyte)]


class _MIB_IPFORWARD_ROW2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64), ("InterfaceIndex", ctypes.c_ulong),
        ("DestinationPrefix", _IP_ADDRESS_PREFIX), ("NextHop", _SOCKADDR_INET),
        ("SitePrefixLength", ctypes.c_ubyte), ("ValidLifetime", ctypes.c_ulong),
        ("PreferredLifetime", ctypes.c_ulong), ("Metric", ctypes.c_ulong),
        ("Protocol", ctypes.c_uint), ("Loopback", ctypes.c_ubyte),
        ("AutoconfigureAddress", ctypes.c_ubyte), ("Publish", ctypes.c_ubyte),
        ("Immortal", ctypes.c_ubyte), ("Age", ctypes.c_ulong), ("Origin", ctypes.c_uint),
    ]


class _MIB_IPFORWARD_TABLE2(ctypes.Structure):
    _fields_ = [("NumEntries", ctypes.c_ulong), ("Table", _MIB_IPFORWARD_ROW2 * 1)]  # variable-length


_iphlpapi.GetIpForwardTable2.argtypes = [ctypes.c_ushort, ctypes.POINTER(ctypes.POINTER(_MIB_IPFORWARD_TABLE2))]
_iphlpapi.GetIpForwardTable2.restype = wintypes.DWORD


def _net_default_gateway_via_route_table(index):
    """Lowest-metric IPv4 next-hop of the 0.0.0.0/0 route(s) owned by this InterfaceIndex, read
    straight from the live route table - the same source Get-NetRoute/Get-NetIPConfiguration
    use. Fallback for adapter_ip_info() below: measured on real hardware, GetAdaptersAddresses'
    own FirstGatewayAddress list can come back empty for an adapter that unquestionably has a
    gateway. None if this adapter genuinely has no default route."""
    table_ptr = ctypes.POINTER(_MIB_IPFORWARD_TABLE2)()
    try:
        if _iphlpapi.GetIpForwardTable2(_AF_INET, ctypes.byref(table_ptr)) != 0:
            return None
        try:
            n = table_ptr.contents.NumEntries
            rows = (_MIB_IPFORWARD_ROW2 * n).from_address(ctypes.addressof(table_ptr.contents.Table))
            best_metric, best_ip = None, None
            for row in rows:
                if row.InterfaceIndex != index or row.DestinationPrefix.PrefixLength != 0:
                    continue
                if row.NextHop.family != _AF_INET:
                    continue
                sa = ctypes.cast(ctypes.byref(row.NextHop), ctypes.POINTER(_NET_SOCKADDR_IN)).contents
                ip = "%d.%d.%d.%d" % tuple(sa.sin_addr)
                if ip == "0.0.0.0":
                    continue
                if best_metric is None or row.Metric < best_metric:
                    best_metric, best_ip = row.Metric, ip
            return best_ip
        finally:
            _iphlpapi.FreeMibTable(table_ptr)
    except OSError:
        return None


def adapter_ip_info(index):
    """{'ipv4': ..., 'gateway': ...} (either may be None) for the adapter with this IfIndex (the
    same 'index' network_adapters() returns), or None if the adapter/addresses can't be found."""
    try:
        size = ctypes.c_ulong(15000)  # MS-recommended starting size; avoids a second call in the common case
        buf = None
        for _ in range(3):
            buf = (ctypes.c_ubyte * size.value)()
            ret = _iphlpapi.GetAdaptersAddresses(
                _AF_INET, _GAA_FLAG_SKIP_ANYCAST | _GAA_FLAG_SKIP_MULTICAST | _GAA_FLAG_SKIP_DNS_SERVER,
                None, ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_ADDRESSES)), ctypes.byref(size))
            if ret == 0:
                break
            if ret == _ERROR_BUFFER_OVERFLOW:
                continue  # size.value was updated to the required size by the API; retry
            return None
        else:
            return None

        node = ctypes.cast(buf, ctypes.POINTER(_IP_ADAPTER_ADDRESSES))
        while node:
            n = node.contents
            if n.IfIndex == index:
                ipv4 = _net_ipv4_str(n.FirstUnicastAddress.contents.Address.lpSockaddr) if n.FirstUnicastAddress else None
                gateway = _net_ipv4_str(n.FirstGatewayAddress.contents.Address.lpSockaddr) if n.FirstGatewayAddress else None
                if gateway is None:
                    gateway = _net_default_gateway_via_route_table(index)
                return {"ipv4": ipv4, "gateway": gateway}
            node = n.Next
        return None
    except OSError:
        return None


# -- wlanapi.dll: Wi-Fi signal quality -------------------------------------------------------
_WLAN_INTF_OPCODE_CURRENT_CONNECTION = 7


class _WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [("InterfaceGuid", _NET_GUID), ("strInterfaceDescription", ctypes.c_wchar * 256),
                ("isState", ctypes.c_uint)]


class _WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [("dwNumberOfItems", ctypes.c_ulong), ("dwIndex", ctypes.c_ulong),
                ("InterfaceInfo", _WLAN_INTERFACE_INFO * 1)]  # variable-length


class _DOT11_SSID(ctypes.Structure):
    _fields_ = [("uSSIDLength", ctypes.c_ulong), ("ucSSID", ctypes.c_ubyte * 32)]


class _WLAN_ASSOCIATION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("dot11Ssid", _DOT11_SSID), ("dot11BssType", ctypes.c_uint),
        ("dot11Bssid", ctypes.c_ubyte * 6), ("dot11PhyType", ctypes.c_uint),
        ("uDot11PhyIndex", ctypes.c_ulong), ("wlanSignalQuality", ctypes.c_ulong),
        ("ulRxRate", ctypes.c_ulong), ("ulTxRate", ctypes.c_ulong),
    ]


class _WLAN_SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("bSecurityEnabled", ctypes.c_int), ("bOneXEnabled", ctypes.c_int),
                ("dot11AuthAlgorithm", ctypes.c_uint), ("dot11CipherAlgorithm", ctypes.c_uint)]


class _WLAN_CONNECTION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("isState", ctypes.c_uint), ("wlanConnectionMode", ctypes.c_uint),
        ("strProfileName", ctypes.c_wchar * 256),
        ("wlanAssociationAttributes", _WLAN_ASSOCIATION_ATTRIBUTES),
        ("wlanSecurityAttributes", _WLAN_SECURITY_ATTRIBUTES),
    ]


if _wlanapi is not None:
    _wlanapi.WlanOpenHandle.argtypes = [wintypes.DWORD, ctypes.c_void_p,
                                        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.HANDLE)]
    _wlanapi.WlanOpenHandle.restype = wintypes.DWORD
    _wlanapi.WlanCloseHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    _wlanapi.WlanCloseHandle.restype = wintypes.DWORD
    _wlanapi.WlanEnumInterfaces.argtypes = [wintypes.HANDLE, ctypes.c_void_p,
                                            ctypes.POINTER(ctypes.POINTER(_WLAN_INTERFACE_INFO_LIST))]
    _wlanapi.WlanEnumInterfaces.restype = wintypes.DWORD
    _wlanapi.WlanQueryInterface.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_NET_GUID), ctypes.c_uint, ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint),
    ]
    _wlanapi.WlanQueryInterface.restype = wintypes.DWORD
    _wlanapi.WlanFreeMemory.argtypes = [ctypes.c_void_p]
    _wlanapi.WlanFreeMemory.restype = None


def wifi_signal_percent():
    """Best-effort Wi-Fi signal quality 0-100, or None on ANY failure: no wlanapi.dll (locked
    down build), WLAN AutoConfig service not running (WlanOpenHandle fails), no Wi-Fi adapter
    (WlanEnumInterfaces returns zero items), or nothing currently associated (WlanQueryInterface
    fails for every interface found). None of these are errors on a wired-only machine - same
    contract as lhm_sensors()/nvidia_stats(): never invent a value, never raise into the caller."""
    if _wlanapi is None:
        return None
    try:
        handle = wintypes.HANDLE()
        negotiated = wintypes.DWORD()
        if _wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle)) != 0:
            return None
        try:
            iface_list_ptr = ctypes.POINTER(_WLAN_INTERFACE_INFO_LIST)()
            if _wlanapi.WlanEnumInterfaces(handle, None, ctypes.byref(iface_list_ptr)) != 0:
                return None
            try:
                n = iface_list_ptr.contents.dwNumberOfItems
                if n == 0:
                    return None
                ifaces = (_WLAN_INTERFACE_INFO * n).from_address(
                    ctypes.addressof(iface_list_ptr.contents.InterfaceInfo))
                for iface in ifaces:
                    data_size = wintypes.DWORD()
                    data_ptr = ctypes.c_void_p()
                    opcode_type = ctypes.c_uint()
                    ret = _wlanapi.WlanQueryInterface(
                        handle, ctypes.byref(iface.InterfaceGuid), _WLAN_INTF_OPCODE_CURRENT_CONNECTION,
                        None, ctypes.byref(data_size), ctypes.byref(data_ptr), ctypes.byref(opcode_type))
                    if ret != 0 or not data_ptr.value:
                        continue  # this interface isn't connected - try the next one, if any
                    try:
                        conn = ctypes.cast(data_ptr, ctypes.POINTER(_WLAN_CONNECTION_ATTRIBUTES)).contents
                        quality = conn.wlanAssociationAttributes.wlanSignalQuality
                        if 0 <= quality <= 100:
                            return int(quality)
                    finally:
                        _wlanapi.WlanFreeMemory(data_ptr)
                return None
            finally:
                _wlanapi.WlanFreeMemory(iface_list_ptr)
        finally:
            _wlanapi.WlanCloseHandle(handle, None)
    except OSError:
        return None


def active_network_snapshot(prev):
    """One tick's worth of network state for the live dashboard/telemetry, plus the updated
    `prev` state to pass into the next call. Never called with locks/UI access - safe to run on
    the worker thread exactly like the CPU/GPU polling around it.

    Rate computation mirrors cpu_times()'s own old/now-delta-over-time pattern: down/up Mbps
    need TWO consecutive samples of the SAME adapter, so the first tick after startup - and any
    tick where the active adapter has just changed (e.g. Wi-Fi to Ethernet) - correctly reports
    down_mbps/up_mbps as None rather than dividing by a delta between two unrelated counters."""
    idx = default_route_interface_index()
    adapter = None
    if idx is not None:
        adapter = next((a for a in network_adapters() if a["index"] == idx), None)

    now_t = time.time()
    down_mbps = up_mbps = None
    if adapter is not None and prev.get("index") == idx and prev.get("time") is not None:
        dt = now_t - prev["time"]
        if dt > 0:
            down_mbps = max(0.0, (adapter["in_octets"] - prev["in_octets"]) * 8 / dt / 1e6)
            up_mbps = max(0.0, (adapter["out_octets"] - prev["out_octets"]) * 8 / dt / 1e6)

    new_prev = ({"index": idx, "in_octets": adapter["in_octets"], "out_octets": adapter["out_octets"], "time": now_t}
               if adapter is not None else {"index": None, "in_octets": None, "out_octets": None, "time": None})

    return {"adapter": adapter, "down_mbps": down_mbps, "up_mbps": up_mbps}, new_prev


NET_TOP_PROCESS_COUNT = 5  # how many rows the dashboard's TOP PROCESSES list shows


def process_network_rates(payload, prev):
    """Turns the bridge's cumulative per-process byte counters (network_processes.json) into
    live Mbps, the same delta-over-time approach active_network_snapshot() already uses for the
    whole adapter. A PID absent from `prev` - the first tick after startup, a genuinely new
    process, or the bridge having just restarted and reset its own accumulator - correctly
    reports rates as None rather than fabricating a spike against an unrelated baseline. A
    counter that appears to DECREASE (bridge restart mid-session) also reports None, not a
    clamped 0.0: unlike a single adapter's rare counter wrap, a per-process table reset is a
    real gap in the measurement window, and 0.0 would misrepresent it as "confirmed no
    traffic" rather than "unknown for this interval"."""
    now_t = time.time()
    processes = payload.get("processes") or []
    rates = []
    new_prev = {}
    for proc in processes:
        pid = proc.get("pid")
        if pid is None:
            continue
        bytes_in = proc.get("bytes_in") or 0
        bytes_out = proc.get("bytes_out") or 0
        new_prev[pid] = {"bytes_in": bytes_in, "bytes_out": bytes_out, "time": now_t}
        prior = prev.get(pid)
        down_mbps = up_mbps = None
        if prior is not None:
            dt = now_t - prior["time"]
            if dt > 0 and bytes_in >= prior["bytes_in"] and bytes_out >= prior["bytes_out"]:
                down_mbps = (bytes_in - prior["bytes_in"]) * 8 / dt / 1e6
                up_mbps = (bytes_out - prior["bytes_out"]) * 8 / dt / 1e6
        rates.append({
            "pid": pid, "name": proc.get("name"),
            "bytes_in": bytes_in, "bytes_out": bytes_out,
            "down_mbps": down_mbps, "up_mbps": up_mbps,
        })
    return rates, new_prev


# --- v1.1 Phase 3 - Connection Intelligence -------------------------------------------------
# Who is connected to what, right now: every real TCP connection and bound UDP endpoint, with
# its owning process. Fully unprivileged (GetExtendedTcpTable/GetExtendedUdpTable, confirmed
# during Phase 2 research against 27 real TCP + 69 real UDP rows on this machine) - no bridge
# involvement at all, unlike Phase 2's byte counters. Local/remote IP:port and connection state
# only - never packet content, matching every other layer's "measuring traffic, not spying" rule.
_iphlpapi_conn = ctypes.WinDLL("iphlpapi", use_last_error=True)
_AF_INET = 2
_TCP_TABLE_OWNER_PID_ALL = 5
_UDP_TABLE_OWNER_PID = 1
_ERROR_INSUFFICIENT_BUFFER = 122

_TCP_STATE_NAMES = {
    1: "CLOSED", 2: "LISTEN", 3: "SYN_SENT", 4: "SYN_RCVD", 5: "ESTABLISHED",
    6: "FIN_WAIT1", 7: "FIN_WAIT2", 8: "CLOSE_WAIT", 9: "CLOSING", 10: "LAST_ACK",
    11: "TIME_WAIT", 12: "DELETE_TCB",
}


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("dwState", ctypes.c_ulong), ("dwLocalAddr", ctypes.c_ulong),
                ("dwLocalPort", ctypes.c_ulong), ("dwRemoteAddr", ctypes.c_ulong),
                ("dwRemotePort", ctypes.c_ulong), ("dwOwningPid", ctypes.c_ulong)]


class _MIB_UDPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [("dwLocalAddr", ctypes.c_ulong), ("dwLocalPort", ctypes.c_ulong), ("dwOwningPid", ctypes.c_ulong)]


_GetExtendedTcpTable = _iphlpapi_conn.GetExtendedTcpTable
_GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                 ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
_GetExtendedTcpTable.restype = ctypes.c_ulong
_GetExtendedUdpTable = _iphlpapi_conn.GetExtendedUdpTable
_GetExtendedUdpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                 ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
_GetExtendedUdpTable.restype = ctypes.c_ulong


def _conn_ip_str(v):
    return ".".join(str((v >> (8 * i)) & 0xFF) for i in range(4))


def _conn_port(v):
    # Ports come back big-endian (network byte order) inside a little-endian ULONG.
    return ((v & 0xFF) << 8) | ((v >> 8) & 0xFF)


def _raw_tcp_connections():
    """[{pid, state (raw int), local, remote}, ...] for every real TCP connection - [] on any
    failure. No byte counts; this table doesn't have them (see Phase 2's research)."""
    size = wintypes.DWORD(0)
    ret = _GetExtendedTcpTable(None, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_PID_ALL, 0)
    if ret != _ERROR_INSUFFICIENT_BUFFER:
        return []
    buf = ctypes.create_string_buffer(size.value)
    if _GetExtendedTcpTable(buf, ctypes.byref(size), False, _AF_INET, _TCP_TABLE_OWNER_PID_ALL, 0) != 0:
        return []
    num_entries = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong))[0]
    rows = (_MIB_TCPROW_OWNER_PID * num_entries).from_buffer_copy(buf, ctypes.sizeof(ctypes.c_ulong))
    return [{"pid": r.dwOwningPid, "state": r.dwState,
             "local": f"{_conn_ip_str(r.dwLocalAddr)}:{_conn_port(r.dwLocalPort)}",
             "remote": f"{_conn_ip_str(r.dwRemoteAddr)}:{_conn_port(r.dwRemotePort)}"} for r in rows]


def _raw_udp_endpoints():
    """[{pid, local}, ...] for every bound UDP endpoint - [] on any failure. UDP is
    connectionless, so this table has no remote endpoint or connection state at all."""
    size = wintypes.DWORD(0)
    ret = _GetExtendedUdpTable(None, ctypes.byref(size), False, _AF_INET, _UDP_TABLE_OWNER_PID, 0)
    if ret != _ERROR_INSUFFICIENT_BUFFER:
        return []
    buf = ctypes.create_string_buffer(size.value)
    if _GetExtendedUdpTable(buf, ctypes.byref(size), False, _AF_INET, _UDP_TABLE_OWNER_PID, 0) != 0:
        return []
    num_entries = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong))[0]
    rows = (_MIB_UDPROW_OWNER_PID * num_entries).from_buffer_copy(buf, ctypes.sizeof(ctypes.c_ulong))
    return [{"pid": r.dwOwningPid, "local": f"{_conn_ip_str(r.dwLocalAddr)}:{_conn_port(r.dwLocalPort)}"} for r in rows]


def active_connections(name_cache=None):
    """Every real TCP connection and bound UDP endpoint right now, with its owning process name
    resolved via the same QueryFullProcessImageNameW path foreground_process()/cpu_top use.
    `name_cache` (optional, caller-owned {pid: name}) lets the worker thread avoid re-opening a
    process handle for a PID it already resolved this run - a PID whose process has since exited
    simply keeps its last-known name (a real historical fact, not a fabrication) rather than
    reverting to "pid:N"."""
    cache = name_cache if name_cache is not None else {}

    def resolve(pid):
        if pid in cache:
            return cache[pid]
        name = None
        if pid:
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if h:
                try:
                    name = _process_image_name(h)
                finally:
                    ctypes.windll.kernel32.CloseHandle(h)
        resolved = name or (f"pid:{pid}" if pid else "System")
        cache[pid] = resolved
        return resolved

    out = []
    for row in _raw_tcp_connections():
        out.append({"protocol": "TCP", "pid": row["pid"], "name": resolve(row["pid"]),
                    "local": row["local"], "remote": row["remote"],
                    "state": _TCP_STATE_NAMES.get(row["state"], f"UNKNOWN({row['state']})")})
    for row in _raw_udp_endpoints():
        out.append({"protocol": "UDP", "pid": row["pid"], "name": resolve(row["pid"]),
                    "local": row["local"], "remote": "-", "state": "-"})
    return out


# Sensor types the bridge/direct fallback collect for the full redesign.
_SENSOR_TYPES = "'Temperature','Fan','Power','Clock','Voltage','Control'"

BRIDGE_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ThermalWatch"
BRIDGE_SENSORS_PATH = BRIDGE_DIR / "sensors.json"
BRIDGE_STATUS_PATH = BRIDGE_DIR / "bridge_status.json"
BRIDGE_NETPROC_PATH = BRIDGE_DIR / "network_processes.json"
BRIDGE_FRESH_SECONDS = 10  # unchanged value - was already the freshness cutoff below, now named
BRIDGE_RECOVERY_MIN_INTERVAL_S = 45  # rate limit: don't re-trigger elevation/UAC more often than this


def network_processes():
    """Per-process cumulative network byte counters from the elevated bridge's ETW capture
    (v1.1 Phase 2), or an honest empty/inactive result if unavailable. Unlike lhm_sensors(),
    there is no WMI/direct fallback here - unprivileged code cannot read the Kernel-Network ETW
    provider at all (confirmed during Phase 2 research), so a bridge that doesn't support this
    yet, hasn't started capture, or has gone stale is a real capability gap, not just a missed
    fallback tier."""
    try:
        payload = json.loads(BRIDGE_NETPROC_PATH.read_text(encoding="utf-8-sig"))
        if time.time() - float(payload["timestamp"]) < BRIDGE_FRESH_SECONDS:
            return payload
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass
    return {"capture_active": False, "capture_error": None, "processes": []}


def lhm_sensors():
    # The elevated bridge keeps privileged driver access out of the UI process.
    try:
        payload = json.loads(BRIDGE_SENSORS_PATH.read_text(encoding="utf-8-sig"))
        if time.time() - float(payload["timestamp"]) < BRIDGE_FRESH_SECONDS:
            return payload.get("sensors", [])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        pass

    # Prefer the WMI feed when LibreHardwareMonitor exposes it.
    ps = (
        "Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -ErrorAction SilentlyContinue|"
        f"Where-Object {{$_.SensorType -in @({_SENSOR_TYPES})}}|Select Name,SensorType,Value,Parent|ConvertTo-Json -Compress"
    )
    raw = run_hidden(["powershell", "-NoProfile", "-Command", ps], 3)
    try:
        data = json.loads(raw); return data if isinstance(data, list) else [data]
    except Exception:
        pass

    # Newer portable releases do not always create the WMI namespace. Read the
    # bundled library directly instead. The parent app should be elevated so
    # LibreHardwareMonitor's low-level AMD/Intel sensor driver can be accessed.
    lib = _APP_DIR / "LibreHardwareMonitor" / "LibreHardwareMonitorLib.dll"
    if not lib.exists():
        return []
    lib_dir = str(lib.parent).replace("'", "''")
    dll = str(lib).replace("'", "''")
    direct = (
        f"[Environment]::CurrentDirectory='{lib_dir}';Add-Type -Path '{dll}';"
        "$c=New-Object LibreHardwareMonitor.Hardware.Computer;"
        "$c.IsCpuEnabled=$true;$c.IsGpuEnabled=$true;$c.IsMotherboardEnabled=$true;$c.IsStorageEnabled=$true;$c.Open();"
        "$a=@();foreach($h in $c.Hardware){$h.Update();foreach($sh in $h.SubHardware){$sh.Update()};"
        "foreach($dev in (@($h)+@($h.SubHardware))){foreach($s in $dev.Sensors){"
        f"if($s.SensorType -in @({_SENSOR_TYPES})){{$a+=[pscustomobject]@{{"
        "Name=$s.Name;SensorType=$s.SensorType.ToString();Value=$s.Value;Parent=($dev.HardwareType.ToString()+' '+$dev.Name)}}}}}}};"
        "$c.Close();$a|ConvertTo-Json -Compress"
    )
    raw = run_hidden(["powershell", "-NoProfile", "-Command", direct], 8)
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else ([data] if data else [])
    except Exception:
        return []


# --- bridge lifecycle / health -------------------------------------------------------------
# The bridge (sensor_bridge.ps1) is session-persistent by design: nothing in App ever stops it
# (see App.close()), so once it's alive, re-opening Thermal Watch never needs a fresh UAC prompt.
# This section only ever STARTS a replacement bridge (rate-limited) when Tier 1 has clearly gone
# stale; it never touches CPU/GPU/drive/RAM sensor interpretation.

def _process_exists(pid):
    """Cheap, unelevated liveness check via OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION).
    Works across the privilege boundary (querying a higher-integrity process's existence is
    allowed even though this process itself is unelevated), and needs no subprocess spawn."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def bridge_tier1_age():
    """Seconds since sensors.json was last written, or None if it's missing/unreadable.
    Independent of lhm_sensors()'s own tiered fallback - this checks Tier 1 specifically,
    even when Tier 2/3 are quietly covering for it."""
    try:
        payload = json.loads(BRIDGE_SENSORS_PATH.read_text(encoding="utf-8-sig"))
        return time.time() - float(payload["timestamp"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def bridge_status():
    """The bridge's own self-reported {pid, state, consecutive_errors, last_error, ...}, or
    None if bridge_status.json is missing/unreadable (e.g. an old bridge pre-dating this file)."""
    try:
        return json.loads(BRIDGE_STATUS_PATH.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def compute_bridge_health(tier1_age, status):
    """HEALTHY / STALE / ERROR / MISSING - pure function of the two health signals above,
    kept separate from RESTARTING/FALLBACK, which only App (the orchestrator) can know about."""
    if tier1_age is None:
        return "MISSING"
    if status and status.get("state") == "ERROR" and tier1_age >= BRIDGE_FRESH_SECONDS:
        return "ERROR"
    if tier1_age < BRIDGE_FRESH_SECONDS:
        return "HEALTHY"
    return "STALE"


def spawn_bridge_recovery():
    """Best-effort, non-blocking elevation attempt. If the user declines UAC or it otherwise
    fails, this simply doesn't start a new bridge - callers must keep working via Tier 2/3."""
    bridge_ps1 = str(_APP_DIR / "sensor_bridge.ps1")
    inner = (
        "Start-Process -FilePath 'powershell.exe' -ArgumentList "
        f"'-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{bridge_ps1}\"' "
        f"-WorkingDirectory '{_APP_DIR}' -WindowStyle Hidden -Verb RunAs"
    )
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-Command", inner],
                         creationflags=CREATE_NO_WINDOW,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False


def fmt_hms(seconds):
    seconds = int(seconds)
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_dur(seconds):
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_net_bytes(n):
    """Byte count -> human-readable string (binary units, matching Windows' own convention -
    Task Manager/ipconfig report network totals in GiB/MiB, not decimal GB/MB). None -> 'N/A',
    never a fabricated 0."""
    if n is None:
        return "N/A"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------
class MetricCard(tk.Frame):
    """Bar-style readout card: big value, status badge, peak/avg, threshold bar."""

    def __init__(self, parent, title, unit, color, threshold, scale_max, zone_fn=None, ticks=None, **kw):
        super().__init__(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER, **kw)
        self.color, self.threshold, self.scale_max, self.zone_fn = color, threshold, scale_max, zone_fn
        top = tk.Frame(self, bg=PANEL); top.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(top, text=title, bg=PANEL, fg=MUTED, font=(MONO, 9), anchor="w").pack(side="left")
        self.status = tk.Label(top, text="--", bg=PANEL, fg=MUTED, font=(MONO, 8))
        self.status.pack(side="right")

        row = tk.Frame(self, bg=PANEL); row.pack(fill="x", padx=16, pady=(6, 0))
        # Fixed width so e.g. "9" -> "100" doesn't shift the unit label next to it.
        self.value = tk.Label(row, text="--", bg=PANEL, fg=color, font=(MONO, 30, "bold"), width=4, anchor="w")
        self.value.pack(side="left")
        tk.Label(row, text=unit, bg=PANEL, fg=MUTED, font=(MONO, 12)).pack(side="left", padx=(6, 0), anchor="s", pady=(0, 4))
        self.peakavg = tk.Label(row, text="peak --\navg --", bg=PANEL, fg=MUTED, font=(SANS, 9), justify="right")
        self.peakavg.pack(side="right", anchor="e")

        bar_wrap = tk.Frame(self, bg=BORDER2, height=6); bar_wrap.pack(fill="x", padx=16, pady=(12, 2))
        bar_wrap.pack_propagate(False)
        self.fill = tk.Frame(bar_wrap, bg=color); self.fill.place(relx=0, rely=0, relwidth=0, relheight=1)
        for tick_val, tick_color in (ticks if ticks is not None else [(threshold, TEXT)]):
            tick_x = min(0.98, max(0.0, tick_val / scale_max))
            tk.Frame(bar_wrap, bg=tick_color, width=2).place(relx=tick_x, rely=0, relheight=1, anchor="nw")

        foot = tk.Frame(self, bg=PANEL); foot.pack(fill="x", padx=16, pady=(0, 12))
        tk.Label(foot, text="0", bg=PANEL, fg=DIM, font=(MONO, 7)).pack(side="left")
        self.foot_mid = tk.Label(foot, text="", bg=PANEL, fg=DIM, font=(MONO, 7))
        self.foot_mid.pack(side="left", expand=True)
        tk.Label(foot, text=str(int(scale_max)), bg=PANEL, fg=DIM, font=(MONO, 7)).pack(side="right")

    def set_footer(self, text):
        self.foot_mid.config(text=text)

    def update_value(self, value, sub_status, peak, avg, ratio=None):
        if value is None:
            self.value.config(text="N/A", fg=MUTED)
            self.status.config(text="NO SENSOR", fg=MUTED)
            self.fill.place(relwidth=0)
            self.peakavg.config(text="peak --\navg --")
            return
        if self.zone_fn:
            zone = self.zone_fn(value)
            color, status_text = zone["color"], zone["short"]
        else:
            over = value >= self.threshold
            color = ORANGE if over else self.color
            status_text = "OVER THRESHOLD" if over else sub_status
        # Never clamp the displayed reading itself - only the bar fill (visual, 0-1) is clamped.
        self.value.config(text=f"{value:.0f}", fg=color)
        self.status.config(text=status_text, fg=color)
        r = 0 if ratio is None else max(0.0, min(1.0, ratio))
        self.fill.config(bg=color)
        self.fill.place(relwidth=r)
        self.peakavg.config(text=f"peak {peak:.1f}\navg {avg:.1f}")


class HistoryChart(tk.Canvas):
    """Static items (grid/threshold lines+labels) are built once and only repositioned on an
    actual resize. Every poll only moves the CPU/GPU line + end-marker coordinates via
    coords()/itemconfig() on the SAME canvas item ids - no delete("all"), no recreation."""

    # Headroom above the 100°C emergency line so readings up to Tjmax aren't visually clipped.
    CHART_MAX = 110
    GRID_TEMPS = (0, 20, 40, 60, 80, 100)
    THRESHOLDS = ((CPU_YELLOW, AMBER, "80°C WARNING"), (CPU_ORANGE, ORANGE, "90°C CRITICAL"),
                 (CPU_RED, RED, "100°C EMERGENCY"))

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=PANEL, highlightthickness=0, height=230, **kw)
        self.points = []  # list of (epoch_seconds, cpu, gpu)
        self.range_seconds = RANGES[1][1]
        self._geom = None  # (w, h) the static items were last built for
        self._grid_ids = []  # (line_id, label_id) per GRID_TEMPS entry
        self._threshold_ids = []  # (line_id, label_id) per THRESHOLDS entry
        self._series_ids = {"cpu": None, "gpu": None}  # line item id per series
        self._oval_ids = {"cpu": None, "gpu": None}
        self._collecting_id = None
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_points(self, points):
        self.points = points
        self._redraw()

    def set_range(self, seconds):
        self.range_seconds = seconds
        self._redraw()

    def _plot_rect(self):
        w, h = self.winfo_width(), self.winfo_height()
        return w, h, 42, 14, w - 14, h - 24  # w, h, l, t, r, b

    def _build_static(self, l, t, r, b):
        for line_id, label_id in self._grid_ids:
            self.delete(line_id); self.delete(label_id)
        for line_id, label_id in self._threshold_ids:
            self.delete(line_id); self.delete(label_id)
        self._grid_ids, self._threshold_ids = [], []
        for temp in self.GRID_TEMPS:
            y = b - (temp / self.CHART_MAX) * (b - t)
            line_id = self.create_line(l, y, r, y, fill=BORDER2)
            label_id = self.create_text(l - 8, y, text=str(temp), fill=DIM, anchor="e", font=(MONO, 8))
            self._grid_ids.append((line_id, label_id))
        for temp, color, label in self.THRESHOLDS:
            ty = b - (temp / self.CHART_MAX) * (b - t)
            line_id = self.create_line(l, ty, r, ty, fill=color, dash=(6, 4))
            label_id = self.create_text(r - 4, ty - 8, text=label, fill=color, anchor="e", font=(MONO, 7))
            self._threshold_ids.append((line_id, label_id))
        # Series/markers must render above the static grid, and any leftover "collecting" text
        # above everything - re-raise since the static items were just recreated on top.
        for series_id in self._series_ids.values():
            if series_id is not None:
                self.tag_raise(series_id)
        for oval_id in self._oval_ids.values():
            if oval_id is not None:
                self.tag_raise(oval_id)

    def _redraw(self):
        w, h, l, t, r, b = self._plot_rect()
        if w < 20 or h < 20:
            return
        if self._geom != (w, h):
            self._build_static(l, t, r, b)
            self._geom = (w, h)

        now = time.time()
        start = now - self.range_seconds
        pts = [p for p in self.points if p[0] >= start] or self.points[-1:]

        if len(pts) < 2:
            for key in ("cpu", "gpu"):
                if self._series_ids[key] is not None:
                    self.delete(self._series_ids[key]); self._series_ids[key] = None
                if self._oval_ids[key] is not None:
                    self.delete(self._oval_ids[key]); self._oval_ids[key] = None
            if self._collecting_id is None:
                self._collecting_id = self.create_text((l + r) / 2, (t + b) / 2, text="COLLECTING SAMPLES...",
                                                        fill=DIM, font=(MONO, 9))
            else:
                self.coords(self._collecting_id, (l + r) / 2, (t + b) / 2)
            return
        if self._collecting_id is not None:
            self.delete(self._collecting_id)
            self._collecting_id = None

        def xy(ts, val):
            x = l + (ts - start) / self.range_seconds * (r - l)
            y = b - max(0, min(self.CHART_MAX, val)) / self.CHART_MAX * (b - t)
            return x, y

        for key, idx, color in (("cpu", 1, ORANGE), ("gpu", 2, GREEN)):
            coords = []
            for p in pts:
                v = p[idx]
                if v is None:
                    continue
                x, y = xy(p[0], v)
                coords += [x, y]
            if len(coords) < 4:
                if self._series_ids[key] is not None:
                    self.delete(self._series_ids[key]); self._series_ids[key] = None
                if self._oval_ids[key] is not None:
                    self.delete(self._oval_ids[key]); self._oval_ids[key] = None
                continue
            if self._series_ids[key] is None:
                self._series_ids[key] = self.create_line(*coords, fill=color, width=2, smooth=True)
            else:
                self.coords(self._series_ids[key], *coords)
            ex, ey = coords[-2], coords[-1]
            if self._oval_ids[key] is None:
                self._oval_ids[key] = self.create_oval(ex - 3, ey - 3, ex + 3, ey + 3, fill=color, outline="")
            else:
                self.coords(self._oval_ids[key], ex - 3, ey - 3, ex + 3, ey + 3)


def style_option_menu(om, padx=8, pady=3):
    """Dark-themes an OptionMenu AND its underlying dropdown Menu - a SEPARATE widget only
    reachable via om["menu"], since tk.OptionMenu is classic Tk, not ttk, so it renders as a
    native white/gray Windows control unless both pieces are configured explicitly. One shared
    helper so every OptionMenu in the app (History/Analytics/Sessions/Trends/Experiments/
    Timeline/Reports/SensorHistory) looks and behaves identically, instead of N slightly
    different hand-rolled configure() calls that could quietly drift apart."""
    om.configure(bg=PANEL, fg=TEXT, activebackground=BORDER2, activeforeground=TEXT, relief="flat",
                bd=0, highlightthickness=1, highlightbackground=BORDER, font=(MONO, 9), cursor="hand2",
                padx=padx, pady=pady)
    om["menu"].configure(bg=PANEL, fg=TEXT, activebackground=BORDER2, activeforeground=TEXT,
                        relief="flat", bd=0, font=(MONO, 9))
    return om


def style_scrollbar(sb):
    """Dark-themes a classic tk.Scrollbar - left alone it renders as the native light-gray
    Windows scrollbar (trough, thumb AND arrow buttons all in the system theme), the same kind
    of clash tk.OptionMenu has. One shared helper for the same reason style_option_menu is: every
    scrollbar in the app should look identical rather than accumulating N slightly different
    hand-rolled configure() calls."""
    sb.configure(bg=BORDER2, activebackground=MUTED, troughcolor=PANEL, highlightthickness=0,
                relief="flat", bd=0, elementborderwidth=0)
    return sb


def _colorref(hex_color):
    """'#rrggbb' -> a Win32 COLORREF DWORD (0x00BBGGRR - byte order reversed from the usual hex
    string), the format every DWMWA_*_COLOR attribute below expects."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return (b << 16) | (g << 8) | r


def apply_dark_titlebar(window):
    """Forces the native Windows title bar into dark mode via DWM, AND (Windows 11 build 22000+
    only) paints the caption/border/text with this app's own BG/TEXT colors exactly, rather than
    leaving Windows' generic dark gray sitting next to this app's near-black - the two read as
    visibly different shades otherwise. Tkinter has no built-in way to do either.

    window.winfo_id() is NOT the real top-level HWND on this Tk build. Two earlier versions of
    this both targeted it directly (one routed it through GetParent() first, on the assumption
    Tk returns a child window - GetParent() of a true top-level returns NULL, so that failed
    outright with E_HANDLE; the other called DwmSetWindowAttribute on winfo_id() itself, which
    returned S_OK from every single call yet the title bar visibly never changed). A direct probe
    settled it: winfo_id() and the actual OS-owned decorated frame Windows draws the title bar
    for are two DIFFERENT handles - DwmSetWindowAttribute happily "succeeds" against the wrong
    one. FindWindowW() by the window's own (always-unique, always-already-set) title text finds
    the real one reliably.

    Windows-only and best-effort throughout: every call's result is ignored (0 = success; a
    nonzero HRESULT on an older Windows 10 build - which has no per-window caption-color API -
    just means dark mode alone is what that OS can do). The title bar is purely cosmetic and
    must never be able to stop a window from opening.

    The real work is deferred via window.after() rather than run synchronously here, and retried
    a few times. window.update_idletasks() alone is enough to make FindWindowW succeed for a
    window created directly (e.g. the App root, or History opened straight from it) - confirmed
    by DwmGetWindowAttribute reading back the value that was just set - but NOT reliably enough
    for a window opened from inside another window's button-click handler (e.g. Timeline via
    History's sidebar): still inside that click's own event, update_idletasks() does not always
    push Tk far enough through its OWN realization cycle for the OS-level top-level window to
    exist yet. Giving control back to the event loop for a few real ticks is what actually fixes
    that; a fixed retry count with a short gap is cheap insurance against how far "a few ticks"
    needs to be on a loaded machine, and stops on the first successful match rather than always
    running the full count."""
    if os.name != "nt":
        return
    window.after(30, lambda: _apply_dark_titlebar_attempt(window, tries_left=5))


def _find_own_top_level(title):
    """Like FindWindowW, but scoped to a window owned by THIS process. Nothing enforces
    single-instance here - a user can launch Thermal Watch twice (nothing stops them, and
    pythonw.exe leaves no console window as a hint they already have one running), and plain
    FindWindowW(None, "Thermal Watch") then has two equally-real matches to choose from: it can
    silently hand back the OTHER process's window, whose title happens to be identical. That
    process's own call can just as easily grab this one's window right back - both windows
    exist, dark mode gets applied twice, just not to the window each call actually meant.
    Filtering candidates to this process's own PID (via GetWindowThreadProcessId) makes the
    match unambiguous no matter how many other Thermal Watch windows share the same title."""
    my_pid = os.getpid()
    found = []

    def _cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != my_pid:
            return True
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
        if buf.value == title:
            found.append(hwnd)
            return False  # stop enumerating - found it
        return True

    ctypes.windll.user32.EnumWindows(ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(_cb), 0)
    return found[0] if found else None


def _apply_dark_titlebar_attempt(window, tries_left):
    if tries_left <= 0 or not window.winfo_exists():
        return
    try:
        hwnd = _find_own_top_level(window.title())
        if not hwnd:
            window.after(30, lambda: _apply_dark_titlebar_attempt(window, tries_left - 1))
            return
        dark = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (20 on 20H1+/11, 19 on older 10)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(dark), ctypes.sizeof(dark)) == 0:
                break
        # Windows 11's default title-bar backdrop (Mica) is translucent and blends the desktop
        # wallpaper through it - a solid CAPTION_COLOR underneath still reads as a wallpaper-
        # tinted wash, not the flat near-black the rest of this window uses. DWMSBT_NONE (1)
        # turns that material off so the caption color below renders opaque.
        backdrop = ctypes.c_int(1)  # DWMSBT_NONE
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop))
        for attr, hex_color in ((35, BG), (34, BG), (36, TEXT)):  # CAPTION_, BORDER_, TEXT_COLOR
            value = wintypes.DWORD(_colorref(hex_color))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
        # DwmSetWindowAttribute returning S_OK only means the value was STORED - the compositor
        # does not necessarily repaint the already-realized non-client frame on its own. Without
        # this nudge every call above visibly does nothing (confirmed by screenshot: HRESULT 0
        # from every call, title bar still light) even though the API reports success. This is
        # the standard fix: SWP_FRAMECHANGED forces Windows to recalculate and redraw the frame;
        # the NOMOVE/NOSIZE/NOZORDER/NOACTIVATE flags make it a pure repaint with no side effect
        # on this window's position, size, stacking order or focus.
        SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
        ctypes.windll.user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, SWP_FLAGS)
    except OSError:
        pass


class ScrollFrame(tk.Frame):
    """Vertically-scrollable container. Pack real content into `.inner`, not `self`.
    Mousewheel is bound directly on the canvas (not bind_all), so Tk's normal
    under-the-cursor event routing scopes it correctly even when nested."""

    def __init__(self, parent, bg=PANEL, height=1, **kw):
        super().__init__(parent, bg=bg, **kw)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, height=height)
        vsb = style_scrollbar(tk.Scrollbar(self, orient="vertical", command=self.canvas.yview, width=10))
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self._window, width=e.width))
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


class Panel(tk.Frame):
    """A bordered card with a title, used for fans/voltages/disks/event log."""

    def __init__(self, parent, title, scrollable=False, **kw):
        super().__init__(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER, **kw)
        head = tk.Frame(self, bg=PANEL); head.pack(fill="x", padx=16, pady=(12, 8))
        tk.Label(head, text=title, bg=PANEL, fg=MUTED, font=(MONO, 9)).pack(side="left")
        self.head = head
        self.scroll = None
        if scrollable:
            self.scroll = ScrollFrame(self, bg=PANEL, height=1)
            self.scroll.pack(fill="both", expand=True, padx=16)
            self.body = self.scroll.inner
        else:
            self.body = tk.Frame(self, bg=PANEL); self.body.pack(fill="both", expand=True, padx=16)
        self.foot = tk.Frame(self, bg=PANEL); self.foot.pack(fill="x", padx=16, pady=(8, 12))

    def clear_body(self):
        for w in self.body.winfo_children():
            w.destroy()


# ---------------------------------------------------------------------------
# Per-app thermal analytics - a pure, standalone layer over the ALREADY-PERSISTED completed
# incident history (read_incidents_file()). Like the export layer, nothing here touches live
# sensor state, the incident lifecycle, or active_alerts - it only ever reads a list of
# incident dicts and computes numbers from them. No new incident database, no continuous
# sampling: this only ever runs on demand (view opens, filter/rank changes), never on the
# 2-second telemetry poll.
# ---------------------------------------------------------------------------
NOT_IDENTIFIED_KEY = "not identified"
NOT_IDENTIFIED_DISPLAY = "Not identified"

ANALYTICS_COMPONENT_KEYS = ("cpu", "gpu_core", "gpu_hotspot", "gpu_vram", "ram", "drive")
# context_peak keys worth their own aggregate section (power/load/memory - the temperatures are
# already covered per-component above via peak_value, not context_peak). net_down_mbps/
# net_up_mbps (v1.1 Phase 9, Cross-System Intelligence) have been captured here automatically
# since Phase 1 - _incident_touch() already folds every last_context key into context_peak
# generically, this was simply never surfaced. Purely observational, like every other context
# key here: "network activity peaked at X Mbps during this workload's thermal incidents" is a
# fact about what else was happening, never a claim that it caused anything.
ANALYTICS_CONTEXT_KEYS = ("cpu_power", "gpu_power", "cpu_load", "gpu_load", "mem_pct",
                          "net_down_mbps", "net_up_mbps")

RANK_MODES = [
    ("Most incidents", "most_incidents"), ("Most Critical incidents", "most_critical"),
    ("Highest CPU peak", "cpu_peak"), ("Highest GPU Core peak", "gpu_core_peak"),
    ("Highest GPU Hotspot peak", "gpu_hotspot_peak"), ("Highest GPU Memory peak", "gpu_vram_peak"),
    ("Longest thermal time", "longest_thermal_time"), ("Most recent", "most_recent"),
]


def canonical_workload_name(incident):
    """(canonical_key, display_name) for ONE incident. Prefers the existing (already
    conservative) dominant_workload field. Normalization is deliberately limited to trimming
    whitespace and case-folding for aggregation purposes ("Cyberpunk2077.exe" and
    "cyberpunk2077.exe" are the same key) - it never fuzzy-matches or guesses that two
    DIFFERENT executable names are related (python.exe never becomes a specific AI app just
    because it looks similar to something). Missing/absent/already-"Not identified" all map to
    one stable bucket, same word Thermal Watch already uses elsewhere for "no confident
    attribution". Delegates to the shared _normalize_workload_name() helper (also used by the
    session engine) so incidents and sessions can never disagree about workload identity."""
    return _normalize_workload_name(incident.get("dominant_workload"))


def group_incidents_by_workload(incidents):
    """{canonical_key: {"display_name": ..., "incidents": [...]}}. The display name kept for a
    key is whichever incident's raw casing was encountered FIRST in the input order - simple
    and deterministic, never a fuzzy/majority-vote guess."""
    groups = {}
    for inc in incidents:
        key, display = canonical_workload_name(inc)
        group = groups.setdefault(key, {"display_name": display, "incidents": []})
        group["incidents"].append(inc)
    return groups


def filter_incidents_by_range(incidents, window_seconds, now=None):
    """Mirrors HistoryWindow._apply_filters()'s own date-range semantics exactly (same
    end_timestamp-based cutoff, same treatment of window_seconds=None as "no filter") so
    Analytics and History never disagree about what "last 7 days" means. Reuses
    HistoryWindow.RANGE_SECONDS as the shared source of truth for the actual second counts."""
    if window_seconds is None:
        return list(incidents)
    now = now if now is not None else time.time()
    return [i for i in incidents if (now - i.get("end_timestamp", 0)) <= window_seconds]


def _avg(values):
    return sum(values) / len(values) if values else None


def _peak_component_stats(incidents, component):
    """count/avg/max of peak_value for incidents of ONE component - or None if this workload
    has no incidents with that component AND a real peak_value (never fabricated as 0)."""
    peaks = [i["peak_value"] for i in incidents
            if i.get("component") == component and i.get("peak_value") is not None]
    if not peaks:
        return None
    return {"count": len(peaks), "avg_peak": _avg(peaks), "max_peak": max(peaks)}


def _context_peak_stats(incidents, context_key):
    """count/avg/max of context_peak[context_key] across ALL of a workload's incidents
    (regardless of which component each incident itself was about) - or None if never
    captured. This is deliberately not restricted to gpu_* incidents: a CPU incident's own
    context snapshot can still contain a genuinely observed gpu_power reading, for example."""
    values = [i["context_peak"][context_key] for i in incidents
             if isinstance(i.get("context_peak"), dict) and i["context_peak"].get(context_key) is not None]
    if not values:
        return None
    return {"count": len(values), "avg_peak": _avg(values), "max_peak": max(values)}


def compute_workload_stats(key, display_name, incidents, now=None):
    """Pure function: one workload's incident list -> its full statistics structure. Every
    average/maximum is computed ONLY from incidents that actually contain that measurement -
    never converts a missing value to 0 (item 6). Duration aggregates only ever include
    incidents whose duration_exact is not explicitly False (item 7); incidents missing the
    field entirely predate the monitoring-gap system and are treated as exact, matching what
    that field's absence has always meant historically. Gap-affected incidents are counted
    separately (gap_incident_count) rather than silently folded into "total thermal time"."""
    now = now if now is not None else time.time()

    def within(seconds):
        return sum(1 for i in incidents if (now - i.get("end_timestamp", now)) <= seconds)

    exact = [i for i in incidents if i.get("duration_exact", True) is not False and i.get("duration_seconds") is not None]
    gap_count = sum(1 for i in incidents if i.get("duration_exact") is False)

    severity_counts = {}
    for i in incidents:
        z = i.get("max_zone")
        if z:
            severity_counts[z] = severity_counts.get(z, 0) + 1

    last_ts_candidates = [i.get("end_timestamp") or i.get("start_timestamp") for i in incidents
                          if i.get("end_timestamp") or i.get("start_timestamp")]
    longest = max(exact, key=lambda i: i["duration_seconds"], default=None)

    components = {c: _peak_component_stats(incidents, c) for c in ANALYTICS_COMPONENT_KEYS}
    overall_peaks = [c["max_peak"] for c in components.values() if c]

    return {
        "workload_key": key,
        "display_name": display_name,
        "total_incidents": len(incidents),
        "incidents_24h": within(86400),
        "incidents_7d": within(7 * 86400),
        "incidents_30d": within(30 * 86400),
        "exact_duration_incident_count": len(exact),
        "gap_incident_count": gap_count,
        "total_duration_seconds": sum(i["duration_seconds"] for i in exact) if exact else None,
        "avg_duration_seconds": _avg([i["duration_seconds"] for i in exact]) if exact else None,
        "longest_incident_seconds": longest["duration_seconds"] if longest else None,
        "longest_incident_id": longest["incident_id"] if longest else None,
        "last_incident_timestamp": max(last_ts_candidates) if last_ts_candidates else None,
        "severity_counts": severity_counts,
        "critical_count": severity_counts.get("RED", 0),
        "overall_max_peak": max(overall_peaks) if overall_peaks else None,
        "components": components,
        "context": {ck: _context_peak_stats(incidents, ck) for ck in ANALYTICS_CONTEXT_KEYS},
    }


def _rank_sort_key(value):
    """Missing (None) values sort strictly after every real measurement, regardless of rank
    mode or direction - never treated as (or compared against) zero (item 9)."""
    return (value is None, -(value if value is not None else 0))


_RANK_EXTRACTORS = {
    "most_incidents": lambda s: s["total_incidents"],
    "most_critical": lambda s: s["critical_count"],
    "cpu_peak": lambda s: (s["components"].get("cpu") or {}).get("max_peak"),
    "gpu_core_peak": lambda s: (s["components"].get("gpu_core") or {}).get("max_peak"),
    "gpu_hotspot_peak": lambda s: (s["components"].get("gpu_hotspot") or {}).get("max_peak"),
    "gpu_vram_peak": lambda s: (s["components"].get("gpu_vram") or {}).get("max_peak"),
    "longest_thermal_time": lambda s: s["total_duration_seconds"],
    "most_recent": lambda s: s["last_incident_timestamp"],
}


def rank_workloads(all_stats, mode="most_incidents"):
    """Pure function: list of compute_workload_stats() results -> the same list, sorted for
    the given rank mode. Unknown mode falls back to most_incidents rather than raising."""
    extractor = _RANK_EXTRACTORS.get(mode, _RANK_EXTRACTORS["most_incidents"])
    return sorted(all_stats, key=lambda s: _rank_sort_key(extractor(s)))


class HistoryWindow(tk.Toplevel):
    """Separate window for reviewing thermal incident history - deliberately NOT part of the
    main dashboard (opened on demand via the header's HISTORY button). Re-reads the incidents
    file fresh whenever it opens/refreshes, so it always reflects the complete, already-pruned
    on-disk record rather than App's capped in-memory cache.

    Layout is a sidebar-nav + filter-chips + summary-tiles + table + detail-panel design (see
    "History UI" reference). The sidebar's ANALYSIS/INTELLIGENCE items don't switch this
    window's own content the way a web nav would - each opens (or refocuses) its own real
    Toplevel, exactly as the old export-row buttons already did; this is only a visual
    reorganization of the same singleton-window methods, not a new navigation model. Every
    figure shown (summary tiles, sidebar coverage/records, per-incident threshold/excursion) is
    read from a store or an EXISTING helper - nothing here is a second, hand-computed copy of a
    number that already exists elsewhere in this file."""

    RANGE_SECONDS = {"All": None, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
    RANGE_ORDER = ("24h", "7d", "30d", "All")
    RANGE_LABELS = {"24h": "24H", "7d": "7D", "30d": "30D", "All": "ALL"}
    COMPONENTS = ["All", "cpu", "gpu_core", "gpu_hotspot", "gpu_vram", "ram", "drive", "network"]
    SEVERITIES = ["All", "YELLOW", "ORANGE", "RED"]
    # Reuses the app's existing zone-severity colors verbatim (SensorHistoryWindow.MARKER_COLORS,
    # the alert-badge coloring in update_data) rather than the darker orange the design reference
    # mockup used for "ORANGE" - this app's ORANGE constant is already the brand accent used in
    # the wordmark/buttons everywhere else, so reusing it here keeps this window visually
    # consistent with the rest of the running app instead of introducing a second, slightly-off
    # "orange".
    SEVERITY_COLOR = {"YELLOW": AMBER, "ORANGE": ORANGE, "RED": RED}
    SPARK_CHARS = "▁▂▃▄▅▆▇█"
    # component -> the REAL zone table its incidents are classified against, so the detail
    # panel's THRESHOLD/EXCURSION stats are looked up from the exact tables the live zone
    # engines use, never a second hand-copied threshold number.
    _ZONE_TABLE_BY_COMPONENT = {"cpu": CPU_ZONES, "gpu_core": GPU_CORE_ZONES, "gpu_hotspot": GPU_HOTSPOT_ZONES,
                                "gpu_vram": GPU_VRAM_ZONES, "ram": RAM_ZONES, "drive": DRIVE_ZONES}
    CURRENT_VIEW_LABEL = "Incidents"
    NAV_SECTIONS = (
        ("OVERVIEW", (("Incidents", "_reload"), ("Timeline", "open_timeline"), ("Sessions", "open_sessions"))),
        ("ANALYSIS", (("Analytics", "open_analytics"), ("Trends", "open_trends"),
                      ("Recommendations", "open_recommendations"), ("Fan Intelligence", "open_fan_intelligence"),
                      ("Maintenance", "open_maintenance"))),
        ("INTELLIGENCE", (("Reports", "open_reports"), ("Ask", "open_ask"), ("Experiments", "open_experiments"),
                          ("AI Settings", "open_ai_settings"))),
    )

    def __init__(self, master):
        super().__init__(master)
        self.title("Thermal Watch — History")
        self.geometry("1240x800")
        self.minsize(1000, 640)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self.filtered = []
        self._row_incident = {}
        self.analytics_window = None
        self.trends_window = None
        self.recommendations_window = None
        self.fan_window = None
        self.experiments_window = None
        self.timeline_window = None
        self.reports_window = None
        self.maintenance_window = None
        self.ask_window = None
        # ai_settings_window is NOT a plain instance attribute here - see the property just below
        # __init__. Phase 17 promoted this singleton slot to App itself (self.master.
        # ai_settings_window) so AskWindow's own "AI Settings" button and this window's menu entry
        # can never open two AISettingsWindow instances at once. The property keeps this exact
        # attribute name working unchanged for any existing caller (including
        # tools/verify_ai_settings.py, which reads/writes hw.ai_settings_window directly).
        self._build()
        self._reload()

    @property
    def ai_settings_window(self):
        return self.master.ai_settings_window

    @ai_settings_window.setter
    def ai_settings_window(self, value):
        self.master.ai_settings_window = value

    def _build(self):
        self.range_var = tk.StringVar(value="All")
        self.component_var = tk.StringVar(value="All")
        self.severity_var = tk.StringVar(value="All")
        self.workload_var = tk.StringVar()

        self._build_header()
        body = tk.Frame(self, bg=BG); body.pack(fill="both", expand=True)
        self._build_sidebar(body)
        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")
        main = tk.Frame(body, bg=BG); main.pack(side="left", fill="both", expand=True)
        self._build_filter_bar(main)
        self._build_summary_strip(main)
        self._build_table(main)
        self._build_detail_panel(main)

    def _build_header(self):
        top = tk.Frame(self, bg=BG); top.pack(fill="x", padx=20, pady=(14, 10))
        brand = tk.Frame(top, bg=BG); brand.pack(side="left")
        tk.Label(brand, text="THERMAL", bg=BG, fg=TEXT, font=(MONO, 13, "bold")).pack(side="left")
        tk.Label(brand, text=" WATCH", bg=BG, fg=ORANGE, font=(MONO, 13, "bold")).pack(side="left")
        tk.Label(top, text="  HISTORY", bg=BG, fg=DIM, font=(MONO, 9)).pack(side="left", padx=(10, 0))

        # Honest substitute for the reference mockup's "LIVE · 2s" indicator: this window is a
        # point-in-time snapshot, reloaded on demand - it does NOT poll, so claiming it's "live"
        # would be exactly the kind of fabricated status this app's own design rules forbid.
        self.refreshed_label = tk.Label(top, text="", bg=BG, fg=DIM, font=(MONO, 9))
        self.refreshed_label.pack(side="right", padx=(0, 14))
        tk.Button(top, text="← DASHBOARD", command=self._focus_dashboard, bg=BG, fg=MUTED, relief="flat",
                 font=(MONO, 9), padx=8, pady=4, cursor="hand2", highlightthickness=1,
                 highlightbackground=ALERT_BORDER).pack(side="right", padx=(0, 8))
        tk.Button(top, text="EXPORT", command=self._export_filtered, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right", padx=(0, 8))
        tk.Button(top, text="REFRESH", command=self._reload, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right", padx=(0, 8))
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")

    def _focus_dashboard(self):
        self.master.deiconify()
        self.master.lift()
        self.master.focus_force()

    def _build_sidebar(self, body):
        sidebar = tk.Frame(body, bg=BG, width=190); sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Frame(sidebar, bg=BG, height=14).pack()
        for title, items in self.NAV_SECTIONS:
            tk.Label(sidebar, text=title, bg=BG, fg=DIM, font=(MONO, 8), anchor="w").pack(
                fill="x", padx=20, pady=(10, 6))
            for label, method_name in items:
                active = label == self.CURRENT_VIEW_LABEL
                handler = getattr(self, method_name)
                row_bg = BORDER2 if active else BG
                row = tk.Frame(sidebar, bg=row_bg)
                row.pack(fill="x")
                tk.Frame(row, bg=ORANGE if active else row_bg, width=3).pack(side="left", fill="y")
                lbl = tk.Label(row, text=label, bg=row_bg, fg=TEXT if active else MUTED, font=(SANS, 10),
                               anchor="w", cursor="hand2")
                lbl.pack(side="left", fill="x", expand=True, padx=(9, 6), pady=6)
                for w in (row, lbl):
                    w.bind("<Button-1>", lambda _e, h=handler: h())
                if not active:
                    for w in (row, lbl):
                        w.bind("<Enter>", lambda _e, l=lbl: l.config(fg=TEXT))
                        w.bind("<Leave>", lambda _e, l=lbl: l.config(fg=MUTED))
        tk.Frame(sidebar, bg=BG).pack(fill="both", expand=True)
        self.sidebar_stats = tk.Label(sidebar, text="", bg=BG, fg=DIM, font=(MONO, 8), justify="left", anchor="w")
        self.sidebar_stats.pack(fill="x", padx=20, pady=(0, 16))

    def _update_sidebar_stats(self):
        """RECORDS/RETENTION are read straight off already-loaded state; COVERAGE reuses the
        same compute_coverage() every other window (Timeline, Reports) already calls - only on
        open/refresh, never on the live poll, matching this window's existing read pattern."""
        now = time.time()
        start = now - INCIDENT_RETENTION_DAYS * 86400
        buckets = read_telemetry_file(since_ts=start)
        _, _, coverage_pct = compute_coverage(buckets, INCIDENT_RETENTION_DAYS * 86400)
        self.sidebar_stats.config(text=(
            f"COVERAGE {coverage_pct:.1f}%\nRECORDS {len(self.all_incidents)}\n"
            f"RETENTION {INCIDENT_RETENTION_DAYS}D"))

    def _chip(self, parent, text, command):
        return tk.Button(parent, text=text, font=(MONO, 9), relief="flat", bd=0, padx=10, pady=5,
                         cursor="hand2", highlightthickness=1, command=command)

    def _build_filter_bar(self, main):
        bar = tk.Frame(main, bg=BG); bar.pack(fill="x", padx=20, pady=(16, 10))
        self._range_chips = {}
        for value in self.RANGE_ORDER:
            btn = self._chip(bar, self.RANGE_LABELS[value],
                             lambda v=value: (self.range_var.set(v), self._apply_filters()))
            btn.pack(side="left", padx=(0, 4))
            self._range_chips[value] = btn
        tk.Frame(bar, bg=BORDER, width=1, height=16).pack(side="left", padx=8)
        self._sev_chips = {}
        for value in self.SEVERITIES:
            btn = self._chip(bar, value.upper(), lambda v=value: (self.severity_var.set(v), self._apply_filters()))
            btn.pack(side="left", padx=(0, 4))
            self._sev_chips[value] = btn

        tk.Label(bar, text="COMPONENT", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left", padx=(14, 4))
        om = tk.OptionMenu(bar, self.component_var, *self.COMPONENTS, command=lambda _v: self._apply_filters())
        style_option_menu(om).pack(side="left")

        entry = tk.Entry(bar, textvariable=self.workload_var, width=20, bg=PANEL, fg=TEXT,
                         insertbackground=TEXT, relief="flat")
        entry.pack(side="right")
        entry.bind("<KeyRelease>", lambda _e: self._apply_filters())
        tk.Label(bar, text="WORKLOAD", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="right", padx=(0, 6))

    def _restyle_chips(self):
        cur_range = self.range_var.get()
        for value, btn in self._range_chips.items():
            on = value == cur_range
            btn.configure(bg=BORDER2 if on else PANEL, fg=TEXT if on else DIM,
                         highlightbackground=MUTED if on else BORDER)
        cur_sev = self.severity_var.get()
        for value, btn in self._sev_chips.items():
            on = value == cur_sev
            accent = self.SEVERITY_COLOR.get(value, TEXT)
            btn.configure(bg=BORDER2 if on else PANEL, fg=accent if on else DIM,
                         highlightbackground=accent if on else BORDER)

    def _build_summary_strip(self, main):
        strip = tk.Frame(main, bg=BG); strip.pack(fill="x", padx=20, pady=(0, 10))
        self._summary_tiles = []
        for i, label in enumerate(("INCIDENTS 24H", "SHOWN / TOTAL", "PEAK CPU", "PEAK GPU HOTSPOT", "RED / CRITICAL")):
            tile = tk.Frame(strip, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            tile.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0))
            strip.grid_columnconfigure(i, weight=1)
            tk.Label(tile, text=label, bg=PANEL, fg=DIM, font=(MONO, 8), anchor="w").pack(
                fill="x", padx=12, pady=(8, 2))
            val = tk.Label(tile, text="—", bg=PANEL, fg=TEXT, font=(MONO, 16), anchor="w")
            val.pack(fill="x", padx=12, pady=(0, 8))
            self._summary_tiles.append(val)
        self.analytics_label = tk.Label(main, bg=BG, fg=DIM, font=(MONO, 9), justify="left",
                                        anchor="w", wraplength=1150)
        self.analytics_label.pack(fill="x", padx=20, pady=(0, 10))

    def _build_table(self, main):
        style = ttk.Style(self)
        # Windows' native ttk themes ("vista"/"xpnative") hook straight into uxtheme.dll and
        # ignore style.configure()'s background/fieldbackground entirely - the Treeview's own
        # rows render fine (bg is drawn per-cell), but the BLANK space below the last row (there
        # almost always is some, since the table fills its frame rather than being height-capped)
        # stays the OS's native white. "clam" is one of the always-bundled Tcl/Tk themes and
        # actually honors ttk style colors, so switching to it is what makes fieldbackground=PANEL
        # real instead of a no-op. ttk themes are process-global (one Tcl interpreter), so this
        # single call also fixes the identical latent issue in every other Thermal.Treeview window
        # (Analytics/Sessions/Timeline/Reports/Experiments/Maintenance) for the rest of this run.
        style.theme_use("clam")
        # clam's default Treeview.field element draws a beveled border using bordercolor/
        # lightcolor/darkcolor regardless of borderwidth=0 above (that only zeroes padding, not
        # the bevel itself) - lightcolor defaults to #eeebe7, near-white, which is exactly the
        # bright border seen around every Treeview until these three are pinned to match BORDER.
        style.configure("Thermal.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=24, borderwidth=0, font=(MONO, 9),
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Thermal.Treeview.Heading", background=BORDER, foreground=MUTED, relief="flat",
                        font=(MONO, 8))
        style.map("Thermal.Treeview", background=[("selected", BORDER2)], foreground=[("selected", TEXT)])

        table_frame = tk.Frame(main, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        table_frame.pack(fill="both", expand=True, padx=20)

        columns = ("when", "component", "severity", "peak", "duration", "workload", "excursion")
        headers = {"when": "TIME", "component": "COMPONENT", "severity": "SEVERITY", "peak": "PEAK",
                  "duration": "DURATION", "workload": "WORKLOAD", "excursion": "EXCURSION"}
        widths = {"when": 140, "component": 140, "severity": 95, "peak": 65, "duration": 85,
                 "workload": 210, "excursion": 150}
        anchors = {"peak": "e", "duration": "e", "excursion": "e"}
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", style="Thermal.Treeview")
        for c in columns:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor=anchors.get(c, "w"))
        for zone in ("YELLOW", "ORANGE", "RED"):
            self.tree.tag_configure(f"sev_{zone}", foreground=self.SEVERITY_COLOR[zone])
        vsb = style_scrollbar(tk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview, width=10))
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.table_footer = tk.Label(main, text="", bg=BG, fg=DIM, font=(MONO, 9), anchor="w")
        self.table_footer.pack(fill="x", padx=20, pady=(4, 10))

    def _build_detail_panel(self, main):
        detail = tk.Frame(main, bg=PANEL); detail.pack(fill="x", padx=20, pady=(0, 8))
        self.detail_accent = tk.Frame(detail, bg=BORDER, height=2)
        self.detail_accent.pack(fill="x")

        dh = tk.Frame(detail, bg=PANEL); dh.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(dh, text="INCIDENT DETAIL", bg=PANEL, fg=DIM, font=(MONO, 9)).pack(side="left")
        self.detail_title = tk.Label(dh, text="", bg=PANEL, fg=TEXT, font=(MONO, 9))
        self.detail_title.pack(side="left", padx=(12, 0))
        tk.Button(dh, text="EXPORT SELECTED", command=self._export_selected, bg=PANEL, fg=MUTED, relief="flat",
                 font=(MONO, 9), padx=8, pady=3, cursor="hand2", highlightthickness=1,
                 highlightbackground=BORDER).pack(side="right")
        tk.Button(dh, text="COPY SUMMARY", command=self._copy_summary, bg=PANEL, fg=MUTED, relief="flat",
                 font=(MONO, 9), padx=8, pady=3, cursor="hand2", highlightthickness=1,
                 highlightbackground=BORDER).pack(side="right", padx=(0, 8))

        stat_row = tk.Frame(detail, bg=PANEL); stat_row.pack(fill="x", padx=16, pady=(12, 0))
        self.detail_stats = []
        for i, label in enumerate(("WINDOW", "PEAK", "THRESHOLD", "EXCURSION", "DURATION", "SAMPLES")):
            cell = tk.Frame(stat_row, bg=PANEL)
            cell.grid(row=0, column=i, sticky="w", padx=(0 if i == 0 else 24, 0))
            tk.Label(cell, text=label, bg=PANEL, fg=DIM, font=(MONO, 8)).pack(anchor="w")
            val = tk.Label(cell, text="—", bg=PANEL, fg=TEXT, font=(MONO, 12))
            val.pack(anchor="w", pady=(3, 0))
            self.detail_stats.append(val)

        self.detail_text = tk.Label(detail, text="Select an incident above to see details.", bg=PANEL,
                                    fg=MUTED, font=(SANS, 10), justify="left", anchor="nw", wraplength=1160)
        self.detail_text.pack(fill="x", padx=16, pady=(12, 4), anchor="w")
        self.detail_chart = tk.Canvas(detail, bg=PANEL, height=100, highlightthickness=0)
        self.detail_chart.pack(fill="x", padx=16, pady=(0, 12))

        self.export_status_label = tk.Label(main, text="", bg=BG, fg=DIM, font=(MONO, 9), anchor="w")
        self.export_status_label.pack(fill="x", padx=20, pady=(0, 14))

    def open_sessions(self):
        """Sessions live under Application Analytics (item 14) - opens/reuses Analytics and
        asks IT to open Sessions, the exact same path TimelineWindow's session markers already
        use (_on_marker_click), never a second way to reach the same window."""
        self.open_analytics()
        self.analytics_window.open_sessions()
        sw = self.analytics_window.sessions_window
        if sw is not None and sw.winfo_exists():
            sw.lift()
            sw.focus_force()

    def open_analytics(self):
        if self.analytics_window is not None and self.analytics_window.winfo_exists():
            self.analytics_window.lift()
            self.analytics_window.focus_force()
            return
        self.analytics_window = AnalyticsWindow(self.master)

    def open_trends(self):
        if self.trends_window is not None and self.trends_window.winfo_exists():
            self.trends_window.lift()
            self.trends_window.focus_force()
            return
        self.trends_window = TrendsWindow(self.master)

    def open_recommendations(self):
        if self.recommendations_window is not None and self.recommendations_window.winfo_exists():
            self.recommendations_window.lift()
            self.recommendations_window.focus_force()
            return
        self.recommendations_window = RecommendationsWindow(self.master)

    def open_fan_intelligence(self):
        if self.fan_window is not None and self.fan_window.winfo_exists():
            self.fan_window.lift()
            self.fan_window.focus_force()
            return
        self.fan_window = FanIntelligenceWindow(self.master)

    def open_experiments(self):
        if self.experiments_window is not None and self.experiments_window.winfo_exists():
            self.experiments_window.lift()
            self.experiments_window.focus_force()
            return
        self.experiments_window = ExperimentsWindow(self.master)

    def open_timeline(self):
        if self.timeline_window is not None and self.timeline_window.winfo_exists():
            self.timeline_window.lift()
            self.timeline_window.focus_force()
            return
        self.timeline_window = TimelineWindow(self.master)

    def open_reports(self):
        if self.reports_window is not None and self.reports_window.winfo_exists():
            self.reports_window.lift()
            self.reports_window.focus_force()
            return
        self.reports_window = ReportsWindow(self.master)

    def open_maintenance(self):
        if self.maintenance_window is not None and self.maintenance_window.winfo_exists():
            self.maintenance_window.lift()
            self.maintenance_window.focus_force()
            return
        self.maintenance_window = MaintenanceWindow(self.master)

    def open_ask(self):
        if self.ask_window is not None and self.ask_window.winfo_exists():
            self.ask_window.lift()
            self.ask_window.focus_force()
            return
        self.ask_window = AskWindow(self.master)

    def open_ai_settings(self):
        if self.ai_settings_window is not None and self.ai_settings_window.winfo_exists():
            self.ai_settings_window.lift()
            self.ai_settings_window.focus_force()
            return
        self.ai_settings_window = AISettingsWindow(self.master)

    def _reload(self):
        self.all_incidents = read_incidents_file()
        self.refreshed_label.config(text=f"REFRESHED {datetime.now().strftime('%H:%M:%S')}")
        self._update_sidebar_stats()
        self._apply_filters()

    def _apply_filters(self):
        now = time.time()
        window = self.RANGE_SECONDS.get(self.range_var.get())
        comp = self.component_var.get()
        sev = self.severity_var.get()
        workload_q = self.workload_var.get().strip().lower()
        out = []
        for inc in self.all_incidents:
            if window is not None and (now - inc.get("end_timestamp", 0)) > window:
                continue
            if comp != "All" and inc.get("component") != comp:
                continue
            if sev != "All" and inc.get("max_zone") != sev:
                continue
            if workload_q and workload_q not in (inc.get("dominant_workload") or "").lower():
                continue
            out.append(inc)
        self.filtered = out
        self._restyle_chips()
        self._populate_table()
        self._populate_analytics()

    @classmethod
    def _text_sparkline(cls, samples, width=16):
        """Compact block-character sparkline built from an incident's REAL recorded samples -
        the text-cell equivalent of the detail panel's mini chart. Resamples onto `width`
        time-buckets by real-value blending, never interpolation: a bucket with no sample in
        range is left blank rather than guessed at (matches _draw_mini_chart's "never fabricate
        a missing reading" rule one level down, in a ttk.Treeview cell that can't host a
        canvas)."""
        valid = [(ts, v) for ts, v in (samples or []) if v is not None]
        if len(valid) < 2:
            return "—"
        times = [p[0] for p in valid]
        vmin, vmax = min(p[1] for p in valid), max(p[1] for p in valid)
        vrange = max(0.001, vmax - vmin)
        t0, t1 = times[0], times[-1]
        trange = max(0.001, t1 - t0)
        buckets = [None] * width
        for ts, v in valid:
            idx = min(width - 1, int((ts - t0) / trange * width))
            buckets[idx] = v if buckets[idx] is None else (buckets[idx] + v) / 2
        return "".join(" " if v is None else
                      cls.SPARK_CHARS[int((v - vmin) / vrange * (len(cls.SPARK_CHARS) - 1))]
                      for v in buckets)

    def _populate_table(self):
        self.tree.delete(*self.tree.get_children())
        self._row_incident = {}
        for i, inc in enumerate(self.filtered):
            iid = str(i)
            when = datetime.fromtimestamp(inc["start_timestamp"]).strftime("%b %d %I:%M %p")
            comp_label = COMPONENT_LABELS.get(inc.get("component"), str(inc.get("component", "?")).upper())
            peak = inc.get("peak_value")
            peak_text = f"{peak:.0f}°C" if peak is not None else "N/A"
            dur = fmt_dur(inc["duration_seconds"]) if inc.get("duration_seconds") is not None else "N/A"
            workload = inc.get("dominant_workload") or "Not identified"
            sev = inc.get("max_zone", "?")
            spark = self._text_sparkline(inc.get("samples") or [])
            tags = (f"sev_{sev}",) if sev in self.SEVERITY_COLOR else ()
            self.tree.insert("", "end", iid=iid, tags=tags,
                             values=(when, comp_label, sev, peak_text, dur, workload, spark))
            self._row_incident[iid] = inc
        range_label = self.RANGE_LABELS.get(self.range_var.get(), self.range_var.get())
        self.table_footer.config(text=(
            f"SHOWING {len(self.filtered)} OF {len(self.all_incidents)} INCIDENTS · {range_label}"
            f"{'' if range_label == 'ALL' else ' WINDOW'}          SORTED BY TIME ↓ (newest first)"))

    def _populate_analytics(self):
        incs = self.filtered
        now = time.time()

        def count_within(seconds):
            return sum(1 for i in incs if (now - i.get("end_timestamp", now)) <= seconds)

        def peak_of(component):
            vals = [i["peak_value"] for i in incs if i.get("component") == component and i.get("peak_value") is not None]
            return max(vals) if vals else None

        by_workload = {}
        red_count = 0
        for i in incs:
            wl = i.get("dominant_workload") or "Not identified"
            by_workload[wl] = by_workload.get(wl, 0) + 1
            if i.get("max_zone") == "RED":
                red_count += 1
        top_workload = max(by_workload.items(), key=lambda kv: kv[1]) if by_workload else None
        top_workload_text = f"{top_workload[0]} ({top_workload[1]})" if top_workload else "N/A"

        cpu_peak, gpu_peak = peak_of("cpu"), peak_of("gpu_hotspot")
        tile_values = [
            (str(count_within(86400)), TEXT),
            (f"{len(incs)} / {len(self.all_incidents)}", TEXT),
            (f"{cpu_peak:.0f}°C" if cpu_peak is not None else "N/A", ORANGE if cpu_peak is not None else DIM),
            (f"{gpu_peak:.0f}°C" if gpu_peak is not None else "N/A", AMBER if gpu_peak is not None else DIM),
            (str(red_count), RED if red_count else GREEN),
        ]
        for lbl, (val, color) in zip(self._summary_tiles, tile_values):
            lbl.config(text=val, fg=color)

        gpu_core_peak, gpu_vram_peak = peak_of("gpu_core"), peak_of("gpu_vram")
        gpu_core_text = f"{gpu_core_peak:.0f}°C" if gpu_core_peak is not None else "N/A"
        gpu_vram_text = f"{gpu_vram_peak:.0f}°C" if gpu_vram_peak is not None else "N/A"
        self.analytics_label.config(text=(
            f"7d {count_within(7 * 86400)} · 30d {count_within(30 * 86400)}   |   "
            f"GPU CORE PEAK {gpu_core_text}   |   GPU MEM PEAK {gpu_vram_text}   |   "
            f"TOP WORKLOAD {top_workload_text}"
        ))

    def _on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        inc = self._row_incident.get(sel[0])
        if inc:
            self._show_detail(inc)
            self._set_export_status("")

    def _selected_incident(self):
        sel = self.tree.selection()
        return self._row_incident.get(sel[0]) if sel else None

    def select_incident_by_id(self, incident_id):
        """Public drill-down entry point (Sensor History's incident-overlay markers, item 9):
        resets every filter to unrestricted, reloads, and selects the matching row - so an
        incident is findable regardless of whatever filters this window happened to have set
        previously. Reuses the existing table/detail machinery unchanged; never re-implements
        an incident viewer (item 9: "do not duplicate incident records")."""
        self.range_var.set("All")
        self.component_var.set("All")
        self.severity_var.set("All")
        self.workload_var.set("")
        self._reload()
        iid = next((k for k, inc in self._row_incident.items() if inc.get("incident_id") == incident_id), None)
        if iid is None:
            return False
        self.tree.selection_set(iid)
        self.tree.see(iid)
        self._on_select(None)
        return True

    def _set_export_status(self, text):
        self.export_status_label.config(text=text)

    @staticmethod
    def _write_csv(path, incidents):
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
            w.writeheader()
            for inc in incidents:
                w.writerow(incident_to_csv_row(inc))

    def _current_filters_dict(self):
        return {"range": self.range_var.get(), "component": self.component_var.get(),
               "severity": self.severity_var.get(), "workload": self.workload_var.get()}

    def _export_filtered(self):
        """Exports exactly self.filtered - the same list _apply_filters() already produced and
        the table is already showing (item 6) - never a separately reimplemented filter pass."""
        if not self.filtered:
            self._set_export_status("No incidents match the current filters - nothing to export.")
            return
        path = filedialog.asksaveasfilename(
            title="Export Filtered Incidents",
            defaultextension=".csv",
            initialfile=f"ThermalWatch_Incidents_{datetime.now():%Y-%m-%d}.csv",
            filetypes=[("CSV files", "*.csv"), ("JSON files", "*.json")],
        )
        if not path:
            return  # user cancelled - no partial file, nothing was ever opened for writing yet
        try:
            if Path(path).suffix.lower() == ".json":
                payload = build_json_export(self.filtered, self._current_filters_dict())
                Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            else:
                self._write_csv(path, self.filtered)
        except OSError as e:
            self._set_export_status(f"Export failed: {e}")
            return
        self._set_export_status(f"Exported {len(self.filtered)} incident(s) to {Path(path).name}")

    def _export_selected(self):
        inc = self._selected_incident()
        if not inc:
            self._set_export_status("Select an incident in the table first.")
            return
        comp = sanitize_filename_part(COMPONENT_LABELS.get(inc.get("component"), inc.get("component")))
        ts = (datetime.fromtimestamp(inc["start_timestamp"]).strftime("%Y-%m-%d_%H%M%S")
             if inc.get("start_timestamp") else "unknown")
        path = filedialog.asksaveasfilename(
            title="Export Selected Incident",
            defaultextension=".json",
            initialfile=f"ThermalWatch_{comp}_{ts}.json",
            filetypes=[("JSON files", "*.json"), ("CSV files", "*.csv")],
        )
        if not path:
            return
        try:
            if Path(path).suffix.lower() == ".csv":
                self._write_csv(path, [inc])
            else:
                payload = build_json_export([inc], {"selected_incident_id": inc.get("incident_id")})
                Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError as e:
            self._set_export_status(f"Export failed: {e}")
            return
        self._set_export_status(f"Exported incident to {Path(path).name}")

    def _copy_summary(self):
        inc = self._selected_incident()
        if not inc:
            self._set_export_status("Select an incident in the table first.")
            return
        self.clipboard_clear()
        self.clipboard_append(build_incident_summary(inc))
        self._set_export_status("Summary copied to clipboard.")

    @classmethod
    def _incident_threshold(cls, inc):
        """The real zone floor this incident's OWN starting_zone crossed, looked up from the
        exact same threshold tables the live zone engines use (_ZONE_TABLE_BY_COMPONENT) -
        never a second, hand-copied number. None if the component/zone can't be matched (e.g.
        an old/minimal-schema incident)."""
        table = cls._ZONE_TABLE_BY_COMPONENT.get(inc.get("component"))
        zone = inc.get("starting_zone")
        if not table or not zone:
            return None
        for entry in table:
            if entry[1] == zone:
                return entry[0]
        return None

    def _show_detail(self, inc):
        comp_label = COMPONENT_LABELS.get(inc.get("component"), str(inc.get("component", "?")).upper())
        start = datetime.fromtimestamp(inc["start_timestamp"]).strftime("%I:%M:%S %p") if inc.get("start_timestamp") else "N/A"
        end = datetime.fromtimestamp(inc["end_timestamp"]).strftime("%I:%M:%S %p") if inc.get("end_timestamp") else "N/A"
        dur = fmt_dur(inc["duration_seconds"]) if inc.get("duration_seconds") is not None else "N/A"
        peak_val = inc.get("peak_value")
        peak = f"{peak_val:.0f}°C" if peak_val is not None else "N/A"
        fg = inc.get("foreground_process") or "N/A"
        fg_title = inc.get("foreground_title")

        sev = inc.get("max_zone") or "GREEN"
        accent = self.SEVERITY_COLOR.get(sev, MUTED)
        self.detail_accent.config(bg=accent)
        when = datetime.fromtimestamp(inc["start_timestamp"]).strftime("%b %d %H:%M") if inc.get("start_timestamp") else "N/A"
        self.detail_title.config(text=f"{when} · {comp_label} · {peak} PEAK", fg=accent)

        threshold = self._incident_threshold(inc)
        excursion = f"+{peak_val - threshold:.0f}°C" if peak_val is not None and threshold is not None else "N/A"
        stat_values = [
            f"{start} → {end}", peak,
            f"{threshold:.0f}°C" if threshold is not None else "N/A",
            excursion, dur, str(len(inc.get("samples") or [])),
        ]
        for lbl, val in zip(self.detail_stats, stat_values):
            lbl.config(text=val)

        lines = [
            f"THERMAL INCIDENT — {comp_label.upper()}",
            "",
            f"Start: {start}      End: {end}      Duration: {dur}",
            f"Peak: {peak}      Maximum state: {inc.get('max_zone', 'N/A')}      "
            f"Starting state: {inc.get('starting_zone', 'N/A')}",
            f"Dominant workload: {inc.get('dominant_workload') or 'Not identified'}",
            f"Foreground at start: {fg}" + (f" — {fg_title}" if fg_title else ""),
        ]
        # Minimal additions for an incident that spans a Thermal Watch restart (item 10) - a
        # normal incident has no monitoring_gaps and looks exactly as it always has.
        gaps = inc.get("monitoring_gaps") or []
        if gaps:
            total_gap = inc.get("monitoring_gap_seconds") or sum(g.get("gap_seconds", 0) for g in gaps)
            lines.append(f"Monitoring gap: {fmt_dur(total_gap)} (Thermal Watch was closed/restarted during this incident)")
            if inc.get("recovery_during_monitoring_gap"):
                lines.append("Recovery occurred while monitoring was offline — exact recovery time/value unknown")
            elif inc.get("close_reason") == "sensor_unavailable":
                lines.append("Sensor was no longer available when monitoring resumed")
            if inc.get("duration_exact") is False:
                monitored = inc.get("monitored_duration_seconds")
                lines.append(f"Duration shown is an upper bound, not an exact measurement"
                            + (f" (confirmed hot for at least {fmt_dur(monitored)})" if monitored is not None else ""))
        ctx = inc.get("context_peak") or {}
        ctx_fields = [
            ("cpu_temp", "CPU peak", "°C"), ("gpu_core_temp", "GPU Core peak", "°C"),
            ("gpu_hotspot_temp", "GPU Hotspot peak", "°C"), ("gpu_vram_temp", "Memory Junction peak", "°C"),
            ("cpu_power", "CPU power peak", "W"), ("gpu_power", "GPU power peak", "W"),
            ("cpu_load", "CPU load peak", "%"), ("gpu_load", "GPU load peak", "%"), ("mem_pct", "Memory peak", "%"),
        ]
        ctx_line = "   ·   ".join(
            f"{label}: {ctx[k]:.0f}{unit}" if ctx.get(k) is not None else f"{label}: N/A"
            for k, label, unit in ctx_fields
        )
        # v1.1 Phase 9 - purely observational context, same as every field above: what network
        # activity peaked at while this thermal incident was happening, never a claim it
        # contributed to it. Appended as its own segment (not merged into ctx_fields above) so
        # every pre-existing field keeps its exact original "label: N/A" behavior untouched -
        # network is the only one of these that's genuinely new since Phase 1 and can be
        # legitimately absent (an older incident predating that capture, or a network blackout),
        # so it's the only one that omits itself instead of showing a placeholder.
        net_ctx_fields = [("net_down_mbps", "Network down peak", " Mbps"), ("net_up_mbps", "Network up peak", " Mbps")]
        net_ctx_parts = [f"{label}: {ctx[k]:.1f}{unit}" for k, label, unit in net_ctx_fields if ctx.get(k) is not None]
        if net_ctx_parts:
            ctx_line += "   ·   " + "   ·   ".join(net_ctx_parts)
        lines.append("")
        lines.append(ctx_line)
        self.detail_text.config(text="\n".join(lines))
        self._draw_mini_chart(inc.get("samples") or [])

    # A gap this much larger than the normal ~2s incident-sample interval means Thermal Watch
    # was offline for that stretch, not just a slightly slow poll - draw it as a break, never
    # as an interpolated (fabricated) line segment across it.
    CHART_GAP_THRESHOLD_S = 10

    def _draw_mini_chart(self, samples):
        c = self.detail_chart
        c.delete("all")
        c.update_idletasks()
        w, h = max(c.winfo_width(), 200), 100
        valid = [(ts, v) for ts, v in samples if v is not None]
        if len(valid) < 2:
            c.create_text(w / 2, h / 2, text="No timeline samples captured for this incident.",
                          fill=DIM, font=(MONO, 9))
            return
        l, t, r, b = 36, 10, w - 10, h - 10
        times = [p[0] for p in valid]
        vals = [p[1] for p in valid]
        t0, t1 = times[0], times[-1]
        vmin, vmax = min(vals), max(vals)
        vrange = max(1.0, vmax - vmin)

        def xy(ts, v):
            x = l + (ts - t0) / max(1.0, (t1 - t0)) * (r - l)
            y = b - (v - vmin) / vrange * (b - t)
            return x, y

        # Split into separate runs at any gap >= CHART_GAP_THRESHOLD_S so a monitoring-offline
        # period never gets drawn as a continuous (implicitly measured) line.
        runs, current = [], [valid[0]]
        for prev, cur in zip(valid, valid[1:]):
            if cur[0] - prev[0] >= self.CHART_GAP_THRESHOLD_S:
                runs.append(current)
                current = []
            current.append(cur)
        runs.append(current)

        for run in runs:
            if len(run) < 2:
                if run:
                    x, y = xy(*run[0])
                    c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=ORANGE, outline="")
                continue
            coords = []
            for ts, v in run:
                x, y = xy(ts, v)
                coords += [x, y]
            c.create_line(*coords, fill=ORANGE, width=2, smooth=True)

        # Mark each break with a small dashed vertical + label so the gap reads as "no data
        # here", not as a rendering glitch.
        for run_before, run_after in zip(runs, runs[1:]):
            x1, _ = xy(*run_before[-1])
            x2, _ = xy(*run_after[0])
            xm = (x1 + x2) / 2
            c.create_line(xm, t, xm, b, fill=DIM, dash=(2, 3))
            c.create_text(xm, (t + b) / 2, text="GAP", fill=DIM, font=(MONO, 7), angle=90)

        c.create_text(l - 4, t, text=f"{vmax:.0f}", fill=DIM, font=(MONO, 7), anchor="ne")
        c.create_text(l - 4, b, text=f"{vmin:.0f}", fill=DIM, font=(MONO, 7), anchor="se")


class AnalyticsWindow(tk.Toplevel):
    """Separate window (opened from History, not the main dashboard) ranking workloads by
    their associated thermal incident history. Purely on-demand: recomputes only when opened
    or when its range/rank controls change - never on the live telemetry poll."""

    _LABEL_TO_MODE = {label: mode for label, mode in RANK_MODES}

    def __init__(self, app):
        super().__init__(app)
        self.app = app  # for VIEW INCIDENTS -> reuses the existing History window/filters
        self.title("Thermal Watch — Application Analytics")
        self.geometry("1080x680")
        self.minsize(860, 560)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self.all_stats = []
        self._row_stats = {}
        self._selected_stats = None
        self.sessions_window = None
        self._build()
        self._recompute()

    def _build(self):
        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(bar, text="RANGE", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.range_var = tk.StringVar(value="All")
        style_option_menu(tk.OptionMenu(bar, self.range_var, *list(HistoryWindow.RANGE_SECONDS),
                                       command=lambda _v: self._recompute())).pack(side="left", padx=(4, 14))

        tk.Label(bar, text="RANK BY", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.rank_var = tk.StringVar(value=RANK_MODES[0][0])
        style_option_menu(tk.OptionMenu(bar, self.rank_var, *[label for label, _mode in RANK_MODES],
                                       command=lambda _v: self._recompute())).pack(side="left", padx=(4, 14))

        tk.Button(bar, text="REFRESH", command=self._recompute, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right")

        self.status_label = tk.Label(self, bg=BG, fg=DIM, font=(MONO, 9), anchor="w")
        self.status_label.pack(fill="x", padx=16, pady=(0, 8))

        # Same Treeview styling as History - idempotent to configure again even if History
        # already registered it, so Analytics doesn't depend on being opened after History.
        style = ttk.Style(self)
        style.theme_use(style.theme_use())
        # clam's default Treeview.field element draws a beveled border using bordercolor/
        # lightcolor/darkcolor regardless of borderwidth=0 above (that only zeroes padding, not
        # the bevel itself) - lightcolor defaults to #eeebe7, near-white, which is exactly the
        # bright border seen around every Treeview until these three are pinned to match BORDER.
        style.configure("Thermal.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=24, borderwidth=0, font=(MONO, 9),
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Thermal.Treeview.Heading", background=BORDER, foreground=MUTED, relief="flat",
                        font=(MONO, 8))
        style.map("Thermal.Treeview", background=[("selected", BORDER2)], foreground=[("selected", TEXT)])

        body = tk.Frame(self, bg=BG); body.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        body.grid_columnconfigure(0, weight=1, minsize=420)
        body.grid_columnconfigure(1, weight=1, minsize=420)
        body.grid_rowconfigure(0, weight=1)

        left = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        tk.Label(left, text="WORKLOAD RANKING", bg=PANEL, fg=MUTED, font=(MONO, 9)).pack(
            anchor="w", padx=14, pady=(12, 6))
        columns = ("workload", "sessions", "incidents", "critical", "worst")
        headers = {"workload": "WORKLOAD", "sessions": "SESSIONS", "incidents": "INCIDENTS",
                  "critical": "CRITICAL", "worst": "WORST"}
        widths = {"workload": 150, "sessions": 80, "incidents": 80, "critical": 70, "worst": 70}
        self.tree = ttk.Treeview(left, columns=columns, show="headings", height=18, style="Thermal.Treeview")
        for c in columns:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        right = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        right.grid(row=0, column=1, sticky="nsew")
        self.detail_text = tk.Label(right, text="Select a workload to see its statistics.", bg=PANEL,
                                    fg=MUTED, font=(SANS, 10), justify="left", anchor="nw", wraplength=460)
        self.detail_text.pack(fill="both", expand=True, padx=14, pady=(12, 6), anchor="nw")
        btn_row = tk.Frame(right, bg=PANEL); btn_row.pack(anchor="w", padx=14, pady=(0, 14))
        tk.Button(btn_row, text="VIEW INCIDENTS", command=self._view_incidents, bg="#181b1f", fg=TEXT,
                 relief="flat", font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="left", padx=(0, 8))
        tk.Button(btn_row, text="WORKLOAD SESSIONS", command=self.open_sessions, bg="#181b1f", fg=TEXT,
                 relief="flat", font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="left")

    def _recompute(self):
        """The only place this window ever touches disk - reads the same, already-pruned
        completed-incident file History reads, PLUS the entirely separate completed-session
        file (item 16), never a second/competing incident store and never reinterpreting
        historical incidents as sessions - the two counts are computed independently and just
        displayed side by side. The ranking table lists the UNION of workloads with incidents
        AND workloads with only sessions (most sessions never produce an incident at all) -
        otherwise a workload's baseline could never be seen unless it happened to also misbehave
        thermally, defeating the point of having a baseline to compare against in the first
        place."""
        incidents = read_incidents_file()
        window_seconds = HistoryWindow.RANGE_SECONDS.get(self.range_var.get())
        incidents = filter_incidents_by_range(incidents, window_seconds)
        groups = group_incidents_by_workload(incidents)
        now = time.time()

        sessions = filter_incidents_by_range(read_sessions_file(), window_seconds, now)
        session_groups = group_sessions_by_workload(sessions)

        self.all_stats = []
        for key in set(groups) | set(session_groups):
            display_name = groups.get(key, {}).get("display_name") or session_groups.get(key, {}).get("display_name")
            workload_incidents = groups.get(key, {}).get("incidents", [])
            workload_sessions = session_groups.get(key, {}).get("sessions", [])
            stat = compute_workload_stats(key, display_name, workload_incidents, now)
            stat["recorded_sessions"] = len(workload_sessions)
            # Baseline learning (item: "what's normal for THIS workload on THIS machine") -
            # deliberately built from the SAME range-filtered session list as everything else in
            # this window, so "Baseline (30d)" and "Recorded sessions (30d)" always agree on what
            # they're counting; select "All" to see the baseline across every recorded session.
            stat["baseline"] = compute_workload_baseline(workload_sessions)
            # Anomaly detection rollup - "how many of this workload's own sessions stood out
            # against the others" - None (not 0) when there are too few sessions for any
            # leave-one-out baseline to ever be established, so it's never misread as "all
            # normal" when it actually means "can't tell yet".
            stat["anomalous_sessions"] = count_anomalous_sessions(workload_sessions)
            # Transparent health score (item: "derived from measured behavior, never gamified/
            # random") - the average of each of this workload's own sessions' scores, each
            # already shown with its full breakdown in SessionsWindow.
            stat["health"] = compute_workload_health_average(compute_workload_session_health_scores(workload_sessions))
            self.all_stats.append(stat)

        mode = self._LABEL_TO_MODE.get(self.rank_var.get(), "most_incidents")
        ranked = rank_workloads(self.all_stats, mode)
        self._populate_table(ranked)
        self.status_label.config(text=f"{len(incidents)} incident(s), {len(sessions)} session(s) across "
                                      f"{len(ranked)} workload(s) in range: {self.range_var.get()}")

    def _populate_table(self, ranked):
        self.tree.delete(*self.tree.get_children())
        self._row_stats = {}
        for i, s in enumerate(ranked):
            iid = str(i)
            worst = s.get("overall_max_peak")
            worst_text = f"{worst:.0f}°C" if worst is not None else "N/A"
            self.tree.insert("", "end", iid=iid, values=(s["display_name"], s.get("recorded_sessions", 0),
                                                          s["total_incidents"], s["critical_count"], worst_text))
            self._row_stats[iid] = s

    def _on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        s = self._row_stats.get(sel[0])
        if s:
            self._selected_stats = s
            self._show_detail(s)

    def _show_detail(self, s):
        def comp_block(key, title, unit="°C"):
            c = s["components"].get(key)
            if not c:
                return []
            return [title, f"  Incidents            {c['count']}",
                   f"  Avg incident peak    {c['avg_peak']:.0f}{unit}",
                   f"  Highest recorded     {c['max_peak']:.0f}{unit}", ""]

        def ctx_block(key, title, unit):
            c = s["context"].get(key)
            if not c:
                return []
            # Mbps commonly sits under 1 - see the BASELINE section's own precision note above.
            prec = 1 if unit == " Mbps" else 0
            return [title, f"  Avg incident peak    {c['avg_peak']:.{prec}f}{unit}",
                   f"  Highest recorded     {c['max_peak']:.{prec}f}{unit}", ""]

        last = s.get("last_incident_timestamp")
        anomalous = s.get("anomalous_sessions")
        anomalous_text = (f"{anomalous} of {s.get('recorded_sessions', 0)} (vs this workload's own baseline)"
                          if anomalous is not None else "not enough sessions yet")
        health = s.get("health")
        health_text = f"{health['score']:.0f}/100 — {health['label']} (avg of this workload's own sessions)" \
            if health else "no completed sessions yet"
        lines = [
            s["display_name"].upper(),
            "",
            f"Recorded sessions          {s.get('recorded_sessions', 0)}",
            f"Health score               {health_text}",
            f"Anomalous sessions         {anomalous_text}",
            f"Associated incidents      {s['total_incidents']}  "
            f"(24h {s['incidents_24h']} · 7d {s['incidents_7d']} · 30d {s['incidents_30d']})",
        ]
        if s["total_duration_seconds"] is not None:
            lines.append(f"Total thermal time (exact)  {fmt_dur(s['total_duration_seconds'])}")
        else:
            lines.append("Total thermal time (exact)  N/A")
        if s["gap_incident_count"]:
            lines.append(f"{s['gap_incident_count']} incident(s) contained monitoring gaps "
                        f"(excluded from exact thermal time)")
        lines.append(f"Last incident              "
                    f"{datetime.fromtimestamp(last).strftime('%b %d, %Y %I:%M %p') if last else 'N/A'}")
        lines.append("")
        sev = s.get("severity_counts") or {}
        lines.append("Severity distribution      " + (", ".join(f"{k}:{v}" for k, v in sev.items()) or "N/A"))
        lines.append(f"Critical incidents         {s['critical_count']}")
        lines.append("")

        baseline = s.get("baseline") or {}
        established = [(m["label"], m["unit"], m["stats"]) for m in baseline.values()
                       if m["stats"] and m["stats"]["established"]]
        pending = [(m["label"], m["stats"]) for m in baseline.values()
                  if m["stats"] and not m["stats"]["established"]]
        if established or pending:
            lines.append("BASELINE — what's normal for this workload on this machine")
            for label, unit, stat in established:
                # Mbps commonly sits under 1 for light/idle-ish workloads - 0 decimals would
                # render as an uninformative "0-0 Mbps" for exactly the sessions where the real
                # number matters most. Every other unit here (deg C, W) keeps its existing
                # 0-decimal formatting unchanged.
                prec = 1 if unit == " Mbps" else 0
                if stat["stddev"] is not None:
                    lo, hi = stat["mean"] - stat["stddev"], stat["mean"] + stat["stddev"]
                    lines.append(f"  {label:<36}{lo:.{prec}f}–{hi:.{prec}f}{unit}  (n={stat['count']})")
                else:
                    lines.append(f"  {label:<36}{stat['mean']:.{prec}f}{unit}  (n={stat['count']})")
            for label, stat in pending:
                lines.append(f"  {label:<36}not enough data yet ({stat['count']}/{BASELINE_MIN_SESSIONS} sessions)")
            lines.append("")

        for block in (comp_block("cpu", "CPU"), comp_block("gpu_core", "GPU CORE"),
                     comp_block("gpu_hotspot", "GPU HOTSPOT"), comp_block("gpu_vram", "GPU MEMORY"),
                     comp_block("ram", "RAM"), comp_block("drive", "DRIVE"),
                     ctx_block("cpu_power", "CPU POWER", "W"), ctx_block("gpu_power", "GPU POWER", "W"),
                     ctx_block("net_down_mbps", "NETWORK DOWNLOAD (during thermal incidents)", " Mbps"),
                     ctx_block("net_up_mbps", "NETWORK UPLOAD (during thermal incidents)", " Mbps")):
            lines.extend(block)
        self.detail_text.config(text="\n".join(lines))

    def _view_incidents(self):
        """Reuses History's own workload text filter (already substring-matches
        dominant_workload) instead of building a second incident viewer (item 11)."""
        if not self._selected_stats:
            self.status_label.config(text="Select a workload first.")
            return
        self.app.open_history()
        hw = self.app.history_window
        hw.workload_var.set(self._selected_stats["display_name"])
        hw._apply_filters()
        hw.lift()
        hw.focus_force()

    def open_sessions(self):
        """Opens the separate Workload Sessions window (item 14: reachable from Application
        Analytics, never the main dashboard), pre-filtered to the currently selected workload
        if one is selected - otherwise showing every recorded session."""
        initial_workload = self._selected_stats["display_name"] if self._selected_stats else None
        if self.sessions_window is not None and self.sessions_window.winfo_exists():
            if initial_workload:
                self.sessions_window.workload_var.set(initial_workload)
                self.sessions_window._reload()
            self.sessions_window.lift()
            self.sessions_window.focus_force()
            return
        self.sessions_window = SessionsWindow(self.app, initial_workload=initial_workload)


class SessionsWindow(tk.Toplevel):
    """Separate window (opened from Application Analytics, never the main dashboard - item 14)
    listing completed workload sessions. Re-reads SESSIONS_PATH fresh on open/refresh, exactly
    like HistoryWindow does for incidents - completely independent store, completely independent
    view."""

    RANGE_SECONDS = HistoryWindow.RANGE_SECONDS

    def __init__(self, app, initial_workload=None):
        super().__init__(app)
        self.app = app
        self.title("Thermal Watch — Workload Sessions")
        self.geometry("1040x680")
        self.minsize(820, 560)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self.all_sessions = []
        self.filtered = []
        self._row_session = {}
        self._build()
        if initial_workload:
            self.workload_var.set(initial_workload)
        self._reload()

    def _build(self):
        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(bar, text="RANGE", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.range_var = tk.StringVar(value="All")
        style_option_menu(tk.OptionMenu(bar, self.range_var, *list(self.RANGE_SECONDS),
                                       command=lambda _v: self._apply_filters())).pack(side="left", padx=(4, 14))

        tk.Label(bar, text="WORKLOAD", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.workload_var = tk.StringVar()
        entry = tk.Entry(bar, textvariable=self.workload_var, width=20, bg=PANEL, fg=TEXT,
                         insertbackground=TEXT, relief="flat")
        entry.pack(side="left", padx=(4, 14))
        entry.bind("<KeyRelease>", lambda _e: self._apply_filters())

        tk.Button(bar, text="REFRESH", command=self._reload, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right")

        self.status_label = tk.Label(self, bg=BG, fg=DIM, font=(MONO, 9), anchor="w")
        self.status_label.pack(fill="x", padx=16, pady=(0, 8))

        style = ttk.Style(self)
        style.theme_use(style.theme_use())
        # clam's default Treeview.field element draws a beveled border using bordercolor/
        # lightcolor/darkcolor regardless of borderwidth=0 above (that only zeroes padding, not
        # the bevel itself) - lightcolor defaults to #eeebe7, near-white, which is exactly the
        # bright border seen around every Treeview until these three are pinned to match BORDER.
        style.configure("Thermal.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=24, borderwidth=0, font=(MONO, 9),
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Thermal.Treeview.Heading", background=BORDER, foreground=MUTED, relief="flat",
                        font=(MONO, 8))
        style.map("Thermal.Treeview", background=[("selected", BORDER2)], foreground=[("selected", TEXT)])

        columns = ("when", "workload", "duration", "cpu_peak", "gpu_hotspot_peak", "incidents")
        headers = {"when": "DATE", "workload": "WORKLOAD", "duration": "DURATION", "cpu_peak": "CPU PEAK",
                  "gpu_hotspot_peak": "GPU HOTSPOT PEAK", "incidents": "INCIDENTS"}
        widths = {"when": 150, "workload": 220, "duration": 90, "cpu_peak": 90, "gpu_hotspot_peak": 130,
                 "incidents": 80}
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=14, style="Thermal.Treeview")
        for c in columns:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="both", expand=True, padx=16)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        detail_frame = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        detail_frame.pack(fill="x", padx=16, pady=14)
        self.detail_text = tk.Label(detail_frame, text="Select a session above to see details.", bg=PANEL,
                                    fg=MUTED, font=(SANS, 10), justify="left", anchor="nw", wraplength=980)
        self.detail_text.pack(fill="x", padx=14, pady=(10, 10), anchor="w")

    def _reload(self):
        self.all_sessions = read_sessions_file()
        self._apply_filters()

    def _apply_filters(self):
        now = time.time()
        window = self.RANGE_SECONDS.get(self.range_var.get())
        workload_q = self.workload_var.get().strip().lower()
        out = []
        for s in self.all_sessions:
            if window is not None and (now - s.get("end_timestamp", 0)) > window:
                continue
            if workload_q and workload_q not in (s.get("workload") or "").lower():
                continue
            out.append(s)
        self.filtered = out
        self._populate_table()

    def _populate_table(self):
        self.tree.delete(*self.tree.get_children())
        self._row_session = {}
        for i, s in enumerate(self.filtered):
            iid = str(i)
            when = (datetime.fromtimestamp(s["start_timestamp"]).strftime("%b %d %I:%M %p")
                   if s.get("start_timestamp") else "N/A")
            dur = fmt_dur(s["duration_seconds"]) if s.get("duration_seconds") is not None else "N/A"
            cpu_peak = (s.get("cpu") or {}).get("peak_temp")
            cpu_peak_text = f"{cpu_peak:.0f}°C" if cpu_peak is not None else "N/A"
            hot_peak = (s.get("gpu") or {}).get("peak_hotspot_temp")
            hot_peak_text = f"{hot_peak:.0f}°C" if hot_peak is not None else "N/A"
            self.tree.insert("", "end", iid=iid, values=(when, s.get("workload", "?"), dur, cpu_peak_text,
                                                          hot_peak_text, s.get("incident_count", 0)))
            self._row_session[iid] = s
        self.status_label.config(text=f"{len(self.filtered)} session(s)")

    def _on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        s = self._row_session.get(sel[0])
        if s:
            self._show_detail(s)

    def select_session_by_id(self, session_id):
        """Public drill-down entry point (Sensor History's session-band overlays, item 10):
        resets range/workload filters to unrestricted, reloads, and selects the matching row.
        Reuses the existing table/detail machinery unchanged."""
        self.range_var.set("All")
        self.workload_var.set("")
        self._reload()
        iid = next((k for k, s in self._row_session.items() if s.get("session_id") == session_id), None)
        if iid is None:
            return False
        self.tree.selection_set(iid)
        self.tree.see(iid)
        self._on_select(None)
        return True

    def _show_detail(self, s):
        def stat_line(label, value, unit=""):
            return f"{label:<24}{value:.0f}{unit}" if value is not None else None

        cpu, gpu, mem = s.get("cpu") or {}, s.get("gpu") or {}, s.get("memory") or {}
        start = (datetime.fromtimestamp(s["start_timestamp"]).strftime("%I:%M:%S %p")
                if s.get("start_timestamp") else "N/A")
        end = (datetime.fromtimestamp(s["end_timestamp"]).strftime("%I:%M:%S %p")
              if s.get("end_timestamp") else "N/A")
        dur = fmt_dur(s["duration_seconds"]) if s.get("duration_seconds") is not None else "N/A"

        lines = [
            "WORKLOAD SESSION", "", s.get("workload", "?"), "",
            f"{start} → {end}", f"Duration: {dur}" + ("" if s.get("duration_exact", True) else " (uncertain - ended during a monitoring gap)"),
            f"Observed PIDs: {', '.join(str(p) for p in s.get('observed_pids', [])) or 'N/A'}",
        ]
        if s.get("foreground_seconds") is not None:
            lines.append(f"Foreground time: {fmt_dur(s['foreground_seconds'])}")
        lines.append("")

        lines.append("CPU")
        for line in (stat_line("  Average temperature:", cpu.get("avg_temp"), "°C"),
                    stat_line("  Peak temperature:", cpu.get("peak_temp"), "°C"),
                    stat_line("  Average utilization:", cpu.get("avg_util"), "%"),
                    stat_line("  Peak utilization:", cpu.get("peak_util"), "%"),
                    stat_line("  Average power:", cpu.get("avg_power"), "W"),
                    stat_line("  Peak power:", cpu.get("peak_power"), "W")):
            if line:
                lines.append(line)
        lines.append("")

        lines.append("GPU")
        for line in (stat_line("  Average core:", gpu.get("avg_core_temp"), "°C"),
                    stat_line("  Peak core:", gpu.get("peak_core_temp"), "°C"),
                    stat_line("  Average hotspot:", gpu.get("avg_hotspot_temp"), "°C"),
                    stat_line("  Peak hotspot:", gpu.get("peak_hotspot_temp"), "°C"),
                    stat_line("  Average memory junction:", gpu.get("avg_vram_temp"), "°C"),
                    stat_line("  Peak memory junction:", gpu.get("peak_vram_temp"), "°C"),
                    stat_line("  Average utilization:", gpu.get("avg_util"), "%"),
                    stat_line("  Peak utilization:", gpu.get("peak_util"), "%"),
                    stat_line("  Average power:", gpu.get("avg_power"), "W"),
                    stat_line("  Peak power:", gpu.get("peak_power"), "W")):
            if line:
                lines.append(line)
        lines.append("")

        if mem.get("avg_pct") is not None or mem.get("peak_pct") is not None:
            lines.append("MEMORY")
            for line in (stat_line("  Average RAM usage:", mem.get("avg_pct"), "%"),
                        stat_line("  Peak RAM usage:", mem.get("peak_pct"), "%")):
                if line:
                    lines.append(line)
            lines.append("")

        # v1.1 Phase 5 - whole-active-adapter Mbps observed while this session was active (see
        # the "network" block's own comment in _finalize_session_record for the non-causal
        # framing). Header only appears when at least one real sample exists, same rule as MEMORY
        # above - an older session recorded before Phase 5 simply has no "network" key at all.
        net = s.get("network") or {}

        def net_line(label, value):
            return f"{label:<24}{value:.1f} Mbps" if value is not None else None

        if net.get("avg_down_mbps") is not None or net.get("peak_down_mbps") is not None:
            lines.append("NETWORK")
            for line in (net_line("  Average download:", net.get("avg_down_mbps")),
                        net_line("  Peak download:", net.get("peak_down_mbps")),
                        net_line("  Average upload:", net.get("avg_up_mbps")),
                        net_line("  Peak upload:", net.get("peak_up_mbps"))):
                if line:
                    lines.append(line)
            lines.append("")

        lines.append("THERMAL")
        lines.append(f"  Associated incidents:  {s.get('incident_count', 0)}")
        lines.append(f"  Maximum severity:      {s.get('max_incident_severity') or 'N/A'}")
        zone_time = s.get("zone_time") or {}
        zone_titles = {"cpu": "CPU", "gpu_core": "GPU Core", "gpu_hotspot": "GPU Hotspot", "gpu_vram": "GPU Memory"}
        for comp, title in zone_titles.items():
            times = zone_time.get(comp) or {}
            for zone_key in ("YELLOW", "ORANGE", "RED"):
                secs = times.get(zone_key, 0.0)
                if secs > 0:
                    lines.append(f"  Time in {title} {zone_key.title()}: {fmt_dur(secs)}")

        gaps = s.get("monitoring_gaps") or []
        if gaps:
            lines.append("")
            lines.append(f"  Monitoring gaps: {len(gaps)}")

        # Anomaly detection (baseline learning's direct follow-on): this session's own numbers
        # vs a baseline built from every OTHER completed session of the SAME workload - never
        # against itself, never claiming a cause, only "this session's own reading differed
        # notably from this workload's established pattern on this machine".
        workload_key = s.get("workload_key") or _normalize_workload_name(s.get("workload"))[0]
        others = [o for o in self.all_sessions
                 if o.get("workload_key") == workload_key and o.get("session_id") != s.get("session_id")]
        baseline = compute_workload_baseline(others)
        anomalies = evaluate_session_anomalies(s, baseline)
        unusual = [v for v in anomalies.values() if v["anomaly"]["unusual"]]
        has_established_metric = any(m["stats"] and m["stats"]["established"] for m in baseline.values())
        if unusual or has_established_metric:
            lines.append("")
            lines.append(f"VS BASELINE (compared with this workload's other {len(others)} session(s))")
            if unusual:
                for v in unusual:
                    a = v["anomaly"]
                    sign = "+" if a["delta"] >= 0 else ""
                    z_text = f", z={a['z_score']:.1f}" if a["z_score"] is not None else ""
                    prec = 1 if v["unit"] == " Mbps" else 0  # see the BASELINE section's own precision note
                    lines.append(f"  ⚠ UNUSUAL: {v['label']}  {v['current']:.{prec}f}{v['unit']} vs baseline "
                                f"{a['baseline_mean']:.{prec}f}{v['unit']}  ({sign}{a['delta']:.{prec}f}{v['unit']}{z_text})")
            else:
                lines.append("  No metric deviated notably from this workload's established baseline")

        # Cross-sensor diagnostics (one layer up from the VS BASELINE anomaly flags above): does
        # the PATTERN across two-or-more sensors together resemble a recognizable thermal-
        # behavior signature, not just "this one number is off". Session-scoped patterns use the
        # SAME leave-one-out `others` baseline; the trend pattern needs this workload's full
        # ordered history, so it's given `others + [s]` instead.
        workload_display = s.get("workload", "?")
        session_findings = run_session_diagnostics(s, others, workload_display)
        trend_findings = run_session_trend_diagnostics(others + [s], workload_display)
        diag_findings = session_findings + trend_findings
        if unusual or has_established_metric:
            lines.append("")
            lines.append("CROSS-SENSOR DIAGNOSTICS")
            if diag_findings:
                for finding in diag_findings:
                    lines.append("")
                    lines.extend(f"  {line}" for line in format_diagnostic_finding(finding))
            else:
                lines.append("  Sensor pattern inconclusive — no cross-sensor pattern found for this session")

        # Transparent health score - built entirely from what's already on this page (THERMAL's
        # zone time, VS BASELINE's anomalies, CROSS-SENSOR DIAGNOSTICS' own-session findings -
        # never the workload-level trend findings, see compute_session_health_score's docstring).
        health = compute_session_health_score(s, anomalies, session_findings)
        lines.append("")
        lines.extend(format_health_score(health))

        self.detail_text.config(text="\n".join(lines))


class TrendsWindow(tk.Toplevel):
    """TREND INTELLIGENCE - opened from History, not the main dashboard (same pattern as
    Analytics/Sessions). Two on-demand reports, purely on-demand (recomputes only on open/
    REFRESH/workload-selection-change, never the live telemetry poll): a machine-wide WEEK OVER
    WEEK digest (7d vs previous 7d), and a per-workload GPU COOLING — N DAY TREND (30-day
    trajectory) for whichever workload is picked from the OptionMenu."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Thermal Watch — Trend Intelligence")
        self.geometry("880x760")
        self.minsize(760, 620)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self.workload_var = tk.StringVar()
        self._workload_keys = {}  # display_name -> workload_key, for the OptionMenu lookup
        self._build()
        self._recompute()

    def _build(self):
        wow_panel = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        wow_panel.pack(fill="x", padx=16, pady=(14, 8))
        self.wow_text = tk.Label(wow_panel, text="", bg=PANEL, fg=TEXT, font=(SANS, 10),
                                 justify="left", anchor="nw", wraplength=820)
        self.wow_text.pack(fill="x", padx=14, pady=12, anchor="w")

        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=16, pady=(0, 8))
        tk.Label(bar, text="WORKLOAD", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.workload_menu = tk.OptionMenu(bar, self.workload_var, "")
        style_option_menu(self.workload_menu).pack(side="left", padx=(4, 14))
        tk.Button(bar, text="REFRESH", command=self._recompute, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right")

        trend_panel = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        trend_panel.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self.trend_text = tk.Label(trend_panel, text="", bg=PANEL, fg=TEXT, font=(SANS, 10),
                                   justify="left", anchor="nw", wraplength=820)
        self.trend_text.pack(fill="both", expand=True, padx=14, pady=12, anchor="nw")

    def _recompute(self):
        """The only place this window ever touches disk: one week-over-week report (its own
        telemetry/incident/session reads) plus a rebuild of the workload picker from the
        entirely-separate completed-session file, then one per-workload cooling-trend report for
        whichever workload ends up selected."""
        wow = compute_week_over_week_report()
        self.wow_text.config(text="\n".join(format_week_over_week_report(wow)))

        groups = group_sessions_by_workload(read_sessions_file())
        ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]["sessions"]))
        self._workload_keys = {group["display_name"]: key for key, group in ranked}
        display_names = list(self._workload_keys.keys())

        menu = self.workload_menu["menu"]
        menu.delete(0, "end")
        for name in display_names:
            menu.add_command(label=name, command=lambda n=name: self._on_workload_change(n))
        if display_names and self.workload_var.get() not in display_names:
            self.workload_var.set(display_names[0])
        self._recompute_trend_report()

    def _on_workload_change(self, name):
        self.workload_var.set(name)
        self._recompute_trend_report()

    def _recompute_trend_report(self):
        name = self.workload_var.get()
        key = self._workload_keys.get(name)
        if key is None:
            self.trend_text.config(text="No workloads with recorded sessions yet.")
            return
        report = compute_workload_cooling_trend_report(key, TREND_MONTH_LOOKBACK_DAYS)
        self.trend_text.config(text="\n".join(format_workload_cooling_trend_report(report)))


def _live_system_temp_moderate(app):
    """Whether the CURRENT live 'System' motherboard sensor reading is NOT itself elevated
    relative to its own established idle baseline - the same corroboration check
    diagnose_cpu_cooling_ceiling/diagnose_gpu_cooling_ceiling already use (SensorHistoryWindow
    has its own equivalent inline lookup for the same reason - kept separate here rather than
    refactored into a shared helper, to avoid touching that already-verified code for this
    phase). Returns None (not True/False) when there isn't enough idle history to judge yet -
    RecommendationsWindow treats None as "no live corroboration available", never as False."""
    system_sensor = next((sn for sn in getattr(app, "_lhm", []) or []
                          if sn.get("SensorType") == "Temperature"
                          and "superio" in sn.get("Parent", "").lower() and sn.get("Name") == "System"), None)
    if system_sensor is None:
        return None
    system_ref = {"kind": "sensor", "key": _sensor_bucket_key(sensor_identity(system_sensor))}
    since_ts = time.time() - TELEMETRY_RANGE_SECONDS["30d"]
    buckets = read_telemetry_file(since_ts=since_ts, sensor_key=system_ref["key"])
    sessions = overlapping_sessions(read_sessions_file(), since_ts, time.time())
    idle_baseline = compute_idle_baseline(filter_idle_buckets(buckets, sessions), system_ref)
    current_temp = (app.last_context or {}).get("system_temp")
    anomaly = evaluate_anomaly(current_temp, idle_baseline, "°C")
    if anomaly is None:
        return None
    return not (anomaly["unusual"] and anomaly["delta"] > 0)


class RecommendationsWindow(tk.Toplevel):
    """RECOMMENDATIONS - opened from History, not the main dashboard (same pattern as Analytics/
    Sessions/Trends). Deterministic, evidence-backed, purely on-demand (recomputes only on open/
    REFRESH, never the live telemetry poll) - see the module-level design note above
    RECOMMENDATION_MIN_OCCURRENCES for the full rationale. ADVISORY ONLY: this window only ever
    displays text: it contains no control/actuation of any kind."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Thermal Watch — Recommendations")
        self.geometry("880x760")
        self.minsize(760, 560)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self._build()
        self._recompute()

    def _build(self):
        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(bar, text="Deterministic, evidence-backed, advisory only - never controls fans, "
                          "power limits, voltages, clocks, or BIOS settings.",
                 bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        tk.Button(bar, text="REFRESH", command=self._recompute, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right")

        scroll = ScrollFrame(self, bg=BG); scroll.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self.list_frame = scroll.inner

    def _recompute(self):
        """The only place this window ever touches disk/live state: one recommendations pass over
        the already-persisted session history, plus the CURRENT live System-sensor/CPU-fan
        reading (never a new hardware poll - last_context/_lhm are already populated by the
        normal 2s poll; this just reads them)."""
        for w in self.list_frame.winfo_children():
            w.destroy()
        live_moderate = _live_system_temp_moderate(self.app)
        live_fan_rpm = (self.app.last_context or {}).get("cpu_fan_rpm")
        recs = compute_recommendations(live_moderate, live_fan_rpm)
        for i, rec in enumerate(recs):
            card = tk.Frame(self.list_frame, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            card.pack(fill="x", pady=(0 if i == 0 else 10))
            tk.Label(card, text="\n".join(format_recommendation(rec)), bg=PANEL, fg=TEXT, font=(SANS, 10),
                    justify="left", anchor="nw", wraplength=800).pack(fill="x", padx=14, pady=12, anchor="w")


class FanIntelligenceWindow(tk.Toplevel):
    """COOLING/FAN INTELLIGENCE - opened from History, not the main dashboard (same pattern as
    Analytics/Sessions/Trends/Recommendations). RECOMMENDATIONS ONLY, same standing constraint as
    every other layer: this window only ever reads already-persisted telemetry and describes what
    it observed - it never writes a fan curve or touches fan control of any kind. Purely on-
    demand (recomputes only on open/REFRESH, never the live telemetry poll). On a machine with no
    fan-speed-varying history yet (see the module-level design note above
    FAN_RESPONSE_MIN_BUCKETS_PER_BIN), both panels correctly show "not enough data yet" - that is
    the honest answer until real history accumulates, never a live-snapshot standing in for one."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Thermal Watch — Cooling/Fan Intelligence")
        self.geometry("880x640")
        self.minsize(760, 520)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self._build()
        self._recompute()

    def _build(self):
        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(bar, text="Recommendations only - Thermal Watch never controls fan speed.",
                 bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        tk.Button(bar, text="REFRESH", command=self._recompute, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right")

        body = tk.Frame(self, bg=BG); body.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self.panels = {}
        for i, component in enumerate(("gpu", "cpu")):
            panel = tk.Frame(body, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            panel.pack(fill="x", pady=(0 if i == 0 else 10))
            label = tk.Label(panel, text="", bg=PANEL, fg=TEXT, font=(SANS, 10),
                             justify="left", anchor="nw", wraplength=800)
            label.pack(fill="x", padx=14, pady=12, anchor="w")
            self.panels[component] = label

    def _recompute(self):
        """The only place this window ever touches disk: one fan-cooling-response pass per
        component over the already-persisted telemetry history - no new hardware poll."""
        for component, label in self.panels.items():
            report = compute_fan_cooling_response(component)
            text = "\n".join(format_fan_cooling_response(report, component.upper()))
            label.config(text=text)


class ExperimentsWindow(tk.Toplevel):
    """HARDWARE-CHANGE EXPERIMENTS - opened from History, same pattern as Analytics/Sessions/
    Trends/Recommendations/Fan Intelligence. The FIRST window in this app that writes user-authored
    data (an experiment marker); it still never writes anything measured - markers are annotations
    on the timeline, and every number shown is computed on demand from already-persisted telemetry
    and sessions. Purely on-demand: recomputes on open/REFRESH/selection/marker change, never on
    the live 2s poll."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Thermal Watch — Hardware-Change Experiments")
        self.geometry("1040x760")
        self.minsize(860, 620)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self.experiments = []
        self._row_experiment = {}
        self._build()
        self._reload()

    def _build(self):
        form = tk.Frame(self, bg=BG); form.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(form, text="WHAT CHANGED", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.description_var = tk.StringVar()
        tk.Entry(form, textvariable=self.description_var, width=34, bg=PANEL, fg=TEXT,
                insertbackground=TEXT, relief="flat").pack(side="left", padx=(4, 14))

        tk.Label(form, text="AFFECTS", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.component_var = tk.StringVar(value=EXPERIMENT_COMPONENT_LABELS[EXPERIMENT_COMPONENT_ORDER[0]])
        style_option_menu(tk.OptionMenu(form, self.component_var,
                                       *[EXPERIMENT_COMPONENT_LABELS[c] for c in EXPERIMENT_COMPONENT_ORDER])
                          ).pack(side="left", padx=(4, 14))

        tk.Label(form, text="WHEN", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.when_var = tk.StringVar(value=time.strftime("%Y-%m-%d %H:%M"))
        tk.Entry(form, textvariable=self.when_var, width=17, bg=PANEL, fg=TEXT,
                insertbackground=TEXT, relief="flat").pack(side="left", padx=(4, 14))

        tk.Button(form, text="MARK CHANGE", command=self._mark_change, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="left")

        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=16, pady=(0, 6))
        self.status_label = tk.Label(bar, text="", bg=BG, fg=DIM, font=(MONO, 9), anchor="w")
        self.status_label.pack(side="left")
        tk.Button(bar, text="REFRESH", command=self._reload, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right")
        tk.Button(bar, text="DELETE SELECTED", command=self._delete_selected, bg="#181b1f", fg=TEXT,
                 relief="flat", font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right", padx=(0, 8))

        style = ttk.Style(self)
        style.theme_use(style.theme_use())
        # clam's default Treeview.field element draws a beveled border using bordercolor/
        # lightcolor/darkcolor regardless of borderwidth=0 above (that only zeroes padding, not
        # the bevel itself) - lightcolor defaults to #eeebe7, near-white, which is exactly the
        # bright border seen around every Treeview until these three are pinned to match BORDER.
        style.configure("Thermal.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=24, borderwidth=0, font=(MONO, 9),
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Thermal.Treeview.Heading", background=BORDER, foreground=MUTED, relief="flat",
                        font=(MONO, 8))
        style.map("Thermal.Treeview", background=[("selected", BORDER2)], foreground=[("selected", TEXT)])

        columns = ("when", "component", "description", "result")
        headers = {"when": "CHANGED ON", "component": "AFFECTS", "description": "WHAT CHANGED",
                  "result": "RESULT"}
        widths = {"when": 150, "component": 130, "description": 420, "result": 220}
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=8, style="Thermal.Treeview")
        for c in columns:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="x", padx=16)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        detail = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        detail.pack(fill="both", expand=True, padx=16, pady=14)
        self.detail_text = tk.Label(detail, text="", bg=PANEL, fg=TEXT, font=(SANS, 10),
                                    justify="left", anchor="nw", wraplength=960)
        self.detail_text.pack(fill="both", expand=True, padx=14, pady=12, anchor="nw")

    def _mark_change(self):
        """Validate, then persist ONE marker. Every rejection says exactly what was wrong rather
        than silently doing nothing - and a marker is only added to the view after it actually
        reached disk, never optimistically."""
        description = self.description_var.get().strip()
        if not description:
            self.status_label.config(text="Describe what was changed first (e.g. \"replaced GPU thermal paste\").")
            return
        change_ts = parse_experiment_timestamp(self.when_var.get())
        if change_ts is None:
            self.status_label.config(text="WHEN must be a past date as YYYY-MM-DD or YYYY-MM-DD HH:MM.")
            return
        label_to_key = {v: k for k, v in EXPERIMENT_COMPONENT_LABELS.items()}
        component = label_to_key.get(self.component_var.get(), "system")
        record = new_experiment_record(description, change_ts, component,
                                       existing_ids=[e.get("experiment_id") for e in read_experiments_file()])
        if not append_experiment(record):
            self.status_label.config(text="Could not write the experiment file - marker not saved.")
            return
        self.description_var.set("")
        self._reload(select_id=record["experiment_id"])
        self.status_label.config(text=f"Marked: {description}")

    def _delete_selected(self):
        experiment = self._selected_experiment()
        if experiment is None:
            self.status_label.config(text="Select an experiment in the table first.")
            return
        if not delete_experiment(experiment["experiment_id"]):
            self.status_label.config(text="Could not update the experiment file - nothing was deleted.")
            return
        self._reload()
        self.status_label.config(text=f"Deleted: {experiment['description']}")

    def _selected_experiment(self):
        selection = self.tree.selection()
        return self._row_experiment.get(selection[0]) if selection else None

    def _reload(self, select_id=None):
        """The only place this window touches disk: exactly ONE read of each store (markers,
        sessions, telemetry) however many markers are listed - the reports are then computed from
        those in memory. One report per marker, computed here rather than lazily per selection, so
        the table's RESULT column and the detail panel can never disagree about the same
        experiment."""
        self.experiments = read_experiments_file()
        self._reports = {}
        self.tree.delete(*self.tree.get_children())
        self._row_experiment = {}
        now = time.time()
        sessions = read_sessions_file()
        buckets = read_telemetry_file(since_ts=now - EXPERIMENT_MAX_HISTORY_DAYS * 86400)
        for experiment in self.experiments:
            report = compute_experiment_report(experiment, now=now, sessions=sessions, buckets=buckets,
                                               experiments=self.experiments)
            self._reports[experiment["experiment_id"]] = report
            if report["direction"] is None:
                result = "Not enough data yet"
            else:
                result = f"{report['direction']} ({report['confidence']})"
            row = self.tree.insert("", "end", values=(
                format_experiment_timestamp(experiment["change_timestamp"]),
                EXPERIMENT_COMPONENT_LABELS.get(experiment.get("component"), experiment.get("component")),
                experiment["description"], result))
            self._row_experiment[row] = experiment
            if select_id is not None and experiment["experiment_id"] == select_id:
                self.tree.selection_set(row)
        if not self.experiments:
            self.detail_text.config(text="No hardware changes marked yet.\n\nMark one above (a new fan, a "
                                        "repaste, extra case fans, a dust clean) and Thermal Watch will "
                                        "compare its own measurements from before and after that date - "
                                        f"once at least {EXPERIMENT_MIN_ELAPSED_DAYS} day has passed and "
                                        f"{TREND_MIN_SAMPLES} comparable sessions exist on each side.")
        elif not self.tree.selection():
            self.tree.selection_set(self.tree.get_children()[0])

    def _on_select(self, _event=None):
        experiment = self._selected_experiment()
        if experiment is None:
            return
        report = self._reports.get(experiment["experiment_id"]) or compute_experiment_report(experiment)
        self.detail_text.config(text="\n".join(format_experiment_report(report)))


class ConnectionsWindow(tk.Toplevel):
    """ACTIVE CONNECTIONS (v1.1 Phase 3, Connection Intelligence) - opened from the NETWORK
    panel's connection-count cell. Purely on-demand, same convention as every other analysis
    window: populated from a live active_connections() call on open and on REFRESH, never tied
    to the 2s poll (a Treeview holding 150+ live-refreshing rows every 2s would be real,
    pointless UI churn for a list meant to be read, not watched). Metadata only - owning
    process, local/remote address:port, protocol, TCP state - never packet content, same rule
    as every other network layer in this app."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Thermal Watch — Active Connections")
        self.geometry("980x620")
        self.minsize(760, 420)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self._build()
        self._reload()

    def _build(self):
        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=16, pady=(14, 8))
        self.count_label = tk.Label(bar, text="", bg=BG, fg=DIM, font=(MONO, 9))
        self.count_label.pack(side="left")
        tk.Button(bar, text="REFRESH", command=self._reload, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right")

        style = ttk.Style(self)
        style.theme_use(style.theme_use())
        style.configure("Thermal.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=22, borderwidth=0, font=(MONO, 9),
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Thermal.Treeview.Heading", background=BORDER, foreground=MUTED, relief="flat",
                        font=(MONO, 8))
        style.map("Thermal.Treeview", background=[("selected", BORDER2)], foreground=[("selected", TEXT)])

        columns = ("process", "pid", "protocol", "local", "remote", "state")
        headers = {"process": "PROCESS", "pid": "PID", "protocol": "PROTO", "local": "LOCAL",
                  "remote": "REMOTE", "state": "STATE"}
        widths = {"process": 190, "pid": 70, "protocol": 60, "local": 190, "remote": 190, "state": 110}
        self.tree = ttk.Treeview(self, columns=columns, show="headings", style="Thermal.Treeview")
        for c in columns:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        tk.Label(self, text="Connection metadata only (owning process, address:port, TCP state) - "
                            "never packet content.",
                bg=BG, fg=DIM, font=(MONO, 9)).pack(anchor="w", padx=16, pady=(0, 12))

    def _reload(self):
        """The only place this window touches live state: one fresh active_connections() call,
        independent of the worker thread's own tick (a name_cache of its own, so REFRESH always
        shows the current owning process even if the worker's cache is mid-resolve)."""
        connections = active_connections()
        connections.sort(key=lambda c: (c["name"].lower(), c["protocol"], c["local"]))
        self.tree.delete(*self.tree.get_children())
        for c in connections:
            self.tree.insert("", "end", values=(c["name"], c["pid"], c["protocol"],
                                                 c["local"], c["remote"], c["state"]))
        tcp_n = sum(1 for c in connections if c["protocol"] == "TCP")
        udp_n = sum(1 for c in connections if c["protocol"] == "UDP")
        self.count_label.config(text=f"{tcp_n} TCP connection(s) · {udp_n} UDP endpoint(s)")


class TimelineWindow(tk.Toplevel):
    """UNIFIED FLIGHT RECORDER TIMELINE - opened from History, same pattern as every other analysis
    window. Read-only and on-demand (recomputes on open/REFRESH/range or filter change, never on
    the live 2s poll); it merges stores it never writes to.

    One deliberate honesty rule in the wiring: the SUMMARY LINE is always computed from the
    unfiltered timeline, and the kind checkboxes only affect which rows are listed. Hiding the
    'not monitored' rows must never quietly change the reported coverage percentage - the display
    filter is about what you want to read, not about what was true."""

    RANGE_ORDER = ("6h", "24h", "7d", "30d")

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Thermal Watch — Flight Recorder Timeline")
        self.geometry("1100x760")
        self.minsize(900, 620)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self.events = []
        self._row_event = {}
        self._build()
        self._reload()

    def _build(self):
        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(bar, text="RANGE", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.range_var = tk.StringVar(value="24h")
        style_option_menu(tk.OptionMenu(bar, self.range_var, *self.RANGE_ORDER,
                                       command=lambda _v: self._reload())).pack(side="left", padx=(4, 14))

        tk.Label(bar, text="SHOW", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left", padx=(0, 4))
        self.kind_vars = {}
        for kind in ("gap", "incident", "session", "experiment", "log"):
            var = tk.BooleanVar(value=True)
            self.kind_vars[kind] = var
            tk.Checkbutton(bar, text=TIMELINE_KIND_LABELS[kind], variable=var, command=self._render,
                          bg=BG, fg=MUTED, selectcolor=PANEL, activebackground=BG, activeforeground=TEXT,
                          font=(MONO, 8), relief="flat", highlightthickness=0).pack(side="left")

        tk.Button(bar, text="REFRESH", command=self._reload, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right")

        self.summary_label = tk.Label(self, bg=BG, fg=DIM, font=(MONO, 9), anchor="w")
        self.summary_label.pack(fill="x", padx=16, pady=(0, 8))

        style = ttk.Style(self)
        style.theme_use(style.theme_use())
        # clam's default Treeview.field element draws a beveled border using bordercolor/
        # lightcolor/darkcolor regardless of borderwidth=0 above (that only zeroes padding, not
        # the bevel itself) - lightcolor defaults to #eeebe7, near-white, which is exactly the
        # bright border seen around every Treeview until these three are pinned to match BORDER.
        style.configure("Thermal.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=24, borderwidth=0, font=(MONO, 9),
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Thermal.Treeview.Heading", background=BORDER, foreground=MUTED, relief="flat",
                        font=(MONO, 8))
        style.map("Thermal.Treeview", background=[("selected", BORDER2)], foreground=[("selected", TEXT)])

        columns = ("when", "kind", "duration", "what")
        headers = {"when": "WHEN", "kind": "KIND", "duration": "DURATION", "what": "WHAT HAPPENED"}
        widths = {"when": 165, "kind": 140, "duration": 95, "what": 620}
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=16, style="Thermal.Treeview")
        for c in columns:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="both", expand=True, padx=16)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        detail = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        detail.pack(fill="x", padx=16, pady=14)
        self.detail_text = tk.Label(detail, text="Select an entry above to see its detail.", bg=PANEL,
                                    fg=MUTED, font=(SANS, 10), justify="left", anchor="nw", wraplength=1020)
        self.detail_text.pack(fill="x", padx=14, pady=12, anchor="w")

    def _reload(self):
        """The only place this window touches disk: exactly one read of each store per refresh,
        shared by the timeline and its summary."""
        now = time.time()
        start_ts = now - TIMELINE_RANGE_SECONDS[self.range_var.get()]
        buckets = read_telemetry_file(since_ts=start_ts)
        self.events = build_timeline(start_ts, now, incidents=read_incidents_file(),
                                     sessions=read_sessions_file(), experiments=read_experiments_file(),
                                     buckets=buckets, log_records=read_event_log_file())
        self.summary_label.config(text=format_timeline_summary(
            summarize_timeline(self.events, buckets, start_ts, now)))
        self._render()

    def _render(self):
        """Applies the kind checkboxes to the already-built timeline - never re-reads anything, and
        never touches the summary line above (see the class docstring)."""
        self.tree.delete(*self.tree.get_children())
        self._row_event = {}
        shown = [e for e in self.events if self.kind_vars[e["kind"]].get()]
        for event in shown:
            duration = ("—" if event["end_timestamp"] is None
                       else fmt_timeline_span(event["end_timestamp"] - event["timestamp"]))
            row = self.tree.insert("", "end", values=(
                datetime.fromtimestamp(event["timestamp"]).strftime("%b %d  %I:%M:%S %p"),
                TIMELINE_KIND_LABELS.get(event["kind"], event["kind"].upper()), duration, event["title"]))
            self._row_event[row] = event
        if not shown:
            self.detail_text.config(text="Nothing recorded in this window for the selected kinds.")

    def _on_select(self, _event=None):
        selection = self.tree.selection()
        event = self._row_event.get(selection[0]) if selection else None
        if event is not None:
            self.detail_text.config(text="\n".join(format_timeline_event(event)))


class ReportsWindow(tk.Toplevel):
    """SCHEDULED HEALTH REPORTS - opened from History, same pattern as every other analysis window;
    the main dashboard is untouched. Viewing is strictly READ-ONLY: a report records what Thermal
    Watch concluded from the data available when it was generated, so selecting one renders its
    STORED payload and never recomputes or rewrites anything. Only the explicit REGENERATE button
    replaces a stored payload, and it says so when the source data it would need has since been
    pruned."""

    FILTERS = ("All", "DAILY", "WEEKLY", "MONTHLY")

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Thermal Watch — System Health Reports")
        self.geometry("1100x780")
        self.minsize(900, 620)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self.reports = []
        self._row_report = {}
        self._build()
        self._reload()

    def _build(self):
        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(bar, text="TYPE", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.filter_var = tk.StringVar(value="All")
        style_option_menu(tk.OptionMenu(bar, self.filter_var, *self.FILTERS,
                                       command=lambda _v: self._reload())).pack(side="left", padx=(4, 14))
        tk.Button(bar, text="REFRESH", command=self._reload, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right")
        for text, cmd in (("REGENERATE", self._regenerate), ("EXPORT JSON", self._export_json),
                         ("EXPORT CSV", self._export_csv), ("EXPORT TEXT", self._export_text),
                         ("COPY TEXT", self._copy_text)):
            tk.Button(bar, text=text, command=cmd, bg="#181b1f", fg=TEXT, relief="flat",
                     font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right", padx=(0, 8))

        self.status_label = tk.Label(self, bg=BG, fg=DIM, font=(MONO, 9), anchor="w")
        self.status_label.pack(fill="x", padx=16, pady=(0, 8))

        style = ttk.Style(self)
        style.theme_use(style.theme_use())
        # clam's default Treeview.field element draws a beveled border using bordercolor/
        # lightcolor/darkcolor regardless of borderwidth=0 above (that only zeroes padding, not
        # the bevel itself) - lightcolor defaults to #eeebe7, near-white, which is exactly the
        # bright border seen around every Treeview until these three are pinned to match BORDER.
        style.configure("Thermal.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=24, borderwidth=0, font=(MONO, 9),
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("Thermal.Treeview.Heading", background=BORDER, foreground=MUTED, relief="flat",
                        font=(MONO, 8))
        style.map("Thermal.Treeview", background=[("selected", BORDER2)], foreground=[("selected", TEXT)])

        columns = ("period", "type", "coverage", "status", "generated")
        headers = {"period": "PERIOD", "type": "TYPE", "coverage": "COVERAGE", "status": "STATUS",
                  "generated": "GENERATED"}
        widths = {"period": 220, "type": 100, "coverage": 110, "status": 230, "generated": 190}
        self.tree = ttk.Treeview(self, columns=columns, show="headings", height=9, style="Thermal.Treeview")
        for c in columns:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="w")
        self.tree.pack(fill="x", padx=16)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        detail = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        detail.pack(fill="both", expand=True, padx=16, pady=14)
        scroll = ScrollFrame(detail, bg=PANEL); scroll.pack(fill="both", expand=True, padx=2, pady=2)
        self.detail_text = tk.Label(scroll.inner, text="", bg=PANEL, fg=TEXT, font=(MONO, 9),
                                    justify="left", anchor="nw")
        self.detail_text.pack(fill="both", expand=True, padx=14, pady=12, anchor="nw")

    def _reload(self):
        report_type = None if self.filter_var.get() == "All" else self.filter_var.get()
        self.reports = read_reports(report_type)
        self.tree.delete(*self.tree.get_children())
        self._row_report = {}
        for rep in self.reports:
            payload = rep.get("payload") or {}
            row = self.tree.insert("", "end", values=(
                payload.get("period_label", rep["period_start_date"]), rep["report_type"],
                f"{rep['coverage_pct']:.1f}%" if rep["coverage_pct"] is not None else "—",
                rep["status"] or "—",
                datetime.fromtimestamp(rep["generated_timestamp"]).strftime("%Y-%m-%d %H:%M")))
            self._row_report[row] = rep
        if not self.reports:
            self.detail_text.config(text="No reports yet.\n\nThermal Watch generates a report for each "
                                        "COMPLETED day, week (Monday–Sunday) and month the next time it "
                                        "runs after that period ends. Nothing is reported for a period "
                                        "still in progress.")
            self.status_label.config(text="")
        else:
            self.tree.selection_set(self.tree.get_children()[0])

    def _selected(self):
        selection = self.tree.selection()
        return self._row_report.get(selection[0]) if selection else None

    def _on_select(self, _event=None):
        """Rendering only - a stored payload in, text out. No recomputation, no write."""
        rep = self._selected()
        if rep is None:
            return
        payload = rep.get("payload")
        if not payload:
            self.detail_text.config(text="This report's stored payload could not be read.")
            return
        self.detail_text.config(text="\n".join(format_report_text(payload)))

    def _regenerate(self):
        rep = self._selected()
        if rep is None:
            self.status_label.config(text="Select a report in the table first.")
            return
        payload = regenerate_report(rep["report_id"])
        if payload is None:
            self.status_label.config(text="Could not regenerate this report - the report store is unwritable.")
            return
        self._reload()
        note = (payload.get("reconstruction") or {}).get("note")
        self.status_label.config(text=note or f"Regenerated {rep['report_id']} from currently-retained data.")

    def _export(self, kind):
        rep = self._selected()
        if rep is None or not rep.get("payload"):
            self.status_label.config(text="Select a report in the table first.")
            return None, None
        stem = sanitize_filename_part(f"ThermalWatch_{rep['report_type']}_{rep['period_start_date']}")
        ext, types = {"json": (".json", [("JSON files", "*.json")]),
                     "csv": (".csv", [("CSV files", "*.csv")]),
                     "txt": (".txt", [("Text files", "*.txt")])}[kind]
        path = filedialog.asksaveasfilename(title="Export Report", defaultextension=ext,
                                            initialfile=f"{stem}{ext}", filetypes=types)
        return (Path(path) if path else None), rep

    def _export_json(self):
        path, rep = self._export("json")
        if path is None:
            return
        try:
            path.write_text(json.dumps(rep["payload"], indent=2), encoding="utf-8")
        except OSError as e:
            self.status_label.config(text=f"Export failed: {e}")
            return
        self.status_label.config(text=f"Exported structured report to {path.name}")

    def _export_csv(self):
        path, rep = self._export("csv")
        if path is None:
            return
        try:
            with path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(REPORT_CSV_COLUMNS)
                writer.writerows(build_report_csv_rows(rep["payload"]))
        except OSError as e:
            self.status_label.config(text=f"Export failed: {e}")
            return
        self.status_label.config(text=f"Exported report table to {path.name}")

    def _export_text(self):
        path, rep = self._export("txt")
        if path is None:
            return
        try:
            path.write_text("\n".join(format_report_text(rep["payload"])), encoding="utf-8")
        except OSError as e:
            self.status_label.config(text=f"Export failed: {e}")
            return
        self.status_label.config(text=f"Exported report text to {path.name}")

    def _copy_text(self):
        rep = self._selected()
        if rep is None or not rep.get("payload"):
            self.status_label.config(text="Select a report in the table first.")
            return
        self.clipboard_clear()
        self.clipboard_append("\n".join(format_report_text(rep["payload"])))
        self.status_label.config(text="Report text copied to clipboard.")


class MaintenanceWindow(tk.Toplevel):
    """MAINTENANCE OUTLOOK - opened from History, same pattern as every other analysis window.
    Read-only and on-demand. Every line is a conditional statement about an observed trend, and the
    standing caveat that this is not a failure forecast is part of the view itself rather than
    something a reader has to remember."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Thermal Watch — Maintenance Outlook")
        self.geometry("900x640")
        self.minsize(760, 520)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self._build()
        self._recompute()

    def _build(self):
        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=16, pady=(14, 8))
        tk.Label(bar, text="Projections of observed trends - never a failure forecast, never a countdown.",
                 bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        tk.Button(bar, text="REFRESH", command=self._recompute, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right")
        panel = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        panel.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        self.text = tk.Label(panel, text="", bg=PANEL, fg=TEXT, font=(SANS, 10), justify="left",
                             anchor="nw", wraplength=830)
        self.text.pack(fill="both", expand=True, padx=14, pady=12, anchor="nw")

    def _recompute(self):
        self.text.config(text="\n".join(format_maintenance_outlook(compute_maintenance_outlook())))


def _ai_evidence_ids_in(data):
    """Every `evidence_id` value present anywhere inside one dispatched evidence item's `data`,
    walked recursively - mirrors ai/grounding_guard.py's own _collect_evidence_ids() but stays
    local to app.py (the AI module must not import app.py, see ai/ai_settings.py's docstring)."""
    found = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "evidence_id" and isinstance(v, str):
                    found.append(v)
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return found


def _format_ai_evidence_lines(evidence, grounding):
    """Bounded, human-readable "Show Evidence" panel content for AI Analysis mode. Deliberately
    NOT a raw JSON dump: only evidence_id, source/operation, timestamp/window, the specific
    claimed-vs-evidence value, and coverage/monitoring-gap status are ever shown. No filesystem
    path, DATA_DIR, database path, or secret/API-key material exists anywhere in a ProviderResponse
    to begin with, but this stays curated on purpose rather than trusting that structurally."""
    lines = []
    claims = list(getattr(grounding, "claims", None) or []) if grounding is not None else []
    if claims:
        lines.append("CLAIMS CHECKED AGAINST EVIDENCE:")
        for claim in claims:
            header = f"  - [{claim.verdict.upper()}]"
            if claim.field:
                header += f" {claim.field}"
            if claim.evidence_id:
                header += f"  (evidence_id: {claim.evidence_id})"
            lines.append(header)
            if claim.claimed_value is not None or claim.evidence_value is not None:
                lines.append(f"      claimed={claim.claimed_value!r}  evidence={claim.evidence_value!r}")
        lines.append("")
    if evidence:
        lines.append("EVIDENCE RETRIEVED THIS TURN:")
        for item in evidence:
            if not isinstance(item, dict):
                continue
            operation = item.get("operation", "?")
            status = item.get("evidence_status", "?")
            generated_at = item.get("generated_at")
            when = "-"
            if isinstance(generated_at, (int, float)):
                try:
                    when = datetime.fromtimestamp(generated_at).strftime("%Y-%m-%d %H:%M:%S")
                except (OSError, OverflowError, ValueError):
                    when = "-"
            row = f"  - {operation}  [{status}]  as of {when}"
            ids = sorted(set(_ai_evidence_ids_in(item.get("data"))))
            if ids:
                row += f"  evidence_id(s): {', '.join(ids)}"
            coverage = item.get("coverage")
            if isinstance(coverage, dict) and coverage.get("coverage_pct") is not None:
                row += f"  coverage: {coverage['coverage_pct']}%"
            lines.append(row)
    return lines


class AskWindow(tk.Toplevel):
    """ASK THERMAL WATCH - a question box over the structured stores, plus (Phase 17) an optional
    AI Analysis mode wired to the already-built Phase 11-16 AI stack.

    EVIDENCE mode (default, unchanged): read-only and on-demand; it answers only from records, and
    says so at the end of every answer. Not a chatbot: there is no model here, and no sentence in
    an answer exists that was not selected from retrieved evidence.

    AI ANALYSIS mode: a thin UI + async layer around ONE call, `self.app.ai_adapter.ask(question)`
    (read fresh every request, never cached - so a config change in AISettingsWindow is picked up
    on the very next submit with no restart). That single call already does provider construction,
    the bounded evidence tool-call loop, and GroundingGuard review internally
    (ai/provider_registry.py's UniversalAIAdapter.ask()) - this window only ever renders the
    `.answer`/`.grounding`/`.evidence` it gets back, never anything from a provider directly.
    Conversation scope is deliberately ONE-SHOT ONLY: each submission is independent, exactly like
    Evidence mode; no chat history, no persistence of question/answer text to any Thermal Watch
    store, nothing surviving past this window's own transient widget state."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Thermal Watch — Ask")
        self.geometry("940x680")
        self.minsize(780, 540)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self._ai_request_in_flight = False
        self._ai_evidence_visible = False
        self._ai_status_text = "Ready"
        self._ai_last_response = None
        self._build()
        self._show_welcome()

    def _build(self):
        mode_bar = tk.Frame(self, bg=BG); mode_bar.pack(fill="x", padx=16, pady=(14, 0))
        tk.Label(mode_bar, text="MODE", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left", padx=(0, 8))
        self.evidence_mode_btn = tk.Button(mode_bar, text="EVIDENCE", command=lambda: self._set_mode("evidence"),
                                           relief="flat", font=(MONO, 9), padx=10, pady=4, cursor="hand2")
        self.evidence_mode_btn.pack(side="left")
        self.ai_mode_btn = tk.Button(mode_bar, text="AI ANALYSIS", command=lambda: self._set_mode("ai"),
                                     relief="flat", font=(MONO, 9), padx=10, pady=4, cursor="hand2")
        self.ai_mode_btn.pack(side="left", padx=(6, 0))

        self.evidence_frame = tk.Frame(self, bg=BG)
        self.ai_frame = tk.Frame(self, bg=BG)
        self._build_evidence_frame()
        self._build_ai_frame()

        self.mode_var = tk.StringVar(value="evidence")
        self.evidence_frame.pack(fill="both", expand=True)
        self._update_mode_buttons()

    # -- EVIDENCE mode: identical widgets/behavior to the pre-Phase-17 window, just parented to
    # self.evidence_frame instead of self so it can coexist with the new mode toggle. ------------
    def _build_evidence_frame(self):
        bar = tk.Frame(self.evidence_frame, bg=BG); bar.pack(fill="x", padx=16, pady=(6, 6))
        tk.Label(bar, text="ASK", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.question_var = tk.StringVar()
        entry = tk.Entry(bar, textvariable=self.question_var, bg=PANEL, fg=TEXT,
                        insertbackground=TEXT, relief="flat", font=(SANS, 10))
        entry.pack(side="left", fill="x", expand=True, padx=(6, 8), ipady=4)
        entry.bind("<Return>", lambda _e: self._ask())
        entry.focus_set()
        tk.Button(bar, text="ASK", command=self._ask, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=12, pady=4, cursor="hand2").pack(side="left")

        examples = tk.Frame(self.evidence_frame, bg=BG); examples.pack(fill="x", padx=16, pady=(0, 8))
        tk.Label(examples, text="TRY", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left", padx=(0, 6))
        for question in ASK_EXAMPLES[:3]:
            tk.Button(examples, text=question, command=lambda q=question: self._ask(q),
                     bg="#181b1f", fg=MUTED, relief="flat", font=(MONO, 8), padx=8, pady=3,
                     cursor="hand2").pack(side="left", padx=(0, 6))

        panel = tk.Frame(self.evidence_frame, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        panel.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        scroll = ScrollFrame(panel, bg=PANEL); scroll.pack(fill="both", expand=True, padx=2, pady=2)
        self.answer_text = tk.Label(scroll.inner, text="", bg=PANEL, fg=TEXT, font=(SANS, 10),
                                    justify="left", anchor="nw", wraplength=860)
        self.answer_text.pack(fill="both", expand=True, padx=14, pady=12, anchor="nw")

    def _show_welcome(self):
        self.answer_text.config(text="\n".join(
            ["Ask about anything Thermal Watch has recorded.", "",
             "Answers come only from stored records - incidents, workload sessions, telemetry, "
             "reports and experiments. Nothing here is generated or guessed, and every answer ends "
             "by naming the records it drew on.", "", "For example:"]
            + [f"  • {q}" for q in ASK_EXAMPLES]))

    def _ask(self, question=None):
        if question is not None:
            self.question_var.set(question)
        text = self.question_var.get().strip()
        if not text:
            self._show_welcome()
            return
        answer = answer_question(text)
        self.answer_text.config(text="\n".join([f"Q: {text}", ""] + answer["lines"]))

    # -- mode toggle ---------------------------------------------------------------------------
    def _set_mode(self, mode):
        self.mode_var.set(mode)
        if mode == "evidence":
            self.ai_frame.pack_forget()
            self.evidence_frame.pack(fill="both", expand=True)
        else:
            self.evidence_frame.pack_forget()
            self.ai_frame.pack(fill="both", expand=True)
            self._refresh_ai_availability()
        self._update_mode_buttons()

    def _update_mode_buttons(self):
        active_bg, inactive_bg = "#2a2f38", "#181b1f"
        mode = self.mode_var.get()
        self.evidence_mode_btn.config(bg=active_bg if mode == "evidence" else inactive_bg, fg=TEXT)
        self.ai_mode_btn.config(bg=active_bg if mode == "ai" else inactive_bg, fg=TEXT)

    # -- AI ANALYSIS mode ------------------------------------------------------------------------
    def _build_ai_frame(self):
        status_bar = tk.Frame(self.ai_frame, bg=BG); status_bar.pack(fill="x", padx=16, pady=(6, 6))
        self.ai_status_label = tk.Label(status_bar, text="", bg=BG, fg=MUTED, font=(MONO, 9),
                                        justify="left", anchor="w")
        self.ai_status_label.pack(side="left", fill="x", expand=True)
        tk.Button(status_bar, text="AI SETTINGS", command=self._open_ai_settings, bg="#181b1f", fg=TEXT,
                 relief="flat", font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right")

        # Shown only when self.app.ai_adapter is None - no network activity is ever attempted in
        # this state (see _ai_ask()'s guard, which re-checks this fresh before doing anything else).
        self.ai_unavailable_frame = tk.Frame(self.ai_frame, bg=BG)
        tk.Label(self.ai_unavailable_frame, text="AI Analysis unavailable.", bg=BG, fg=AMBER,
                font=(SANS, 10, "bold")).pack(anchor="w", padx=16, pady=(4, 2))
        tk.Label(self.ai_unavailable_frame, text="No AI provider is configured. Configure one in AI "
                "Settings, or keep using Evidence mode.", bg=BG, fg=MUTED, font=(SANS, 9),
                wraplength=860, justify="left").pack(anchor="w", padx=16, pady=(0, 6))
        tk.Button(self.ai_unavailable_frame, text="Use Evidence Mode",
                 command=lambda: self._set_mode("evidence"), bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(anchor="w", padx=16, pady=(0, 10))

        bar = tk.Frame(self.ai_frame, bg=BG); bar.pack(fill="x", padx=16, pady=(0, 6))
        self._ai_ask_bar = bar
        tk.Label(bar, text="ASK (AI)", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.ai_question_var = tk.StringVar()
        ai_entry = tk.Entry(bar, textvariable=self.ai_question_var, bg=PANEL, fg=TEXT,
                           insertbackground=TEXT, relief="flat", font=(SANS, 10))
        ai_entry.pack(side="left", fill="x", expand=True, padx=(6, 8), ipady=4)
        ai_entry.bind("<Return>", lambda _e: self._ai_ask())
        self.ai_ask_button = tk.Button(bar, text="ASK", command=self._ai_ask, bg="#181b1f", fg=TEXT,
                                       relief="flat", font=(MONO, 9), padx=12, pady=4, cursor="hand2")
        self.ai_ask_button.pack(side="left")

        panel = tk.Frame(self.ai_frame, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        panel.pack(fill="both", expand=True, padx=16, pady=(0, 14))
        scroll = ScrollFrame(panel, bg=PANEL); scroll.pack(fill="both", expand=True, padx=2, pady=2)

        self.ai_verdict_label = tk.Label(scroll.inner, text="", bg=PANEL, fg=MUTED, font=(MONO, 8, "bold"),
                                         justify="left", anchor="nw")
        self.ai_verdict_label.pack(fill="x", padx=14, pady=(12, 0), anchor="nw")
        self.ai_answer_text = tk.Label(scroll.inner, text="", bg=PANEL, fg=TEXT, font=(SANS, 10),
                                       justify="left", anchor="nw", wraplength=860)
        self.ai_answer_text.pack(fill="x", padx=14, pady=(4, 4), anchor="nw")
        self.ai_note_label = tk.Label(scroll.inner, text="", bg=PANEL, fg=AMBER, font=(SANS, 9, "italic"),
                                      justify="left", anchor="nw", wraplength=860)
        # not packed by default - only shown for a "corrected" verdict, see _render_ai_response()

        self.ai_evidence_toggle_btn = tk.Button(scroll.inner, text="Show Evidence",
                                                command=self._toggle_ai_evidence, bg="#181b1f", fg=MUTED,
                                                relief="flat", font=(MONO, 8), padx=8, pady=3, cursor="hand2")
        self.ai_evidence_toggle_btn.pack(anchor="w", padx=14, pady=(8, 4))
        self.ai_evidence_container = tk.Frame(scroll.inner, bg=PANEL)
        # not packed by default - toggled by _toggle_ai_evidence()
        self.ai_evidence_text = tk.Label(self.ai_evidence_container, text="", bg=PANEL, fg=MUTED,
                                         font=(MONO, 8), justify="left", anchor="nw", wraplength=860)
        self.ai_evidence_text.pack(fill="x", padx=14, pady=(0, 12), anchor="nw")

        self._refresh_ai_availability()

    def _open_ai_settings(self):
        """Opens the existing AISettingsWindow rather than rebuilding a settings form here -
        reuses the same App-level singleton slot HistoryWindow.open_ai_settings() uses, so the two
        entry points can never have two AISettingsWindow instances open at once."""
        app = self.app
        if app.ai_settings_window is not None and app.ai_settings_window.winfo_exists():
            app.ai_settings_window.lift()
            app.ai_settings_window.focus_force()
            return
        app.ai_settings_window = AISettingsWindow(app)

    def _refresh_ai_availability(self):
        if self.app.ai_adapter is None:
            self.ai_unavailable_frame.pack(fill="x", before=self._ai_ask_bar)
        else:
            self.ai_unavailable_frame.pack_forget()
        self._render_ai_status_line()

    def _render_ai_status_line(self):
        # Reads self.app.ai_config/.ai_adapter fresh every call - never a cached copy - so a
        # provider change made through AISettingsWindow is reflected the next time this is drawn.
        if self.app.ai_adapter is None:
            self.ai_status_label.config(text="MODE: AI ANALYSIS   STATUS: Unavailable")
            return
        status = ai_settings.status_from_config(self.app.ai_config)
        provider_label = AI_PROVIDER_LABELS.get(status.provider or "none", status.provider or "-")
        model = status.model or "—"
        self.ai_status_label.config(
            text=f"MODE: AI ANALYSIS   PROVIDER: {provider_label}   MODEL: {model}   STATUS: {self._ai_status_text}")

    def _set_ai_status(self, text):
        self._ai_status_text = text
        self._render_ai_status_line()

    def _toggle_ai_evidence(self):
        if self._ai_evidence_visible:
            self.ai_evidence_container.pack_forget()
            self.ai_evidence_toggle_btn.config(text="Show Evidence")
        else:
            self.ai_evidence_container.pack(fill="x", anchor="nw")
            self.ai_evidence_toggle_btn.config(text="Hide Evidence")
        self._ai_evidence_visible = not self._ai_evidence_visible

    def _set_ai_evidence(self, evidence, grounding):
        lines = _format_ai_evidence_lines(evidence, grounding)
        self.ai_evidence_text.config(text="\n".join(lines) if lines else "No evidence was returned for this request.")

    def _ai_ask(self, question=None):
        if question is not None:
            self.ai_question_var.set(question)
        text = self.ai_question_var.get().strip()
        # Read self.app.ai_adapter fresh, never a cached reference - this is what makes a config
        # change made via AISettingsWindow take effect on the very next submit with no restart.
        adapter = self.app.ai_adapter
        self._refresh_ai_availability()
        if adapter is None:
            return  # unconfigured: no network activity is ever attempted
        if not text:
            return
        if self._ai_request_in_flight:
            return  # a request is already in flight - the ask control is also disabled below
        self._ai_request_in_flight = True
        self.ai_ask_button.config(state="disabled")
        self._set_ai_status("Analyzing…")
        result_queue = queue.Queue()
        thread = threading.Thread(target=self._ai_worker, args=(adapter, text, result_queue), daemon=True)
        thread.start()
        self.after(100, self._poll_ai_answer, result_queue)

    def _ai_worker(self, adapter, question, result_queue):
        """Runs off the Tk main thread. Touches no widget - only the adapter and the queue. The
        try/except here is belt-and-suspenders on top of UniversalAIAdapter.ask()'s own internal
        safety (ai/provider_registry.py) so a truly unexpected error can never crash this thread
        silently or leak a raw traceback anywhere."""
        try:
            response = adapter.ask(question)
        except Exception:
            response = ProviderResponse.failure("unknown", "provider_failure", "AI provider failed safely")
        try:
            result_queue.put(response)
        except Exception:
            pass  # nobody will ever read it (e.g. window destroyed) - harmless, see class docstring

    def _poll_ai_answer(self, result_queue):
        if not self.winfo_exists():
            return  # window was closed mid-request - stop rescheduling, touch nothing
        try:
            response = result_queue.get_nowait()
        except queue.Empty:
            self.after(100, self._poll_ai_answer, result_queue)
            return
        self._ai_request_in_flight = False
        if not self.winfo_exists():
            return
        try:
            self.ai_ask_button.config(state="normal")
        except tk.TclError:
            pass
        self._render_ai_response(response)

    def _render_ai_response(self, response):
        """The ONLY place an AI answer is ever drawn. Always renders response.answer/.grounding as
        already produced by UniversalAIAdapter.ask() (which always runs GroundingGuard internally)
        - there is no code path here that reads anything from a provider directly."""
        self._ai_last_response = response
        if response is None or not getattr(response, "ok", False) or getattr(response, "grounding", None) is None:
            error = getattr(response, "error", None) or {}
            message = error.get("message") or "the AI provider could not complete this request"
            self._set_ai_status("Provider unavailable")
            self.ai_verdict_label.config(text="RESULT: UNAVAILABLE", fg=AMBER)
            self.ai_answer_text.config(text=f"AI Analysis unavailable: {message}", fg=AMBER)
            self.ai_note_label.pack_forget()
            self._set_ai_evidence([], None)
            return
        self._set_ai_status("Ready")
        verdict = getattr(response.grounding, "verdict", "clean")
        if verdict == "blocked":
            self.ai_verdict_label.config(text="RESULT: BLOCKED (SAFE FAILURE)", fg=RED)
            self.ai_answer_text.config(text=response.answer or "", fg=RED)
            self.ai_note_label.pack_forget()
        elif verdict == "corrected":
            self.ai_verdict_label.config(text="RESULT: CORRECTED", fg=AMBER)
            self.ai_answer_text.config(text=response.answer or "", fg=TEXT)
            self.ai_note_label.config(
                text="Note: Thermal Watch adjusted part of this answer to match its own recorded evidence.")
            self.ai_note_label.pack(fill="x", padx=14, pady=(0, 8), anchor="nw", before=self.ai_evidence_toggle_btn)
        else:
            self.ai_verdict_label.config(text="RESULT: CLEAN", fg=GREEN)
            self.ai_answer_text.config(text=response.answer or "", fg=TEXT)
            self.ai_note_label.pack_forget()
        self._set_ai_evidence(response.evidence, response.grounding)


AI_PROVIDER_LABELS = {
    "none": "Disabled (No AI)",
    "nox": "Nox",
    "openai_compatible": "OpenAI-Compatible",
    "custom": "Custom",
}
AI_PROVIDER_ORDER = ("none", "nox", "openai_compatible", "custom")
AI_STATUS_COLOR = {"Connected": GREEN, "Invalid configuration": RED, "Authentication failed": RED,
                   "Endpoint unavailable": AMBER, "Model unavailable": AMBER, "Provider unavailable": AMBER}


class AISettingsWindow(tk.Toplevel):
    """Phase 16 - AI Integration Settings. Configures WHICH AI provider (if any) the optional
    UniversalAIAdapter (ai/provider_registry.py, Phase 12) uses, and persists that choice
    (ai/ai_settings.py) - settings/config only. This window never itself answers a question or
    talks to a model; it only prepares the ProviderConfig that ai_settings.load_provider_config()
    and App.reload_ai_config() turn into an adapter elsewhere. Saving or resetting here rebuilds
    only App.ai_config/App.ai_adapter in memory - it never restarts, re-inits, or otherwise
    touches sensor polling, incidents, sessions, or any other .after()-scheduled subsystem.

    Fields shown are provider-specific, not a fixed form with irrelevant rows left visible:
    Nox and Custom are supplied by a Python callable injected in code (not this screen), so their
    network fields (endpoint/API key/allow-remote) are never even built for those providers;
    only OpenAI-Compatible - the one provider a JSON settings file is actually enough to drive -
    shows Endpoint/Model/API Key/Allow Remote."""

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Thermal Watch — AI Integration")
        self.geometry("640x580")
        self.minsize(560, 500)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self._build()
        self._load_current()

    def _build(self):
        top = tk.Frame(self, bg=BG); top.pack(fill="x", padx=18, pady=(16, 4))
        tk.Label(top, text="AI INTEGRATION", bg=BG, fg=TEXT, font=(MONO, 12, "bold")).pack(side="left")
        tk.Label(self, text="Choose an optional AI provider for Ask Thermal Watch's evidence tool. "
                            "Thermal Watch itself never generates or guesses text - a provider only ever "
                            "sees read-only evidence through the same bounded tool every provider shares.",
                bg=BG, fg=MUTED, font=(SANS, 9), justify="left", wraplength=580).pack(
                    fill="x", padx=18, pady=(0, 10), anchor="w")

        row = tk.Frame(self, bg=BG); row.pack(fill="x", padx=18, pady=(0, 6))
        tk.Label(row, text="PROVIDER", bg=BG, fg=DIM, font=(MONO, 8), width=18, anchor="w").pack(side="left")
        self.provider_var = tk.StringVar(value=AI_PROVIDER_LABELS["none"])
        style_option_menu(tk.OptionMenu(row, self.provider_var,
                                        *[AI_PROVIDER_LABELS[p] for p in AI_PROVIDER_ORDER],
                                        command=lambda _v: self._rebuild_fields())
                          ).pack(side="left", padx=(4, 0))

        self.note_label = tk.Label(self, text="", bg=BG, fg=DIM, font=(SANS, 9), justify="left",
                                   wraplength=580, anchor="w")
        self.note_label.pack(fill="x", padx=18, pady=(4, 10))

        self.fields_frame = tk.Frame(self, bg=BG)
        self.fields_frame.pack(fill="x", padx=18)

        self.endpoint_var = tk.StringVar()
        self.model_var = tk.StringVar()
        self.api_key_var = tk.StringVar()
        self.allow_remote_var = tk.BooleanVar(value=False)

        btns = tk.Frame(self, bg=BG); btns.pack(fill="x", padx=18, pady=(14, 6))
        tk.Button(btns, text="TEST CONNECTION", command=self._test_connection, bg="#181b1f", fg=TEXT,
                 relief="flat", font=(MONO, 9), padx=10, pady=5, cursor="hand2").pack(side="left")
        tk.Button(btns, text="SAVE", command=self._save, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=5, cursor="hand2").pack(side="left", padx=(8, 0))
        tk.Button(btns, text="RESET / DISABLE", command=self._reset, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=5, cursor="hand2").pack(side="left", padx=(8, 0))

        self.status_label = tk.Label(self, text="", bg=BG, fg=DIM, font=(MONO, 9), justify="left",
                                     wraplength=580, anchor="w")
        self.status_label.pack(fill="x", padx=18, pady=(6, 4))

        current = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        current.pack(fill="both", expand=True, padx=18, pady=(6, 16))
        tk.Label(current, text="ACTIVE CONFIGURATION", bg=PANEL, fg=DIM, font=(MONO, 8)).pack(
            anchor="w", padx=12, pady=(10, 0))
        self.current_label = tk.Label(current, text="", bg=PANEL, fg=TEXT, font=(MONO, 9),
                                      justify="left", anchor="nw")
        self.current_label.pack(fill="both", expand=True, padx=12, pady=(4, 10), anchor="nw")

    def _row_entry(self, label, var, show=None):
        row = tk.Frame(self.fields_frame, bg=BG); row.pack(fill="x", pady=(0, 8))
        tk.Label(row, text=label, bg=BG, fg=DIM, font=(MONO, 8), width=18, anchor="w").pack(side="left")
        kwargs = {"show": show} if show else {}
        tk.Entry(row, textvariable=var, bg=PANEL, fg=TEXT, insertbackground=TEXT, relief="flat",
                **kwargs).pack(side="left", fill="x", expand=True, padx=(4, 0), ipady=3)

    def _selected_provider(self):
        label_to_value = {v: k for k, v in AI_PROVIDER_LABELS.items()}
        return label_to_value.get(self.provider_var.get(), "none")

    def _rebuild_fields(self):
        for w in self.fields_frame.winfo_children():
            w.destroy()
        provider = self._selected_provider()
        if provider == "none":
            self.note_label.config(text="AI is disabled. Ask Thermal Watch keeps working exactly as "
                                        "before (the existing deterministic Ask) - no adapter is built.")
        elif provider == "nox":
            self.note_label.config(text="Nox queries Thermal Watch through its own local persona tool "
                                        "loop. There is no endpoint, model, or API key for this screen to "
                                        "collect, and no live connection test is possible from here - Nox "
                                        "supplies its own transport in code, not through this config file.")
        elif provider == "custom":
            self.note_label.config(text="Custom expects a Python callable injected by code - there is no "
                                        "callable a JSON settings file can itself supply. Selecting it here "
                                        "only records the choice; it becomes usable once code injects a "
                                        "handler.")
        else:
            self.note_label.config(text="OpenAI-compatible chat-completions endpoint (e.g. a local Ollama "
                                        "or LM Studio server). A loopback endpoint (127.0.0.1/localhost) "
                                        "works with no further action; a remote endpoint requires "
                                        "explicitly checking Allow Remote below.")
            self._row_entry("ENDPOINT", self.endpoint_var)
            self._row_entry("MODEL", self.model_var)
            self._row_entry("API KEY (optional)", self.api_key_var, show="*")
            remote_row = tk.Frame(self.fields_frame, bg=BG); remote_row.pack(fill="x", pady=(2, 4))
            tk.Checkbutton(remote_row, text="Allow remote (non-loopback) endpoint",
                          variable=self.allow_remote_var, bg=BG, fg=TEXT, selectcolor=PANEL,
                          activebackground=BG, activeforeground=TEXT, font=(SANS, 9)).pack(side="left")

    def _load_current(self):
        status = ai_settings.status_from_config(self.app.ai_config)
        self.provider_var.set(AI_PROVIDER_LABELS.get(status.provider or "none", AI_PROVIDER_LABELS["none"]))
        self.endpoint_var.set(status.endpoint or "")
        self.model_var.set(status.model or "")
        self.api_key_var.set("")  # never populated from disk - the plaintext key is never re-serialized
        self.allow_remote_var.set(status.allow_remote)
        self._rebuild_fields()
        self._refresh_current_label(status)

    def _refresh_current_label(self, status=None):
        status = status or ai_settings.status_from_config(self.app.ai_config)
        lines = [
            f"Provider:      {AI_PROVIDER_LABELS.get(status.provider or 'none', status.provider)}",
            f"Endpoint:      {status.endpoint or '-'}",
            f"Model:         {status.model or '-'}",
            f"Allow remote:  {status.allow_remote}",
            f"API key saved: {status.credential_configured}",
        ]
        self.current_label.config(text="\n".join(lines))

    def _collect_fields(self):
        provider = self._selected_provider()
        if provider == "none":
            return {"provider": "none", "endpoint": None, "model": None, "allow_remote": False, "api_key": None}
        if provider in ("nox", "custom"):
            # Endpoint/API key/allow_remote are never shown in the UI for these providers - never
            # sent even if a previous openai_compatible session left something in those variables.
            model = self.model_var.get().strip() or None if provider == "custom" else None
            return {"provider": provider, "endpoint": None, "model": model, "allow_remote": False, "api_key": None}
        return {"provider": provider, "endpoint": self.endpoint_var.get().strip() or None,
               "model": self.model_var.get().strip() or None, "allow_remote": bool(self.allow_remote_var.get()),
               "api_key": self.api_key_var.get().strip() or None}

    def _save(self):
        fields = self._collect_fields()
        if fields["provider"] == "none":
            ai_settings.disable_provider()
            self.app.reload_ai_config()
            self.status_label.config(text="AI integration disabled and saved.", fg=DIM)
            self._refresh_current_label()
            return
        try:
            ai_settings.save_provider_config(**fields)
        except ProviderContractError as exc:
            self.status_label.config(text=f"Not saved - {exc.message}", fg=RED)
            return
        self.app.reload_ai_config()
        self.api_key_var.set("")
        self.status_label.config(text="Saved.", fg=DIM)
        self._refresh_current_label()

    def _reset(self):
        ai_settings.disable_provider()
        self.app.reload_ai_config()
        self.provider_var.set(AI_PROVIDER_LABELS["none"])
        self.endpoint_var.set("")
        self.model_var.set("")
        self.api_key_var.set("")
        self.allow_remote_var.set(False)
        self._rebuild_fields()
        self.status_label.config(text="Reset - AI integration disabled.", fg=DIM)
        self._refresh_current_label()

    def _test_connection(self):
        fields = self._collect_fields()
        if fields["provider"] == "none":
            self.status_label.config(text="Select a provider first.", fg=DIM)
            return
        result = ai_settings.test_connection(**fields)
        color = AI_STATUS_COLOR.get(result["status"], DIM)
        self.status_label.config(text=f"{result['status']} - {result['detail']}", fg=color)


# 2-3 compatible-unit metric pairs a user can compare on one chart (item 12) - deliberately a
# small curated table, not a generic "pick any two metrics" engine, so a comparison never mixes
# incompatible units (a temperature against a percentage) onto one shared axis.
TELEMETRY_COMPARE_OPTIONS = {
    "cpu_temp": ["gpu_core_temp", "gpu_hotspot_temp", "gpu_vram_temp"],
    "gpu_core_temp": ["gpu_hotspot_temp", "gpu_vram_temp", "cpu_temp"],
    "gpu_hotspot_temp": ["gpu_core_temp", "gpu_vram_temp", "cpu_temp"],
    "gpu_vram_temp": ["gpu_core_temp", "gpu_hotspot_temp", "cpu_temp"],
    "cpu_power": ["gpu_power"], "gpu_power": ["cpu_power"],
    "cpu_util": ["gpu_util"], "gpu_util": ["cpu_util"],
}


class TelemetryChart(tk.Canvas):
    """Canvas chart for ONE sensor's historical avg/max(/min) line plus incident markers and
    workload-session bands - entirely separate from the live dashboard's HistoryChart (item 8):
    different data shape (downsampled telemetry buckets, not raw 2s samples), no coupling to
    live chart state or CPU-specific thresholds. Redraws fully on data/resize - telemetry charts
    open rarely (on-demand, item 20), so the render-optimization row-cache discipline used for
    the always-running live dashboard doesn't apply here."""

    MARKER_COLORS = {"YELLOW": AMBER, "ORANGE": ORANGE, "RED": RED}
    SESSION_PALETTE = ["#3a6ea5", "#5a8f5a", "#9a6a3a", "#8a4a8a", "#4a8a8a", "#6a6a3a"]

    def __init__(self, parent, **kw):
        super().__init__(parent, bg=PANEL, highlightthickness=0, height=280, **kw)
        self.points = []
        self.compare_points = None
        self.incidents = []
        self.sessions = []
        self.range_start = 0.0
        self.range_end = 1.0
        self.unit = ""
        self.show_max = True
        self.show_min = False
        self._marker_hit = {}
        self.on_marker_click = None
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Button-1>", self._on_click)

    def set_data(self, points, incidents, sessions, range_start, range_end, unit,
                show_max=True, show_min=False, compare_points=None):
        self.points, self.incidents, self.sessions = points, incidents, sessions
        self.range_start, self.range_end, self.unit = range_start, range_end, unit
        self.show_max, self.show_min, self.compare_points = show_max, show_min, compare_points
        self._redraw()

    def _redraw(self):
        self.delete("all")
        self._marker_hit = {}
        w, h = self.winfo_width(), self.winfo_height()
        if w < 20 or h < 20:
            return
        pad_l, pad_r, pad_t, band_h, pad_b = 46, 12, 14, 16, 22
        plot_l, plot_r = pad_l, w - pad_r
        plot_t, plot_b = pad_t, h - pad_b - band_h - 6
        if plot_r <= plot_l or plot_b <= plot_t:
            return
        span = max(1.0, self.range_end - self.range_start)

        def x_of(ts):
            return plot_l + (ts - self.range_start) / span * (plot_r - plot_l)

        all_series = [self.points] + ([self.compare_points] if self.compare_points else [])
        vals = []
        for series in all_series:
            for p in series:
                m = p.get("metric")
                if not m:
                    continue
                vals.append(m["avg"])
                if series is self.points:
                    if self.show_max:
                        vals.append(m["max"])
                    if self.show_min:
                        vals.append(m["min"])
        vmin, vmax = (min(vals), max(vals)) if vals else (0.0, 1.0)
        if vmax - vmin < 1e-6:
            vmax = vmin + 1.0
        pad_v = (vmax - vmin) * 0.1
        vmin, vmax = vmin - pad_v, vmax + pad_v

        def y_of(v):
            return plot_b - (v - vmin) / (vmax - vmin) * (plot_b - plot_t)

        # Mbps commonly spans well under 1 across a whole range (e.g. idle periods) - at 0
        # decimals every gridline would read "0", a uniquely uninformative axis for exactly the
        # unit most likely to need one. Every other unit keeps its existing 0-decimal labels.
        axis_prec = 1 if self.unit == " Mbps" else 0
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            gy = plot_t + frac * (plot_b - plot_t)
            self.create_line(plot_l, gy, plot_r, gy, fill=BORDER2)
            self.create_text(plot_l - 6, gy, text=f"{vmax - frac * (vmax - vmin):.{axis_prec}f}",
                             fill=DIM, font=(MONO, 7), anchor="e")

        def draw_line(points, value_key, color, dash=None):
            run = []
            for p in points:
                m = p.get("metric")
                if not m or p.get("gap_before"):
                    if len(run) >= 2:
                        self.create_line(*[c for xy in run for c in xy], fill=color, width=2, dash=dash)
                    run = []
                if m:
                    run.append((x_of(p["start_timestamp"]), y_of(m[value_key])))
            if len(run) >= 2:
                self.create_line(*[c for xy in run for c in xy], fill=color, width=2, dash=dash)

        if self.show_min:
            draw_line(self.points, "min", "#4a7fb5", dash=(2, 2))
        if self.show_max:
            draw_line(self.points, "max", ORANGE, dash=(3, 2))
        draw_line(self.points, "avg", GREEN)
        if self.compare_points:
            draw_line(self.compare_points, "avg", BLUE, dash=(4, 2))

        for inc in self.incidents:
            ts = inc.get("start_timestamp")
            if ts is None or not (self.range_start <= ts <= self.range_end):
                continue
            x = x_of(ts)
            color = self.MARKER_COLORS.get(inc.get("max_zone"), MUTED)
            item = self.create_polygon(x - 5, plot_t - 2, x + 5, plot_t - 2, x, plot_t + 7,
                                       fill=color, outline="")
            self._marker_hit[item] = ("incident", inc.get("incident_id"))

        band_y = h - pad_b - band_h
        for i, s in enumerate(self.sessions):
            s_start = max(self.range_start, s.get("start_timestamp", self.range_start))
            s_end = min(self.range_end, s.get("end_timestamp", self.range_end))
            if s_end <= s_start:
                continue
            x1, x2 = x_of(s_start), x_of(s_end)
            color = self.SESSION_PALETTE[i % len(self.SESSION_PALETTE)]
            item = self.create_rectangle(x1, band_y, x2, band_y + band_h, fill=color, outline="")
            self._marker_hit[item] = ("session", s.get("session_id"))
            if x2 - x1 > 46:
                self.create_text((x1 + x2) / 2, band_y + band_h / 2, text=(s.get("workload") or "")[:16],
                                 fill="#e8e8ec", font=(MONO, 7))

        for frac, anchor in ((0.0, "w"), (0.5, "center"), (1.0, "e")):
            ts = self.range_start + frac * span
            x = max(plot_l + 2, min(plot_r - 2, x_of(ts)))
            self.create_text(x, h - 8, text=datetime.fromtimestamp(ts).strftime("%m/%d %H:%M"),
                             fill=DIM, font=(MONO, 7), anchor=anchor)

    def _on_click(self, event):
        if not self._marker_hit:
            return
        item = self.find_closest(event.x, event.y, halo=6)
        if not item:
            return
        hit = self._marker_hit.get(item[0])
        if hit and self.on_marker_click:
            self.on_marker_click(*hit)


class SensorHistoryWindow(tk.Toplevel):
    """SENSOR HISTORY - opened via drill-down from a live sensor label/card (item 7), or from an
    incident/session overlay elsewhere. Reads long-term telemetry buckets from TELEMETRY_PATH
    only on open/range-change (item 20: never on the 2s poll). sensor_ref identifies WHICH
    sensor: {'kind': 'scalar'|'sensor', 'key': ..., 'label': ..., 'unit': ..., 'is_temp': bool,
    'component': incident-component-or-None}."""

    RANGE_LABELS = ["1h", "6h", "24h", "7d", "30d"]

    def __init__(self, app, sensor_ref):
        super().__init__(app)
        self.app = app
        self.sensor_ref = sensor_ref
        self.title(f"Thermal Watch — Sensor History — {sensor_ref['label']}")
        self.geometry("1080x700")
        self.minsize(860, 580)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        self.range_var = tk.StringVar(value="24h")
        self.compare_var = tk.StringVar(value="None")
        self.show_max_var = tk.BooleanVar(value=True)
        self.show_min_var = tk.BooleanVar(value=sensor_ref.get("is_temp", False))
        self._last_buckets, self._last_range_key = [], "24h"
        self._build()
        self._recompute()

    def _build(self):
        header = tk.Frame(self, bg=BG); header.pack(fill="x", padx=16, pady=(14, 6))
        tk.Label(header, text=self.sensor_ref["label"].upper(), bg=BG, fg=TEXT,
                 font=(MONO, 13, "bold")).pack(side="left")
        self.current_label = tk.Label(header, text="", bg=BG, fg=MUTED, font=(MONO, 10))
        self.current_label.pack(side="right")

        bar = tk.Frame(self, bg=BG); bar.pack(fill="x", padx=16, pady=(0, 8))
        tk.Label(bar, text="RANGE", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
        style_option_menu(tk.OptionMenu(bar, self.range_var, *self.RANGE_LABELS,
                                       command=lambda _v: self._recompute())).pack(side="left", padx=(4, 14))

        compare_opts = ["None"] + TELEMETRY_COMPARE_OPTIONS.get(self.sensor_ref["key"], []) \
            if self.sensor_ref["kind"] == "scalar" else ["None"]
        if len(compare_opts) > 1:
            tk.Label(bar, text="COMPARE WITH", bg=BG, fg=DIM, font=(MONO, 8)).pack(side="left")
            style_option_menu(tk.OptionMenu(bar, self.compare_var,
                                           *(["None"] + [TELEMETRY_SCALAR_LABELS[k][0] for k in compare_opts[1:]]),
                                           command=lambda _v: self._recompute())).pack(side="left", padx=(4, 14))

        if self.sensor_ref.get("is_temp"):
            tk.Checkbutton(bar, text="MAX", variable=self.show_max_var, command=self._recompute,
                          bg=BG, fg=TEXT, selectcolor=PANEL, activebackground=BG,
                          font=(MONO, 8)).pack(side="left", padx=(0, 8))
            tk.Checkbutton(bar, text="MIN", variable=self.show_min_var, command=self._recompute,
                          bg=BG, fg=TEXT, selectcolor=PANEL, activebackground=BG,
                          font=(MONO, 8)).pack(side="left", padx=(0, 8))

        tk.Button(bar, text="REFRESH", command=self._recompute, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right")
        tk.Button(bar, text="EXPORT JSON", command=self._export_json, bg="#181b1f", fg=TEXT, relief="flat",
                 font=(MONO, 9), padx=10, pady=4, cursor="hand2").pack(side="right", padx=(0, 8))

        chart_frame = tk.Frame(self, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        chart_frame.pack(fill="both", expand=True, padx=16, pady=(0, 10))
        self.chart = TelemetryChart(chart_frame)
        self.chart.pack(fill="both", expand=True, padx=2, pady=2)
        self.chart.on_marker_click = self._on_marker_click

        self.summary_label = tk.Label(self, bg=BG, fg=MUTED, font=(MONO, 9), justify="left",
                                      anchor="w", wraplength=1040)
        self.summary_label.pack(fill="x", padx=16, pady=(0, 14))

    def _current_value(self):
        if self.sensor_ref["kind"] == "scalar":
            ctx = self.app.last_context or {}
            return ctx.get(TELEMETRY_SCALAR_CONTEXT_MAP[self.sensor_ref["key"]])
        return None

    def _recompute(self):
        range_key = self.range_var.get()
        window_seconds = TELEMETRY_RANGE_SECONDS[range_key]
        now = time.time()
        since_ts = now - window_seconds

        # sensor_key is passed ONLY for a 'sensor' (drive/DIMM/motherboard) ref - a 'scalar' ref
        # never needs the sensor_readings table at all (item 20's query-cost finding).
        query_sensor_key = self.sensor_ref["key"] if self.sensor_ref["kind"] == "sensor" else None
        buckets = read_telemetry_file(since_ts=since_ts, sensor_key=query_sensor_key)
        self._last_buckets, self._last_range_key = buckets, range_key
        points = normalize_bucket_series(buckets, self.sensor_ref)
        group_size = TELEMETRY_DOWNSAMPLE_GROUPING[range_key]
        display_points = downsample_series(points, group_size, since_ts)

        compare_display = None
        compare_label = self.compare_var.get()
        if compare_label and compare_label != "None":
            compare_key = next((k for k, v in TELEMETRY_SCALAR_LABELS.items() if v[0] == compare_label), None)
            if compare_key:
                cmp_ref = {"kind": "scalar", "key": compare_key}
                compare_display = downsample_series(normalize_bucket_series(buckets, cmp_ref), group_size, since_ts)

        incidents = overlapping_incidents(read_incidents_file(), since_ts, now, self.sensor_ref.get("component"))
        sessions = overlapping_sessions(read_sessions_file(), since_ts, now)

        current = self._current_value()
        if current is None:
            last_with_data = next((p for p in reversed(points) if p["metric"]), None)
            current = last_with_data["metric"]["avg"] if last_with_data else None

        valid_metrics = [p["metric"] for p in points if p["metric"]]
        if valid_metrics:
            total = sum(m["count"] for m in valid_metrics)
            avg = sum(m["avg"] * m["count"] for m in valid_metrics) / total if total else None
            mn = min(m["min"] for m in valid_metrics)
            mx = max(m["max"] for m in valid_metrics)
        else:
            avg = mn = mx = None
        valid_buckets, expected_buckets, coverage_pct = compute_coverage(buckets, window_seconds)

        # Idle baseline (baseline learning, "what's normal for this machine at rest") - reuses
        # the SAME buckets/sessions this method already fetched for the chart/coverage above, so
        # this never costs a separate history query (item 20).
        idle_baseline = compute_idle_baseline(filter_idle_buckets(buckets, sessions), self.sensor_ref)

        unit = self.sensor_ref["unit"]
        # Mbps commonly sits under 1 for light/idle-ish periods - 0 decimals would render as an
        # uninformative "0-0 Mbps" for exactly the ranges where the real number matters most (see
        # the BASELINE section's own precision note in AnalyticsWindow for the same reasoning).
        # Every other unit here (deg C, W, %) keeps its existing 0-decimal formatting unchanged.
        prec = 1 if unit == " Mbps" else 0
        self.current_label.config(text=f"Current: {current:.{prec}f}{unit}" if current is not None else "Current: N/A")
        self.chart.set_data(display_points, incidents, sessions, since_ts, now, unit,
                           show_max=self.show_max_var.get(), show_min=self.show_min_var.get(),
                           compare_points=compare_display)

        def fmt(v):
            return f"{v:.{prec}f}{unit}" if v is not None else "N/A"

        if idle_baseline and idle_baseline["established"]:
            if idle_baseline["stddev"] is not None:
                lo, hi = idle_baseline["mean"] - idle_baseline["stddev"], idle_baseline["mean"] + idle_baseline["stddev"]
                idle_line = f"Idle baseline: {lo:.{prec}f}–{hi:.{prec}f}{unit}  (n={idle_baseline['count']} idle buckets)"
            else:
                idle_line = f"Idle baseline: {idle_baseline['mean']:.{prec}f}{unit}  (n={idle_baseline['count']} idle bucket)"
        elif idle_baseline:
            idle_line = f"Idle baseline: not enough idle data yet ({idle_baseline['count']}/{BASELINE_MIN_IDLE_BUCKETS} buckets)"
        else:
            idle_line = "Idle baseline: no idle-time data in this range"

        # Live cooling-ceiling cross-sensor diagnostics - only meaningful (and only computed) on
        # the two pages this pattern actually applies to: CPU Package temp and GPU Hotspot temp.
        # Live-only (see the fan-RPM scope note above diagnose_cpu_cooling_ceiling) so it always
        # reflects the current moment, never a historical range - shown regardless of which RANGE
        # is selected. The System sensor's own idle baseline needs a second, separate query since
        # this page's own `buckets` was fetched WITHOUT sensor_readings (query_sensor_key=None for
        # a scalar ref) - one extra indexed query, on-demand only, same cost class as opening a
        # drive/DIMM/motherboard sensor's own history page.
        diag_lines = []
        if self.sensor_ref["key"] in ("cpu_temp", "gpu_hotspot_temp"):
            ctx = self.app.last_context or {}
            system_sensor = next((sn for sn in getattr(self.app, "_lhm", []) or []
                                  if sn.get("SensorType") == "Temperature"
                                  and "superio" in sn.get("Parent", "").lower() and sn.get("Name") == "System"), None)
            system_idle_baseline = None
            if system_sensor is not None:
                system_ref = {"kind": "sensor", "key": _sensor_bucket_key(sensor_identity(system_sensor))}
                system_buckets = read_telemetry_file(since_ts=since_ts, sensor_key=system_ref["key"])
                system_idle_baseline = compute_idle_baseline(filter_idle_buckets(system_buckets, sessions), system_ref)
            if self.sensor_ref["key"] == "cpu_temp":
                finding = diagnose_cpu_cooling_ceiling(ctx.get("cpu_temp"), ctx.get("cpu_power"),
                                                       ctx.get("cpu_fan_rpm"), ctx.get("system_temp"), system_idle_baseline)
            else:
                finding = diagnose_gpu_cooling_ceiling(ctx.get("gpu_hotspot_temp"), ctx.get("gpu_power"),
                                                       ctx.get("gpu_fan_pct"), ctx.get("system_temp"), system_idle_baseline)
            if finding is not None:
                diag_lines = ["", "DIAGNOSTICS (live)", ""] + format_diagnostic_finding(finding)

        self.summary_label.config(text=(
            f"{self.sensor_ref['label'].upper()} — LAST {range_key.upper()}\n"
            f"Average: {fmt(avg)}    Minimum: {fmt(mn)}    Maximum: {fmt(mx)}\n"
            f"Coverage: {coverage_pct:.0f}%  ({valid_buckets}/{expected_buckets} expected 1-min buckets)\n"
            f"Thermal incidents: {len(incidents)} (associated - overlapping time, not causal)    "
            f"Workload sessions: {len(sessions)} (overlapping workload activity)\n"
            f"{idle_line}" + ("\n" + "\n".join(diag_lines) if diag_lines else "")
        ))

    def _on_marker_click(self, kind, record_id):
        if not record_id:
            return
        if kind == "incident":
            self.app.open_history()
            self.app.history_window.select_incident_by_id(record_id)
        elif kind == "session":
            self.app.open_history()
            hw = self.app.history_window
            hw.open_analytics()
            aw = hw.analytics_window
            aw.open_sessions()
            aw.sessions_window.select_session_by_id(record_id)

    def _export_json(self):
        """Exports exactly the buckets the current RANGE already fetched (self._last_buckets,
        set by _recompute()) - never a separately reimplemented query - as a portable JSON
        snapshot. Independent of the SQLite backend: the file this writes is the same shape a
        Storage v1 JSONL export always was."""
        if not self._last_buckets:
            return
        path = filedialog.asksaveasfilename(
            title="Export Sensor History",
            defaultextension=".json",
            initialfile=f"ThermalWatch_{sanitize_filename_part(self.sensor_ref['label'])}_{self._last_range_key}_"
                       f"{datetime.now():%Y-%m-%d_%H%M%S}.json",
            filetypes=[("JSON files", "*.json")],
        )
        if not path:
            return
        payload = build_telemetry_json_export(self._last_buckets, self.sensor_ref,
                                              {"range": self._last_range_key})
        try:
            Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Thermal Watch")
        self.geometry("1360x900")
        self.minsize(1080, 760)
        self.configure(bg=BG)
        apply_dark_titlebar(self)
        # default=True applies this to the root AND every Toplevel created afterward (History,
        # Timeline, Ask, ...) - one call here covers every window's title-bar icon, not just the
        # main dashboard's. Purely cosmetic: never let a missing/unreadable .ico file stop the
        # app from starting.
        try:
            self.iconbitmap(default=str(_APP_DIR / "thermal_watch.ico"))
        except tk.TclError:
            pass

        self.info = hardware_info()
        self.samples = []
        self.chart_points = []
        self.events = deque(maxlen=200)
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.last_cpu = cpu_times()
        self.start_time = time.time()
        self.silence_until = 0.0

        self.cpu_peak = self.gpu_peak = 0.0
        self.cpu_sum = self.gpu_sum = 0.0
        self.cpu_n = self.gpu_n = 0

        self.active_alerts = {}  # key -> {"since": ts, "text": str, "zone": optional str}
        self.curve_escalated = False

        self.cpu_zone_confirmed = "GREEN"
        self.cpu_zone_pending = {"zone": "GREEN", "since": time.time()}
        self.drive_zone_state = {}  # drive_key -> {"confirmed": str, "pending": {"zone": str, "since": ts}}
        self.sensor_zone_state = {}  # generic per-sensor state for GPU sub-sensors / RAM, same shape
        self.cpu_fan_alert_state = {"confirmed": False, "pending_since": None}

        self.bridge_health = "HEALTHY"
        self.last_bridge_recovery_attempt = 0.0

        # Row caches for the dynamic sensor panels: key -> {"frame": ..., <widget refs>}.
        # Rows are created once per sensor identity and updated in place thereafter - see
        # _sync_rows(). Keeps a poll from tearing down/rebuilding widgets that haven't changed.
        self.fan_rows, self.volt_rows, self.disk_rows = {}, {}, {}
        self.gpu_thermal_rows, self.mobo_rows, self.ram_rows = {}, {}, {}
        self.alert_strip_visible = False
        self.widget_stats = {"rows_created": 0, "rows_destroyed": 0}

        # Workload attribution: main-thread-owned rolling history (worker thread only ever
        # puts a small dict on self.q; this deque is appended to here, on the Tk thread, from
        # update_data - never mutated from the worker thread, so no lock is needed).
        # 30 samples * POLL_SECONDS(2s) = ~60s of rolling context, per the 30-60s window ask.
        self.workload_history = deque(maxlen=30)
        self.last_foreground = None
        self.last_cpu_top = []
        self.last_gpu_top = []

        # Incident history (see INCIDENTS_PATH above). incidents_active is keyed EXACTLY like
        # active_alerts ("cpu", "disk:<parent>", "sensor:gpu_hotspot", "sensor:dimm:DIMM #1")
        # so one incident tracks one physical sensor/health key, same as one alert does.
        # incidents_recent is a light in-app cache (NOT the authoritative source for the
        # History view, which always re-reads the full pruned file via read_incidents_file()).
        #
        # incident_restore_pending holds incidents loaded from ACTIVE_INCIDENTS_PATH at startup
        # that haven't been reconciled against live monitoring yet - see
        # _reconcile_restored_incidents(). They are deliberately kept OUT of incidents_active
        # until then: active_alerts is empty for the first few seconds after every restart
        # (the debounce engine has to reconfirm from scratch, same as any cold boot), and if a
        # restored incident were placed directly into incidents_active, _incident_observe would
        # see "no active alert yet" on the very first tick and immediately (and wrongly) close
        # it as recovered before the debounce engine ever got a chance to reconfirm it.
        # last_component_values tracks the latest raw reading per alert key (independent of
        # whether it's currently in an alert zone) so reconciliation can tell "sensor reads
        # nominal" apart from "sensor isn't reporting at all anymore" (see item 8/scenario G).
        self.incidents_active = {}
        self.incidents_recent = deque(maxlen=500)
        self.incident_restore_pending = {}
        self.last_component_values = {}
        self._active_incidents_dirty = False
        self.last_context = {}
        self.history_window = None
        # Phase 17 - promoted from HistoryWindow so a single AISettingsWindow singleton is shared
        # by both HistoryWindow's "AI Settings" menu entry and AskWindow's own "AI Settings"
        # button; see HistoryWindow.ai_settings_window (the proxying property just above
        # HistoryWindow.__init__) for the compatibility shim that keeps existing callers working.
        self.ai_settings_window = None
        self.connections_window = None
        self._prev_net_adapter = _NET_STATE_UNSET  # v1.1 Phase 4 - see _detect_network_flight_events
        self.network_zone_state = {"confirmed": "GREEN", "pending": {"zone": "GREEN", "since": time.time()}}
        self.sensor_history_windows = {}  # "kind:key" -> SensorHistoryWindow, one per distinct sensor

        # Workload session tracking (see the constants/helpers block above _incident_open et
        # al. for the full design note). workload_sessions holds BOTH unconfirmed candidates
        # and confirmed/active sessions, keyed by canonical workload key - never touched by
        # the incident engine, and touching no incident state itself except read-only lookups
        # into incidents_recent inside _session_link_incidents(). session_restore_pending
        # mirrors incident_restore_pending's "don't touch until reconciled" staging exactly,
        # for exactly the same reason (real per-tick sampling needs a chance to reconfirm
        # activity before a restored session is resumed or closed).
        self.workload_sessions = {}
        self.sessions_recent = deque(maxlen=500)
        self.session_restore_pending = {}
        self.last_workload_activity = {}
        self._session_linked_incident_ids = set()
        self._session_last_tick_time = time.time()
        self._active_sessions_dirty = False
        # Wall clock of the previous live update_data() tick - the only state live suspend/resume
        # detection needs (see MONITORING_DISCONTINUITY_S). Seeded to now so the first tick after
        # startup can never look like a gap.
        self._last_tick_wall_time = time.time()

        # Long-term telemetry history (see the constants/helpers block above _new_session_record
        # for the full design note). Unlike incidents/sessions, completed buckets are NEVER
        # cached in memory here - telemetry_bucket is only ever the single CURRENT in-progress
        # 60s accumulator; everything already finalized is read back from TELEMETRY_PATH on
        # demand (History view open, range change), per item 20's "don't query history on every
        # poll" requirement.
        self.telemetry_bucket = _new_telemetry_bucket(time.time())

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        self.worker_thread = threading.Thread(target=self.worker, daemon=True)
        self.worker_thread.start()
        self.after(100, self.poll)
        self.after(1000, self.tick_uptime)
        self.after(5000, self.check_bridge_health)
        self.after(RECONCILE_DELAY_MS, self._reconcile_restored_incidents)
        self.after(ACTIVE_INCIDENTS_FLUSH_INTERVAL_MS, self._flush_active_incidents_periodic)
        self.after(SESSION_RECONCILE_DELAY_MS, self._reconcile_restored_sessions)
        self.after(SESSION_ACTIVE_FLUSH_INTERVAL_MS, self._flush_active_sessions_periodic)
        self.after(REPORT_STARTUP_DELAY_MS, self._check_due_reports)
        self.after(EVIDENCE_SNAPSHOT_INTERVAL_MS, self._flush_evidence_periodic)

        # Phase 16 - AI Integration Settings. One-time, best-effort load: config only needs to
        # (re)load at startup and whenever AISettingsWindow saves/resets it (via
        # reload_ai_config() below), so no new recurring .after() timer is added here. Any
        # failure - missing file, malformed JSON, wrong schema, a credential_ref that fails to
        # decrypt - leaves ai_config/ai_adapter at their disabled defaults; monitoring is
        # unaffected either way, and this never touches sensor polling, incident/session state,
        # or any other .after() timer's cadence/order.
        self.ai_config = None
        self.ai_adapter = None
        self.reload_ai_config()

    def reload_ai_config(self):
        """(Re)builds the in-memory AI provider config/adapter from the persisted Phase 16
        settings file (ai/ai_settings.py). Called once at startup and again whenever
        AISettingsWindow saves or resets a change - never restarts, re-inits, or otherwise
        touches any monitoring subsystem. Best-effort and defensive: ai_settings.load_provider_
        config() already never raises, but this wraps it too so a defect there can never take
        down App.__init__ or a settings-window save."""
        try:
            self.ai_config = ai_settings.load_provider_config()
        except (OSError, ValueError, TypeError):
            self.ai_config = None
        self.ai_adapter = None if self.ai_config is None else UniversalAIAdapter(self.ai_config)

    # -- scheduled health reports ---------------------------------------
    def _check_due_reports(self):
        """Catch-up generation for COMPLETED periods that have no report yet - the whole scheduling
        model, with no Windows service involved: if the PC was off when a period ended, the report
        is simply generated the next time Thermal Watch runs.

        Deliberately NOT on the telemetry poll. This runs once shortly after startup and then every
        REPORT_DUE_CHECK_INTERVAL_MS, and the common case costs three calendar computations plus
        three primary-key lookups - real analytics run only when a completed period is genuinely
        missing its report. Any failure is swallowed: a report is a convenience, and nothing here
        may ever take down monitoring."""
        if self.stop_event.is_set():
            return
        try:
            created = generate_due_reports()
            for report_id in created:
                self.log_event("INFO", f"Generated scheduled health report: {report_id}")
        except (OSError, sqlite3.DatabaseError, ValueError):
            pass
        self.after(REPORT_DUE_CHECK_INTERVAL_MS, self._check_due_reports)

    # -- layout ---------------------------------------------------------
    def build(self):
        root_pad = tk.Frame(self, bg=BG); root_pad.pack(fill="both", expand=True, padx=20, pady=16)

        # Fixed area: header + alert strip never scroll away, per the layout fix -
        # everything else lives in the scrollable `page` below so panels can never
        # be pushed outside the visible window again regardless of content height.
        top = tk.Frame(root_pad, bg=BG); top.pack(fill="x")

        # header
        header = tk.Frame(top, bg=BG, highlightthickness=0)
        header.pack(fill="x")
        tk.Frame(top, bg=BORDER, height=1).pack(fill="x", pady=(8, 10))

        title_box = tk.Frame(header, bg=BG); title_box.pack(side="left")
        tk.Label(title_box, text="THERMAL", bg=BG, fg=TEXT, font=(MONO, 15, "bold")).pack(side="left")
        tk.Label(title_box, text="WATCH", bg=BG, fg=ORANGE, font=(MONO, 15, "bold")).pack(side="left")
        tk.Label(title_box, text=f"v{APP_VERSION}", bg=BG, fg=DIM, font=(MONO, 9)).pack(side="left", padx=(10, 0), pady=(4, 0))

        hw = tk.Frame(header, bg=BG, highlightthickness=1, highlightbackground=BORDER)
        self.hw_label = tk.Label(hw, text=self.info.get("cpu", "CPU"), bg=BG, fg="#c7ccd4", font=(SANS, 10))
        self.hw_label.pack(padx=10, pady=4)
        hw.pack(side="left", padx=16)

        self.alert_badge = tk.Label(header, text="", bg=ALERT_BG, fg=ORANGE, font=(MONO, 9, "bold"))
        self.live_badge = tk.Label(header, text="\u25cf LIVE \u00b7 2s", bg=BG, fg=GREEN, font=(MONO, 9))
        self.live_badge.pack(side="right", padx=(10, 0))
        self.alert_badge.pack(side="right", padx=(10, 0))

        export_btn = tk.Button(header, text="EXPORT CSV", command=self.export, bg="#181b1f", fg=TEXT,
                                activebackground="#22252b", activeforeground=TEXT, relief="flat",
                                font=(MONO, 9), padx=12, pady=6, bd=0, cursor="hand2")
        export_btn.pack(side="right")

        # Only main-dashboard change for the incident-history feature: one small button that
        # opens a separate Toplevel (HistoryWindow) - history/analytics never appear inline.
        history_btn = tk.Button(header, text="HISTORY", command=self.open_history, bg="#181b1f", fg=TEXT,
                                activebackground="#22252b", activeforeground=TEXT, relief="flat",
                                font=(MONO, 9), padx=12, pady=6, bd=0, cursor="hand2")
        history_btn.pack(side="right", padx=(0, 8))

        # alert strip - stays in the fixed `top` area (not the scrollable page) so it's
        # always visible; toggled via pack()/pack_forget() in update_data, no ordering
        # trick needed since it's no longer a sibling of `cards`.
        self.alert_strip = tk.Frame(top, bg=ALERT_STRIP_BG, highlightthickness=1, highlightbackground=ALERT_BORDER)
        self.alert_tag = tk.Label(self.alert_strip, text="ALERT", bg=ORANGE, fg=BG, font=(MONO, 9, "bold"), padx=12, pady=8)
        self.alert_tag.pack(side="left", fill="y")
        self.alert_text = tk.Label(self.alert_strip, text="", bg=ALERT_STRIP_BG, fg="#a8865c", font=(SANS, 10),
                                    anchor="w", justify="left")
        self.alert_text.pack(side="left", fill="x", expand=True, padx=12)
        btns = tk.Frame(self.alert_strip, bg=ALERT_STRIP_BG); btns.pack(side="right", padx=10, pady=6)
        tk.Button(btns, text="SILENCE 15M", command=self.silence, bg=ALERT_STRIP_BG, fg="#c7ccd4",
                  activebackground=ALERT_STRIP_BG, relief="flat", bd=1, font=(MONO, 8), padx=8,
                  highlightthickness=0, cursor="hand2").pack(side="left", padx=4)
        # strip stays unpacked until an active, unsilenced alert exists

        # scrollable body: everything below the fixed header/alert area
        page = ScrollFrame(root_pad, bg=BG, height=1)
        page.pack(fill="both", expand=True, pady=(12, 0))
        outer = page.inner

        # primary readouts
        cards = tk.Frame(outer, bg=BG); cards.pack(fill="x", pady=(0, 0))
        self.cards = cards
        for i in range(3):
            cards.grid_columnconfigure(i, weight=1, uniform="cards")
        self.cpu_card = MetricCard(cards, "CPU PACKAGE", "\u00b0C", GREEN, CPU_ORANGE, TJMAX, zone_fn=cpu_zone_for,
                                    ticks=[(CPU_YELLOW, AMBER), (CPU_ORANGE, ORANGE), (CPU_RED, RED)])
        self.gpu_card = MetricCard(cards, "GPU CORE", "\u00b0C", GREEN, 83.0, GPU_TMAX,
                                    zone_fn=lambda v: zone_for(v, GPU_CORE_ZONES),
                                    ticks=[(75.0, AMBER), (83.0, ORANGE), (90.0, RED)])
        self.mem_card = MetricCard(cards, "SYSTEM MEMORY", "%", BLUE, THRESH_MEM, 100)
        self.cpu_card.set_footer(f"TJMAX {TJMAX:.0f} \u00b7 80 WARN \u00b7 90 CRIT \u00b7 100 EMERGENCY")
        self.gpu_card.set_footer(f"MAX {GPU_TMAX:.0f} \u00b7 75 WARM \u00b7 83 HOT \u00b7 90 CRITICAL")
        self.mem_card.set_footer(f"ALERT AT {THRESH_MEM:.0f}%")
        for i, c in enumerate((self.cpu_card, self.gpu_card, self.mem_card)):
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
        # Sensor drill-down (item 7) - CPU Package/GPU Core are clickable straight to their
        # history; System Memory is left alone (not a per-tick telemetry scalar sensor in the
        # same sense - it's a live utilization card, not something item 2 asks to drill into).
        self._bind_click(self.cpu_card, lambda: self.open_sensor_history(scalar_sensor_ref("cpu_temp")))
        self._bind_click(self.gpu_card, lambda: self.open_sensor_history(scalar_sensor_ref("gpu_core_temp")))

        # chart + event log - bounded height (not expand=True) so this row can't greedily
        # consume all remaining space and push the panels below it out of view; the event
        # log panel scrolls internally instead (Panel(..., scrollable=True) below).
        mid = tk.Frame(outer, bg=BG, height=340); mid.pack_propagate(False)
        mid.pack(fill="x", pady=(12, 0))
        mid.grid_columnconfigure(0, weight=1)
        mid.grid_columnconfigure(1, minsize=340)
        mid.grid_rowconfigure(0, weight=1)

        chart_panel = tk.Frame(mid, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        chart_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        chead = tk.Frame(chart_panel, bg=PANEL); chead.pack(fill="x", padx=16, pady=(12, 4))
        tk.Label(chead, text="TEMPERATURE HISTORY", bg=PANEL, fg=MUTED, font=(MONO, 9)).pack(side="left")
        self.range_buttons = {}
        rbtn_box = tk.Frame(chead, bg=PANEL); rbtn_box.pack(side="right")
        for label, seconds in RANGES:
            b = tk.Button(rbtn_box, text=label, font=(MONO, 8), bd=1, relief="flat", padx=8, pady=3,
                          command=lambda s=seconds, l=label: self.set_range(s, l), cursor="hand2")
            b.pack(side="left", padx=2)
            self.range_buttons[label] = b
        legend = tk.Frame(chead, bg=PANEL); legend.pack(side="right", padx=(0, 14))
        tk.Label(legend, text="\u2014 CPU", bg=PANEL, fg=ORANGE, font=(MONO, 8)).pack(side="left", padx=4)
        tk.Label(legend, text="\u2014 GPU", bg=PANEL, fg=GREEN, font=(MONO, 8)).pack(side="left", padx=4)

        self.chart = HistoryChart(chart_panel)
        self.chart.pack(fill="both", expand=True, padx=16, pady=(0, 4))
        cfoot = tk.Frame(chart_panel, bg=PANEL); cfoot.pack(fill="x", padx=16, pady=(0, 12))
        self.chart_axis_label = tk.Label(cfoot, text="-60 MIN", bg=PANEL, fg=DIM, font=(MONO, 8))
        self.chart_axis_label.pack(side="left")
        tk.Label(cfoot, text="80\u00b0C WARN \u00b7 90\u00b0C CRIT \u00b7 100\u00b0C EMERGENCY", bg=PANEL, fg=DIM, font=(MONO, 8)).pack(
            side="left", expand=True)
        tk.Label(cfoot, text="NOW", bg=PANEL, fg=DIM, font=(MONO, 8)).pack(side="right")
        self.current_range_label = "1H"
        self._style_range_buttons()

        # event log
        log_panel = Panel(mid, "EVENT LOG", scrollable=True)
        log_panel.grid(row=0, column=1, sticky="nsew")
        self.log_body = log_panel.body
        self.log_scroll = log_panel.scroll
        self.log_rows = []  # ordered newest-first, parallel to what's actually displayed
        self.log_empty = self._make_empty_label(log_panel, "No events yet.")
        self.log_empty_shown = False
        tk.Button(log_panel.foot, text="CLEAR LOG", command=self.clear_log, bg=PANEL, fg=MUTED,
                  activebackground=PANEL, relief="flat", bd=1, font=(MONO, 8), padx=6, pady=5,
                  highlightthickness=1, highlightbackground=BORDER, cursor="hand2").pack(fill="x")

        # sensor panels
        sensors = tk.Frame(outer, bg=BG); sensors.pack(fill="x", pady=(12, 0))
        for i in range(3):
            sensors.grid_columnconfigure(i, weight=1, uniform="sensors")
        self.fan_panel = Panel(sensors, "FANS & PUMP")
        self.volt_panel = Panel(sensors, "VOLTAGES")
        self.disk_panel = Panel(sensors, "DRIVE TEMPS")
        for i, p in enumerate((self.fan_panel, self.volt_panel, self.disk_panel)):
            p.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
        self.curve_label = tk.Label(self.fan_panel.foot, text="CURVE: AUTO", bg=PANEL, fg=DIM, font=(MONO, 9))
        self.curve_label.pack(side="left")
        self.volt_dev_label = tk.Label(self.volt_panel.foot, text="RAIL DEVIATION MAX --", bg=PANEL, fg=DIM, font=(MONO, 9))
        self.volt_dev_label.pack(side="left")
        self.disk_smart_label = tk.Label(self.disk_panel.foot,
                                         text=f"{DRIVE_YELLOW:.0f} WARM \u00b7 {DRIVE_ORANGE:.0f} HOT \u00b7 {DRIVE_RED:.0f} CRITICAL",
                                         bg=PANEL, fg=DIM, font=(MONO, 9))
        self.disk_smart_label.pack(side="left")
        self.fan_empty = self._make_empty_label(self.fan_panel, "No fan sensors detected.")
        self.volt_empty = self._make_empty_label(self.volt_panel, "No voltage sensors detected.")
        self.disk_empty = self._make_empty_label(self.disk_panel, "No drive sensors detected.")
        self.fan_empty_shown = self.volt_empty_shown = self.disk_empty_shown = False

        # component health panels (GPU sub-sensors, motherboard/chipset, RAM) - same Panel
        # widget and grid pattern as the row above, not a redesign.
        sensors2 = tk.Frame(outer, bg=BG); sensors2.pack(fill="x", pady=(12, 0))
        for i in range(3):
            sensors2.grid_columnconfigure(i, weight=1, uniform="sensors2")
        self.gpu_thermal_panel = Panel(sensors2, "GPU THERMAL")
        self.mobo_panel = Panel(sensors2, "MOTHERBOARD / CHIPSET")
        self.ram_panel = Panel(sensors2, "RAM TEMPS")
        for i, p in enumerate((self.gpu_thermal_panel, self.mobo_panel, self.ram_panel)):
            p.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 6, 0 if i == 2 else 6))
        tk.Label(self.gpu_thermal_panel.foot, text="75 WARM \u00b7 85/95/105 (hotspot) \u00b7 90/100/105 (vram)",
                 bg=PANEL, fg=DIM, font=(MONO, 9)).pack(side="left")
        self.mobo_footer_label = tk.Label(self.mobo_panel.foot,
                                          text="No confidently-known safe ranges - raw readings only",
                                          bg=PANEL, fg=DIM, font=(MONO, 9))
        self.mobo_footer_label.pack(side="left")
        tk.Label(self.ram_panel.foot, text=f"{RAM_ZONES[2][0]:.0f} WARM \u00b7 {RAM_ZONES[1][0]:.0f} HOT \u00b7 {RAM_ZONES[0][0]:.0f} CRITICAL",
                 bg=PANEL, fg=DIM, font=(MONO, 9)).pack(side="left")
        self.gpu_thermal_empty = self._make_empty_label(self.gpu_thermal_panel, "No GPU sensors detected.")
        self.mobo_empty = self._make_empty_label(self.mobo_panel, "No motherboard/chipset sensors detected.")
        self.ram_empty = self._make_empty_label(self.ram_panel, "No DIMM temperature sensors detected.")
        self.gpu_thermal_empty_shown = self.mobo_empty_shown = self.ram_empty_shown = False

        # network (v1.1 Phase 1 - Network Foundation) - a new, purely additive row below the
        # existing sensor panels; nothing above is resized, moved, or restyled. One full-width
        # panel rather than the 3-column grid fan/voltage/drive use, since network has ONE
        # active-adapter summary to show (whichever adapter GetBestInterfaceEx says currently
        # carries real internet traffic - see active_network_snapshot()), not a variable-length
        # per-sensor list.
        net_row = tk.Frame(outer, bg=BG); net_row.pack(fill="x", pady=(12, 0))
        self.net_panel = Panel(net_row, "NETWORK")
        self.net_panel.pack(fill="x")

        net_head = tk.Frame(self.net_panel.body, bg=PANEL); net_head.pack(fill="x", pady=(0, 10))
        self.net_adapter_label = tk.Label(net_head, text="--", bg=PANEL, fg=TEXT, font=(MONO, 11))
        self.net_adapter_label.pack(side="left")
        self.net_state_label = tk.Label(net_head, text="", bg=PANEL, fg=DIM, font=(MONO, 9))
        self.net_state_label.pack(side="left", padx=(10, 0))
        self.net_speed_label = tk.Label(net_head, text="", bg=PANEL, fg=DIM, font=(MONO, 9))
        self.net_speed_label.pack(side="right")

        net_rates = tk.Frame(self.net_panel.body, bg=PANEL); net_rates.pack(fill="x")
        for i in range(2):
            net_rates.grid_columnconfigure(i, weight=1, uniform="net_rates")

        def net_rate_cell(col, label_text):
            cell = tk.Frame(net_rates, bg=PANEL)
            cell.grid(row=0, column=col, sticky="w", padx=(0 if col == 0 else 24, 0))
            tk.Label(cell, text=label_text, bg=PANEL, fg=DIM, font=(MONO, 8)).pack(anchor="w")
            val = tk.Label(cell, text="--", bg=PANEL, fg=TEXT, font=(MONO, 20))
            val.pack(anchor="w")
            return val

        self.net_down_label = net_rate_cell(0, "DOWNLOAD")
        self.net_up_label = net_rate_cell(1, "UPLOAD")

        net_detail = tk.Frame(self.net_panel.body, bg=PANEL); net_detail.pack(fill="x", pady=(10, 0))
        self.net_detail_labels = {}
        # "TOTAL" here means cumulative since the adapter itself last came up (the raw OS/driver
        # counter - InOctets/OutOctets), NOT since Thermal Watch started watching. Labeling this
        # "session" would imply a Thermal-Watch-scoped counter that doesn't exist yet.
        for key, label_text in (("rx", "TOTAL RX"), ("tx", "TOTAL TX"),
                                ("ip", "IP ADDRESS"), ("gw", "GATEWAY"), ("signal", "WI-FI SIGNAL"),
                                ("connections", "CONNECTIONS")):
            cell = tk.Frame(net_detail, bg=PANEL); cell.pack(side="left", padx=(0, 28))
            tk.Label(cell, text=label_text, bg=PANEL, fg=DIM, font=(MONO, 8)).pack(anchor="w")
            val = tk.Label(cell, text="--", bg=PANEL, fg=MUTED, font=(MONO, 10))
            val.pack(anchor="w")
            self.net_detail_labels[key] = {"cell": cell, "val": val}
        self._bind_click(net_rates, lambda: self.open_sensor_history(scalar_sensor_ref("net_down_mbps")))
        # v1.1 Phase 3 - clicking the connection count opens the full live list (same "clickable
        # summary -> detail window" convention as net_rates -> SensorHistoryWindow above).
        self._bind_click(self.net_detail_labels["connections"]["cell"], self.open_connections_window)

        # Per-process network (v1.1 Phase 2) - purely additive below the existing adapter detail
        # row, same panel rather than a second one, since it's still "network" at a glance. Rows
        # are rebuilt each tick (see _update_network_process_list()), same pattern as
        # RecommendationsWindow's card list - cheap at up to NET_TOP_PROCESS_COUNT rows/2s.
        net_proc_header = tk.Frame(self.net_panel.body, bg=PANEL); net_proc_header.pack(fill="x", pady=(12, 4))
        tk.Label(net_proc_header, text="TOP PROCESSES", bg=PANEL, fg=DIM, font=(MONO, 8)).pack(side="left")
        self.net_proc_list = tk.Frame(self.net_panel.body, bg=PANEL)
        self.net_proc_list.pack(fill="x")

        tk.Label(self.net_panel.foot,
                text="Active adapter only - whichever one Windows currently uses to reach the internet",
                bg=PANEL, fg=DIM, font=(MONO, 9)).pack(side="left")
        self.net_empty = self._make_empty_label(self.net_panel, "No active network adapter detected.")
        self.net_empty_shown = False

        # stat strip
        strip = tk.Frame(outer, bg=BG); strip.pack(fill="x", pady=(12, 0))
        for i in range(6):
            strip.grid_columnconfigure(i, weight=1, uniform="stats")
        self.stat_labels = {}
        for i, (key, label) in enumerate((("cpu_load", "CPU LOAD"), ("cpu_clock", "MAX CLOCK"),
                                           ("gpu_load", "GPU LOAD"), ("gpu_mem", "GPU MEMORY"),
                                           ("gpu_power", "GPU POWER"), ("cpu_power", "CPU POWER"))):
            cell = tk.Frame(strip, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
            cell.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 3, 0 if i == 5 else 3))
            tk.Label(cell, text=label, bg=PANEL, fg=DIM, font=(MONO, 8)).pack(anchor="w", padx=12, pady=(8, 2))
            lbl = tk.Label(cell, text="--", bg=PANEL, fg=TEXT, font=(MONO, 13)); lbl.pack(anchor="w", padx=12, pady=(0, 8))
            self.stat_labels[key] = lbl
            # Sensor drill-down (item 7): only the two power readings are their own tracked
            # telemetry scalar in the sense item 2 asks for - load/clock/memory here are either
            # already covered elsewhere (CPU/GPU Core cards) or not a historized scalar.
            if key in ("cpu_power", "gpu_power"):
                self._bind_click(cell, lambda key=key: self.open_sensor_history(scalar_sensor_ref(key)))

        # footer
        foot = tk.Frame(outer, bg=BG); foot.pack(fill="x", pady=(12, 0))
        self.sensor_status = tk.Label(foot, text="Looking for sensors\u2026", bg=BG, fg=DIM, font=(MONO, 9))
        self.sensor_status.pack(side="left")
        self.uptime_label = tk.Label(foot, text="UPTIME 00:00:00", bg=BG, fg=DIM, font=(MONO, 9))
        self.uptime_label.pack(side="right")

        self.load_events()
        self.log_event("INFO", f"Polling interval set to {POLL_SECONDS * 1000}ms")
        self.load_incidents()
        self.load_active_incidents()
        self.load_sessions()
        self.load_active_sessions()
        self.init_telemetry_store()

    def _style_range_buttons(self):
        for label, b in self.range_buttons.items():
            on = label == self.current_range_label
            b.config(bg=BORDER if on else BG, fg=TEXT if on else DIM,
                     highlightbackground="#3a3f47" if on else BORDER, highlightthickness=1,
                     activebackground=BORDER)

    def set_range(self, seconds, label):
        self.current_range_label = label
        self._style_range_buttons()
        self.chart.set_range(seconds)
        axis = {"15M": "-15 MIN", "1H": "-60 MIN", "6H": "-6 HR", "24H": "-24 HR"}[label]
        self.chart_axis_label.config(text=axis)

    def silence(self):
        self.silence_until = time.time() + 15 * 60
        self.alert_strip.pack_forget()
        self.alert_strip_visible = False
        self.log_event("INFO", "Alerts silenced for 15 minutes")

    def clear_log(self):
        self.events.clear()
        try:
            EVENT_LOG_PATH.write_text("", encoding="utf-8")
        except OSError:
            pass
        self.render_log()

    # -- background worker ----------------------------------------------
    def worker(self):
        tick = 0
        # Workload-attribution state lives entirely on this thread (never touched by the Tk
        # main thread) - only the resulting small dict crosses over, via the same queue as
        # everything else, per the "reuse the existing snapshot/queue architecture" ask.
        prev_proc_times = {}
        prev_sample_time = time.time()
        gpu_sampler = GpuProcessSampler()
        # Network: prev_net carries the previous tick's (adapter index, byte counters, time) so
        # active_network_snapshot() can compute a real Mbps rate - same "keep last sample on this
        # thread" pattern as prev_proc_times above, never touched by the Tk main thread. IP/
        # gateway/Wi-Fi signal change far less often than a 2s tick needs, so they're refreshed
        # only every NET_SLOW_REFRESH_TICKS ticks (same throttling idea as the tick%2 LHM call
        # below) rather than paying GetAdaptersAddresses/WLAN handle-open cost every single tick;
        # the last known values are carried forward on the ticks in between, never blanked out.
        prev_net = {}
        net_slow = {"index": None, "ip_info": None, "wifi_signal": None}
        # Per-process network (v1.1 Phase 2): prev_net_proc carries the previous tick's per-PID
        # cumulative byte counters, same role as prev_net above but keyed by PID instead of a
        # single adapter - see process_network_rates(). Correctly empty/inactive on a bridge
        # that predates this feature or hasn't started ETW capture (e.g. still unprivileged).
        prev_net_proc = {}
        # Connection intelligence (v1.1 Phase 3): conn_name_cache persists across ticks so a PID
        # already resolved once (e.g. a long-lived browser holding dozens of connections) never
        # pays a fresh OpenProcess/QueryFullProcessImageNameW per tick - see active_connections().
        conn_name_cache = {}
        while not self.stop_event.is_set():
            old_idle, old_total = self.last_cpu; now = cpu_times(); self.last_cpu = now
            dt = now[1] - old_total; load = 100 * (1 - (now[0] - old_idle) / dt) if dt else 0
            mem_pct, mem_used, mem_total = memory(); gpus = nvidia_stats()
            lhm = lhm_sensors() if tick % 2 == 0 else None

            net, prev_net = active_network_snapshot(prev_net)
            net_idx = net["adapter"]["index"] if net["adapter"] else None
            if tick % NET_SLOW_REFRESH_TICKS == 0 or net_slow["index"] != net_idx:
                net_slow = {"index": net_idx,
                           "ip_info": adapter_ip_info(net_idx) if net_idx is not None else None,
                           "wifi_signal": wifi_signal_percent() if net["adapter"] and net["adapter"]["type"] == "Wi-Fi" else None}
            net["ip_info"] = net_slow["ip_info"]
            net["wifi_signal"] = net_slow["wifi_signal"]

            netproc_payload = network_processes()
            netproc_rates, prev_net_proc = process_network_rates(netproc_payload, prev_net_proc)
            netproc_rates.sort(key=lambda r: (r["down_mbps"] or 0) + (r["up_mbps"] or 0), reverse=True)
            net_procs = {"capture_active": netproc_payload.get("capture_active", False),
                        "capture_error": netproc_payload.get("capture_error"),
                        "top": netproc_rates[:NET_TOP_PROCESS_COUNT]}

            connections = active_connections(conn_name_cache)

            sample_time = time.time()
            curr_proc_times = _sample_process_cpu_times()
            cpu_top = cpu_top_processes(prev_proc_times, curr_proc_times, sample_time - prev_sample_time)
            names_by_pid = {pid: name for pid, (name, _) in curr_proc_times.items()}
            gpu_top = gpu_top_processes(gpu_sampler.sample(), names_by_pid)
            workload = {"time": sample_time, "foreground": foreground_process(),
                       "cpu_top": cpu_top, "gpu_top": gpu_top}
            prev_proc_times, prev_sample_time = curr_proc_times, sample_time

            self.q.put({"time": datetime.now(), "cpu_load": load, "mem_pct": mem_pct, "mem_used": mem_used,
                        "mem_total": mem_total, "gpus": gpus, "lhm": lhm, "workload": workload, "net": net,
                        "net_procs": net_procs, "connections": connections})
            tick += 1
            self.stop_event.wait(POLL_SECONDS)

    def poll(self):
        try:
            while True:
                self.update_data(self.q.get_nowait())
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.after(200, self.poll)

    def tick_uptime(self):
        self.uptime_label.config(text=f"UPTIME {fmt_hms(time.time() - self.start_time)}")
        if not self.stop_event.is_set():
            self.after(1000, self.tick_uptime)

    def check_bridge_health(self):
        """Runs every 5s on the Tk main thread (cheap: one small file read, no subprocess).
        Tracks self.bridge_health for future UI use and, when Tier 1 is stale/missing, attempts
        a rate-limited recovery - never more often than BRIDGE_RECOVERY_MIN_INTERVAL_S, and
        never blocking (spawn_bridge_recovery is fire-and-forget). Tier 2/3 fallback inside
        lhm_sensors() is untouched and keeps working regardless of what happens here."""
        age = bridge_tier1_age()
        status = bridge_status()
        health = compute_bridge_health(age, status)

        if health in ("STALE", "MISSING", "ERROR"):
            now = time.time()
            if now - self.last_bridge_recovery_attempt >= BRIDGE_RECOVERY_MIN_INTERVAL_S:
                self.last_bridge_recovery_attempt = now
                if spawn_bridge_recovery():
                    self.bridge_health = "RESTARTING"
                    self.log_event("INFO", "Sensor bridge stale/unavailable - attempting automatic recovery")
                else:
                    self.bridge_health = health
            # else: recovery was already attempted recently: keep reporting the real
            # health rather than sitting in RESTARTING forever if it didn't come back.
            elif self.bridge_health == "RESTARTING" and now - self.last_bridge_recovery_attempt > 15:
                self.bridge_health = health
        else:
            self.bridge_health = health

        if not self.stop_event.is_set():
            self.after(5000, self.check_bridge_health)

    # -- data -> UI --------------------------------------------------------
    @staticmethod
    def _colors_for(kind):
        return {"WARN": (ORANGE2, ALERT_BORDER), "CRIT": (AMBER, "#4a3a15"), "INFO": (MUTED, BORDER),
                "NETWORK": (BLUE, "#1f3a52")}.get(kind, (MUTED, BORDER))

    def load_events(self):
        """Restore the event log from disk and prune entries past the retention window."""
        if not EVENT_LOG_PATH.exists():
            return
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        kept = []
        try:
            for line in EVENT_LOG_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue  # valid JSON but not a record - never treat it as one
                if rec.get("ts", 0) >= cutoff:
                    kept.append(rec)
        except OSError:
            return
        for rec in kept:
            fg, border = self._colors_for(rec.get("kind", "INFO"))
            self.events.appendleft({"time": datetime.fromtimestamp(rec["ts"]).strftime("%H:%M:%S"),
                                    "kind": rec.get("kind", "INFO"), "fg": fg, "border": border,
                                    "text": rec.get("text", ""), "meta": rec.get("meta")})
        # Atomic rewrite: a crash mid-prune must never leave this store truncated (see
        # atomic_write_lines). On failure the pre-prune file stays intact, which is the safe
        # outcome - stale records are harmless, a lost history is not.
        atomic_write_lines(EVENT_LOG_PATH, [json.dumps(rec) for rec in kept])
        self.render_log()  # startup only: many rows arrive at once, a full build is correct here

    def load_incidents(self):
        """Restore completed incidents from disk and prune ones past the retention window.
        Never touches incidents_active (nothing active is ever written to this file - see the
        documented restart limitation in __init__)."""
        if not INCIDENTS_PATH.exists():
            return
        cutoff = time.time() - INCIDENT_RETENTION_DAYS * 86400
        kept = []
        try:
            for line in INCIDENTS_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue  # valid JSON but not a record - never treat it as one
                if rec.get("end_timestamp", 0) >= cutoff:
                    kept.append(rec)
        except OSError:
            return
        for rec in kept:
            self.incidents_recent.appendleft(rec)
        # Atomic rewrite - see atomic_write_lines. A truncated incident history is unrecoverable;
        # keeping the pre-prune file on failure is always the better outcome.
        atomic_write_lines(INCIDENTS_PATH, [json.dumps(rec) for rec in kept])

    def _persist_incident(self, inc):
        try:
            with INCIDENTS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(inc) + "\n")
        except OSError:
            pass

    # -- active-incident durability (survive close/crash/restart) --------------------------
    @staticmethod
    def _incident_to_persistable(key, inc):
        """In-memory incident -> JSON-safe dict for ACTIVE_INCIDENTS_PATH. Keeps field names
        matching the completed-incident schema (task item 2: "preserve existing field names").
        _workload_tally (internal, underscore-prefixed to keep it out of completed records) is
        renamed to the plain, persisted `workload_tally`; _bias is dropped since it's cheaply
        re-derivable from `component` via INCIDENT_BIAS and isn't part of the incident's own
        data. `evidence_id` (Phase 14) is not assigned until close, so a still-ACTIVE incident
        simply has no such key here - the generic dict comprehension below already round-trips
        it unchanged for any incident that DOES carry one (e.g. one snapshotted mid-restore)."""
        # _live_gap_pending is dropped alongside _bias: it is single-tick live state meaning
        # "the very next observation is the first since a gap". Persisting it would let a stale
        # flag survive a restart and wrongly mark a much later, ordinary recovery as having
        # happened during a gap. After a restart the restore path derives its own gap anyway.
        clean = {k: v for k, v in inc.items() if k not in ("_bias", "_live_gap_pending")}
        if "_workload_tally" in clean:
            clean["workload_tally"] = clean.pop("_workload_tally")
        clean["alert_key"] = key
        return clean

    @staticmethod
    def _incident_from_persisted(rec):
        """Reverse of _incident_to_persistable() - restores the internal-only fields
        _incident_touch()/_incident_close() expect."""
        inc = {k: v for k, v in rec.items() if k != "alert_key"}
        inc["_workload_tally"] = inc.pop("workload_tally", None) or {}
        inc["_bias"] = INCIDENT_BIAS.get(inc.get("component"), "cpu")
        return inc

    def _save_active_incidents(self):
        """Atomic write (temp file + replace) of every incident that's either fully active or
        still awaiting post-restart reconciliation - so a crash mid-write can never corrupt
        this file into something worse than "slightly stale", and never touches
        INCIDENTS_PATH (completed incidents) at all."""
        try:
            merged = dict(self.incidents_active)
            merged.update(self.incident_restore_pending)
            snapshot = {key: self._incident_to_persistable(key, inc) for key, inc in merged.items()}
            payload = {"saved_at": time.time(), "incidents": snapshot}
            tmp = ACTIVE_INCIDENTS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(ACTIVE_INCIDENTS_PATH)
        except OSError:
            pass

    def load_active_incidents(self):
        """Loads incidents that were active when Thermal Watch last stopped into
        incident_restore_pending (NOT incidents_active - see the comment in __init__ on why).
        Tolerates a missing/empty/malformed/truncated file entirely - always safe to start with
        nothing restored rather than risk a crash on damaged recovery metadata (item 8)."""
        self.incident_restore_pending = {}
        if not ACTIVE_INCIDENTS_PATH.exists():
            return
        try:
            raw = ACTIVE_INCIDENTS_PATH.read_text(encoding="utf-8")
            if not raw.strip():
                return
            payload = json.loads(raw)
            incidents = payload.get("incidents") if isinstance(payload, dict) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.log_event("WARN", "Saved active-incident state was unreadable (starting fresh) - "
                                  "possibly an unclean previous shutdown")
            return
        if not isinstance(incidents, dict):
            return
        # Idempotency guard (item 9): an incident already present in the completed history -
        # whether from the capped in-memory cache or the full on-disk file - was already closed
        # properly; a matching "active" entry here is just a stale leftover from a write that
        # completed the JSONL append but got interrupted before the active-state file could be
        # updated to drop it. Discard silently rather than resuming or re-closing it.
        completed_ids = {i.get("incident_id") for i in self.incidents_recent}
        completed_ids |= {i.get("incident_id") for i in read_incidents_file()}
        restored = 0
        for key, rec in incidents.items():
            if not isinstance(rec, dict) or not rec.get("incident_id"):
                continue  # malformed entry - skip rather than crash
            if not all(f in rec for f in ("start_timestamp", "component", "sensor_name")):
                continue
            if rec["incident_id"] in completed_ids:
                continue
            self.incident_restore_pending[key] = rec
            restored += 1
        if restored:
            self.log_event("INFO", f"Found {restored} thermal incident(s) still active before the last shutdown - "
                                  f"reconciling against current sensors...")

    def _reconcile_restored_incidents(self):
        """Runs once, RECONCILE_DELAY_MS after startup - long enough for the (untouched) zone
        engines to get a fair chance to reconfirm a still-hot sensor from scratch. For each
        restored incident: if that alert key is active again right now, RESUME it (same
        incident_id/start/peak/tally, zone.max updated if it's escalated further); otherwise
        close it with explicit monitoring-gap/recovery-uncertainty metadata rather than
        pretending to know what happened while offline."""
        if self.stop_event.is_set() or not self.incident_restore_pending:
            return
        now = time.time()
        for key in list(self.incident_restore_pending.keys()):
            rec = self.incident_restore_pending.pop(key)
            last_observed = rec.get("last_observed_timestamp") or rec.get("start_timestamp") or now
            gap = {"last_sample_before": last_observed, "first_sample_after": now,
                  "gap_seconds": max(0.0, now - last_observed)}
            live_alert = self.active_alerts.get(key)
            sensor_name = rec.get("sensor_name", "Sensor")
            if live_alert:
                inc = self._incident_from_persisted(rec)
                inc.setdefault("monitoring_gaps", []).append(gap)
                inc["monitoring_gap_seconds"] = inc.get("monitoring_gap_seconds", 0.0) + gap["gap_seconds"]
                live_zone = live_alert.get("zone")
                # We know it's back to (at least) this zone NOW - we do not claim to know
                # exactly when during the gap it got there (item 6/scenario D).
                if live_zone and ZONE_SEVERITY.get(live_zone, 0) > ZONE_SEVERITY.get(inc.get("max_zone", "GREEN"), 0):
                    inc["max_zone"] = live_zone
                live_value = self.last_component_values.get(key)
                if live_value is not None:
                    samples = inc.setdefault("samples", [])
                    samples.append([now, live_value])  # a REAL reading at a REAL time, never fabricated
                    if len(samples) > INCIDENT_MAX_SAMPLES:
                        inc["samples"] = samples[::2]
                self.incidents_active[key] = inc
                self.log_event("INFO", f"{sensor_name} — resumed after a {fmt_dur(gap['gap_seconds'])} "
                                      f"monitoring gap (currently {live_zone or '?'})")
            else:
                value_now = self.last_component_values.get(key)
                reason = "recovered_during_gap" if value_now is not None else "sensor_unavailable"
                self._close_incident_after_gap(rec, gap, reason, value_now)
        self._save_active_incidents()

    def _close_incident_after_gap(self, rec, gap, reason, value_now):
        """Closes a restored incident whose sensor is NOT currently alerting. Never invents an
        exact recovery moment or value - only records the true monitored bound (last confirmed
        alert reading) and the true post-gap bound (first reading after monitoring resumed),
        and flags duration_exact=False so nothing downstream mistakes the gap-spanning
        duration_seconds for a precisely measured one."""
        inc = self._incident_from_persisted(rec)
        now = gap["first_sample_after"]
        inc["end_timestamp"] = now
        inc["duration_seconds"] = now - inc["start_timestamp"]
        inc["duration_exact"] = False
        inc["monitored_duration_seconds"] = gap["last_sample_before"] - inc["start_timestamp"]
        inc.setdefault("monitoring_gaps", []).append(gap)
        inc["monitoring_gap_seconds"] = inc.get("monitoring_gap_seconds", 0.0) + gap["gap_seconds"]
        inc["recovery_during_monitoring_gap"] = (reason == "recovered_during_gap")
        inc["close_reason"] = reason
        inc["last_observed_alert_timestamp"] = gap["last_sample_before"]
        inc["first_observed_recovered_timestamp"] = now if reason == "recovered_during_gap" else None
        inc["first_observed_recovered_value"] = value_now  # a real reading, or None if truly unavailable
        inc["recovery_value"] = None  # genuinely unknown - never fabricated
        tally = inc.pop("_workload_tally", {})
        inc["dominant_workload"] = self._dominant_workload(tally) or "Not identified"
        inc.pop("_bias", None)
        assign_incident_evidence_id(inc)  # Phase 14: freeze once, right before the first persist
        self.incidents_recent.appendleft(inc)
        self._persist_incident(inc)
        sensor_name = inc.get("sensor_name", "Sensor")
        if reason == "recovered_during_gap":
            self.log_event("INFO", f"{sensor_name} — recovered while monitoring was offline "
                                  f"(gap {fmt_dur(gap['gap_seconds'])}, peak {inc.get('peak_value')})")
        else:
            self.log_event("WARN", f"{sensor_name} — sensor no longer available after restart; "
                                  f"incident closed (gap {fmt_dur(gap['gap_seconds'])})")

    # -- live suspend/resume (the same gap semantics, without a restart) --------------------
    def _detect_monitoring_discontinuity(self):
        """Called at the very top of update_data(), before any engine observes this tick.
        Returns the standard gap dict if the wall clock jumped far enough to count as
        unmonitored time, else None.

        This is the live counterpart to _reconcile_restored_incidents(): identical gap dict
        shape ({last_sample_before, first_sample_after, gap_seconds}), identical
        duration_exact/monitoring_gaps semantics. Nothing new is invented here - the only thing
        that was missing was a way to REACH those semantics when the process survives the
        outage instead of being restarted through it."""
        now = time.time()
        last = self._last_tick_wall_time
        self._last_tick_wall_time = now
        if last is None or (now - last) < MONITORING_DISCONTINUITY_S:
            return None
        return {"last_sample_before": last, "first_sample_after": now,
                "gap_seconds": max(0.0, now - last)}

    def _apply_monitoring_discontinuity(self, gap):
        """Routes ONE detected live gap through telemetry, incidents and sessions. Runs before
        this tick's samples are observed, so the first post-resume reading lands on the far side
        of the gap in every store rather than being folded into pre-gap state."""
        self._telemetry_split_across_gap(gap)
        self._incidents_record_live_gap(gap)
        self._sessions_record_live_gap(gap)
        self.log_event("INFO", f"Monitoring gap — nothing was recorded for "
                              f"{fmt_dur(gap['gap_seconds'])} (system suspended or stalled); "
                              f"durations spanning it are not exact",
                      meta={"gap_seconds": gap["gap_seconds"],
                            "last_sample_before": gap["last_sample_before"],
                            "first_sample_after": gap["first_sample_after"]})

    def _telemetry_split_across_gap(self, gap):
        """Closes the in-progress bucket at the last moment actually observed and opens a fresh
        one at the first post-gap sample, so the unmonitored interval is genuinely ABSENT from
        the store instead of being swallowed by one enormous bucket.

        Nothing is synthesized for the missing time - no empty placeholder buckets, no
        interpolation. timeline_gap_events() derives the gap from the hole these two real
        buckets leave between them, which is precisely how it already handles a period when the
        app was not running at all. A bucket with no samples yet is simply restarted rather than
        persisted, matching the store's existing rule that a metric never observed is never
        written."""
        bucket = self.telemetry_bucket
        if bucket["sample_count"] > 0:
            self._persist_telemetry_bucket(dict(bucket, end_timestamp=gap["last_sample_before"]))
        self.telemetry_bucket = _new_telemetry_bucket(gap["first_sample_after"])

    @staticmethod
    def _append_gap_once(record, gap):
        """Appends `gap` to record['monitoring_gaps'] unless that exact discontinuity is already
        recorded. One suspend/resume must produce exactly one logical gap per incident/session,
        even if this ran twice for the same resume (a re-entrant poll drain, a retry). Identity
        is first_sample_after: two genuinely different outages cannot share a resume instant."""
        gaps = record.setdefault("monitoring_gaps", [])
        if any(g.get("first_sample_after") == gap["first_sample_after"] for g in gaps):
            return False
        gaps.append(gap)
        return True

    def _incidents_record_live_gap(self, gap):
        """Every incident open across the gap gets exactly what a restart-restored one gets in
        _reconcile_restored_incidents()'s still-alerting branch: the gap appended, the total gap
        seconds accumulated, and duration_exact=False so the eventual duration_seconds - which
        necessarily spans unobserved time - can never be mistaken for a measured one. The
        incident keeps its own incident_id, start, peak and workload tally: it is the SAME
        incident with a hole in the middle, not a new one.

        _live_gap_pending records that the next observation of this incident is the first since
        the gap. If that observation shows it still alerting, _incident_touch() clears it (it was
        continuously hot, as far as we can honestly say). If instead it shows recovery,
        _incident_close() uses it to apply the existing uncertain-recovery semantics, because the
        real recovery moment and value happened while nothing was watching."""
        for inc in self.incidents_active.values():
            if not self._append_gap_once(inc, gap):
                continue
            inc["monitoring_gap_seconds"] = inc.get("monitoring_gap_seconds", 0.0) + gap["gap_seconds"]
            inc["duration_exact"] = False
            inc["_live_gap_pending"] = gap
        if self.incidents_active:
            self._save_active_incidents()

    def _sessions_record_live_gap(self, gap):
        """Same honesty for workload sessions. Note what is deliberately NOT done here: no zone
        time, foreground time or process activity is added for the gap. That already holds
        without any change - _session_observe_tick() clamps its per-tick dt to
        SESSION_GAP_THRESHOLD_S, so a suspend contributes exactly 0.0 seconds to every
        accumulator. All that was missing was the gap METADATA, and duration_exact following
        from it (see _finalize_session_record)."""
        touched = False
        for rec in self.workload_sessions.values():
            touched = self._append_gap_once(rec, gap) or touched
        if touched:
            self._active_sessions_dirty = True

    def _flush_active_incidents_periodic(self):
        """Throttled disk write for non-escalation changes (peak/context/sample updates) -
        open/escalate/close already save immediately (see below); this just catches up on
        anything that only ever "touched" quietly, every ACTIVE_INCIDENTS_FLUSH_INTERVAL_MS,
        not every 2s poll."""
        if self.stop_event.is_set():
            return
        if self._active_incidents_dirty:
            self._save_active_incidents()
            self._active_incidents_dirty = False
        self.after(ACTIVE_INCIDENTS_FLUSH_INTERVAL_MS, self._flush_active_incidents_periodic)

    # -- Evidence API (v1.1 Phase 10) - see the module-level design note near EVIDENCE_* consts -
    def _build_evidence_snapshot(self):
        """Assembles the full local evidence payload from state this app has ALREADY computed -
        no new hardware polling, no new aggregation logic, no causal language anywhere in it.
        Active incidents/sessions reuse the exact same sanitization/finalization helpers their
        own durability paths already use (_incident_to_persistable, _finalize_session_record),
        so the evidence file can never show a shape those consumers don't already trust. Recent
        incidents/sessions and coverage are the same already-existing read/compute functions
        every history view uses - never a second, competing calculation."""
        now = time.time()
        ctx = self.last_context or {}
        # last_net/last_net_procs/last_connections are only ever set inside update_data() (see
        # Phase 1-3), never in __init__ - if this periodic flush fires before the very first
        # real worker tick completes (a real, if narrow, startup race - e.g. a slow first bridge
        # connection), the plain attribute would not exist yet. getattr() with a default matches
        # the same defensive pattern _sample_process_cpu_times() already uses for self._lhm.
        net = getattr(self, "last_net", None) or {}
        adapter = net.get("adapter") or {}
        net_procs = getattr(self, "last_net_procs", None) or {}
        connections = getattr(self, "last_connections", None) or []
        fg = self.last_foreground or {}

        active_incidents = [self._incident_to_persistable(key, inc) for key, inc in self.incidents_active.items()]
        active_sessions = [self._finalize_session_record(rec, now, uncertain=False)
                          for rec in self.workload_sessions.values() if rec.get("confirmed")]

        recent_incidents = [i for i in read_incidents_file()
                           if i.get("end_timestamp", 0) >= now - EVIDENCE_RECENT_WINDOW_S]
        recent_sessions = [s for s in read_sessions_file()
                          if s.get("end_timestamp", 0) >= now - EVIDENCE_RECENT_WINDOW_S]

        buckets = read_telemetry_file(since_ts=now - EVIDENCE_RECENT_WINDOW_S)
        valid_buckets, expected_buckets, coverage_pct = compute_coverage(buckets, EVIDENCE_RECENT_WINDOW_S)
        # Phase 14 - Evidence IDs: citable monitoring-gap evidence for the same 24h window, each
        # carrying the same COV-YYYYMMDD-NNNN id a direct timeline/coverage query would produce -
        # see coverage_gap_events_for_day()/timeline_gap_events().
        gap_events = timeline_gap_events(buckets, now - EVIDENCE_RECENT_WINDOW_S, now)
        coverage_gaps = [
            {"evidence_id": g["source_id"], "source_type": "coverage_gap",
             "start_timestamp": g["timestamp"], "end_timestamp": g["end_timestamp"],
             "duration_seconds": (g["end_timestamp"] or now) - g["timestamp"]}
            for g in gap_events
        ]

        return {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "generated_at": now,
            "app_version": APP_VERSION,
            "system": {
                "cpu_model": self.info.get("cpu"), "cpu_cores": self.info.get("cores"),
                "cpu_threads": self.info.get("threads"), "uptime_seconds": now - self.start_time,
            },
            "live": {
                "cpu": {"temp_c": ctx.get("cpu_temp"), "load_pct": ctx.get("cpu_load"),
                       "power_w": ctx.get("cpu_power"), "fan_rpm": ctx.get("cpu_fan_rpm")},
                "gpu": {"core_temp_c": ctx.get("gpu_core_temp"), "hotspot_temp_c": ctx.get("gpu_hotspot_temp"),
                       "vram_temp_c": ctx.get("gpu_vram_temp"), "load_pct": ctx.get("gpu_load"),
                       "power_w": ctx.get("gpu_power"), "vram_used_mb": ctx.get("gpu_vram_used_mb"),
                       "fan_pct": ctx.get("gpu_fan_pct")},
                "memory": {"used_pct": ctx.get("mem_pct")},
                "network": {
                    "adapter_name": adapter.get("name"), "adapter_type": adapter.get("type"),
                    "connected": adapter.get("media_connect_state"),
                    "down_mbps": net.get("down_mbps"), "up_mbps": net.get("up_mbps"),
                    "total_rx_bytes": adapter.get("in_octets"), "total_tx_bytes": adapter.get("out_octets"),
                    "tcp_connections": sum(1 for c in connections if c.get("protocol") == "TCP"),
                    "udp_connections": sum(1 for c in connections if c.get("protocol") == "UDP"),
                    "per_process_capture_active": net_procs.get("capture_active", False),
                    # Already-computed Phase 2 rows, copied as evidence for read-only external
                    # clients. This does not poll, decode ETW, or recalculate rates.
                    "top_processes": [dict(row) for row in (net_procs.get("top") or [])],
                },
                "bridge_health": compute_bridge_health(bridge_tier1_age(), bridge_status()),
                "foreground_process": fg.get("name"),
            },
            "active_incidents": active_incidents,
            "active_sessions": active_sessions,
            "recent_incidents_24h": recent_incidents,
            "recent_sessions_24h": recent_sessions,
            "coverage_24h": {"valid_buckets": valid_buckets, "expected_buckets": expected_buckets,
                             "coverage_pct": coverage_pct, "gaps": coverage_gaps},
        }

    def _write_evidence_snapshot(self):
        """Atomic write (temp file + replace), same pattern as every other store in this app.
        Best-effort and silent on failure: this is optional infrastructure for an external
        reader that may not even exist - a write failure must never affect monitoring itself,
        the same contract sensors.json's own writer already holds to."""
        try:
            payload = self._build_evidence_snapshot()
            tmp = EVIDENCE_SNAPSHOT_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(EVIDENCE_SNAPSHOT_PATH)
        except OSError:
            pass

    def _flush_evidence_periodic(self):
        if self.stop_event.is_set():
            return
        self._write_evidence_snapshot()
        self.after(EVIDENCE_SNAPSHOT_INTERVAL_MS, self._flush_evidence_periodic)

    # -- workload session tracking (see the module-level design note near SESSION_* consts) ---
    def load_sessions(self):
        """Restore completed workload sessions from disk and prune ones past the retention
        window - exact mirror of load_incidents(), against the entirely separate SESSIONS_PATH."""
        if not SESSIONS_PATH.exists():
            return
        cutoff = time.time() - SESSION_RETENTION_DAYS * 86400
        kept = []
        try:
            for line in SESSIONS_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue  # valid JSON but not a record - never treat it as one
                if rec.get("end_timestamp", 0) >= cutoff:
                    kept.append(rec)
        except OSError:
            return
        for rec in kept:
            self.sessions_recent.appendleft(rec)
        # Atomic rewrite - see atomic_write_lines. Same reasoning as the incident prune above.
        atomic_write_lines(SESSIONS_PATH, [json.dumps(rec) for rec in kept])

    def _persist_session(self, record):
        try:
            with SESSIONS_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass

    @staticmethod
    def _session_to_persistable(record):
        """Working session record -> JSON-safe dict for ACTIVE_SESSIONS_PATH. Strips the
        transient debounce counter (_consecutive), which has no meaning once reloaded - a
        restored session resumes with its already-confirmed stats intact, never a partial
        debounce count."""
        return {k: v for k, v in record.items() if k != "_consecutive"}

    @staticmethod
    def _session_from_persisted(rec):
        rec = dict(rec)
        rec.setdefault("_consecutive", 0)
        return rec

    def _save_active_sessions(self):
        """Atomic write (temp file + replace), same pattern as _save_active_incidents(): every
        CONFIRMED session plus anything still awaiting post-restart reconciliation. Unconfirmed
        candidates (still inside the start debounce window) are deliberately NOT persisted - one
        is cheap to lose and re-detect, and persisting it would risk resuming a "session" that
        was never actually confirmed as one in the first place."""
        try:
            merged = {k: v for k, v in self.workload_sessions.items() if v.get("confirmed")}
            merged.update(self.session_restore_pending)
            snapshot = {key: self._session_to_persistable(rec) for key, rec in merged.items()}
            payload = {"saved_at": time.time(), "sessions": snapshot}
            tmp = ACTIVE_SESSIONS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(ACTIVE_SESSIONS_PATH)
        except OSError:
            pass

    def load_active_sessions(self):
        """Loads sessions active when Thermal Watch last stopped into session_restore_pending
        (not workload_sessions directly) - see _reconcile_restored_sessions(). Tolerates a
        missing/empty/malformed file entirely, exactly like load_active_incidents()."""
        self.session_restore_pending = {}
        if not ACTIVE_SESSIONS_PATH.exists():
            return
        try:
            raw = ACTIVE_SESSIONS_PATH.read_text(encoding="utf-8")
            if not raw.strip():
                return
            payload = json.loads(raw)
            sessions = payload.get("sessions") if isinstance(payload, dict) else None
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self.log_event("WARN", "Saved active-session state was unreadable (starting fresh) - "
                                  "possibly an unclean previous shutdown")
            return
        if not isinstance(sessions, dict):
            return
        # Idempotency guard, mirroring load_active_incidents(): a session_id already present in
        # completed history was already closed properly - a matching "active" entry here is a
        # stale leftover from a write that appended to SESSIONS_PATH but got interrupted before
        # ACTIVE_SESSIONS_PATH could be updated to drop it.
        completed_ids = {s.get("session_id") for s in self.sessions_recent}
        completed_ids |= {s.get("session_id") for s in read_sessions_file()}
        restored = 0
        for key, rec in sessions.items():
            if not isinstance(rec, dict) or not rec.get("session_id"):
                continue
            if not all(f in rec for f in ("start_timestamp", "workload_key", "workload")):
                continue
            if rec["session_id"] in completed_ids:
                continue
            self.session_restore_pending[key] = self._session_from_persisted(rec)
            restored += 1
        if restored:
            self.log_event("INFO", f"Found {restored} workload session(s) still active before the last "
                                  f"shutdown - reconciling against current activity...")

    def _reconcile_restored_sessions(self):
        """Runs once, SESSION_RECONCILE_DELAY_MS after startup - long enough for real per-tick
        sampling to observe whether the workload is still meaningfully active. For each restored
        session: if recent activity was observed for that workload key, RESUME it (same
        session_id, same accumulated stats); otherwise close it with explicit monitoring-gap/
        uncertainty metadata rather than inventing when it actually ended (item 12)."""
        if self.stop_event.is_set() or not self.session_restore_pending:
            return
        now = time.time()
        for key in list(self.session_restore_pending.keys()):
            rec = self.session_restore_pending.pop(key)
            last_observed = rec.get("last_observed_timestamp") or rec.get("start_timestamp") or now
            gap = {"last_sample_before": last_observed, "first_sample_after": now,
                  "gap_seconds": max(0.0, now - last_observed)}
            live = self.last_workload_activity.get(key)
            still_active = bool(live and (now - live["timestamp"]) <= SESSION_IDLE_GRACE_S
                                and ((live.get("cpu_pct") or 0) >= SESSION_CPU_ACTIVE_PCT
                                     or (live.get("gpu_pct") or 0) >= SESSION_GPU_ACTIVE_PCT))
            if still_active:
                rec.setdefault("monitoring_gaps", []).append(gap)
                rec["last_active_timestamp"] = live["timestamp"]
                rec["last_observed_timestamp"] = live["timestamp"]
                pid = live.get("pid")
                if pid is not None and pid not in rec.get("observed_pids", []):
                    rec.setdefault("observed_pids", []).append(pid)
                self.workload_sessions[key] = rec
                self.log_event("INFO", f"{rec['workload']} — workload session resumed after a "
                                      f"{fmt_dur(gap['gap_seconds'])} monitoring gap",
                              meta={"workload": rec["workload"], "session_id": rec["session_id"]})
            else:
                self._session_close(key, gap["first_sample_after"], uncertain=True, gap=gap, record=rec)
        self._save_active_sessions()

    def _flush_active_sessions_periodic(self):
        if self.stop_event.is_set():
            return
        if self._active_sessions_dirty:
            self._save_active_sessions()
            self._active_sessions_dirty = False
        self.after(SESSION_ACTIVE_FLUSH_INTERVAL_MS, self._flush_active_sessions_periodic)

    def _finalize_session_record(self, record, end_ts, uncertain, gap=None):
        """Working record (raw {count,sum,max} agg dicts) -> the clean, human-readable
        completed-session schema persisted to SESSIONS_PATH (item 6). Every average/peak comes
        ONLY from samples that actually measured it - missing stays None, never fabricated as 0
        (item 6/item 8)."""
        agg = record["agg"]

        def m(k):
            return _agg_result(agg[k])

        cpu_temp, gpu_core_t, gpu_hot_t, gpu_vram_t = m("cpu_temp"), m("gpu_core_temp"), m("gpu_hotspot_temp"), m("gpu_vram_temp")
        cpu_util, gpu_util, mem = m("cpu_util"), m("gpu_util"), m("mem_pct")
        cpu_power, gpu_power = m("cpu_power"), m("gpu_power")
        proc_cpu, proc_gpu = m("proc_cpu_pct"), m("proc_gpu_pct")
        net_down, net_up = m("net_down_mbps"), m("net_up_mbps")

        out = {
            "session_id": record["session_id"], "workload_key": record["workload_key"],
            "workload": record["workload"], "process_name": record["process_name"],
            "starting_pid": record["starting_pid"], "observed_pids": list(record.get("observed_pids", [])),
            "start_timestamp": record["start_timestamp"], "end_timestamp": end_ts,
            # A session is exact only if it neither ended during a gap (`uncertain`) NOR lived
            # through one. The second half matters for a session that survives the outage and
            # ends normally later: its start and end are both real, but the span between them
            # contains unobserved time, so duration_seconds is an upper bound. This also closes
            # the same hole on the RESTART path, where a resumed session already had a gap
            # appended (_reconcile_restored_sessions) yet would still finalize as exact.
            "duration_seconds": end_ts - record["start_timestamp"],
            "duration_exact": (not uncertain) and not record.get("monitoring_gaps"),
            "foreground_seconds": record.get("foreground_seconds", 0.0),
            "cpu": {
                "avg_temp": cpu_temp["avg"] if cpu_temp else None, "peak_temp": cpu_temp["peak"] if cpu_temp else None,
                "avg_util": cpu_util["avg"] if cpu_util else None, "peak_util": cpu_util["peak"] if cpu_util else None,
                "avg_power": cpu_power["avg"] if cpu_power else None, "peak_power": cpu_power["peak"] if cpu_power else None,
                "avg_process_util": proc_cpu["avg"] if proc_cpu else None,
                "peak_process_util": proc_cpu["peak"] if proc_cpu else None,
            },
            "gpu": {
                "avg_core_temp": gpu_core_t["avg"] if gpu_core_t else None, "peak_core_temp": gpu_core_t["peak"] if gpu_core_t else None,
                "avg_hotspot_temp": gpu_hot_t["avg"] if gpu_hot_t else None, "peak_hotspot_temp": gpu_hot_t["peak"] if gpu_hot_t else None,
                "avg_vram_temp": gpu_vram_t["avg"] if gpu_vram_t else None, "peak_vram_temp": gpu_vram_t["peak"] if gpu_vram_t else None,
                "avg_util": gpu_util["avg"] if gpu_util else None, "peak_util": gpu_util["peak"] if gpu_util else None,
                "avg_power": gpu_power["avg"] if gpu_power else None, "peak_power": gpu_power["peak"] if gpu_power else None,
                "avg_process_util": proc_gpu["avg"] if proc_gpu else None,
                "peak_process_util": proc_gpu["peak"] if proc_gpu else None,
            },
            "memory": {"avg_pct": mem["avg"] if mem else None, "peak_pct": mem["peak"] if mem else None},
            # v1.1 Phase 5 - Network Sessions. Whole-active-adapter Mbps observed while this
            # workload was active (same semantic as cpu/gpu's avg/peak temps - an observed
            # correlation, never a causal "this workload used N Mbps" claim). None on a session
            # with zero net_down_mbps samples (e.g. every tick had no active adapter at all),
            # matching every other block's "never fabricate a 0" rule.
            "network": {
                "avg_down_mbps": net_down["avg"] if net_down else None,
                "peak_down_mbps": net_down["peak"] if net_down else None,
                "avg_up_mbps": net_up["avg"] if net_up else None,
                "peak_up_mbps": net_up["peak"] if net_up else None,
            },
            "zone_time": {c: dict(t) for c, t in record.get("zone_time", {}).items()},
            "incident_ids": list(record.get("incident_ids", [])),
            "incident_count": len(record.get("incident_ids", [])),
            "max_incident_severity": record.get("max_incident_severity"),
            "monitoring_gaps": list(record.get("monitoring_gaps", [])),
            "close_reason": "ended_during_gap" if uncertain else "idle_grace_expired",
        }
        if uncertain and gap is not None:
            out["monitoring_gaps"].append(gap)
        return out

    def _session_close(self, key, now, uncertain, gap=None, record=None):
        """Finalizes and persists ONE session - either a normal idle-grace expiry from the live
        engine (record=None, pops from workload_sessions), or restart reconciliation deciding
        the workload ended while offline (record passed explicitly, since that path pops from
        session_restore_pending instead)."""
        rec = record if record is not None else self.workload_sessions.pop(key, None)
        if rec is None or not rec.get("confirmed", True):
            return
        completed = self._finalize_session_record(rec, now, uncertain, gap)
        assign_session_evidence_id(completed)  # Phase 14: freeze once, right before the first persist
        self.sessions_recent.appendleft(completed)
        self._persist_session(completed)
        self._save_active_sessions()  # drop it from active persistence the moment it's completed
        label = ("ended while monitoring was offline" if uncertain
                else f"ended ({fmt_dur(completed['duration_seconds'])})")
        self.log_event("INFO", f"{completed['workload']} — workload session {label}",
                      meta={"workload": completed["workload"], "session_id": completed["session_id"],
                           "duration_exact": completed["duration_exact"]})

    def _session_observe_tick(self):
        """Called once per 2s poll, from update_data(), after last_context/last_cpu_top/
        last_gpu_top/last_foreground are all current for this tick. Purely observes that
        already-collected snapshot - makes no hardware/process call of its own (item 18), and
        makes no thermal decision (reuses cpu_zone_for()/zone_for() exactly as-is, item 10)."""
        now = time.time()
        dt_raw = now - self._session_last_tick_time
        self._session_last_tick_time = now
        # A gap this large (system sleep, a stall) is unmonitored time - never attributed to any
        # zone or to foreground time (item 10's "do not count unmonitored gaps").
        dt = dt_raw if 0 < dt_raw <= SESSION_GAP_THRESHOLD_S else 0.0

        tick_workloads = {}
        for name, pid, pct in self.last_cpu_top:
            key, display = _normalize_workload_name(name)
            if key == NOT_IDENTIFIED_KEY:
                continue
            e = tick_workloads.setdefault(key, {"display": display, "pid": pid, "cpu_pct": None, "gpu_pct": None})
            e["cpu_pct"] = pct
            e["pid"] = pid
        for name, pid, pct in self.last_gpu_top:
            key, display = _normalize_workload_name(name)
            if key == NOT_IDENTIFIED_KEY:
                continue
            e = tick_workloads.setdefault(key, {"display": display, "pid": pid, "cpu_pct": None, "gpu_pct": None})
            e["gpu_pct"] = pct
            e.setdefault("pid", pid)

        active_this_tick = {k for k, e in tick_workloads.items()
                            if (e["cpu_pct"] or 0) >= SESSION_CPU_ACTIVE_PCT
                            or (e["gpu_pct"] or 0) >= SESSION_GPU_ACTIVE_PCT}

        # Recorded unconditionally, active or not - restart reconciliation needs to know "we did
        # observe this workload recently" independent of the debounce/grace state machine below.
        for key, e in tick_workloads.items():
            self.last_workload_activity[key] = {"pid": e["pid"], "cpu_pct": e["cpu_pct"],
                                                "gpu_pct": e["gpu_pct"], "timestamp": now}

        newly_confirmed = []
        for key in active_this_tick:
            if key in self.session_restore_pending:
                continue  # awaiting _reconcile_restored_sessions() - do not touch in the meantime
            entry = tick_workloads[key]
            record = self.workload_sessions.get(key)
            if record is None:
                record = _new_session_record(key, entry["display"], entry["pid"], now)
                self.workload_sessions[key] = record
            record["_consecutive"] += 1
            self._session_apply_sample(record, key, entry, dt, now)
            if not record["confirmed"] and record["_consecutive"] >= SESSION_START_DEBOUNCE_SAMPLES:
                record["confirmed"] = True
                record["session_id"] = f"{key}-{int(record['start_timestamp'] * 1000)}"
                newly_confirmed.append(record)

        for key in list(self.workload_sessions.keys()):
            if key in active_this_tick or key in self.session_restore_pending:
                continue
            record = self.workload_sessions[key]
            if not record["confirmed"]:
                del self.workload_sessions[key]  # candidate missed a tick - reset (item 2)
                continue
            if now - record["last_active_timestamp"] >= SESSION_IDLE_GRACE_S:
                self._session_close(key, now, uncertain=False)

        for record in newly_confirmed:
            self.log_event("INFO", f"{record['workload']} — workload session started",
                          meta={"workload": record["workload"], "session_id": record["session_id"]})

        self._session_link_incidents()

    def _session_apply_sample(self, record, key, entry, dt, now):
        ctx = self.last_context or {}
        agg = record["agg"]
        _agg_add(agg["cpu_temp"], ctx.get("cpu_temp"))
        _agg_add(agg["cpu_util"], ctx.get("cpu_load"))
        _agg_add(agg["cpu_power"], ctx.get("cpu_power"))
        _agg_add(agg["gpu_core_temp"], ctx.get("gpu_core_temp"))
        _agg_add(agg["gpu_hotspot_temp"], ctx.get("gpu_hotspot_temp"))
        _agg_add(agg["gpu_vram_temp"], ctx.get("gpu_vram_temp"))
        _agg_add(agg["gpu_util"], ctx.get("gpu_load"))
        _agg_add(agg["gpu_power"], ctx.get("gpu_power"))
        _agg_add(agg["mem_pct"], ctx.get("mem_pct"))
        _agg_add(agg["proc_cpu_pct"], entry.get("cpu_pct"))
        _agg_add(agg["proc_gpu_pct"], entry.get("gpu_pct"))
        _agg_add(agg["net_down_mbps"], ctx.get("net_down_mbps"))
        _agg_add(agg["net_up_mbps"], ctx.get("net_up_mbps"))

        if dt > 0:
            for comp, table in SESSION_ZONE_TABLES.items():
                val = ctx.get(SESSION_ZONE_CONTEXT_KEY[comp])
                zone = cpu_zone_for(val) if comp == "cpu" else zone_for(val, table)
                if zone:
                    record["zone_time"][comp][zone["key"]] += dt
            fg = self.last_foreground
            if fg and fg.get("name"):
                fg_key, _ = _normalize_workload_name(fg["name"])
                if fg_key == key:
                    record["foreground_seconds"] += dt

        record["last_active_timestamp"] = now
        record["last_observed_timestamp"] = now
        pid = entry.get("pid")
        if pid is not None and pid not in record["observed_pids"]:
            record["observed_pids"].append(pid)
        self._active_sessions_dirty = True

    def _session_link_incidents(self):
        """Conservative, read-only correlation (item 9): links a just-closed incident to a
        CURRENTLY ACTIVE session only when the incident's own (already-conservative)
        dominant_workload matches that session's workload identity. Never modifies any incident
        field/state - purely appends to the session's own incident_ids. An incident whose
        workload has no active session right now (including one whose matching session already
        closed) is simply never linked - a documented, intentional limitation rather than a
        guess (item 9: "be conservative")."""
        live_ids = {i.get("incident_id") for i in self.incidents_recent}
        self._session_linked_incident_ids &= live_ids
        for inc in self.incidents_recent:
            iid = inc.get("incident_id")
            if not iid or iid in self._session_linked_incident_ids:
                continue
            self._session_linked_incident_ids.add(iid)
            key, _ = _normalize_workload_name(inc.get("dominant_workload"))
            if key == NOT_IDENTIFIED_KEY:
                continue
            session = self.workload_sessions.get(key)
            if not session or not session.get("confirmed") or iid in session["incident_ids"]:
                continue
            session["incident_ids"].append(iid)
            zone = inc.get("max_zone")
            if zone and ZONE_SEVERITY.get(zone, -1) > ZONE_SEVERITY.get(session.get("max_incident_severity") or "GREEN", -1):
                session["max_incident_severity"] = zone
            self._active_sessions_dirty = True

    # -- long-term telemetry history (see the module-level design note near TELEMETRY_* consts) --
    def init_telemetry_store(self):
        """Runs once at startup (never on the 2s poll, item 20): opens/creates/recovers the
        SQLite telemetry store, migrates any legacy Storage v1 JSONL history into it (one-time,
        idempotent - see migrate_telemetry_jsonl_to_sqlite()), then prunes anything past
        retention. Mirrors load_incidents()/load_sessions()'s startup-only placement exactly."""
        conn = open_telemetry_db()
        if conn is None:
            self.log_event("WARN", "Telemetry history storage could not be opened - historical "
                                  "charts are unavailable this session; live monitoring is unaffected")
            return
        try:
            migrated = migrate_telemetry_jsonl_to_sqlite(conn)
        finally:
            conn.close()
        if migrated:
            self.log_event("INFO", f"Migrated {migrated} historical telemetry bucket(s) from the legacy "
                                  f"JSONL store to SQLite")
            try:
                if TELEMETRY_JSONL_PATH.exists():
                    TELEMETRY_JSONL_PATH.rename(TELEMETRY_JSONL_PATH.with_suffix(".jsonl.migrated"))
            except OSError:
                pass  # non-fatal - the migration marker in telemetry_meta already prevents a re-scan
        self.prune_telemetry_history()

    def prune_telemetry_history(self):
        """Runs once at startup - mirrors load_incidents()'s prune-on-load pattern, but nothing
        is cached into memory here (item 20): a direct indexed DELETE, never a read-everything-
        into-Python-then-rewrite-the-file pass like Storage v1 needed."""
        conn = open_telemetry_db()
        if conn is None:
            return
        try:
            cutoff = time.time() - TELEMETRY_RETENTION_DAYS * 86400
            conn.execute("BEGIN")
            conn.execute("DELETE FROM sensor_readings WHERE start_timestamp IN "
                         "(SELECT start_timestamp FROM buckets WHERE end_timestamp IS NOT NULL AND end_timestamp < ?)",
                        (cutoff,))
            conn.execute("DELETE FROM buckets WHERE end_timestamp IS NOT NULL AND end_timestamp < ?", (cutoff,))
            conn.commit()
        except sqlite3.DatabaseError:
            conn.rollback()
        finally:
            conn.close()

    def _telemetry_observe_tick(self, sensor_samples):
        """Called once per 2s poll, from update_data(), with the drive/DIMM/motherboard
        readings update_data() already computed this tick for rendering - (identity, name,
        parent, sensor_type, component, value) tuples, no re-derivation of what counts as
        missing/unpopulated (item 1: no new hardware call, no duplicated filtering logic).
        Scalar metrics are read directly from self.last_context, already current by this point
        in update_data()."""
        now = time.time()
        bucket = self.telemetry_bucket
        ctx = self.last_context or {}
        for key in TELEMETRY_SCALAR_KEYS:
            _bucket_agg_add(bucket["scalars"][key], ctx.get(TELEMETRY_SCALAR_CONTEXT_MAP[key]))
        for identity, name, parent, sensor_type, component, value in sensor_samples:
            if value is None:
                continue
            bucket_key = _sensor_bucket_key(identity)
            entry = bucket["sensors"].get(bucket_key)
            if entry is None:
                entry = {"identifier": identity if isinstance(identity, str) else None,
                         "parent": parent, "name": name, "sensor_type": sensor_type, "component": component,
                         "unverified": identity in UNVERIFIED_SENSOR_LABELS, "agg": _bucket_agg_new()}
                bucket["sensors"][bucket_key] = entry
            _bucket_agg_add(entry["agg"], value)
        bucket["sample_count"] += 1

        if now - bucket["start_timestamp"] >= TELEMETRY_BUCKET_SECONDS:
            self._telemetry_finalize_bucket(now)

    def _persist_telemetry_bucket(self, bucket):
        """Working bucket (raw {count,sum,min,max} agg dicts) -> one row in `buckets` plus one
        row per real per-sensor reading in `sensor_readings`, in a SINGLE transaction (crash-
        safe: a crash mid-write leaves either the fully-written bucket or none of it, never a
        half-written one). INSERT OR REPLACE on buckets and a DELETE-then-INSERT on this
        bucket's own sensor_readings make a re-persist of the same start_timestamp idempotent
        rather than accumulating duplicate rows. A per-sensor entry with zero real samples this
        bucket is dropped entirely rather than persisted as an empty placeholder (item 2: never
        fabricate a value for a sensor that reported nothing)."""
        scalars = {k: _bucket_agg_result(v) for k, v in bucket["scalars"].items()}
        sensors = {k: {"identifier": e["identifier"], "parent": e["parent"], "name": e["name"],
                      "sensor_type": e["sensor_type"], "component": e["component"], "unverified": e["unverified"],
                      **_bucket_agg_result(e["agg"])}
                  for k, e in bucket["sensors"].items() if e["agg"]["count"] > 0}
        conn = open_telemetry_db()
        if conn is None:
            return
        try:
            conn.execute("BEGIN")
            conn.execute(
                "INSERT OR REPLACE INTO buckets (start_timestamp, end_timestamp, sample_count, scalars_json) "
                "VALUES (?, ?, ?, ?)",
                (bucket["start_timestamp"], bucket["end_timestamp"], bucket["sample_count"], json.dumps(scalars)))
            conn.execute("DELETE FROM sensor_readings WHERE start_timestamp = ?", (bucket["start_timestamp"],))
            for sensor_key, s in sensors.items():
                conn.execute(
                    "INSERT INTO sensor_readings (start_timestamp, sensor_key, identifier, parent, name, "
                    "sensor_type, component, unverified, avg, min, max, count) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (bucket["start_timestamp"], sensor_key, s["identifier"], s["parent"], s["name"],
                     s["sensor_type"], s["component"], int(s["unverified"]), s["avg"], s["min"], s["max"], s["count"]))
            conn.commit()
        except sqlite3.DatabaseError:
            conn.rollback()
        finally:
            conn.close()

    def _telemetry_finalize_bucket(self, now):
        self._persist_telemetry_bucket(dict(self.telemetry_bucket, end_timestamp=now))
        self.telemetry_bucket = _new_telemetry_bucket(now)

    def _incident_open(self, key, component, sensor_name, sensor_identifier, zone, value):
        bias = INCIDENT_BIAS.get(component, "cpu")
        a = self._current_attribution(bias)
        now = time.time()
        self.incidents_active[key] = {
            "incident_id": f"{component}-{int(now * 1000)}",
            "start_timestamp": now,
            "end_timestamp": None,
            "component": component,
            "sensor_name": sensor_name,
            "sensor_identifier": sensor_identifier,
            "starting_zone": zone,
            "max_zone": zone,
            "start_value": value,
            "peak_value": value,
            "recovery_value": None,
            "foreground_process": a["foreground_process"],
            "foreground_title": a["foreground_title"],
            "top_cpu_processes": a["top_cpu_processes"],
            "top_gpu_processes": a["top_gpu_processes"],
            "context_peak": {},
            "samples": [[now, value]],
            "last_observed_timestamp": now,
            "last_observed_value": value,
            "last_observed_zone": zone,
            "monitoring_gaps": [],
            "monitoring_gap_seconds": 0.0,
            "_bias": bias,          # internal-only, stripped before persisting to INCIDENTS_PATH
            "_workload_tally": {},  # internal-only, collapsed to dominant_workload at close
        }
        self._save_active_incidents()  # immediately, per item 3: "immediately when an incident opens"

    def _incident_touch(self, key, zone, value):
        """Runs every tick an incident is active, regardless of whether this tick was itself a
        transition - silently updates peak/max-severity/workload-tally/context-peaks/sample
        series. Never logs anything (that's the whole point: no per-poll Event Log spam). Saves
        active-incident state immediately on an escalation (a severity change is exactly the
        kind of "meaningful" state change item 3 calls out); otherwise just marks it dirty for
        the periodic throttled flush, so a normal tick doesn't hit the disk at all."""
        inc = self.incidents_active.get(key)
        if not inc:
            return
        if value is not None:
            inc["peak_value"] = value if inc["peak_value"] is None else max(inc["peak_value"], value)
        escalated = False
        if zone and ZONE_SEVERITY.get(zone, 0) > ZONE_SEVERITY.get(inc["max_zone"], 0):
            inc["max_zone"] = zone
            escalated = True
        inc["last_observed_timestamp"] = time.time()
        inc["last_observed_value"] = value
        inc["last_observed_zone"] = zone
        # Still alerting on the first observation after a live monitoring gap: it did not recover
        # while we were suspended, so the uncertain-recovery semantics do not apply. The gap
        # itself (and duration_exact=False) stays recorded either way.
        inc.pop("_live_gap_pending", None)
        primary = self.last_gpu_top if inc["_bias"] == "gpu" else self.last_cpu_top
        if inc["_bias"] and primary and primary[0][2] >= 5.0:
            name = primary[0][0]
            inc["_workload_tally"][name] = inc["_workload_tally"].get(name, 0) + 1
        for ctx_key, ctx_val in (self.last_context or {}).items():
            if ctx_val is None:
                continue
            if inc["context_peak"].get(ctx_key) is None or ctx_val > inc["context_peak"][ctx_key]:
                inc["context_peak"][ctx_key] = ctx_val
        now = time.time()
        samples = inc["samples"]
        if value is not None and (not samples or now - samples[-1][0] >= 2):
            samples.append([now, value])
            if len(samples) > INCIDENT_MAX_SAMPLES:
                inc["samples"] = samples[::2]  # simple decimation - caps growth, keeps shape
        if escalated:
            self._save_active_incidents()
        else:
            self._active_incidents_dirty = True

    def _incident_close(self, key, recovery_value):
        inc = self.incidents_active.pop(key, None)
        if not inc:
            return
        now = time.time()
        pending_gap = inc.pop("_live_gap_pending", None)
        inc["end_timestamp"] = now
        inc["duration_seconds"] = now - inc["start_timestamp"]
        if pending_gap is not None:
            # The FIRST observation after a live monitoring gap already shows it recovered, so
            # it recovered at some unknown moment while nothing was watching. Identical
            # treatment to _close_incident_after_gap()'s restart case: record the true monitored
            # bound and the true first-post-gap bound, and invent neither the recovery moment
            # nor the recovery value.
            inc["recovery_value"] = None
            inc["recovery_during_monitoring_gap"] = True
            inc["close_reason"] = "recovered_during_gap"
            inc["last_observed_alert_timestamp"] = pending_gap["last_sample_before"]
            inc["first_observed_recovered_timestamp"] = pending_gap["first_sample_after"]
            inc["first_observed_recovered_value"] = recovery_value  # a real reading, at a real time
            inc["monitored_duration_seconds"] = pending_gap["last_sample_before"] - inc["start_timestamp"]
        else:
            inc["recovery_value"] = recovery_value
        inc["dominant_workload"] = self._dominant_workload(inc["_workload_tally"]) or "Not identified"
        del inc["_bias"]
        del inc["_workload_tally"]
        # Schema consistency with gap-closed incidents (item 6): every persisted incident has
        # these fields now, so the History view never has to guess whether they're absent
        # because it's an old record or because nothing eventful happened. A normal incident
        # with no gap looks exactly as it always has otherwise (item 10).
        inc.setdefault("monitoring_gaps", [])
        inc.setdefault("duration_exact", True)
        assign_incident_evidence_id(inc)  # Phase 14: freeze once, right before the first persist
        self.incidents_recent.appendleft(inc)
        self._persist_incident(inc)
        self._save_active_incidents()  # item 9: drop it from active persistence the moment it's completed

    def _incident_observe(self, key, component, sensor_name, sensor_identifier, value):
        """Call immediately after the corresponding (untouched) _update_*_zone() call for this
        tick. Purely observes whatever that already-debounced zone engine did to
        self.active_alerts[key] this tick - makes no zone/threshold/debounce decision of its
        own, so an incident can never open, escalate, or close on a different schedule than the
        alert engine already governs."""
        self.last_component_values[key] = value
        if key in self.incident_restore_pending:
            return  # awaiting _reconcile_restored_incidents() - do not touch it in the meantime
        if value is None:
            return
        after = self.active_alerts.get(key)
        if after:
            if key not in self.incidents_active:
                self._incident_open(key, component, sensor_name, sensor_identifier, after.get("zone", "YELLOW"), value)
            self._incident_touch(key, after.get("zone"), value)
        elif key in self.incidents_active:
            self._incident_close(key, value)

    def log_event(self, kind, text, meta=None):
        """meta: optional structured dict (component/sensor/zone/value/peak/
        foreground_process/foreground_title/top_cpu_processes/top_gpu_processes/
        likely_workload/duration - see workload attribution below). Persisted verbatim
        to the JSONL log even though only `text` is what the GUI renders today, so a future
        History/Analytics page has real structured data to parse rather than scraping text."""
        ts = time.time()
        fg, border = self._colors_for(kind)
        entry = {"time": datetime.fromtimestamp(ts).strftime("%H:%M:%S"), "kind": kind, "fg": fg,
                "border": border, "text": text, "meta": meta}
        self.events.appendleft(entry)
        self._append_log_row(entry)  # incremental: does not touch any existing row
        try:
            record = {"ts": ts, "kind": kind, "text": text}
            if meta:
                record["meta"] = meta
            with EVENT_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except OSError:
            pass

    def _build_log_row(self, parent, e):
        row = tk.Frame(parent, bg=PANEL, highlightthickness=1, highlightbackground=BORDER2)
        inner = tk.Frame(row, bg=PANEL); inner.pack(fill="x", padx=0, pady=6)
        tk.Label(inner, text=e["time"], bg=PANEL, fg=DIM, font=(MONO, 8)).pack(side="left")
        tk.Label(inner, text=e["kind"], bg=PANEL, fg=e["fg"], font=(MONO, 7, "bold"),
                 highlightthickness=1, highlightbackground=e["border"], padx=4, pady=1).pack(side="left", padx=8)
        tk.Label(inner, text=e["text"], bg=PANEL, fg="#c7ccd4", font=(SANS, 9), wraplength=180,
                 justify="left", anchor="w").pack(side="left", fill="x", expand=True)
        return row

    def render_log(self):
        """Full rebuild - used only at startup (load_events) and CLEAR LOG, where every row
        is genuinely changing at once. Normal new-event traffic uses _append_log_row instead."""
        for row in self.log_rows:
            row.destroy()
        self.log_rows = []
        for e in list(self.events)[:40]:
            row = self._build_log_row(self.log_body, e)
            row.pack(fill="x", pady=(0, 1))
            self.log_rows.append(row)
        self.log_empty_shown = self._toggle_visible(self.log_empty, self.log_empty_shown, not self.log_rows,
                                                     anchor="w", pady=6)

    def _append_log_row(self, entry):
        """Inserts exactly one new row above the existing ones (events display newest-first)
        without touching any pre-existing row widget, then trims anything past the 40-row cap.
        Preserves the user's scroll position unless they were already at/near the newest-entry
        edge (the top, since this list is newest-first), in which case it stays pinned there."""
        canvas = self.log_scroll.canvas
        was_at_top = canvas.yview()[0] <= 0.02
        top_canvas_y_before = canvas.canvasy(0)

        self.log_empty_shown = self._toggle_visible(self.log_empty, self.log_empty_shown, False)
        row = self._build_log_row(self.log_body, entry)
        if self.log_rows:
            row.pack(fill="x", pady=(0, 1), before=self.log_rows[0])
        else:
            row.pack(fill="x", pady=(0, 1))
        self.log_rows.insert(0, row)
        while len(self.log_rows) > 40:
            self.log_rows.pop().destroy()

        self.log_body.update_idletasks()
        if was_at_top:
            canvas.yview_moveto(0.0)
        else:
            bbox = canvas.bbox("all")
            total_h = max(1, (bbox[3] - bbox[1])) if bbox else 1
            row_h = row.winfo_height()
            canvas.yview_moveto((top_canvas_y_before + row_h) / total_h)

    # -- workload attribution: "what was the PC doing when this alert fired" ---------------
    # Everything here reads self.last_foreground/last_cpu_top/last_gpu_top, which update_data()
    # already refreshed this tick from the worker thread's queued snapshot - no new sampling
    # happens here, this is purely about WHEN to log it (real alert transitions only) and HOW
    # (concise primary line, then attribution, both in the displayed text and in structured
    # meta persisted to disk).
    def _current_attribution(self, bias):
        """bias: 'cpu' | 'gpu' | None (None = no confident attribution source, e.g. drives)."""
        fg = self.last_foreground
        cpu_top, gpu_top = self.last_cpu_top, self.last_gpu_top
        primary = (gpu_top if bias == "gpu" else cpu_top) if bias else []
        # Floor: never name a barely-active process as "responsible" for a real alert - this
        # is also what keeps an ordinary idle/browsing session from ever producing a
        # misleading "Likely workload" (in practice such sessions never reach alert
        # thresholds at all, but this is the belt-and-suspenders guard regardless).
        likely = primary[0][0] if primary and primary[0][2] >= 5.0 else "Not identified"
        return {
            "foreground_process": fg["name"] if fg else None,
            "foreground_title": (fg.get("title") if fg else None),
            "top_cpu_processes": [[n, p, round(pct, 1)] for n, p, pct in cpu_top],
            "top_gpu_processes": [[n, p, round(pct, 1)] for n, p, pct in gpu_top],
            "likely_workload": likely,
        }

    def _log_alert_with_workload(self, kind, primary_text, bias, extra_meta):
        """Called only from inside a zone/threshold TRANSITION (entry/escalate/de-escalate
        while still active) - never on an unchanged-state poll, so this can't spam the log."""
        a = self._current_attribution(bias)
        lines = [primary_text]
        if a["foreground_process"]:
            title = a["foreground_title"]
            lines.append(f"Foreground: {a['foreground_process']}" + (f" — {title}" if title else ""))
        lines.append(f"Likely workload: {a['likely_workload']}")
        if a["top_gpu_processes"]:
            lines.append("GPU: " + ", ".join(f"{n} {pct:.0f}%" for n, p, pct in a["top_gpu_processes"][:3]))
        if a["top_cpu_processes"]:
            lines.append("CPU: " + ", ".join(f"{n} {pct:.0f}%" for n, p, pct in a["top_cpu_processes"][:3]))
        meta = dict(a)
        meta.update(extra_meta)
        self.log_event(kind, "\n".join(lines), meta=meta)

    @staticmethod
    def _dominant_workload(tally):
        if not tally:
            return None
        return max(tally.items(), key=lambda kv: kv[1])[0]

    def _track_alert_extras(self, alert_key, value, bias):
        """Silently (no log line) updates peak value + a workload tally for an alert that's
        ALREADY active, every tick regardless of whether this tick is a transition. This is
        what lets the eventual recovery event report peak temperature and a dominant workload
        without needing to log anything while the alert merely continues."""
        entry = self.active_alerts.get(alert_key)
        if not entry:
            return
        if value is not None:
            entry["peak"] = max(entry.get("peak", value), value)
        primary = self.last_gpu_top if bias == "gpu" else self.last_cpu_top
        if primary and primary[0][2] >= 5.0:
            tally = entry.setdefault("workload_tally", {})
            name = primary[0][0]
            tally[name] = tally.get(name, 0) + 1

    def _check_alert(self, key, active, message, value=None, bias="cpu"):
        prev = self.active_alerts.get(key)
        if active and not prev:
            entry = {"since": time.time(), "text": message}
            if value is not None:
                entry["peak"] = value
            self.active_alerts[key] = entry
            self._log_alert_with_workload("WARN", message, bias,
                                          {"component": key, "sensor": key, "zone": None, "value": value})
        elif active and prev:
            prev["text"] = message
            self._track_alert_extras(key, value, bias)
        elif not active and prev:
            dur = fmt_dur(time.time() - prev["since"])
            dominant = self._dominant_workload(prev.get("workload_tally"))
            peak = prev.get("peak")
            text = f"{message} cleared after {dur}"
            if peak is not None:
                text += f" — Peak {peak:.0f}%"
            if dominant:
                text += f" — dominant workload: {dominant}"
            self.log_event("INFO", text, meta={"component": key, "duration_s": time.time() - prev["since"],
                                               "peak": peak, "likely_workload": dominant})
            del self.active_alerts[key]

    @staticmethod
    def _cpu_alert_text(zone_key, ct):
        info = next(z for z in CPU_ZONES if z[1] == zone_key)
        return f"CPU package {ct:.1f}°C — {info[3]}"

    def _transition_cpu_zone(self, old_zone, new_zone, ct):
        self.cpu_zone_confirmed = new_zone
        if new_zone == "GREEN":
            entry = self.active_alerts.pop("cpu", None)
            if entry:
                dur = fmt_dur(time.time() - entry["since"])
                peak = entry.get("peak")
                dominant = self._dominant_workload(entry.get("workload_tally"))
                text = f"CPU package back to NOMINAL after {dur}"
                if peak is not None:
                    text += f" — Peak {peak:.0f}°C"
                if dominant:
                    text += f" — dominant workload: {dominant}"
                self.log_event("INFO", text, meta={"component": "cpu", "sensor": "CPU Package", "zone": "GREEN",
                                                    "duration": dur, "peak": peak, "likely_workload": dominant})
            return
        escalating = CPU_ZONE_SEVERITY[new_zone] > CPU_ZONE_SEVERITY.get(old_zone, 0)
        kind = "CRIT" if new_zone == "RED" else ("WARN" if escalating else "INFO")
        text = self._cpu_alert_text(new_zone, ct)
        prev = self.active_alerts.get("cpu", {})
        since = prev.get("since", time.time())
        peak = max(prev.get("peak", ct), ct)
        self.active_alerts["cpu"] = {"since": since, "text": text, "zone": new_zone, "peak": peak,
                                     "workload_tally": prev.get("workload_tally", {})}
        self._log_alert_with_workload(kind, text, "cpu",
                                      {"component": "cpu", "sensor": "CPU Package", "zone": new_zone, "value": ct})

    def _update_cpu_zone(self, ct):
        """Zone-based CPU alerting: YELLOW/ORANGE need CPU_ALERT_DEBOUNCE_S of sustained
        readings before an alert fires (avoids momentary-spike noise); RED (>=100°C) and any
        de-escalation apply immediately. The card's live color/status is NOT debounced - only
        the alert banner/event log are, via active_alerts/log_event below."""
        zone = cpu_zone_for(ct)
        if zone is None:
            return
        if "cpu" in self.active_alerts:
            self._track_alert_extras("cpu", ct, "cpu")
        raw_key = zone["key"]
        now = time.time()
        if raw_key != self.cpu_zone_pending["zone"]:
            self.cpu_zone_pending = {"zone": raw_key, "since": now}
        sustained = now - self.cpu_zone_pending["since"]

        confirmed = self.cpu_zone_confirmed
        if CPU_ZONE_SEVERITY[raw_key] > CPU_ZONE_SEVERITY[confirmed]:
            if raw_key == "RED" or sustained >= CPU_ALERT_DEBOUNCE_S:
                self._transition_cpu_zone(confirmed, raw_key, ct)
        elif CPU_ZONE_SEVERITY[raw_key] < CPU_ZONE_SEVERITY[confirmed]:
            self._transition_cpu_zone(confirmed, raw_key, ct)
        elif "cpu" in self.active_alerts:
            self.active_alerts["cpu"]["text"] = self._cpu_alert_text(raw_key, ct)

    @staticmethod
    def _drive_alert_text(name, zone_key, temp):
        label = next(z[3] for z in DRIVE_ZONES if z[1] == zone_key)
        return f"{name} — {temp:.0f}°C — {label}"

    def _transition_drive_zone(self, key, name, old_zone, new_zone, temp):
        alert_key = f"disk:{key}"
        self.drive_zone_state[key]["confirmed"] = new_zone
        if new_zone == "GREEN":
            entry = self.active_alerts.pop(alert_key, None)
            if entry:
                dur = fmt_dur(time.time() - entry["since"])
                peak = entry.get("peak")
                text = f"{name} — back to NOMINAL after {dur}"
                if peak is not None:
                    text += f" — Peak {peak:.0f}°C"
                self.log_event("INFO", text, meta={"component": "drive", "sensor": name, "zone": "GREEN",
                                                    "duration": dur, "peak": peak})
            return
        escalating = DRIVE_ZONE_SEVERITY[new_zone] > DRIVE_ZONE_SEVERITY.get(old_zone, 0)
        kind = "CRIT" if new_zone == "RED" else ("WARN" if escalating else "INFO")
        text = self._drive_alert_text(name, new_zone, temp)
        prev = self.active_alerts.get(alert_key, {})
        since = prev.get("since", time.time())
        peak = max(prev.get("peak", temp), temp)
        self.active_alerts[alert_key] = {"since": since, "text": text, "zone": new_zone, "peak": peak}
        # No confident per-process disk-I/O attribution source is implemented (task marks this
        # optional and explicitly says not to invent it) - bias=None, so this still shows
        # foreground/CPU/GPU context but always reports "Likely workload: Not identified"
        # rather than falsely implying a CPU-heavy process caused the DRIVE to heat up.
        self._log_alert_with_workload(kind, text, None,
                                      {"component": "drive", "sensor": name, "zone": new_zone, "value": temp})

    def _update_drive_zone(self, key, name, temp):
        """Per-drive mirror of _update_cpu_zone: YELLOW(WARM)/ORANGE(HOT) need
        DRIVE_ALERT_DEBOUNCE_S sustained before alerting; RED(CRITICAL) and any
        de-escalation apply immediately. `temp` must already be a live Composite
        Temperature reading (None/0/setpoint values are filtered out by the caller)."""
        zone = drive_zone_for(temp)
        if zone is None:
            return
        if f"disk:{key}" in self.active_alerts:
            self._track_alert_extras(f"disk:{key}", temp, None)
        raw_key = zone["key"]
        state = self.drive_zone_state.setdefault(key, {"confirmed": "GREEN",
                                                        "pending": {"zone": "GREEN", "since": time.time()}})
        now = time.time()
        if raw_key != state["pending"]["zone"]:
            state["pending"] = {"zone": raw_key, "since": now}
        sustained = now - state["pending"]["since"]

        confirmed = state["confirmed"]
        if DRIVE_ZONE_SEVERITY[raw_key] > DRIVE_ZONE_SEVERITY[confirmed]:
            if raw_key == "RED" or sustained >= DRIVE_ALERT_DEBOUNCE_S:
                self._transition_drive_zone(key, name, confirmed, raw_key, temp)
        elif DRIVE_ZONE_SEVERITY[raw_key] < DRIVE_ZONE_SEVERITY[confirmed]:
            self._transition_drive_zone(key, name, confirmed, raw_key, temp)
        else:
            alert_key = f"disk:{key}"
            if alert_key in self.active_alerts:
                self.active_alerts[alert_key]["text"] = self._drive_alert_text(name, raw_key, temp)

    # -- generic per-sensor zone engine (GPU sub-sensors, RAM DIMMs) --------------------
    # Same debounce contract as CPU/drives (Yellow/Orange need ALERT_DEBOUNCE_S sustained,
    # Red and any de-escalation are immediate), but keyed generically so each physical
    # sensor (each DIMM, GPU Hotspot, GPU VRAM, ...) gets fully independent state - none of
    # this shares state with CPU_ZONE_*/DRIVE_ZONE_* or with each other.
    @staticmethod
    def _sensor_alert_text(label, zone_key, value, unit, table):
        zlabel = next(t[3] for t in table if t[1] == zone_key)
        return f"{label} — {value:.0f}{unit} — {zlabel}"

    @staticmethod
    def _bias_for_sensor_key(key):
        # GPU Core/Hotspot/Memory Junction all flow through this same generic engine (keys
        # "gpu_core"/"gpu_hotspot"/"gpu_vram") -> bias GPU. RAM DIMMs ("dimm:...") have no GPU
        # relevance -> CPU is the closest available proxy (memory-bus load correlates with
        # CPU-heavy workloads more than anything else we can measure here).
        return "gpu" if key.startswith("gpu_") else "cpu"

    def _transition_sensor_zone(self, key, label, old_zone, new_zone, value, unit, table):
        alert_key = f"sensor:{key}"
        self.sensor_zone_state[key]["confirmed"] = new_zone
        if new_zone == "GREEN":
            entry = self.active_alerts.pop(alert_key, None)
            if entry:
                dur = fmt_dur(time.time() - entry["since"])
                peak = entry.get("peak")
                dominant = self._dominant_workload(entry.get("workload_tally"))
                text = f"{label} recovered after {dur}"
                if peak is not None:
                    text += f" — Peak {peak:.0f}{unit}"
                if dominant:
                    text += f" — dominant workload: {dominant}"
                self.log_event("INFO", text, meta={"component": "sensor", "sensor": label, "zone": "GREEN",
                                                    "duration": dur, "peak": peak, "likely_workload": dominant})
            return
        escalating = ZONE_SEVERITY[new_zone] > ZONE_SEVERITY.get(old_zone, 0)
        kind = "CRIT" if new_zone == "RED" else ("WARN" if escalating else "INFO")
        text = self._sensor_alert_text(label, new_zone, value, unit, table)
        prev = self.active_alerts.get(alert_key, {})
        since = prev.get("since", time.time())
        peak = max(prev.get("peak", value), value)
        self.active_alerts[alert_key] = {"since": since, "text": text, "zone": new_zone, "peak": peak,
                                         "workload_tally": prev.get("workload_tally", {})}
        self._log_alert_with_workload(kind, text, self._bias_for_sensor_key(key),
                                      {"component": "sensor", "sensor": label, "zone": new_zone, "value": value})

    def _update_sensor_zone(self, key, label, value, unit, table):
        """Returns the live (undebounced) zone dict for UI display, or None if no reading.
        Alerting (active_alerts/log_event) is debounced per the rules above."""
        zone = zone_for(value, table)
        if zone is None:
            return None
        alert_key = f"sensor:{key}"
        if alert_key in self.active_alerts:
            self._track_alert_extras(alert_key, value, self._bias_for_sensor_key(key))
        raw_key = zone["key"]
        state = self.sensor_zone_state.setdefault(key, {"confirmed": "GREEN",
                                                         "pending": {"zone": "GREEN", "since": time.time()}})
        now = time.time()
        if raw_key != state["pending"]["zone"]:
            state["pending"] = {"zone": raw_key, "since": now}
        sustained = now - state["pending"]["since"]

        confirmed = state["confirmed"]
        if ZONE_SEVERITY[raw_key] > ZONE_SEVERITY[confirmed]:
            if raw_key == "RED" or sustained >= ALERT_DEBOUNCE_S:
                self._transition_sensor_zone(key, label, confirmed, raw_key, value, unit, table)
        elif ZONE_SEVERITY[raw_key] < ZONE_SEVERITY[confirmed]:
            self._transition_sensor_zone(key, label, confirmed, raw_key, value, unit, table)
        else:
            alert_key = f"sensor:{key}"
            if alert_key in self.active_alerts:
                self.active_alerts[alert_key]["text"] = self._sensor_alert_text(label, raw_key, value, unit, table)
        return zone

    def _update_cpu_fan_alert(self, rpm, ct):
        """The ONLY fan-failure rule Thermal Watch makes: CPU fan reads 0 RPM while the CPU
        is already at/above CPU_YELLOW. GPU fans idling at 0 RPM is normal (zero-RPM mode),
        and unpopulated chassis/pump headers legitimately read 0 RPM forever - neither gets
        a health verdict, per the task's explicit "don't guess" instruction. Debounced like
        the other alerts (3s) despite logging as CRIT, since a single-tick tach glitch on an
        otherwise-fine fan should not immediately cry failure."""
        stalled_now = rpm is not None and rpm <= 0 and ct is not None and ct >= CPU_YELLOW
        now = time.time()
        st = self.cpu_fan_alert_state
        if stalled_now:
            if st["pending_since"] is None:
                st["pending_since"] = now
            if not st["confirmed"] and now - st["pending_since"] >= ALERT_DEBOUNCE_S:
                st["confirmed"] = True
                text = f"CPU Fan — 0 RPM while CPU is {ct:.0f}°C — possible fan failure"
                self.active_alerts["cpufan"] = {"since": now, "text": text, "zone": "RED"}
                self._log_alert_with_workload("CRIT", text, "cpu",
                                              {"component": "cpufan", "sensor": "CPU Fan", "zone": "RED", "value": 0})
        else:
            st["pending_since"] = None
            if st["confirmed"]:
                st["confirmed"] = False
                entry = self.active_alerts.pop("cpufan", None)
                if entry:
                    dur = fmt_dur(now - entry["since"])
                    self.log_event("INFO", f"CPU Fan — spinning again, cleared after {dur}")

    # -- render helpers: create-once, update-in-place --------------------------------------
    @staticmethod
    def _make_empty_label(panel, text):
        """Created once at build() time, not packed - _toggle_visible() shows/hides it later
        without ever recreating it."""
        return tk.Label(panel.body, text=text, bg=PANEL, fg=DIM, font=(SANS, 9))

    @staticmethod
    def _row_key(sensor):
        """(primary_key, legacy_keys) for a raw sensor dict, for _sync_rows() below - primary
        is sensor_identity()'s result (Identifier when available), legacy is always the
        (Parent, Name, SensorType) fallback tuple, so a row cached before Identifier appeared
        gets rekeyed in place instead of duplicated once it does."""
        identity = sensor_identity(sensor)
        legacy = (sensor.get("Parent", ""), sensor.get("Name", ""), sensor.get("SensorType", ""))
        return identity, [legacy]

    def _sync_rows(self, cache, body, specs):
        """specs: ordered list of (key, build_fn, update_fn) or (key, build_fn, update_fn,
        legacy_keys). A row is created (build_fn) only the first time its key appears, and
        reused (update_fn) every time after - so a poll where the sensor inventory hasn't
        changed creates/destroys ZERO widgets. Existing rows are never re-packed/reordered; a
        key that stops appearing gets its row destroyed.

        legacy_keys (optional 4th element): alternate key(s) this exact row may have been
        cached under previously - e.g. a (Parent, Name, SensorType) fallback tuple, before the
        bridge started providing a stable Identifier for it. If the primary key isn't cached
        but one of legacy_keys IS, the existing row is REKEYED in place (not destroyed and
        recreated), so a sensor identity scheme upgrading underneath an already-running UI
        causes no duplicate row, no flicker, and no reordering."""
        seen = set()
        for spec in specs:
            key, build_fn, update_fn = spec[0], spec[1], spec[2]
            legacy_keys = spec[3] if len(spec) > 3 else None
            seen.add(key)
            refs = cache.get(key)
            if refs is None and legacy_keys:
                for legacy_key in legacy_keys:
                    if legacy_key != key and legacy_key in cache:
                        refs = cache.pop(legacy_key)
                        cache[key] = refs
                        break
            if refs is None:
                refs = build_fn(body)
                cache[key] = refs
                self.widget_stats["rows_created"] += 1
            update_fn(refs)
        for stale_key in [k for k in cache if k not in seen]:
            cache[stale_key]["frame"].destroy()
            del cache[stale_key]
            self.widget_stats["rows_destroyed"] += 1

    @staticmethod
    def _toggle_visible(widget, currently_shown, show, **pack_opts):
        """pack()/pack_forget() ONLY on an actual visibility change, never unconditionally
        every poll - avoids the geometry-manager churn that a same-state repack still causes."""
        if show and not currently_shown:
            widget.pack(**pack_opts)
            return True
        if not show and currently_shown:
            widget.pack_forget()
            return False
        return currently_shown

    def _detect_network_flight_events(self, net):
        """v1.1 Phase 4 - Network Flight Recorder. Logs a NETWORK-kind event whenever the active
        adapter's identity or connection state genuinely changes (connected/disconnected,
        active-adapter switch, e.g. Ethernet unplugged and Wi-Fi took over, link drop/restore on
        the same adapter). Reuses the existing event log/timeline architecture entirely - no new
        store, no new timeline builder: TIMELINE_LOG_KINDS already includes "NETWORK", so these
        automatically appear on the Flight Recorder Timeline (Phase 14/v1.0) under the same EVENT
        rows WARN/CRIT already use, filtered by the same "log" checkbox.

        Never logs on the very first observation after startup - there is no PRIOR state to have
        changed FROM yet, and treating "just started watching" as a network event would be a
        fabricated transition, not an observed one."""
        adapter = net.get("adapter")
        curr = ({"index": adapter["index"], "name": adapter["name"], "type": adapter["type"],
                "connected": adapter.get("media_connect_state")} if adapter else None)
        prev = self._prev_net_adapter
        if prev is _NET_STATE_UNSET:
            self._prev_net_adapter = curr
            return
        if curr != prev:
            if prev is None and curr is not None:
                self.log_event("NETWORK", f"Network — connected via {curr['name']} ({curr['type']})")
            elif prev is not None and curr is None:
                self.log_event("NETWORK", f"Network — connectivity lost (was: {prev['name']})")
            elif prev["index"] != curr["index"]:
                self.log_event("NETWORK", f"Network — active adapter switched: {prev['name']} → {curr['name']}")
            elif prev["connected"] != curr["connected"]:
                state = "link restored" if curr["connected"] else "link down"
                self.log_event("NETWORK", f"Network — {curr['name']} {state}")
        self._prev_net_adapter = curr

    def _update_network_zone(self, net):
        """v1.1 Phase 6 - the zone-tracking half, structurally mirroring _update_sensor_zone()/
        _transition_sensor_zone(): always keeps active_alerts/network_zone_state current,
        UNCONDITIONALLY - never gated on incident_restore_pending, exactly like every thermal
        sensor's zone engine isn't. This matters: _reconcile_restored_incidents() reads
        active_alerts to decide whether to resume a restored incident, so that dict must reflect
        live reality even while this component's OWN incident bookkeeping is still deferred
        (see _incident_observe()'s docstring - the same split already exists there, just spread
        across two calls instead of two methods for network's single always-on signal).

        Debounce: losing connectivity needs ALERT_DEBOUNCE_S sustained (a route re-resolve blip
        must not alert), recovery is immediate - the same "de-escalation never waits" rule every
        thermal zone follows. Unlike that engine's "RED skips debounce" rule (justified there
        because RED is the most severe of FOUR zones - alert instantly at the worst tier),
        network only has two states, so RED here just means "disconnected" and still waits out
        the full debounce like any other transition."""
        connected = net.get("adapter") is not None
        # Internal-only bookkeeping value for _reconcile_restored_incidents' "is this component
        # still reporting anything at all" check - never rendered to the user as a real reading.
        self.last_component_values["network"] = 1 if connected else 0
        raw_key = "GREEN" if connected else "RED"

        now = time.time()
        state = self.network_zone_state
        if raw_key != state["pending"]["zone"]:
            state["pending"] = {"zone": raw_key, "since": now}
        sustained = now - state["pending"]["since"]
        confirmed = state["confirmed"]

        if raw_key != confirmed and (raw_key == "GREEN" or sustained >= ALERT_DEBOUNCE_S):
            state["confirmed"] = raw_key
            if raw_key == "GREEN":
                entry = self.active_alerts.pop("network", None)
                if entry:
                    dur = fmt_dur(now - entry["since"])
                    self.log_event("INFO", f"Network connectivity — recovered after {dur}",
                                  meta={"component": "network", "zone": "GREEN", "duration": dur})
            else:
                text = "Network connectivity — lost (no active adapter)"
                self.active_alerts["network"] = {"since": now, "text": text, "zone": "RED"}
                self.log_event("CRIT", text, meta={"component": "network", "zone": "RED"})

    def _update_network_incident(self, net):
        """v1.1 Phase 6 - the incident half: purely observes whatever _update_network_zone()
        (called first, always) already decided about active_alerts["network"] this tick - makes
        no zone/debounce decision of its own, exactly matching _incident_observe()'s own
        contract ("makes no zone/threshold/debounce decision of its own, so an incident can
        never open, escalate, or close on a different schedule than the alert engine already
        governs"). Deferred entirely while a restart restore is pending, same as every other
        component. value is always None throughout - open/touch/close all tolerate that already
        (see _incident_touch: "if value is not None" guards the peak-tracking line) - a
        connectivity incident's peak_value/start_value stay honestly None rather than a
        fabricated numeric proxy, already rendered as "N/A"/"unknown peak" everywhere an
        incident's peak is shown."""
        self._update_network_zone(net)
        key = "network"
        if key in self.incident_restore_pending:
            return  # awaiting _reconcile_restored_incidents() - do not touch it in the meantime
        if key in self.active_alerts:
            if key not in self.incidents_active:
                self._incident_open(key, "network", "Network Connectivity", None, "RED", None)
            self._incident_touch(key, self.active_alerts[key]["zone"], None)
        elif key in self.incidents_active:
            self._incident_close(key, None)

    def _update_network_panel(self):
        """Renders self.last_net (already set at the top of update_data) into the NETWORK panel.
        Never fabricates: an unknown link speed shows "unknown", a None Mbps rate (first tick,
        or the active adapter just changed - see active_network_snapshot()) shows "--", and no
        adapter at all shows the empty-state label, matching every other panel's honesty rule."""
        net = self.last_net or {}
        adapter = net.get("adapter")
        self.net_empty_shown = self._toggle_visible(self.net_empty, self.net_empty_shown,
                                                     adapter is None, anchor="w", pady=4)
        if adapter is None:
            self.net_adapter_label.config(text="--")
            self.net_state_label.config(text="")
            self.net_speed_label.config(text="")
            self.net_down_label.config(text="--")
            self.net_up_label.config(text="--")
            for refs in self.net_detail_labels.values():
                refs["val"].config(text="--")
            return

        self.net_adapter_label.config(text=f"{adapter['name']} ({adapter['type']})")
        connected = adapter.get("media_connect_state")
        state_text = "CONNECTED" if connected else ("DISCONNECTED" if connected is False else "UNKNOWN")
        state_color = GREEN if connected else (RED if connected is False else DIM)
        self.net_state_label.config(text=f"● {state_text}", fg=state_color)

        def fmt_link_speed(bps):
            if bps is None:
                return "unknown"
            return f"{bps / 1e9:.1f} Gbps" if bps >= 1e9 else f"{bps / 1e6:.0f} Mbps"

        rx_speed, tx_speed = fmt_link_speed(adapter.get("receive_link_speed_bps")), fmt_link_speed(adapter.get("transmit_link_speed_bps"))
        self.net_speed_label.config(text=f"LINK {rx_speed} ↓ / {tx_speed} ↑")

        def fmt_mbps(v):
            return f"{v:.2f}" if v is not None else "--"

        self.net_down_label.config(text=fmt_mbps(net.get("down_mbps")))
        self.net_up_label.config(text=fmt_mbps(net.get("up_mbps")))

        self.net_detail_labels["rx"]["val"].config(text=fmt_net_bytes(adapter.get("in_octets")))
        self.net_detail_labels["tx"]["val"].config(text=fmt_net_bytes(adapter.get("out_octets")))
        ip_info = net.get("ip_info") or {}
        self.net_detail_labels["ip"]["val"].config(text=ip_info.get("ipv4") or "N/A")
        self.net_detail_labels["gw"]["val"].config(text=ip_info.get("gateway") or "N/A")
        signal = net.get("wifi_signal")
        is_wifi = adapter.get("type") == "Wi-Fi"
        # Wi-Fi signal is only ever meaningful for a Wi-Fi adapter - shown as N/A rather than
        # hidden on Ethernet, so the field's absence reads as "not applicable", not "broken".
        self.net_detail_labels["signal"]["val"].config(text=f"{signal}%" if is_wifi and signal is not None else "N/A")

        # v1.1 Phase 3 - connection count summary, click to open the full live list.
        conns = self.last_connections or []
        tcp_n = sum(1 for c in conns if c["protocol"] == "TCP")
        udp_n = sum(1 for c in conns if c["protocol"] == "UDP")
        self.net_detail_labels["connections"]["val"].config(text=f"{tcp_n} TCP · {udp_n} UDP")

    def open_connections_window(self):
        """Opens ConnectionsWindow (v1.1 Phase 3), reusing an already-open instance rather than
        stacking duplicates - same convention as open_sensor_history's per-key window cache."""
        if getattr(self, "connections_window", None) is not None and self.connections_window.winfo_exists():
            self.connections_window.lift()
            self.connections_window.focus_force()
            return
        self.connections_window = ConnectionsWindow(self)

    def _update_network_process_list(self):
        """Renders self.last_net_procs (v1.1 Phase 2) into the TOP PROCESSES rows below the
        adapter summary. Requires the elevated bridge's ETW capture (see network_processes() /
        process_network_rates()) - on an older bridge, one that hasn't started capture, or while
        unprivileged, capture_active is honestly False and the panel says so instead of showing
        an empty list that could be misread as "confirmed zero per-process traffic"."""
        info = self.last_net_procs or {}
        for w in self.net_proc_list.winfo_children():
            w.destroy()

        if not info.get("capture_active"):
            reason = info.get("capture_error")
            text = "Per-process attribution unavailable" + (f" ({reason})" if reason else " - waiting on the elevated bridge")
            tk.Label(self.net_proc_list, text=text, bg=PANEL, fg=DIM, font=(MONO, 9),
                    anchor="w", justify="left").pack(fill="x", anchor="w")
            return

        top = info.get("top") or []
        if not top:
            tk.Label(self.net_proc_list, text="No per-process traffic observed this interval",
                    bg=PANEL, fg=DIM, font=(MONO, 9), anchor="w").pack(fill="x", anchor="w")
            return

        def fmt_mbps(v):
            return f"{v:.2f}" if v is not None else "--"

        for proc in top:
            row = tk.Frame(self.net_proc_list, bg=PANEL); row.pack(fill="x", pady=(0, 3))
            name = proc.get("name") or f"pid {proc['pid']}"
            tk.Label(row, text=name, bg=PANEL, fg=TEXT, font=(MONO, 9), width=22, anchor="w").pack(side="left")
            tk.Label(row, text=f"↓ {fmt_mbps(proc['down_mbps'])} Mbps", bg=PANEL, fg=MUTED,
                    font=(MONO, 9), width=16, anchor="w").pack(side="left")
            tk.Label(row, text=f"↑ {fmt_mbps(proc['up_mbps'])} Mbps", bg=PANEL, fg=MUTED,
                    font=(MONO, 9), width=16, anchor="w").pack(side="left")

    def update_data(self, d):
        # Before anything observes this tick: did the wall clock jump while we were suspended?
        # Must run first so the pre-gap telemetry bucket is closed and open incidents/sessions
        # are marked BEFORE this tick's readings are folded in on the far side of the gap.
        gap = self._detect_monitoring_discontinuity()
        if gap is not None:
            self._apply_monitoring_discontinuity(gap)

        workload = d.get("workload")
        if workload is not None:
            self.workload_history.append(workload)
            self.last_foreground = workload["foreground"]
            self.last_cpu_top = workload["cpu_top"]
            self.last_gpu_top = workload["gpu_top"]

        if d["lhm"] is not None:
            self._lhm = d["lhm"]
        sensors = getattr(self, "_lhm", [])

        # Network (v1.1 Phase 1). Kept as its own attribute, not folded into last_context, since
        # the live panel needs more than scalar numbers - adapter identity/type/link state/IP/
        # gateway/Wi-Fi signal - the same "raw snapshot separate from derived scalars" split
        # self._lhm/last_context already uses.
        self.last_net = d.get("net") or {}
        self._detect_network_flight_events(self.last_net)
        self._update_network_incident(self.last_net)
        self.last_net_procs = d.get("net_procs") or {"capture_active": False, "capture_error": None, "top": []}
        self.last_connections = d.get("connections") or []

        def find(sensor_type, predicate):
            return [s for s in sensors if s.get("SensorType") == sensor_type and predicate(s)]

        temps = find("Temperature", lambda s: "cpu" in (s.get("Name", "") + s.get("Parent", "")).lower())
        package = next((s for s in temps if any(k in s.get("Name", "").lower() for k in ("package", "tctl", "tdie"))),
                       temps[0] if temps else None)
        ct = float(package["Value"]) if package and package.get("Value") not in (None, 0) else None

        gpu = d["gpus"][0] if d["gpus"] else {}
        gt = gpu.get("temp")
        gpu_short = gpu.get("name", "GPU").replace("NVIDIA GeForce ", "").replace("NVIDIA ", "").strip() or "GPU"
        mem_pct = d["mem_pct"]

        # peak/avg tracking
        if ct is not None:
            self.cpu_peak = max(self.cpu_peak, ct); self.cpu_sum += ct; self.cpu_n += 1
        if gt is not None:
            self.gpu_peak = max(self.gpu_peak, gt); self.gpu_sum += gt; self.gpu_n += 1
        cpu_avg = self.cpu_sum / self.cpu_n if self.cpu_n else 0
        gpu_avg = self.gpu_sum / self.gpu_n if self.gpu_n else 0

        self.cpu_card.update_value(ct, "NOMINAL", self.cpu_peak, cpu_avg, (ct or 0) / TJMAX if ct is not None else None)
        self.gpu_card.update_value(gt, "NOMINAL", self.gpu_peak, gpu_avg, (gt or 0) / GPU_TMAX if gt is not None else None)
        self.mem_card.update_value(mem_pct, "NOMINAL", mem_pct, mem_pct, mem_pct / 100)

        # stat strip
        self.stat_labels["cpu_load"].config(text=f"{d['cpu_load']:.0f}%")
        self.stat_labels["cpu_clock"].config(text=f"{self.info.get('max', 0) / 1000:.2f} GHz")
        self.stat_labels["gpu_load"].config(text=f"{gpu.get('load', 0):.0f}%" if gpu else "N/A")
        self.stat_labels["gpu_mem"].config(
            text=f"{gpu.get('mem_used', 0) / 1024:.1f} / {gpu.get('mem_total', 0) / 1024:.1f} GB" if gpu else "N/A")
        self.stat_labels["gpu_power"].config(
            text=f"{gpu.get('power', 0):.0f} / {gpu.get('power_limit', 0):.0f} W" if gpu else "N/A")
        cpu_power = find("Power", lambda s: "package" in s.get("Name", "").lower() and "cpu" in s.get("Parent", "").lower())
        self.stat_labels["cpu_power"].config(text=f"{float(cpu_power[0]['Value']):.0f} W" if cpu_power else "N/A")

        # fans - CPU Fan is the only one Thermal Watch makes a health call on (0 RPM while CPU
        # is hot); GPU/chassis/pump 0 RPM is legitimate (idle zero-RPM mode / unpopulated
        # header) and gets "--", never a fabricated verdict.
        fans = find("Fan", lambda s: True)
        cpu_fan = next((f for f in fans if f.get("Name") == "CPU Fan"), None)
        self._update_cpu_fan_alert(float(cpu_fan["Value"]) if cpu_fan and cpu_fan.get("Value") is not None else None, ct)
        cpu_fan_stalled = self.cpu_fan_alert_state["confirmed"]

        def build_fan_row(parent):
            row = tk.Frame(parent, bg=PANEL); row.pack(fill="x", pady=3)
            name_lbl = tk.Label(row, bg=PANEL, fg=MUTED, font=(MONO, 8), width=12, anchor="w")
            name_lbl.pack(side="left")
            bar_wrap = tk.Frame(row, bg=BORDER2, height=4); bar_wrap.pack(side="left", fill="x", expand=True, padx=8)
            bar_wrap.pack_propagate(False)
            fill = tk.Frame(bar_wrap, bg=DIM); fill.place(relx=0, rely=0, relwidth=0, relheight=1)
            rpm_lbl = tk.Label(row, bg=PANEL, fg="#c7ccd4", font=(MONO, 9), width=6, anchor="e")
            rpm_lbl.pack(side="left")
            status_lbl = tk.Label(row, bg=PANEL, fg=DIM, font=(MONO, 8), width=8, anchor="e")
            status_lbl.pack(side="left")
            return {"frame": row, "name": name_lbl, "fill": fill, "rpm": rpm_lbl, "status": status_lbl}

        fan_specs = []
        for f in fans[:12]:
            name = f.get("Name", "FAN")
            rpm = float(f.get("Value") or 0)
            pct = max(0.0, min(1.0, rpm / 3000))
            is_cpu_fan = name == "CPU Fan"
            status_text = ("STALLED" if cpu_fan_stalled else "OK") if is_cpu_fan else "--"
            status_color = RED if (is_cpu_fan and cpu_fan_stalled) else (GREEN if is_cpu_fan else DIM)
            bar_color = ORANGE2 if pct > 0.6 else MUTED if pct > 0 else DIM

            def update_fan_row(refs, name=name, rpm=rpm, pct=pct, bar_color=bar_color,
                               status_text=status_text, status_color=status_color):
                refs["name"].config(text=name.upper())
                refs["fill"].config(bg=bar_color)
                refs["fill"].place(relwidth=pct)
                refs["rpm"].config(text=f"{rpm:.0f}")
                refs["status"].config(text=status_text, fg=status_color)

            key, legacy = self._row_key(f)
            fan_specs.append((key, build_fan_row, update_fan_row, legacy))
        self._sync_rows(self.fan_rows, self.fan_panel.body, fan_specs)
        self.fan_empty_shown = self._toggle_visible(self.fan_empty, self.fan_empty_shown, not fans,
                                                     anchor="w", pady=4)

        any_alert = bool(self.active_alerts)
        curve_now = "PERFORMANCE" if any_alert else "AUTO"
        if curve_now != ("PERFORMANCE" if self.curve_escalated else "AUTO"):
            self.log_event("INFO", f"Fan curve {'escalated to PERFORMANCE' if any_alert else 'returned to AUTO'}")
        self.curve_escalated = any_alert
        self.curve_label.config(text=f"CURVE: {curve_now}", fg=(ORANGE2 if any_alert else DIM))

        # voltages - motherboard/CPU rails only: exclude per-core VID targets (16 near-duplicate
        # request values, not measured rails) and GPU voltage sensors (wrong panel/scope).
        volts = find("Voltage", lambda s: "vid" not in s.get("Name", "").lower()
                     and "gpu" not in s.get("Parent", "").lower())
        # Surface the rails the user actually cares about first (Vcore/SoC/12V/5V/3.3V/DIMM/...);
        # anything else the board exposes still shows, just after these.
        VOLT_PRIORITY = ("vcore", "system agent", "soc", "+12v", "12v", "+5v", "5v", "avcc", "3.3",
                         "dimm", "vddio", "cpu i/o", "termination")

        def volt_rank(s):
            name = s.get("Name", "").lower()
            return next((i for i, kw in enumerate(VOLT_PRIORITY) if kw in name), len(VOLT_PRIORITY))

        volts = sorted(volts, key=volt_rank)
        max_dev = 0.0
        checked_any = False

        def build_volt_row(parent):
            row = tk.Frame(parent, bg=PANEL); row.pack(fill="x", pady=3)
            name_lbl = tk.Label(row, bg=PANEL, fg=MUTED, font=(MONO, 8), width=14, anchor="w")
            name_lbl.pack(side="left")
            val_lbl = tk.Label(row, bg=PANEL, fg="#c7ccd4", font=(MONO, 9))
            val_lbl.pack(side="left", expand=True, anchor="w")
            status_lbl = tk.Label(row, bg=PANEL, fg=DIM, font=(MONO, 8))
            status_lbl.pack(side="right")
            return {"frame": row, "name": name_lbl, "val": val_lbl, "status": status_lbl}

        # Only claim OK/OUT OF RANGE for rails matched by EXACT name against the standard +-5%
        # ATX spec (ATX_NOMINAL). Everything else (Vcore, DIMM, SoC, CPU I/O, ...) has no
        # universal fixed nominal, so it's shown as a real reading with no invented pass/fail.
        volt_specs = []
        for v in volts[:8]:
            val = float(v.get("Value") or 0)
            name = v.get("Name", "RAIL")
            spec = ATX_NOMINAL.get(name.strip().lower())
            if spec:
                checked_any = True
                nom, lo, hi = spec
                ok = lo <= val <= hi
                dev_pct = abs(val - nom) / nom * 100
                max_dev = max(max_dev, dev_pct)
                status_text, status_color = ("OK" if ok else "OUT OF RANGE"), (GREEN if ok else ORANGE)
            else:
                status_text, status_color = "--", DIM

            def update_volt_row(refs, name=name, val=val, status_text=status_text, status_color=status_color):
                refs["name"].config(text=name.upper())
                refs["val"].config(text=f"{val:.3f} V")
                refs["status"].config(text=status_text, fg=status_color)

            key, legacy = self._row_key(v)
            volt_specs.append((key, build_volt_row, update_volt_row, legacy))
        self._sync_rows(self.volt_rows, self.volt_panel.body, volt_specs)
        self.volt_empty_shown = self._toggle_visible(self.volt_empty, self.volt_empty_shown, not volts,
                                                      anchor="w", pady=4)
        self.volt_dev_label.config(text=f"ATX RAIL DEVIATION MAX {max_dev:.1f}%" if checked_any else "ATX RAIL DEVIATION MAX --")

        # drive temps - one row per physical drive, using its Composite Temperature sensor (the
        # live reading). Excludes that same drive's static Warning/Critical *setpoint* sensors,
        # which are configured thresholds, not measurements, and previously got misread as a
        # live 80-90C temperature and falsely tripped the disk alert.
        disk_temps = find("Temperature", lambda s: "storage" in s.get("Parent", "").lower()
                          and s.get("Name") == "Composite Temperature")

        def build_disk_row(parent):
            box = tk.Frame(parent, bg=PANEL); box.pack(fill="x", pady=4)
            head_row = tk.Frame(box, bg=PANEL); head_row.pack(fill="x")
            name_lbl = tk.Label(head_row, bg=PANEL, fg=MUTED, font=(MONO, 8))
            name_lbl.pack(side="left")
            temp_lbl = tk.Label(head_row, bg=PANEL, fg=MUTED, font=(MONO, 10))
            temp_lbl.pack(side="right")
            status_lbl = tk.Label(head_row, bg=PANEL, fg=MUTED, font=(MONO, 8))
            status_lbl.pack(side="right", padx=(0, 8))
            bar_wrap = tk.Frame(box, bg=BORDER2, height=4); bar_wrap.pack(fill="x", pady=(3, 0))
            bar_wrap.pack_propagate(False)
            fill = tk.Frame(bar_wrap, bg=MUTED); fill.place(relx=0, rely=0, relwidth=0, relheight=1)
            return {"frame": box, "name": name_lbl, "temp": temp_lbl, "status": status_lbl, "fill": fill}

        # Telemetry-history samples (item 1) collected alongside the SAME already-filtered
        # drive/motherboard/DIMM readings this loop renders from - no new hardware call, no
        # re-derived filtering. Fed to _telemetry_observe_tick() once, at the end of this method.
        telemetry_sensor_samples = []

        disk_specs = []
        for dsk in disk_temps[:4]:
            drive_key = dsk.get("Parent", "DISK")
            drive_name = drive_key.replace("Storage ", "").strip()
            raw = dsk.get("Value")
            dt = float(raw) if raw not in (None, 0) else None
            telemetry_sensor_samples.append((sensor_identity(dsk), drive_name, drive_key, "Temperature", "drive", dt))
            zone = drive_zone_for(dt)  # live, undebounced - matches the CPU card's own display rule
            if zone is not None:
                self._update_drive_zone(drive_key, drive_name, dt)
                self._incident_observe(f"disk:{drive_key}", "drive", drive_name, dsk.get("Identifier"), dt)
                color, status_text = zone["color"], zone["label"]
            else:
                color, status_text = MUTED, "--"
            pct = 0.0 if dt is None else max(0.0, min(1.0, dt / 80))
            temp_text = "N/A" if dt is None else f"{dt:.0f}\u00b0C"

            # Row-cache identity (sensor_identity) is intentionally separate from drive_key,
            # which stays the exact Parent string used to key drive_zone_state/active_alerts
            # (the debounce/alert state) - unchanged, out of scope for this task.
            row_key, row_legacy = self._row_key(dsk)

            def update_disk_row(refs, drive_name=drive_name, temp_text=temp_text, status_text=status_text,
                                color=color, pct=pct, row_key=row_key):
                refs["name"].config(text=drive_name.upper())
                refs["temp"].config(text=temp_text, fg=color)
                refs["status"].config(text=status_text, fg=color)
                refs["fill"].config(bg=color)
                refs["fill"].place(relwidth=pct)
                self._bind_click(refs["frame"], lambda: self.open_sensor_history(
                    {"kind": "sensor", "key": _sensor_bucket_key(row_key), "label": drive_name,
                     "unit": "°C", "is_temp": True, "component": "drive"}))

            disk_specs.append((row_key, build_disk_row, update_disk_row, row_legacy))
        self._sync_rows(self.disk_rows, self.disk_panel.body, disk_specs)
        self.disk_empty_shown = self._toggle_visible(self.disk_empty, self.disk_empty_shown, not disk_temps,
                                                      anchor="w", pady=4)

        # GPU thermal detail - Core stays sourced from nvidia-smi (gt, unchanged); Hot Spot and
        # Memory Junction come from the LHM feed, which nvidia-smi doesn't expose at all.
        hotspot_s = find("Temperature", lambda s: s.get("Name") == "GPU Hot Spot" and "gpu" in s.get("Parent", "").lower())
        vram_s = find("Temperature", lambda s: s.get("Name") == "GPU Memory Junction" and "gpu" in s.get("Parent", "").lower())
        hotspot = float(hotspot_s[0]["Value"]) if hotspot_s and hotspot_s[0].get("Value") is not None else None
        vram = float(vram_s[0]["Value"]) if vram_s and vram_s[0].get("Value") is not None else None

        # System/case-ambient temperature - the motherboard SuperIO "System" sensor, looked up
        # independently of the MOTHERBOARD/CHIPSET panel loop further below (which runs later and
        # is display-only) so it's available here for last_context. Cross-sensor diagnostics uses
        # this as a case-airflow corroboration signal - never a new absolute threshold, only ever
        # compared against ITS OWN idle baseline (see run_live_cooling_ceiling_diagnostics).
        system_temp_s = next((s for s in sensors if s.get("SensorType") == "Temperature"
                              and "superio" in s.get("Parent", "").lower() and s.get("Name") == "System"), None)
        system_temp = float(system_temp_s["Value"]) if system_temp_s and system_temp_s.get("Value") is not None else None

        # Shared per-tick context snapshot for incident "peak context" tracking (item 3) - built
        # once here since every value it needs is already computed by this point; any active
        # incident's _incident_touch() reads this same dict, never invents a missing value.
        self.last_context = {
            "cpu_temp": ct, "gpu_core_temp": gt, "gpu_hotspot_temp": hotspot, "gpu_vram_temp": vram,
            "cpu_power": float(cpu_power[0]["Value"]) if cpu_power else None,
            "gpu_power": gpu.get("power"),
            "cpu_load": d.get("cpu_load"),
            "gpu_load": gpu.get("load"),
            "mem_pct": mem_pct,
            "gpu_vram_used_mb": gpu.get("mem_used"),  # telemetry-history only (item 2's "VRAM usage")
            # Cross-sensor diagnostics only (live-context, not persisted to telemetry history in
            # this pass - fan RPM/case temp were never added to the bucket schema, see roadmap
            # memory for the scoping reason). gpu_fan_pct is a PERCENTAGE (nvidia-smi fan.speed),
            # not RPM - most GPUs don't expose fan RPM the way LHM exposes "CPU Fan".
            "cpu_fan_rpm": float(cpu_fan["Value"]) if cpu_fan and cpu_fan.get("Value") is not None else None,
            "gpu_fan_pct": gpu.get("fan"),
            "system_temp": system_temp,
            "net_down_mbps": self.last_net.get("down_mbps"),
            "net_up_mbps": self.last_net.get("up_mbps"),
            "net_rx_bytes": self.last_net["adapter"]["in_octets"] if self.last_net.get("adapter") else None,
            "net_tx_bytes": self.last_net["adapter"]["out_octets"] if self.last_net.get("adapter") else None,
        }
        self._update_network_panel()
        self._update_network_process_list()

        def build_zone_row(parent):
            row = tk.Frame(parent, bg=PANEL); row.pack(fill="x", pady=3)
            name_lbl = tk.Label(row, bg=PANEL, fg=MUTED, font=(MONO, 8), width=16, anchor="w")
            name_lbl.pack(side="left")
            val_lbl = tk.Label(row, bg=PANEL, fg=MUTED, font=(MONO, 9), width=6, anchor="e")
            val_lbl.pack(side="left")
            status_lbl = tk.Label(row, bg=PANEL, fg=MUTED, font=(MONO, 8), anchor="e")
            status_lbl.pack(side="left", expand=True, fill="x")
            return {"frame": row, "name": name_lbl, "val": val_lbl, "status": status_lbl}

        def gpu_spec(label, value, table, key, sensor_identifier=None):
            zone = self._update_sensor_zone(key, f"{gpu_short} {label}", value, "\u00b0C", table) if value is not None else None
            self._incident_observe(f"sensor:{key}", key, f"{gpu_short} {label}", sensor_identifier, value)
            color, status = (zone["color"], zone["label"]) if zone else (MUTED, "--")
            text = "N/A" if value is None else f"{value:.0f}\u00b0C"

            scalar_key = {"gpu_hotspot": "gpu_hotspot_temp", "gpu_vram": "gpu_vram_temp"}.get(key)

            def update_row(refs, label=label, text=text, status=status, color=color, scalar_key=scalar_key):
                refs["name"].config(text=label.upper())
                refs["val"].config(text=text, fg=color)
                refs["status"].config(text=status, fg=color)
                # GPU Core is skipped here (scalar_key None) - it already drills down via the
                # GPU CORE card itself, so this row isn't separately re-bound to the same target.
                if scalar_key:
                    self._bind_click(refs["frame"], lambda: self.open_sensor_history(scalar_sensor_ref(scalar_key)))

            return key, build_zone_row, update_row

        gpu_specs = []
        if d["gpus"]:
            gpu_specs = [
                # GPU Core has no sensor_identifier: it's sourced from nvidia-smi (gt), never
                # from an LHM sensor object, so there's no Identifier to attach - correctly
                # left None rather than invented.
                gpu_spec("GPU Core", gt, GPU_CORE_ZONES, "gpu_core"),
                gpu_spec("GPU Hotspot", hotspot, GPU_HOTSPOT_ZONES, "gpu_hotspot",
                        hotspot_s[0].get("Identifier") if hotspot_s else None),
                gpu_spec("Memory Junction", vram, GPU_VRAM_ZONES, "gpu_vram",
                        vram_s[0].get("Identifier") if vram_s else None),
            ]
        self._sync_rows(self.gpu_thermal_rows, self.gpu_thermal_panel.body, gpu_specs)
        self.gpu_thermal_empty_shown = self._toggle_visible(self.gpu_thermal_empty, self.gpu_thermal_empty_shown,
                                                             not d["gpus"], anchor="w", pady=4)

        # motherboard/chipset - raw readings only. No alert policy: safe ranges for these
        # specific sensors (which physical die/plane each corresponds to, vendor-specific
        # limits) aren't confidently known, so guessing thresholds here would just manufacture
        # false alarms. A 0 reading is treated as an unpopulated/unavailable probe (-> N/A),
        # not a real 0\u00b0C.
        mobo_temps = find("Temperature", lambda s: "superio" in s.get("Parent", "").lower())

        mobo_specs = []
        mobo_notes = []
        for s in mobo_temps[:8]:
            name = s.get("Name", "SENSOR")
            val = s.get("Value")
            val = float(val) if val not in (None, 0) else None
            telemetry_sensor_samples.append((sensor_identity(s), name, s.get("Parent", ""), "Temperature", "motherboard", val))
            text = "N/A" if val is None else f"{val:.0f}\u00b0C"
            # Investigated-but-unverified sensors (currently just PCIe x1 on this board) get a
            # label/status annotation only - the reading itself, its source, and the complete
            # absence of any threshold/zone/alert are exactly the same as every other
            # motherboard sensor here. No offset or correction is ever applied to the value.
            # Looked up via sensor_identity() so the production Identifier is preferred and the
            # (Parent, Name, SensorType) fallback still matches on Tier 2/3 or an older bridge.
            label_meta = UNVERIFIED_SENSOR_LABELS.get(sensor_identity(s))
            if label_meta:
                mobo_notes.append(label_meta["note"])

            def update_mobo_row(refs, name=name, text=text, label_meta=label_meta):
                suffix = label_meta["suffix"] if label_meta else ""
                refs["name"].config(text=name.upper() + suffix)
                refs["val"].config(text=text, fg="#c7ccd4")
                if label_meta:
                    refs["status"].config(text=label_meta["status"], fg=label_meta["color"])
                else:
                    refs["status"].config(text="--", fg=DIM)

            key, legacy = self._row_key(s)
            mobo_specs.append((key, build_zone_row, update_mobo_row, legacy))
        self._sync_rows(self.mobo_rows, self.mobo_panel.body, mobo_specs)
        self.mobo_empty_shown = self._toggle_visible(self.mobo_empty, self.mobo_empty_shown, not mobo_temps,
                                                      anchor="w", pady=4)
        self.mobo_footer_label.config(text=" \u00b7 ".join(dict.fromkeys(mobo_notes)) if mobo_notes else
                                      "No confidently-known safe ranges - raw readings only")

        # RAM - only real "DIMM #N" live sensors; excludes the SPD's static Resolution/Low
        # Limit/High Limit/Critical Limit entries, which are configured limits, not readings.
        dimm_temps = find("Temperature", lambda s: "memory" in s.get("Parent", "").lower()
                          and s.get("Name", "").startswith("DIMM"))

        ram_specs = []
        for s in dimm_temps[:4]:
            name = s.get("Name", "DIMM")
            val = s.get("Value")
            val = float(val) if val not in (None,) else None
            telemetry_sensor_samples.append((sensor_identity(s), name, s.get("Parent", ""), "Temperature", "ram", val))
            zone = self._update_sensor_zone(f"dimm:{name}", name, val, "\u00b0C", RAM_ZONES) if val is not None else None
            self._incident_observe(f"sensor:dimm:{name}", "ram", name, s.get("Identifier"), val)
            color, status = (zone["color"], zone["label"]) if zone else (MUTED, "--")
            text = "N/A" if val is None else f"{val:.0f}\u00b0C"

            dimm_identity = sensor_identity(s)

            def update_ram_row(refs, name=name, text=text, status=status, color=color, dimm_identity=dimm_identity):
                refs["name"].config(text=name.upper())
                refs["val"].config(text=text, fg=color)
                refs["status"].config(text=status, fg=color)
                self._bind_click(refs["frame"], lambda: self.open_sensor_history(
                    {"kind": "sensor", "key": _sensor_bucket_key(dimm_identity), "label": name,
                     "unit": "°C", "is_temp": True, "component": "ram"}))

            # Row-cache identity is separate from the f"dimm:{name}" key used by
            # sensor_zone_state/active_alerts (the debounce/alert state) - that key is
            # unchanged, out of scope for this task.
            row_key, row_legacy = self._row_key(s)
            ram_specs.append((row_key, build_zone_row, update_ram_row, row_legacy))
        self._sync_rows(self.ram_rows, self.ram_panel.body, ram_specs)
        self.ram_empty_shown = self._toggle_visible(self.ram_empty, self.ram_empty_shown, not dimm_temps,
                                                     anchor="w", pady=4)

        # alert engine
        self._update_cpu_zone(ct)
        self._incident_observe("cpu", "cpu", "CPU Package", package.get("Identifier") if package else None, ct)
        self._check_alert("mem", mem_pct >= THRESH_MEM, f"System memory {mem_pct:.0f}%, above {THRESH_MEM:.0f}% threshold",
                          value=mem_pct, bias="cpu")
        # (drive/GPU-sub-sensor/RAM/CPU-fan alerting happens per-sensor above, inside their own render loops)

        n_active = len(self.active_alerts)
        silenced = time.time() < self.silence_until
        show_strip = bool(n_active and not silenced)
        # pack()/pack_forget() only on an actual visibility change - calling either every poll
        # (even with an unchanged outcome) triggers a geometry-manager pass across the window.
        self.alert_strip_visible = self._toggle_visible(self.alert_strip, self.alert_strip_visible,
                                                         show_strip, fill="x", pady=(10, 0))
        if show_strip:
            zone_severity = {"YELLOW": 1, "ORANGE": 2, "RED": 3}
            zone_color = {"YELLOW": AMBER, "ORANGE": ORANGE, "RED": RED}
            worst = max(self.active_alerts.values(), key=lambda a: zone_severity.get(a.get("zone"), 2))
            accent = zone_color.get(worst.get("zone"), ORANGE)
            self.alert_badge.config(text=f"\u25cf {n_active} ALERT" + ("S" if n_active > 1 else ""), fg=accent)
            self.alert_tag.config(bg=accent)
            self.alert_text.config(text=" \u00b7 ".join(a["text"] for a in self.active_alerts.values()))
        else:
            self.alert_badge.config(text="")

        self.live_badge.config(text=f"\u25cf LIVE \u00b7 {POLL_SECONDS}s", fg=GREEN)
        base_msg = "CPU sensor connected" if ct is not None else "GPU live \u00b7 restart from the desktop shortcut and approve UAC for CPU temp"
        sensor_msg = f"{base_msg} \u00b7 POLLING {POLL_SECONDS * 1000}MS \u00b7 LOG RETENTION {LOG_RETENTION_DAYS}D"
        self.sensor_status.config(text=sensor_msg)

        epoch = d["time"].timestamp()
        self.samples.append({"timestamp": d["time"].isoformat(timespec="seconds"), "cpu_temp_c": ct,
                             "cpu_load_pct": round(d["cpu_load"], 1), "gpu_temp_c": gt,
                             "gpu_load_pct": gpu.get("load"), "gpu_memory_mb": gpu.get("mem_used"),
                             "gpu_power_w": gpu.get("power"), "memory_pct": mem_pct})
        if len(self.samples) > MAX_SAMPLES:
            self.samples = self.samples[-MAX_SAMPLES:]
        self.chart_points.append((epoch, ct, gt))
        if len(self.chart_points) > MAX_SAMPLES:
            self.chart_points = self.chart_points[-MAX_SAMPLES:]
        self.chart.set_points(self.chart_points)

        # Workload session tracking - runs last, after every component's _incident_observe()
        # call this tick, so a just-closed incident is visible to _session_link_incidents() on
        # the SAME tick it closed rather than one 2s poll later. Pure observation of state this
        # method already computed above - no hardware/process call of its own (item 18).
        self._session_observe_tick()

        # Long-term telemetry history - purely folds this tick's already-computed readings into
        # the current 60s bucket; no hardware/process call, no per-poll disk read (item 20).
        self._telemetry_observe_tick(telemetry_sensor_samples)

    def export(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            initialfile=f"thermal-watch-{datetime.now():%Y%m%d-%H%M}.csv",
                                            filetypes=[("CSV files", "*.csv")])
        if path and self.samples:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=self.samples[0]); w.writeheader(); w.writerows(self.samples)
            self.log_event("INFO", f"Exported {len(self.samples)} samples to CSV")

    def open_history(self):
        """Opens the separate incident-history window, or brings the existing one to front
        and refreshes it - never more than one instance."""
        if self.history_window is not None and self.history_window.winfo_exists():
            self.history_window._reload()
            self.history_window.lift()
            self.history_window.focus_force()
            return
        self.history_window = HistoryWindow(self)

    def open_sensor_history(self, sensor_ref):
        """Sensor drill-down entry point (item 7): opens (or refocuses) a SENSOR HISTORY window
        for exactly this sensor, keyed so clicking a DIFFERENT sensor opens its own window
        rather than reusing/overwriting whatever was already open."""
        key = f"{sensor_ref['kind']}:{sensor_ref['key']}"
        win = self.sensor_history_windows.get(key)
        if win is not None and win.winfo_exists():
            win.lift()
            win.focus_force()
            return
        self.sensor_history_windows[key] = SensorHistoryWindow(self, sensor_ref)

    @staticmethod
    def _bind_click(widget, callback):
        """Recursively binds a click handler (and a hand cursor) across a widget and every
        descendant - Tkinter click events don't bubble, so a label nested inside a frame needs
        its own binding to feel clickable as one unit (item 7's "make sensor cards clickable").

        Idempotent per widget: a row-cache widget's click target never legitimately changes while
        the widget itself stays alive (a sensor whose identity actually changes gets a brand NEW
        widget from build_fn, which starts unmarked). Some update_fn callbacks (disk/GPU-thermal/
        RAM row updates, called by _sync_rows on EVERY poll for an already-existing, reused row -
        never just once at row creation) call this unconditionally on every tick. Without this
        guard, Tkinter's bind() replaces the binding SCRIPT but never releases the previous
        Tcl-registered command, so each poll orphaned one CallWrapper per widget per row -
        unbounded, linear growth confirmed via a 30-minute soak (found by diffing gc object-type
        counts: CallWrapper was >96% of leaked objects, tracing back to this exact lambda)."""
        if getattr(widget, "_click_bound", False):
            return
        widget._click_bound = True
        widget.bind("<Button-1>", lambda _e: callback())
        try:
            widget.config(cursor="hand2")
        except tk.TclError:
            pass
        for child in widget.winfo_children():
            App._bind_click(child, callback)

    def close(self):
        # Deliberately does NOT touch the sensor bridge: it's session-persistent by design,
        # not tied to this UI process's lifetime. That means closing/reopening Thermal Watch
        # never needs a fresh UAC prompt as long as the bridge is still healthy - the whole
        # point of the elevated-bridge/unprivileged-UI split. See launch.ps1's
        # Test-BridgeHealthy and check_bridge_health() above for how a dead/stale bridge gets
        # noticed and restarted instead.
        self._save_active_incidents()  # item 3: immediately on a clean shutdown
        self._save_active_sessions()
        # Shutdown policy for the in-progress telemetry bucket (item 18): a partial bucket with
        # at least one real sample is finalized and persisted as-is (a short bucket around a
        # shutdown boundary is honest, not fabricated - its own sample_count/timestamps say
        # exactly how much of the 60s window it actually covers). An empty bucket (app closed
        # within seconds of starting, before any tick landed) is silently discarded - there is
        # nothing real to persist.
        if self.telemetry_bucket["sample_count"] > 0:
            self._telemetry_finalize_bucket(time.time())
        self.stop_event.set()
        self.destroy()

    # Names of App's own recurring, self-rescheduling after() callbacks - see destroy() for why
    # this exact list, not a blanket sweep of every pending after() id in the interpreter.
    _RECURRING_AFTER_METHODS = ("poll", "tick_uptime", "check_bridge_health",
                                "_reconcile_restored_incidents", "_flush_active_incidents_periodic",
                                "_reconcile_restored_sessions", "_flush_active_sessions_periodic",
                                "_check_due_reports", "_flush_evidence_periodic")

    def destroy(self):
        """Cancels App's own pending recurring after() callbacks and joins worker() before
        tearing down the Tcl interpreter.

        __init__ schedules 7 recurring after() callbacks (poll, tick_uptime,
        check_bridge_health, the report/incident/session reconcile-and-flush timers) that keep
        rescheduling themselves indefinitely - by definition, AT LEAST one is always pending, no
        matter how much time has passed since __init__. Plain destroy() never cancelled any of
        them. A pending callback firing AFTER the interpreter is torn down looks exactly like the
        observed symptom - "invalid command name ...poll/...tick_uptime" - and can corrupt Tcl's
        own thread-safety bookkeeping badly enough to crash the interpreter (Tcl_AsyncDelete:
        async handler deleted by the wrong thread), not just print a warning. Reproduced via
        tools/verify_persistence_integrity.py's rapid create-then-immediately-destroy pattern.

        Scoped to these 7 names specifically, NOT a blanket `self.tk.eval('after info')` sweep of
        every pending id in the (shared, interpreter-wide) after-queue: after_cancel() deletes
        the underlying Tcl command regardless of which widget object originally registered it,
        so cancelling an id that actually belongs to a still-open CHILD Toplevel (e.g. a
        HistoryWindow left open when the main window closes - a normal, legitimate case, exactly
        what tools/verify_incident_analytics.py does) deletes that command out from under the
        child without the child's own bookkeeping knowing, and its later, completely normal
        self.destroy() call then fails with "can't delete Tcl command" trying to delete it again.
        Confirmed by hitting exactly that failure with the blanket-sweep version - fixed by
        matching only on these known method names, found via each id's registered Tcl command
        name (which Tkinter derives from the bound method), never touching a child window's own
        callbacks.

        Cancelling first, then joining the worker thread (still needed: worker() runs real OS
        work on its own thread and touches self.q, which must not still be alive when the
        interpreter goes away), covers both real halves of the original race. Every caller
        (close(), and every verify script's own stop_event.set() + destroy()) already expresses
        "shut down now" intent immediately before calling destroy() - this just makes that intent
        actually clean rather than requiring every call site to know about either hazard."""
        self.stop_event.set()
        try:
            for after_id in self.tk.eval("after info").split():
                try:
                    command = self.tk.call("after", "info", after_id)[0]
                except tk.TclError:
                    continue  # already fired/cancelled between listing and inspecting it
                if any(str(command).endswith(name) for name in self._RECURRING_AFTER_METHODS):
                    self.after_cancel(after_id)
        except tk.TclError:
            pass  # interpreter already gone - nothing to cancel, not an error
        worker_thread = getattr(self, "worker_thread", None)
        if worker_thread is not None and worker_thread.is_alive():
            worker_thread.join(timeout=POLL_SECONDS + 2)
        super().destroy()


if __name__ == "__main__":
    if os.name != "nt": raise SystemExit("Thermal Watch currently supports Windows.")
    App().mainloop()
