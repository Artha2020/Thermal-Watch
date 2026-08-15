"""Soak verdict classification, split out of the soak driver so it can be tested deterministically
against synthetic segment series instead of only against 2-hour live runs.

WHY THE OLD RULE WAS REPLACED
The previous rule was ratio-only: "last-segment slope >= 70% of the first segment's => linear leak".
That was calibrated when the leak was ~5 MB/min and the ratio genuinely separated a sustained leak
from a decaying warm-up curve. Once the PDH counter leak was fixed and every segment slope sat near
zero, the ratio compared noise against noise - the post-fix 30-minute run produced segment slopes of
0.326 / 0.457 / 0.172 MB/min (not even monotonic) and the rule mechanically printed BLOCKED because
0.315/0.326 = 0.965. A ratio is meaningless when the numerator and denominator are both ~0.

THRESHOLDS, AND WHERE THEY COME FROM
Three independently measured controls anchor the bands:
  broken Thermal Watch (PDH counter leak)      ~4.0 - 6.0 MB/min sustained, no deceleration over 2h
  standalone Tkinter controls (known-good)     ~0.095 - 0.166 MB/min  (labels / canvas / bars runs)
  post-fix 30-minute run                       ~0.34 MB/min overall, final 10-min segment ~0.172

CONTROL_FLOOR_MB_MIN = 0.20
  The standalone Tk controls are the cleanest available "this is what healthy looks like" signal:
  a process doing nothing but widget updates still drifts up to 0.166 MB/min. 0.20 is that observed
  maximum plus roughly 20% for measurement variance (60s sampling, PowerShell query jitter,
  allocator granularity). A sustained slope at or below this cannot be distinguished from a
  known-good process, so it is not evidence of a leak.
  NOTE: this band is deliberately close to the post-fix run's 0.172 tail. That is not tuning - the
  band is set from the CONTROL runs, and a tail landing just inside it is reported explicitly as
  "at the edge of the control band" so a human can see how narrow the margin is.

SUSTAINED_LEAK_MB_MIN = 1.0
  An order of magnitude below the measured broken behaviour (4-6 MB/min) and 5x above the control
  floor. Anything sustaining >1 MB/min is unambiguously leaking at a rate that matters: ~60 MB/h,
  ~1.4 GB/day. Nothing healthy that has been measured on this project comes close.

DECELERATION_RATIO = 0.5
  Between the floor and the sustained threshold, deceleration is what separates a warm-up curve
  that is still settling from a genuine small linear leak. Requiring the tail to be at most half
  the head is a substantial, clearly-visible decay - not a rounding artifact.

STATES
  PASS       - bounded: tail at/below the control floor, or clearly decelerating toward it.
  REVIEW     - small but NOT decelerating, above the control floor. A genuine residual leak that is
               too slow to be warm-up. This does NOT pass the release gate; it requires a human
               decision. Exit code is non-zero precisely so it cannot be mistaken for a pass.
  BLOCKED    - sustained leak above SUSTAINED_LEAK_MB_MIN.
  INSUFFICIENT_DATA - fewer than MIN_SEGMENTS segments; no verdict is claimed.
"""

CONTROL_FLOOR_MB_MIN = 0.20
SUSTAINED_LEAK_MB_MIN = 1.0
DECELERATION_RATIO = 0.5
MIN_SEGMENTS = 3
CONTROL_OBSERVED_MAX = 0.166  # highest standalone Tk control slope actually measured

PASS = "PASS"
REVIEW = "REVIEW"
BLOCKED = "BLOCKED"
INSUFFICIENT = "INSUFFICIENT_DATA"


