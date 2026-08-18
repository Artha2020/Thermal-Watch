"""Phase 15 - Grounding Guard.

A pure, deterministic reviewer that checks an AI provider's `answer` text against the
`evidence` list already carried on that same `ProviderResponse`, and fails closed: a claim
that contradicts the evidence never survives into the final answer unmodified.

Hard boundary (verified by tools/verify_grounding_guard.py's static-analysis check): this
module does no filesystem I/O, no network I/O, no subprocess, no SQL, and makes no second AI
call. It never imports app.py or thermal_watch_evidence_cli - it works entirely on the plain
str/list/dict/number data already sitting inside the ProviderResponse it is handed. The
`ProviderResponse` name below is a type-checking-only reference (see TYPE_CHECKING import) so
this module carries no runtime dependency on ai.provider_contract either.

Claim extraction is regex/keyword based and deliberately conservative (rule of thumb: when in
doubt, do not extract a claim at all - ordinary prose is not touched). See CHECKABLE_FIELDS and
the per-category _extract_* functions below for exactly what is checked and how.
"""
from __future__ import annotations

import copy
import dataclasses
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - type reference only, no runtime import
    from .provider_contract import ProviderResponse


# ---------------------------------------------------------------------------------------------
# Fixed literals. One consistent string per redaction kind across the whole module, per the
# fail-closed policy: a contradicted claim or a rejected citation is always replaced with one of
# these two literals (never model-generated text, never a guess) unless a targeted, unambiguous
# minimal correction is possible from evidence already bound to that exact claim.
# ---------------------------------------------------------------------------------------------
FIXED_LITERAL_CLAIM = "[Thermal Watch could not verify this statement]"
FIXED_LITERAL_CITATION = "[unverified citation removed]"
BLOCKED_ANSWER_TEXT = "Thermal Watch could not verify this response and is withholding it."

# ---------------------------------------------------------------------------------------------
# Numeric tolerances. Two tiers, chosen deliberately rather than one blanket epsilon:
#   - 0.5 for coarse instrument readings (temperature/percent/watts) that Thermal Watch itself
#     only ever displays to whole-degree/one-decimal precision (see app.py's own zero-decimal
#     formatting for these scalars) - this is rounding safety, not slack that could hide a real
#     mismatch.
#   - 0.05 for network Mbps rates, which are carried through the whole pipeline (and rendered in
#     History/Sessions) to two decimal places - this is the exact tier that must stay tight,
#     because the canonical regression this phase exists to catch (a swapped upload/download
#     claim) is only catchable if a genuinely different field's value is NOT accepted as "close
#     enough".
# ---------------------------------------------------------------------------------------------
TOLERANCE_TEMP_C = 0.5
TOLERANCE_PCT = 0.5
TOLERANCE_POWER_W = 0.5
TOLERANCE_RPM = 1.0
TOLERANCE_MBPS = 0.05
TOLERANCE_MB = 1.0
TOLERANCE_BYTES = 1024.0

_MISSING = object()


@dataclass
class ClaimResult:
    claim_text: str
    field: str | None
    claimed_value: Any
    evidence_value: Any | None
    verdict: str  # "supported" | "contradicted" | "unsupported" | "unverifiable"
    reason: str
    evidence_id: str | None = None


@dataclass
class GroundingReport:
    claims: list[ClaimResult] = field(default_factory=list)
    citations_valid: list[str] = field(default_factory=list)
    citations_rejected: list[str] = field(default_factory=list)
    corrected: bool = False
    verdict: str = "clean"  # "clean" | "corrected" | "blocked"


@dataclass
class FieldSpec:
    """One checkable evidence field: where to find it (operation + path inside that operation's
    `data`), how it might be phrased in prose (aliases, longest phrase first so e.g. "download
    speed" is matched before a bare "download" could shadow it), and the tolerance to compare a
    claimed number against the real one."""
    key: str
    label: str
    operation: str
    path: tuple
    aliases: tuple
    tolerance: float
    alias_re: "re.Pattern" = field(init=False, repr=False)

    def __post_init__(self):
        ordered = tuple(sorted(set(self.aliases), key=len, reverse=True))
        pattern = "|".join(re.escape(a) for a in ordered)
        # Phrase-boundary approximation: no letter immediately touching either edge of the
        # matched alias, so "ramifications" cannot match "ram" and "download" cannot match
        # inside a longer unrelated word.
        self.aliases = ordered
        self.alias_re = re.compile(r"(?<![A-Za-z])(?:" + pattern + r")(?![A-Za-z])", re.IGNORECASE)


