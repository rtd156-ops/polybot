#!/usr/bin/env python3
"""Local, LLM-free polybot outbox watcher (daemon).

Replaces the token-burning OpenClaw/Codex cron that woke an LLM every minute.
This polls data/outbox.jsonl, alerts only on real events using fixed templates,
and delivers via a configured command (WhatsApp/OpenClaw) or stdout/journald.

Config is via environment (see deploy/WATCHER.md). Examples:
    WATCHER_SEND_CMD          shell cmd, message delivered on stdin (no secrets in repo)
    WATCHER_ALERT_RISK_BLOCK  "1" to also alert risk blocks (default off)
    WATCHER_POLL_SECONDS      poll cadence (default 5)
    WATCHER_FROM_START        "1" to replay the whole outbox once (default off)

Usage:
    python scripts/local_watcher.py            # daemon (for systemd)
    python scripts/local_watcher.py --once     # process new events once, exit
    python scripts/local_watcher.py --dry-run  # force stdout delivery (ignore send cmd)
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path

# Make the package importable when run directly (no install needed).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from polybot.watcher import Watcher, load_watcher_config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="polybot local outbox watcher")
    ap.add_argument("--once", action="store_true", help="process new events once and exit")
    ap.add_argument("--dry-run", action="store_true", help="force stdout delivery")
    ap.add_argument("--from-start", action="store_true", help="replay whole outbox once")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    cfg = load_watcher_config()
    if args.dry_run:
        cfg.send_cmd = ""           # force stdout/journald delivery
    if args.from_start:
        cfg.from_start = True

    watcher = Watcher(cfg)
    signal.signal(signal.SIGTERM, watcher.request_stop)
    signal.signal(signal.SIGINT, watcher.request_stop)

    if args.once:
        n = watcher.run_once()
        logging.getLogger("polybot.watcher").info("processed %d alert(s)", n)
        return 0
    watcher.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
