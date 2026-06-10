from __future__ import annotations

import pytest

from polybot.config import (
    Config,
    ConfigError,
    Secrets,
    deep_merge,
    load_config,
    validate_config,
)


def test_deep_merge_overrides_nested():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    override = {"a": {"y": 20}, "c": 4}
    out = deep_merge(base, override)
    assert out == {"a": {"x": 1, "y": 20}, "b": 3, "c": 4}
    # original untouched
    assert base["a"]["y"] == 2


def test_deep_merge_list_replaces():
    base = {"k": [1, 2, 3]}
    out = deep_merge(base, {"k": [9]})
    assert out["k"] == [9]


def test_load_config_precedence(tmp_path):
    (tmp_path / "config.yaml").write_text("mode: analysis\nscanner:\n  min_volume_usd: 1000\n")
    (tmp_path / "config.local.yaml").write_text("scanner:\n  min_volume_usd: 5000\n")
    cfg = load_config(repo_root=tmp_path, load_env=False)
    assert cfg.mode == "analysis"
    assert cfg.get("scanner", "min_volume_usd") == 5000  # local wins


def test_overrides_beat_local(tmp_path):
    (tmp_path / "config.yaml").write_text("mode: analysis\n")
    cfg = load_config(repo_root=tmp_path, overrides={"mode": "paper"}, load_env=False)
    assert cfg.mode == "paper"


def test_invalid_mode_rejected(tmp_path):
    (tmp_path / "config.yaml").write_text("mode: bananas\n")
    with pytest.raises(ConfigError):
        load_config(repo_root=tmp_path, load_env=False)


def test_weights_must_sum_to_one(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "mode: paper\nprobability:\n  weights:\n    momentum: 0.5\n    liquidity: 0.9\n"
    )
    with pytest.raises(ConfigError):
        load_config(repo_root=tmp_path, load_env=False)


def test_live_requires_credentials():
    cfg = Config(
        data=deep_merge(
            __import__("polybot.config", fromlist=["DEFAULTS"]).DEFAULTS,
            {"mode": "live", "execution": {"live_enabled": True}},
        ),
        secrets=Secrets(),  # no creds
    )
    with pytest.raises(ConfigError):
        validate_config(cfg)


def test_live_truly_enabled_false_without_all_conditions():
    from polybot.config import DEFAULTS
    cfg = Config(data=deep_merge(DEFAULTS, {"mode": "paper"}), secrets=Secrets())
    assert cfg.live_truly_enabled is False

    full_secrets = Secrets(
        poly_private_key="0x" + "a" * 64,
        poly_clob_api_key="k", poly_clob_api_secret="s", poly_clob_api_passphrase="p",
    )
    cfg2 = Config(
        data=deep_merge(DEFAULTS, {"mode": "live", "execution": {"live_enabled": True}}),
        secrets=full_secrets,
    )
    assert cfg2.live_truly_enabled is True
