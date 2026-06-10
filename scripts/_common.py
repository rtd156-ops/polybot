"""Shared CLI bootstrap for the scripts."""

from __future__ import annotations

import sys
from pathlib import Path

# Make the package importable when scripts are run directly (no install needed).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from polybot.config import load_config  # noqa: E402
from polybot.logging_setup import setup_logging  # noqa: E402


def bootstrap(mode_override: str | None = None):
    overrides = {"mode": mode_override} if mode_override else None
    cfg = load_config(repo_root=_ROOT, overrides=overrides)
    setup_logging(
        level=str(cfg.get("logging", "level", default="INFO")),
        fmt=str(cfg.get("logging", "format", default="json")),
    )
    return cfg
