"""Configuration loading, merging and validation.

Precedence (highest wins):
    explicit overrides (e.g. CLI flags)
      > config.local.yaml          (per-host, NOT versioned)
        > config.yaml              (versioned base)
          > built-in DEFAULTS

Secrets are read from the environment (.env via python-dotenv), never from YAML.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv is a hard dep, but stay defensive
    def load_dotenv(*_a, **_k):  # type: ignore
        return False


VALID_MODES = ("analysis", "paper", "live")


class ConfigError(ValueError):
    """Raised when configuration is missing or internally inconsistent."""


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` onto a copy of ``base``.

    Dicts merge key-by-key; any non-dict value (including lists) replaces.
    """
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


@dataclass
class Secrets:
    """Secrets pulled from the environment. Never serialized as-is."""

    webhook_url: Optional[str] = None
    webhook_token: Optional[str] = None
    poly_private_key: Optional[str] = None
    poly_clob_api_key: Optional[str] = None
    poly_clob_api_secret: Optional[str] = None
    poly_clob_api_passphrase: Optional[str] = None
    poly_funder_address: Optional[str] = None

    @classmethod
    def from_env(cls) -> "Secrets":
        return cls(
            webhook_url=os.getenv("WEBHOOK_URL") or None,
            webhook_token=os.getenv("WEBHOOK_TOKEN") or None,
            poly_private_key=os.getenv("POLY_PRIVATE_KEY") or None,
            poly_clob_api_key=os.getenv("POLY_CLOB_API_KEY") or None,
            poly_clob_api_secret=os.getenv("POLY_CLOB_API_SECRET") or None,
            poly_clob_api_passphrase=os.getenv("POLY_CLOB_API_PASSPHRASE") or None,
            poly_funder_address=os.getenv("POLY_FUNDER_ADDRESS") or None,
        )

    def has_live_credentials(self) -> bool:
        return bool(
            self.poly_private_key
            and self.poly_clob_api_key
            and self.poly_clob_api_secret
            and self.poly_clob_api_passphrase
        )


@dataclass
class Config:
    """Validated configuration. ``data`` holds the merged YAML tree."""

    data: dict = field(default_factory=dict)
    secrets: Secrets = field(default_factory=Secrets)
    repo_root: Path = field(default_factory=Path.cwd)

    # -- convenience accessors -------------------------------------------------
    @property
    def mode(self) -> str:
        return self.data["mode"]

    def get(self, *path: str, default: Any = None) -> Any:
        node: Any = self.data
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    def section(self, name: str) -> dict:
        return self.data.get(name, {}) or {}

    @property
    def live_truly_enabled(self) -> bool:
        """The ONLY place that decides whether real money can move.

        All three must hold: mode==live, execution.live_enabled==true, and live
        credentials present. Anything short of that => paper/analysis only.
        """
        return (
            self.mode == "live"
            and bool(self.get("execution", "live_enabled", default=False))
            and self.secrets.has_live_credentials()
        )

    def db_path(self) -> Path:
        p = Path(self.get("storage", "db_path", default="data/polybot.db"))
        return p if p.is_absolute() else self.repo_root / p