# ---------------------------------------------------------------------------------------------
# CHECKABLE_FIELDS - built strictly from the real field names in thermal_watch_evidence_cli.py's
# handlers (get_current_sensors / get_network_status / get_top_network_processes / get_coverage),
# confirmed by reading app.py's _build_evidence_snapshot() and handle_request() directly.
#
# Categories confirmed NOT exposed anywhere in the evidence API today (a claim about these is
# correctly classified "unsupported" by the generic resolver below, never invented a field for):
#   - GPU fan speed is exposed only as a PERCENTAGE (gpu.fan_pct) - there is no GPU fan RPM field.
#     CPU fan is the opposite: only RPM (cpu.fan_rpm) is exposed, no CPU fan percentage.
#   - Memory is exposed only as used_pct (a percentage). No absolute used/total memory in MB/GB.
#   - Incident/session-level numeric fields (component/zone/peak/duration, cpu/gpu/memory/network
#     aggregates) ARE present in the evidence payload (see thermal_watch_evidence_cli._compact_
#     incident/_compact_session and app.py's _finalize_session_record) but are deliberately NOT
#     wired into automatic numeric extraction below - entity binding for "which incident/session
#     row" would require guessing at free-text references this module has no reliable way to
#     disambiguate safely. Factual claims about a specific incident/session are instead checked
#     through the evidence-id citation mechanism (rule 6): an answer that cites INC-/SES-/NET-
#     tokens is checked for whether that id is real, which is the citable, unambiguous surface
#     Phase 14 built for exactly this purpose.
# ---------------------------------------------------------------------------------------------
SENSOR_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("cpu_temp", "CPU Package Temperature", "get_current_sensors", ("cpu", "temp_c"),
              ("cpu package temperature", "cpu temperature", "cpu temp", "processor temperature",
               "cpu package"), TOLERANCE_TEMP_C),
    FieldSpec("cpu_util", "CPU Utilization", "get_current_sensors", ("cpu", "load_pct"),
              ("cpu utilization", "cpu usage", "cpu load"), TOLERANCE_PCT),
    FieldSpec("cpu_power", "CPU Power Draw", "get_current_sensors", ("cpu", "power_w"),
              ("cpu power draw", "cpu power"), TOLERANCE_POWER_W),
    FieldSpec("cpu_fan_rpm", "CPU Fan Speed", "get_current_sensors", ("cpu", "fan_rpm"),
              ("cpu fan speed", "cpu fan rpm", "cpu fan"), TOLERANCE_RPM),
    FieldSpec("gpu_core_temp", "GPU Core Temperature", "get_current_sensors", ("gpu", "core_temp_c"),
              ("gpu core temperature", "gpu core temp"), TOLERANCE_TEMP_C),
    FieldSpec("gpu_hotspot_temp", "GPU Hotspot Temperature", "get_current_sensors", ("gpu", "hotspot_temp_c"),
              ("gpu hotspot temperature", "gpu hot spot temperature", "gpu hotspot temp"), TOLERANCE_TEMP_C),
    FieldSpec("gpu_vram_temp", "GPU Memory Junction Temperature", "get_current_sensors", ("gpu", "vram_temp_c"),
              ("gpu vram temperature", "gpu memory junction temperature", "vram temperature", "vram temp"),
              TOLERANCE_TEMP_C),
    FieldSpec("gpu_util", "GPU Utilization", "get_current_sensors", ("gpu", "load_pct"),
              ("gpu utilization", "gpu usage", "gpu load"), TOLERANCE_PCT),
    FieldSpec("gpu_power", "GPU Power Draw", "get_current_sensors", ("gpu", "power_w"),
              ("gpu power draw", "gpu power"), TOLERANCE_POWER_W),
    FieldSpec("gpu_vram_used", "GPU VRAM Used", "get_current_sensors", ("gpu", "vram_used_mb"),
              ("gpu vram usage", "gpu vram used", "vram usage"), TOLERANCE_MB),
    FieldSpec("gpu_fan_pct", "GPU Fan Speed", "get_current_sensors", ("gpu", "fan_pct"),
              ("gpu fan speed", "gpu fan percentage", "gpu fan"), TOLERANCE_PCT),
    FieldSpec("mem_used_pct", "Memory Usage", "get_current_sensors", ("memory", "used_pct"),
              ("memory usage", "memory utilization", "ram usage", "memory used"), TOLERANCE_PCT),
)

