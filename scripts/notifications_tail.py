#!/usr/bin/env python3
"""Stream polybot's notification outbox -- the bridge for Rook/OpenClaw.

The bot appends every alert to data/outbox.jsonl. This script reads new lines
(optionally following the file like `tail -f`) and prints them as compact,
human-readable alerts. Rook/OpenClaw can either:
  * run this with --follow and relay each line it sees, or
  * read data/outbox.jsonl directly (it's append-only JSONL).

A cursor file remembers the last line read, so repeated polls don't repeat
alerts. Use --reset to start from the beginning.

Usage:
    python scripts/notifications_tail.py            # print new events, then exit
    python scripts/notifications_tail.py --follow   # stream continuously
    python scripts/notifications_tail.py --json      # raw JSON lines (for agents)
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from _common import bootstrap


def _fmt(evt: dict) -> str:
    d = evt.get("data", {})
    event = evt.get("event", "?")
    ts = evt.get("ts", "")
    sid = evt.get("strategy_id") or (d.get("strategy_id") if isinstance(d, dict) else None)
    market = d.get("market") or d.get("market_id") or ""
    prefix = f"[{sid}] " if sid and sid != "default" else ""
    bits = [f"[{ts}] {prefix}{event}"]
    if market:
        bits.append(f"· {str(market)[:60]}")
    if d.get("suggested_action"):
        bits.append(f"· {d['suggested_action']}")
    if d.get("edge") is not None:
        bits.append(f"· edge={d['edge']}")
    if d.get("risk_reason"):
        bits.append(f"· blocked: {d['risk_reason']}")
    if d.get("url"):
        bits.append(f"· {d['url']}")
    return " ".join(bits)


def main() -> int:
    ap = argparse.ArgumentParser(description="Tail the polybot notification outbox")
    ap.add_argument("--follow", action="store_true", help="stream new events continuously")
    ap.add_argument("--json", action="store_true", help="emit raw JSON lines")
    ap.add_argument("--reset", action="store_true", help="ignore cursor; read from start")
    ap.add_argument("--interval", type=float, default=2.0, help="poll seconds with --follow")
    args = ap.parse_args()

    cfg = bootstrap()
    path = Path(cfg.get("notifications", "outbox", "path", default="data/outbox.jsonl"))
    if not path.is_absolute():
        path = cfg.repo_root / path
    cursor = path.with_suffix(".cursor")

    pos = 0
    if cursor.exists() and not args.reset:
        try:
            pos = int(cursor.read_text().strip() or "0")
        except ValueError:
            pos = 0

    def drain() -> int:
        nonlocal pos
        if not path.exists():
            return 0
        count = 0
        with path.open("r", encoding="utf-8") as fh:
            fh.seek(pos)
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                print(line if args.json else _fmt(evt), flush=True)
                count += 1
            pos = fh.tell()
        cursor.write_text(str(pos))
        return count

    drain()
    if args.follow:
        try:
            while True:
                time.sleep(args.interval)
                drain()
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