DEFAULTS: dict = {
    "mode": "analysis",
    "data_sources": {
        "gamma_base_url": "https://gamma-api.polymarket.com",
        "clob_base_url": "https://clob.polymarket.com",
        "data_base_url": "https://data-api.polymarket.com",
        "http_timeout_seconds": 15,
        "http_max_retries": 3,
        "http_backoff_seconds": 1.5,
        "user_agent": "polybot/1.0",
        "rate_limit_per_sec": 0,
    },
    "scanner": {
        "max_markets_scanned": 400,
        "require_active": True,
        "require_open": True,
        "min_volume_usd": 20000,
        "min_liquidity_usd": 5000,
        "max_spread": 0.05,
        "min_days_to_resolution": 1,
        "max_days_to_resolution": 180,
        "allowed_categories": [],
        "allow_uncategorized": True,
        "ambiguous_keywords": [],
    },
    "pricing": {
        "min_useful_liquidity_usd": 250,
        "extreme_probability_low": 0.08,
        "extreme_probability_high": 0.92,
        "wide_spread_factor": 1.5,
        "strong_move_threshold": 0.10,
    },
    "probability": {
        "min_edge": 0.04,
        "min_confidence": 0.35,
        "weights": {
            "momentum": 0.30,
            "liquidity": 0.20,
            "time_to_close": 0.15,
            "orderbook_stability": 0.20,
            "category_prior": 0.15,
        },
    },
    "risk": {
        "max_notional_per_market_usd": 100,
        "max_notional_per_category_usd": 400,
        "max_total_exposure_usd": 1000,
        "max_new_positions_per_day": 10,
        "max_daily_loss_usd": 150,
        "max_drawdown_pct": 0.20,
        "min_liquidity_usd": 5000,
        "max_spread": 0.05,
        "min_price": 0.05,
        "max_price": 0.95,
        "min_days_to_resolution": 1,
        "max_days_to_resolution": 180,
        "use_kelly": True,
        "kelly_multiplier": 0.5,
        "max_kelly_fraction": 0.10,
        "min_ticket_usd": 5,
    },
    "paper": {
        "starting_balance_usd": 1000,
        "default_order_notional_usd": 25,
        "fill_against_book": True,
        "taker_slippage": 0.0,
        "fee_bps": 0,
    },
    "arbitrage": {
        "enabled": False,
        "min_edge": 0.01,
        "max_notional_usd": 100,
    },
    "execution": {
        "live_enabled": False,
        "live_order_type": "limit",
        "live_max_order_notional_usd": 50,
        "status_poll_seconds": 5,
    },
    "notifications": {
        "enabled": True,
        "timeout_seconds": 10,
        "outbox": {"enabled": False, "path": "data/outbox.jsonl"},
        "events": {},
    },
    "storage": {"db_path": "data/polybot.db"},
    "logging": {"level": "INFO", "format": "json"},
    "engine": {"loop_interval_seconds": 300, "scan_top_n_for_book": 60},
    "strategies": {"enabled": False, "definitions": {}},
}


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return loaded


def load_config(
    repo_root: Optional[Path] = None,
    base_file: str = "config.yaml",
    local_file: str = "config.local.yaml",
    overrides: Optional[dict] = None,
    load_env: bool = True,
) -> Config:
    """Load, merge and validate configuration."""
    root = Path(repo_root or os.getenv("POLYBOT_ROOT") or Path.cwd()).resolve()

    if load_env:
        env_path = root / ".env"
        load_dotenv(env_path if env_path.exists() else None)

    merged = deep_merge(DEFAULTS, _read_yaml(root / base_file))
    merged = deep_merge(merged, _read_yaml(root / local_file))
    if overrides:
        merged = deep_merge(merged, overrides)

    cfg = Config(data=merged, secrets=Secrets.from_env(), repo_root=root)
    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    """Fail fast on inconsistent configuration. Refuses unsafe live setups."""
    if cfg.mode not in VALID_MODES:
        raise ConfigError(
            f"mode must be one of {VALID_MODES}, got {cfg.mode!r}"
        )

    sc = cfg.section("scanner")
    if not (0 < float(sc.get("max_spread", 0.05)) <= 1):
        raise ConfigError("scanner.max_spread must be in (0, 1]")
    if float(sc.get("min_volume_usd", 0)) < 0:
        raise ConfigError("scanner.min_volume_usd must be >= 0")

    weights = cfg.get("probability", "weights", default={})
    total = sum(float(v) for v in weights.values()) if weights else 0
    if weights and abs(total - 1.0) > 1e-6:
        raise ConfigError(
            f"probability.weights must sum to 1.0 (got {total:.4f})"
        )

    risk = cfg.section("risk")
    if float(risk.get("max_notional_per_market_usd", 0)) <= 0:
        raise ConfigError("risk.max_notional_per_market_usd must be > 0")

    # --- LIVE SAFETY GATE ----------------------------------------------------
    # If the user *configured* live but it cannot run safely, refuse to start so
    # a misconfigured box never silently falls back to (or fakes) real trading.
    if cfg.mode == "live" and cfg.get("execution", "live_enabled", default=False):
        if not cfg.secrets.has_live_credentials():
            raise ConfigError(
                "mode=live with execution.live_enabled=true requires CLOB "
                "credentials in .env (POLY_PRIVATE_KEY, POLY_CLOB_API_KEY, "
                "POLY_CLOB_API_SECRET, POLY_CLOB_API_PASSPHRASE)."
            )
        if cfg.get("execution", "live_order_type") != "limit":
            raise ConfigError(
                "execution.live_order_type must be 'limit' (no aggressive "
                "market orders by default)."
            )