def tail_slope(slopes):
    """Mean slope of the final third of segments (minimum 2) - the steady-state estimate, after
    any warm-up has had time to decay. Using a third rather than just the last segment keeps a
    single noisy segment from deciding the verdict."""
    if not slopes:
        return 0.0
    n = max(2, len(slopes) // 3) if len(slopes) >= 2 else 1
    tail = slopes[-n:]
    return sum(tail) / len(tail)


def head_slope(slopes):
    """Mean slope of the first third (minimum 1) - the warm-up-inclusive starting rate."""
    if not slopes:
        return 0.0
    n = max(1, len(slopes) // 3)
    head = slopes[:n]
    return sum(head) / len(head)


def classify(segment_slopes, total_growth_mb=None, duration_min=None):
    """(state, reason) from the per-segment Private Bytes slopes, in MB/min.

    Negative slopes (memory shrinking) are clamped to 0 for classification: a process giving memory
    back is never evidence of a leak, but a negative number would otherwise distort the means."""
    if len(segment_slopes) < MIN_SEGMENTS:
        return INSUFFICIENT, (f"only {len(segment_slopes)} segment(s); need >= {MIN_SEGMENTS} "
                              f"before claiming a verdict")

    slopes = [max(0.0, s) for s in segment_slopes]
    tail = tail_slope(slopes)
    head = head_slope(slopes)

    if tail > SUSTAINED_LEAK_MB_MIN:
        return BLOCKED, (f"tail slope {tail:.3f} MB/min exceeds the sustained-leak threshold "
                         f"{SUSTAINED_LEAK_MB_MIN} MB/min (~{tail * 60:.0f} MB/h). The broken build "
                         f"measured 4-6 MB/min for comparison.")

    if tail <= CONTROL_FLOOR_MB_MIN:
        edge = ""
        if tail > CONTROL_OBSERVED_MAX:
            edge = (f" NOTE: {tail:.3f} is above the highest measured standalone control "
                    f"({CONTROL_OBSERVED_MAX} MB/min) and sits at the edge of the control band - "
                    f"bounded, but with little margin.")
        return PASS, (f"tail slope {tail:.3f} MB/min is at or below the control floor "
                      f"{CONTROL_FLOOR_MB_MIN} MB/min, i.e. indistinguishable from a known-good "
                      f"process.{edge}")

    # Between the floor and the sustained threshold: decelerating warm-up, or a real slow leak?
    if head > 0 and tail <= DECELERATION_RATIO * head:
        return PASS, (f"tail slope {tail:.3f} MB/min is above the control floor but has decayed to "
                      f"{tail / head:.0%} of the head slope {head:.3f} MB/min - a settling warm-up "
                      f"curve, not a sustained leak.")

    return REVIEW, (f"tail slope {tail:.3f} MB/min is above the control floor "
                    f"{CONTROL_FLOOR_MB_MIN} and is NOT decelerating (tail is "
                    f"{tail / head:.0%} of head {head:.3f}). Too slow to be the known leak, too "
                    f"linear to be warm-up: a small residual leak (~{tail * 60:.0f} MB/h). This "
                    f"does not pass the release gate.")


def render(state, reason, segment_slopes, total_growth_mb=None, duration_min=None):
    """Human-readable verdict block. Returns a list of lines and the process exit code to use."""
    slopes = [max(0.0, s) for s in segment_slopes]
    lines = ["---- VERDICT ----",
             f"SEGMENT_SLOPES_MB_PER_MIN={[round(s, 3) for s in segment_slopes]}",
             f"HEAD_SLOPE_MB_PER_MIN={head_slope(slopes):.3f}",
             f"TAIL_SLOPE_MB_PER_MIN={tail_slope(slopes):.3f}",
             f"CONTROL_FLOOR_MB_PER_MIN={CONTROL_FLOOR_MB_MIN}  "
             f"SUSTAINED_LEAK_MB_PER_MIN={SUSTAINED_LEAK_MB_MIN}"]
    if total_growth_mb is not None and duration_min:
        lines.append(f"TOTAL_GROWTH_MB={total_growth_mb:.1f} over {duration_min:.1f} min "
                     f"(overall {total_growth_mb / duration_min:.3f} MB/min, "
                     f"projected {total_growth_mb / duration_min * 60:.0f} MB/h)")
    lines.append(f"STATE={state}")
    lines.append(f"REASON: {reason}")
    if state == PASS:
        lines.append("SOAK GATE: PASS (memory bounded)")
        code = 0
    elif state == REVIEW:
        lines.append("SOAK GATE: REVIEW - NOT A PASS. Small residual leak, human decision required.")
        code = 2
    elif state == BLOCKED:
        lines.append("v1.0 BLOCKED - sustained native memory leak")
        code = 1
    else:
        lines.append("SOAK GATE: INSUFFICIENT DATA - no verdict claimed")
        code = 3
    return lines, code
