"""Deterministic tests for the soak verdict helper, using synthetic segment series - no live soak,
no app, no timing dependence. This exists because the previous verdict rule was silently miscalibrated
once the leak it was tuned for had been fixed, and nothing caught it except a human reading the raw
numbers. These cases pin the intended behaviour at both ends of the scale.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from soak_verdict import (  # noqa: E402
    classify, PASS, REVIEW, BLOCKED, INSUFFICIENT,
    CONTROL_FLOOR_MB_MIN, SUSTAINED_LEAK_MB_MIN, CONTROL_OBSERVED_MAX,
)

CASES = [
    # (name, segment slopes MB/min, expected state, why this case exists)
    ("broken build, flat 5 MB/min",
     [5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0], BLOCKED,
     "the original PDH leak: sustained multi-MB/min, must always block"),

    ("broken build, slightly decaying 5 -> 4.2",
     [5.0, 4.8, 4.6, 4.5, 4.4, 4.3, 4.25, 4.2], BLOCKED,
     "the REAL pre-fix 2h shape - it decelerated ~17% and must still block, "
     "since a ratio-only rule could be fooled by mild decay"),

    ("warm-up decaying to nothing, 0.4 -> ~0",
     [0.4, 0.3, 0.15, 0.05], PASS,
     "classic settling curve - bounded"),

    ("flat at the control floor, ~0.15",
     [0.15, 0.15, 0.15, 0.15, 0.15, 0.15], PASS,
     "matches the standalone Tk controls (0.095-0.166); healthy processes do this"),

    ("small but clearly linear residual, flat 0.6",
     [0.6, 0.6, 0.6, 0.6, 0.6, 0.6], REVIEW,
     "above the control floor and NOT decelerating: too slow to be the known leak, "
     "too linear to be warm-up - must not silently pass"),

    ("just above sustained threshold, flat 1.2",
     [1.2, 1.2, 1.2, 1.2], BLOCKED,
     "beyond SUSTAINED_LEAK_MB_MIN - ~72 MB/h"),

    ("post-fix 30-min run, real measured values",
     [0.326, 0.457, 0.172], REVIEW,
     "the actual observed post-fix segments. Only 3 segments exist, so the tail window covers the "
     "last TWO (0.457, 0.172 -> mean 0.315): above the floor and not decelerating. This is the "
     "honest answer for a 30-minute run - it cannot separate a settling warm-up from a small "
     "residual leak. The 2-hour run's 8 segments are what resolves it. Deliberately NOT relaxed "
     "to PASS: the thresholds come from the control runs, not from wanting this one to pass"),

    ("noisy but bounded, non-monotonic near floor",
     [0.30, 0.05, 0.25, 0.10, 0.18, 0.12], PASS,
     "real measurements wobble; a single high segment must not flip the verdict"),

    ("memory shrinking",
     [-0.2, -0.1, 0.05, -0.05], PASS,
     "negative slopes clamp to 0 - giving memory back is never a leak"),

    ("too few segments",
     [0.5, 0.5], INSUFFICIENT,
     "never claim a verdict from 2 segments"),

    ("high head decaying into the review band, 3.0 -> 0.5",
     [3.0, 2.0, 1.0, 0.6, 0.5, 0.45], PASS,
     "tail 0.48 is above the floor but only 16% of the 3.0 head - a genuine decay curve"),
]


def main():
    print(f"soak_verdict thresholds: CONTROL_FLOOR={CONTROL_FLOOR_MB_MIN} "
          f"SUSTAINED={SUSTAINED_LEAK_MB_MIN} CONTROL_OBSERVED_MAX={CONTROL_OBSERVED_MAX}\n")
    failures = 0
    for name, slopes, expected, why in CASES:
        state, reason = classify(slopes)
        ok = state == expected
        if not ok:
            failures += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        print(f"        slopes={slopes}")
        print(f"        expected={expected} got={state}")
        print(f"        rationale: {why}")
        if not ok:
            print(f"        REASON GIVEN: {reason}")
        print()

    # Boundary behaviour must be explicit, not accidental.
    print("--- boundary checks ---")
    at_floor = [CONTROL_FLOOR_MB_MIN] * 5
    assert classify(at_floor)[0] == PASS, "exactly at the floor must PASS (<=)"
    print(f"  slope exactly {CONTROL_FLOOR_MB_MIN} (the floor) -> PASS")

    just_over = [CONTROL_FLOOR_MB_MIN + 0.01] * 5
    assert classify(just_over)[0] == REVIEW, "just above the floor, flat, must not pass"
    print(f"  slope {CONTROL_FLOOR_MB_MIN + 0.01} flat (just over floor) -> REVIEW")

    at_sustained = [SUSTAINED_LEAK_MB_MIN] * 5
    assert classify(at_sustained)[0] == REVIEW, "exactly at sustained threshold is REVIEW (>)"
    print(f"  slope exactly {SUSTAINED_LEAK_MB_MIN} -> REVIEW")

    over_sustained = [SUSTAINED_LEAK_MB_MIN + 0.01] * 5
    assert classify(over_sustained)[0] == BLOCKED, "just above sustained threshold must block"
    print(f"  slope {SUSTAINED_LEAK_MB_MIN + 0.01} -> BLOCKED")

    print()
    if failures:
        print(f"{failures} CASE(S) FAILED")
        return 1
    print(f"ALL {len(CASES)} VERDICT CASES PASSED, PLUS BOUNDARY CHECKS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