NETWORK_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("net_down_mbps", "Network Download Rate", "get_network_status", ("down_mbps",),
              ("download speed", "download rate", "network download", "net download", "downloading",
               "download"), TOLERANCE_MBPS),
    FieldSpec("net_up_mbps", "Network Upload Rate", "get_network_status", ("up_mbps",),
              ("upload speed", "upload rate", "network upload", "net upload", "uploading",
               "upload"), TOLERANCE_MBPS),
    FieldSpec("net_total_rx_bytes", "Total Bytes Downloaded", "get_network_status", ("total_rx_bytes",),
              ("total bytes downloaded", "total data downloaded", "cumulative download"), TOLERANCE_BYTES),
    FieldSpec("net_total_tx_bytes", "Total Bytes Uploaded", "get_network_status", ("total_tx_bytes",),
              ("total bytes uploaded", "total data uploaded", "cumulative upload"), TOLERANCE_BYTES),
)

COVERAGE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("coverage_pct", "Monitoring Coverage Percentage", "get_coverage", ("coverage_pct",),
              ("monitoring coverage percentage", "monitoring coverage", "coverage percentage",
               "coverage was"), TOLERANCE_PCT),
)

CHECKABLE_FIELDS: tuple[FieldSpec, ...] = SENSOR_FIELDS + NETWORK_FIELDS + COVERAGE_FIELDS


EVIDENCE_ID_TOKEN_RE = re.compile(r"\b(?:INC|NET|SES|COV)-\d{8}-\d{4}\b")

# Rule 5 - causal language. Mirrors the same non-causal discipline tools/verify_ask_grounding.py
# already enforces on the deterministic Ask layer, reimplemented standalone here (no import of
# that file) for the separate AI-answer path.
CAUSAL_PHRASES = ("the cause was", "resulted in", "caused by", "because of", "due to", "led to",
                   "made it")
CAUSAL_RE = re.compile("|".join(re.escape(p) for p in sorted(CAUSAL_PHRASES, key=len, reverse=True)),
                        re.IGNORECASE)

# Rule 4 - monitoring-gap claims. DEFINITE phrases assert a concrete state across an entire named
# span ("all night" / "overnight" / etc); HEDGED phrases explicitly admit uncertainty. This is a
# fixed, exact-phrase keyword list (not free-form NLP) - deliberately, since anything looser risks
# a false "contradicted" on ordinary hedged prose. See GroundingGuard's module docstring / the
# Phase 15 report for the documented rule this encodes:
#   DEFINITE phrase + a real monitoring gap in this turn's evidence -> contradicted
#   DEFINITE phrase + no gap evidence at all           -> unsupported (no ground truth either way)
#   HEDGED phrase (regardless of gap evidence)          -> unsupported, never contradicted
DEFINITE_GAP_PHRASES = (
    "was fine all night", "was safe all night", "stayed safe all night", "ran cool all night",
    "was fine the whole time", "was safe throughout", "nothing happened overnight",
    "everything was fine overnight", "was completely safe overnight", "stayed normal all night",
)
DEFINITE_GAP_RE = re.compile("|".join(re.escape(p) for p in sorted(DEFINITE_GAP_PHRASES, key=len, reverse=True)),
                              re.IGNORECASE)
HEDGED_GAP_PHRASES = (
    "might have stayed safe", "may have been fine", "possibly stayed cool", "probably stayed safe",
    "may have stayed normal", "might have been fine overnight",
)
HEDGED_GAP_RE = re.compile("|".join(re.escape(p) for p in sorted(HEDGED_GAP_PHRASES, key=len, reverse=True)),
                            re.IGNORECASE)

