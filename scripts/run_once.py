#!/usr/bin/env python3
"""Run a single engine cycle (scan -> analyze -> [paper] -> persist -> alert).

Honors the configured mode (analysis/paper/live). Live stays gated off unless
fully cleared in config + .env. Use this for manual testing on the VPS.

Usage:
    python scripts/run_once.py [--mode analysis|paper] [--loop] [--cycles N]
"""

from __future__ import annotations

import argparse

from _common import bootstrap

from polybot.engine import Engine


def main() -> int:
    ap = argparse.ArgumentParser(description="Run one (or more) engine cycles")
    ap.add_argument("--mode", choices=["analysis", "paper"], default=None,
                    help="override mode (live is intentionally not selectable here)")
    ap.add_argument("--loop", action="store_true", help="run continuously")
    ap.add_argument("--cycles", type=int, default=None, help="max cycles (with --loop)")
    args = ap.parse_args()

    cfg = bootstrap(mode_override=args.mode)
    engine = Engine(cfg)
    try:
        if args.loop:
            engine.run_loop(max_cycles=args.cycles)
        else:
            stats = engine.run_once()
            print("\n=== cycle stats ===")
            for k, v in stats.as_dict().items():
                print(f"  {k:20}: {v}")
    finally:
        engine.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
