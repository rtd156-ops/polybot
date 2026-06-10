"""Multi-strategy support (PAPER).

A "strategy" is just the base configuration with a per-strategy overlay merged
on top. That keeps every existing component (scanner, probability model, risk
manager, paper executor) unchanged -- each simply reads from its own merged
Config. The engine runs one independent paper book per strategy and tags all
ledger rows / events with ``strategy_id``.

Backward compatible: if ``strategies.enabled`` is false/absent, this returns a
single strategy with id ``"default"`` wrapping the base config, so the bot runs
exactly as it did before.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config, deep_merge, validate_config
from .logging_setup import get_logger

log = get_logger("polybot.strategies")

DEFAULT_STRATEGY_ID = "default"

# Sections a strategy overlay is allowed to override. Anything else (data
# sources, notifications, storage, engine, arbitrage) stays global/shared.
OVERRIDABLE_SECTIONS = ("paper", "probability", "risk", "scanner")


@dataclass
class Strategy:
    strategy_id: str
    config: Config


def load_strategies(cfg: Config) -> list[Strategy]:
    """Return the list of strategies to run this process.

    Single-element ``[default]`` unless ``strategies.enabled`` is true with at
    least one definition.
    """
    if not bool(cfg.get("strategies", "enabled", default=False)):
        return [Strategy(DEFAULT_STRATEGY_ID, cfg)]

    definitions = cfg.get("strategies", "definitions", default={}) or {}
    strategies: list[Strategy] = []
    for sid, overlay in definitions.items():
        overlay = overlay or {}
        # Only let strategies touch the sections we intend them to.
        scoped = {k: v for k, v in overlay.items() if k in OVERRIDABLE_SECTIONS}
        ignored = set(overlay) - set(scoped)
        if ignored:
            log.warning("strategy %s: ignoring non-overridable keys %s", sid, sorted(ignored))
        merged = deep_merge(cfg.data, scoped)
        scfg = Config(data=merged, secrets=cfg.secrets, repo_root=cfg.repo_root)
        validate_config(scfg)  # each strategy must still be internally consistent
        strategies.append(Strategy(str(sid), scfg))

    if not strategies:
        log.warning("strategies.enabled but no definitions; falling back to default")
        return [Strategy(DEFAULT_STRATEGY_ID, cfg)]

    log.info("Loaded %d strategies: %s", len(strategies),
             ", ".join(s.strategy_id for s in strategies))
    return strategies