# Rule 8 - a qualitative claim genuinely supported by evidence. Bound to whether a recorded
# incident for that component reached ORANGE/RED, never to an invented temperature threshold.
SAFE_RANGE_COMPONENTS = {
    "cpu": {
        "field_key": "cpu_temp", "incident_components": {"cpu"},
        "phrases": ("cpu temperatures have stayed in a safe range", "cpu temperature has stayed in a safe range",
                    "cpu temperatures stayed in a safe range", "cpu package has stayed in a safe range"),
    },
    "gpu": {
        "field_key": "gpu_hotspot_temp", "incident_components": {"gpu_core", "gpu_hotspot", "gpu_vram"},
        "phrases": ("gpu temperatures have stayed in a safe range", "gpu hotspot has stayed in a safe range",
                    "gpu temperature has stayed in a safe range", "gpu temperatures stayed in a safe range"),
    },
}

# Process-attributed network rate claims. Two phrasings supported; both bind (field, value,
# entity) as one unit per the spec: "<process> is uploading/downloading at <n> Mbps" and
# "<verb>(ing) speed/rate for <process> is <n> Mbps".
PROCESS_RATE_RE = re.compile(
    r"\b(?P<entity>[A-Za-z][A-Za-z0-9_\-]*(?:\.exe)?)(?:['’]s)?\s+is\s+"
    r"(?P<verb>uploading|downloading)\s+at\s+(?P<value>-?\d+(?:\.\d+)?)\s*(?:Mbps)?",
    re.IGNORECASE)
PROCESS_RATE_RE2 = re.compile(
    r"\b(?P<verb>download|upload)(?:ing)?\s+(?:speed|rate)\s+for\s+"
    r"(?P<entity>[A-Za-z][A-Za-z0-9_\-]*(?:\.exe)?)\s+is\s+(?P<value>-?\d+(?:\.\d+)?)\s*(?:Mbps)?",
    re.IGNORECASE)

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


# ---------------------------------------------------------------------------------------------
# Small, dependency-free helpers
# ---------------------------------------------------------------------------------------------

def _get_path(data, path):
    """Walk a fixed key path inside a plain dict. Returns _MISSING if the path does not exist at
    all (the field/record shape is simply not present in this turn's evidence -> "unsupported"),
    as distinct from a real `None` stored at that path (evidence recorded the field as unknown ->
    "contradicted" if a claim asserts a concrete value there, per rule 3)."""
    cur = data
    for key in path:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return _MISSING
    return cur


