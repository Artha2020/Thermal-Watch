"""Simple all-core CPU stress test for exercising Thermal Watch's alerting.

Spawns one busy-loop worker process per logical core and runs them for a
fixed duration, so you can watch CPU temperature/load climb in Thermal Watch.
No external dependencies; safe to Ctrl+C at any time to stop early.
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time


def burn(stop_at: float) -> None:
    # Tight floating-point loop; keeps a core pegged at ~100% without allocating memory.
    x = 0.0001
    while time.time() < stop_at:
        for _ in range(200_000):
            x = x * 1.0000001 + 0.0000001


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=60, help="Duration to run (default: 60)")
    parser.add_argument("--workers", type=int, default=os.cpu_count(),
                         help="Number of worker processes (default: all logical cores)")
    args = parser.parse_args()

    stop_at = time.time() + args.seconds
    print(f"Stress testing {args.workers} logical core(s) for {args.seconds}s. "
          f"Watch Thermal Watch for live temps. Ctrl+C to stop early.")

    procs = [mp.Process(target=burn, args=(stop_at,), daemon=True) for _ in range(args.workers)]
    for p in procs:
        p.start()

    try:
        remaining = args.seconds
        while remaining > 0:
            time.sleep(min(5, remaining))
            remaining -= 5
            if remaining > 0:
                print(f"  ...{remaining}s remaining")
    except KeyboardInterrupt:
        print("Stopping early.")
    finally:
        for p in procs:
            p.join(timeout=2)
            if p.is_alive():
                p.terminate()
        print("Done.")


if __name__ == "__main__":
    main()