def _numeric(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _overlaps(span, spans):
    s0, s1 = span
    return any(not (s1 <= o0 or s0 >= o1) for o0, o1 in spans)


def _find_number_near(text, anchor_start, anchor_end, window=45):
    lo = max(0, anchor_start - window)
    hi = min(len(text), anchor_end + window)
    best, best_dist = None, None
    for m in NUMBER_RE.finditer(text, lo, hi):
        dist = min(abs(m.start() - anchor_end), abs(anchor_start - m.end()))
        if best is None or dist < best_dist:
            best, best_dist = m, dist
    return best


def _normalize_entity(name):
    n = (name or "").strip().lower()
    n = re.sub(r"[’'`]s$", "", n)
    if n.endswith(".exe"):
        n = n[:-4]
    return n


def _flip_verb(word):
    lw = word.lower()
    ing = lw.endswith("ing")
    if lw.startswith("download"):
        return "uploading" if ing else "upload"
    return "downloading" if ing else "download"


def _apply_edits(text, edits):
    for start, end, replacement in sorted(edits, key=lambda e: e[0], reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _build_evidence_index(evidence):
    """operation name -> the first dispatched evidence item for that operation this turn."""
    index = {}
    if not isinstance(evidence, list):
        return index
    for item in evidence:
        if not isinstance(item, dict):
            continue
        op = item.get("operation")
        if isinstance(op, str) and op not in index:
            index[op] = item
    return index


def _collect_evidence_ids(evidence):
    """Every `evidence_id` value present anywhere in the evidence list, walked recursively."""
    found = set()

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "evidence_id" and isinstance(v, str):
                    found.add(v)
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(evidence)
    return found


# ---------------------------------------------------------------------------------------------
# Extraction / resolution, one function per claim category
# ---------------------------------------------------------------------------------------------

def _extract_citations(text, known_ids, spans):
    claims, edits, valid, rejected = [], [], [], []
    for m in EVIDENCE_ID_TOKEN_RE.finditer(text):
        span = m.span()
        if _overlaps(span, spans):
            continue
        token = m.group()
        if token in known_ids:
            claims.append(ClaimResult(
                claim_text=token, field=None, claimed_value=token, evidence_value=token,
                verdict="supported", reason="cited evidence id is present in this turn's evidence",
                evidence_id=token))
            valid.append(token)
        else:
            claims.append(ClaimResult(
                claim_text=token, field=None, claimed_value=token, evidence_value=None,
                verdict="contradicted",
                reason="cited evidence id is not present in this turn's evidence (fabricated, or "
                       "belongs to a different record)",
                evidence_id=None))
            rejected.append(token)
            edits.append((span[0], span[1], FIXED_LITERAL_CITATION))
        spans.append(span)
    return claims, edits, valid, rejected


def _extract_causal(text, spans):
    claims, edits = [], []
    for m in CAUSAL_RE.finditer(text):
        span = m.span()
        if _overlaps(span, spans):
            continue
        claims.append(ClaimResult(
            claim_text=m.group(), field=None, claimed_value=None, evidence_value=None,
            verdict="contradicted",
            reason="causal language is not supported by observational Thermal Watch evidence",
            evidence_id=None))
        edits.append((span[0], span[1], FIXED_LITERAL_CLAIM))
        spans.append(span)
    return claims, edits


def _score_process(text, span, entity, entity_span, verb, value, verb_span, value_span, evidence_index):
    claim_text = text[span[0]:span[1]]
    item = evidence_index.get("get_top_network_processes")
    rows = item.get("data") if isinstance(item, dict) else None
    if not isinstance(rows, list):
        return ClaimResult(claim_text=claim_text, field="net_process_rate", claimed_value=value,
                            evidence_value=None, verdict="unsupported",
                            reason="no per-process network evidence present in this turn"), None

    is_download = verb.lower().startswith("download")
    field_key = "current_download_mbps" if is_download else "current_upload_mbps"
    opp_key = "current_upload_mbps" if is_download else "current_download_mbps"
    norm_entity = _normalize_entity(entity)
    entity_row = next((r for r in rows if isinstance(r, dict)
                        and _normalize_entity(str(r.get("process_name") or "")) == norm_entity), None)

    if entity_row is not None:
        actual = _numeric(entity_row.get(field_key))
        if actual is not None and abs(value - actual) <= TOLERANCE_MBPS:
            return ClaimResult(claim_text=claim_text, field="net_process_rate", claimed_value=value,
                                evidence_value=actual, verdict="supported",
                                reason=f"{entity_row.get('process_name')}'s {field_key} matches evidence"), None
        opp_actual = _numeric(entity_row.get(opp_key))
        if opp_actual is not None and abs(value - opp_actual) <= TOLERANCE_MBPS:
            replacement = _flip_verb(text[verb_span[0]:verb_span[1]])
            return ClaimResult(
                claim_text=claim_text, field="net_process_rate", claimed_value=value,
                evidence_value=opp_actual, verdict="contradicted",
                reason=(f"{entity_row.get('process_name')}'s evidence shows this value under the opposite "
                        f"direction; the claimed verb was wrong")
            ), (verb_span[0], verb_span[1], replacement)
        if actual is None:
            return ClaimResult(
                claim_text=claim_text, field="net_process_rate", claimed_value=value, evidence_value=None,
                verdict="contradicted",
                reason=f"{entity_row.get('process_name')}'s {field_key} is unavailable (null) in evidence"
            ), (value_span[0], value_span[1], FIXED_LITERAL_CLAIM)
        alt = [r for r in rows if r is not entity_row and _numeric(r.get(field_key)) is not None
               and abs(value - _numeric(r.get(field_key))) <= TOLERANCE_MBPS]
        if len(alt) == 1:
            correct_name = alt[0].get("process_name") or "another process"
            return ClaimResult(
                claim_text=claim_text, field="net_process_rate", claimed_value=value,
                evidence_value=_numeric(alt[0].get(field_key)), verdict="contradicted",
                reason=f"the claimed rate belongs to {correct_name}, not {entity}"
            ), (entity_span[0], entity_span[1], correct_name)
        return ClaimResult(
            claim_text=claim_text, field="net_process_rate", claimed_value=value, evidence_value=actual,
            verdict="contradicted",
            reason=f"claimed {value} does not match {entity_row.get('process_name')}'s recorded rate"
        ), (value_span[0], value_span[1], FIXED_LITERAL_CLAIM)

    # Entity not present in this turn's evidence at all - check whether exactly one OTHER row's
    # matching field carries this value (misattribution), per rule 2's process-attribution case.
    alt = [r for r in rows if isinstance(r, dict) and _numeric(r.get(field_key)) is not None
           and abs(value - _numeric(r.get(field_key))) <= TOLERANCE_MBPS]
    if len(alt) == 1:
        correct_name = alt[0].get("process_name") or "another process"
        return ClaimResult(
            claim_text=claim_text, field="net_process_rate", claimed_value=value,
            evidence_value=_numeric(alt[0].get(field_key)), verdict="contradicted",
            reason=f"the claimed rate belongs to {correct_name}; {entity} is not in this turn's evidence"
        ), (entity_span[0], entity_span[1], correct_name)
    if len(alt) > 1:
        return ClaimResult(
            claim_text=claim_text, field="net_process_rate", claimed_value=value, evidence_value=None,
            verdict="unverifiable",
            reason="multiple processes match this rate; the claim cannot be safely bound to one"), None
    return ClaimResult(
        claim_text=claim_text, field="net_process_rate", claimed_value=value, evidence_value=None,
        verdict="unsupported",
        reason=f"{entity} is not present in this turn's per-process network evidence"), None


def _extract_process_claims(text, evidence_index, spans):
    claims, edits = [], []
    matches = list(PROCESS_RATE_RE.finditer(text)) + list(PROCESS_RATE_RE2.finditer(text))
    matches.sort(key=lambda m: m.start())
    for m in matches:
        span = m.span()
        if _overlaps(span, spans):
            continue
        entity = m.group("entity")
        verb = m.group("verb")
        value = float(m.group("value"))
        claim, edit = _score_process(text, span, entity, m.span("entity"), verb, value,
                                      m.span("verb"), m.span("value"), evidence_index)
        claims.append(claim)
        if edit is not None:
            edits.append(edit)
        spans.append(span)
    return claims, edits


def _unsupported_claim(text, span, spec, claimed_value, reason):
    return ClaimResult(claim_text=text[span[0]:span[1]], field=spec.key, claimed_value=claimed_value,
                        evidence_value=None, verdict="unsupported", reason=reason)


def _score_scalar(text, spec, claimed_value, group, evidence_index, claim_span, alias_span, num_span):
    item = evidence_index.get(spec.operation)
    if not isinstance(item, dict):
        return _unsupported_claim(text, claim_span, spec, claimed_value,
                                   f"no {spec.operation} evidence present in this turn"), None
    data = item.get("data")
    value = _get_path(data, spec.path)
    if value is _MISSING:
        return _unsupported_claim(text, claim_span, spec, claimed_value,
                                   f"{spec.label} is not present in the evidence for this turn"), None
    if value is None:
        claim = ClaimResult(claim_text=text[claim_span[0]:claim_span[1]], field=spec.key,
                             claimed_value=claimed_value, evidence_value=None, verdict="contradicted",
                             reason=(f"{spec.label} is unavailable (null) in evidence; a concrete value "
                                     f"cannot be asserted"))
        return claim, (num_span[0], num_span[1], FIXED_LITERAL_CLAIM)
    actual = _numeric(value)
    if actual is None:
        return _unsupported_claim(text, claim_span, spec, claimed_value,
                                   f"{spec.label} evidence is not numeric"), None
    if abs(claimed_value - actual) <= spec.tolerance:
        claim = ClaimResult(claim_text=text[claim_span[0]:claim_span[1]], field=spec.key,
                             claimed_value=claimed_value, evidence_value=actual, verdict="supported",
                             reason=f"{spec.label} matches evidence ({actual})")
        return claim, None
    siblings = [s for s in group if s.key != spec.key and s.operation == spec.operation]
    sib_hits = [s for s in siblings
                if _numeric(_get_path(data, s.path)) is not None
                and abs(claimed_value - _numeric(_get_path(data, s.path))) <= s.tolerance]
    if len(sib_hits) == 1:
        sib = sib_hits[0]
        claim = ClaimResult(claim_text=text[claim_span[0]:claim_span[1]], field=spec.key,
                             claimed_value=claimed_value, evidence_value=actual, verdict="contradicted",
                             reason=f"claimed value matches {sib.label}, not {spec.label} as stated")
        return claim, (alias_span[0], alias_span[1], sib.label)
    claim = ClaimResult(claim_text=text[claim_span[0]:claim_span[1]], field=spec.key,
                         claimed_value=claimed_value, evidence_value=actual, verdict="contradicted",
                         reason=f"claimed {claimed_value} does not match evidence value {actual} for {spec.label}")
    return claim, (num_span[0], num_span[1], FIXED_LITERAL_CLAIM)


def _extract_scalar_claims(text, group, evidence_index, spans):
    claims, edits = [], []
    for spec in group:
        for m in spec.alias_re.finditer(text):
            alias_span = m.span()
            if _overlaps(alias_span, spans):
                continue
            num = _find_number_near(text, alias_span[0], alias_span[1])
            if num is None:
                continue
            num_span = num.span()
            claim_span = (min(alias_span[0], num_span[0]), max(alias_span[1], num_span[1]))
            if _overlaps(claim_span, spans):
                continue
            claimed_value = float(num.group())
            claim, edit = _score_scalar(text, spec, claimed_value, group, evidence_index,
                                         claim_span, alias_span, num_span)
            claims.append(claim)
            if edit is not None:
                edits.append(edit)
            spans.append(claim_span)
    return claims, edits


def _extract_gap_claims(text, evidence_index, spans):
    claims, edits = [], []
    item = evidence_index.get("get_coverage")
    data = item.get("data") if isinstance(item, dict) else None
    coverage_pct = data.get("coverage_pct") if isinstance(data, dict) else None
    has_evidence = item is not None and coverage_pct is not None
    has_gap = has_evidence and isinstance(coverage_pct, (int, float)) and coverage_pct < 100.0

    for m in DEFINITE_GAP_RE.finditer(text):
        span = m.span()
        if _overlaps(span, spans):
            continue
        if not has_evidence:
            claims.append(ClaimResult(claim_text=m.group(), field="monitoring_gap",
                                       claimed_value=m.group(), evidence_value=None, verdict="unsupported",
                                       reason="no monitoring coverage evidence present in this turn"))
        elif has_gap:
            claims.append(ClaimResult(
                claim_text=m.group(), field="monitoring_gap", claimed_value=m.group(),
                evidence_value=coverage_pct, verdict="contradicted",
                reason="a definite state is claimed over a period evidence shows was not fully monitored"))
            edits.append((span[0], span[1], FIXED_LITERAL_CLAIM))
        else:
            claims.append(ClaimResult(
                claim_text=m.group(), field="monitoring_gap", claimed_value=m.group(),
                evidence_value=coverage_pct, verdict="unsupported",
                reason="coverage evidence does not establish a per-period ground truth for this claim"))
        spans.append(span)

    for m in HEDGED_GAP_RE.finditer(text):
        span = m.span()
        if _overlaps(span, spans):
            continue
        claims.append(ClaimResult(
            claim_text=m.group(), field="monitoring_gap", claimed_value=m.group(),
            evidence_value=coverage_pct, verdict="unsupported",
            reason="a hedged claim over a possibly-unmonitored period is not checkable against one data point"))
        spans.append(span)
    return claims, edits


def _extract_safe_range_claims(text, evidence_index, spans):
    claims, edits = [], []
    sensors_item = evidence_index.get("get_current_sensors")
    incidents_item = evidence_index.get("get_recent_incidents")
    incident_rows = incidents_item.get("data") if isinstance(incidents_item, dict) else None
    sensor_by_key = {s.key: s for s in SENSOR_FIELDS}

    for info in SAFE_RANGE_COMPONENTS.values():
        pattern = re.compile("|".join(re.escape(p) for p in sorted(info["phrases"], key=len, reverse=True)),
                              re.IGNORECASE)
        for m in pattern.finditer(text):
            span = m.span()
            if _overlaps(span, spans):
                continue
            spec = sensor_by_key[info["field_key"]]
            value = _get_path(sensors_item.get("data"), spec.path) if isinstance(sensors_item, dict) else _MISSING

            bad_incident = None
            if isinstance(incident_rows, list):
                for row in incident_rows:
                    if (isinstance(row, dict) and row.get("component") in info["incident_components"]
                            and row.get("max_zone") in ("ORANGE", "RED")):
                        bad_incident = row
                        break

            if value is _MISSING or value is None:
                claims.append(ClaimResult(
                    claim_text=m.group(), field=spec.key, claimed_value=None, evidence_value=None,
                    verdict="unsupported",
                    reason=f"no current {spec.label} reading is present to confirm this claim"))
            elif bad_incident is not None:
                claims.append(ClaimResult(
                    claim_text=m.group(), field=spec.key, claimed_value=None, evidence_value=value,
                    verdict="contradicted",
                    reason=f"a recorded {bad_incident.get('max_zone')} incident conflicts with a claimed safe range",
                    evidence_id=bad_incident.get("evidence_id")))
                edits.append((span[0], span[1], FIXED_LITERAL_CLAIM))
            else:
                claims.append(ClaimResult(
                    claim_text=m.group(), field=spec.key, claimed_value=None, evidence_value=value,
                    verdict="supported",
                    reason=f"no recorded incident conflicts with the claimed safe range; current reading is {value}"))
            spans.append(span)
    return claims, edits


class GroundingGuard:
    """Pure, deterministic reviewer for one ProviderResponse's answer text against its own
    evidence list. See the module docstring for the full boundary this class holds to."""

    def review(self, response: "ProviderResponse") -> "ProviderResponse":
        raw_evidence = getattr(response, "evidence", None)
        evidence = copy.deepcopy(raw_evidence) if isinstance(raw_evidence, list) else []
        evidence_index = _build_evidence_index(evidence)
        known_ids = _collect_evidence_ids(evidence)

        answer = getattr(response, "answer", None)
        if not isinstance(answer, str) or not answer:
            report = GroundingReport(claims=[], citations_valid=[], citations_rejected=[],
                                      corrected=False, verdict="clean")
            return dataclasses.replace(response, evidence=evidence, grounding=report)

        text = answer
        spans: list[tuple] = []
        claims: list[ClaimResult] = []
        edits: list[tuple] = []

        c_claims, c_edits, valid_ids, rejected_ids = _extract_citations(text, known_ids, spans)
        claims += c_claims
        edits += c_edits

        causal_claims, causal_edits = _extract_causal(text, spans)
        claims += causal_claims
        edits += causal_edits

        proc_claims, proc_edits = _extract_process_claims(text, evidence_index, spans)
        claims += proc_claims
        edits += proc_edits

        for group in (SENSOR_FIELDS, NETWORK_FIELDS, COVERAGE_FIELDS):
            g_claims, g_edits = _extract_scalar_claims(text, group, evidence_index, spans)
            claims += g_claims
            edits += g_edits

        gap_claims, gap_edits = _extract_gap_claims(text, evidence_index, spans)
        claims += gap_claims
        edits += gap_edits

        safe_claims, safe_edits = _extract_safe_range_claims(text, evidence_index, spans)
        claims += safe_claims
        edits += safe_edits

        corrected_text = _apply_edits(text, edits)
        corrected = corrected_text != text
        report = GroundingReport(
            claims=claims, citations_valid=sorted(set(valid_ids)), citations_rejected=sorted(set(rejected_ids)),
            corrected=corrected, verdict="corrected" if corrected else "clean")
        return dataclasses.replace(response, answer=corrected_text, evidence=evidence, grounding=report)
