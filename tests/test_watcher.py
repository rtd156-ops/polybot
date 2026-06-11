from __future__ import annotations

import json

import pytest

from polybot.watcher import (
    CursorState,
    Sender,
    Watcher,
    WatcherConfig,
    format_message,
    load_state,
    load_watcher_config,
    read_new_events,
    should_alert,
)


def _cfg(tmp_path, **kw):
    return WatcherConfig(
        outbox_path=tmp_path / "outbox.jsonl",
        state_path=tmp_path / "state.json",
        failed_path=tmp_path / "failed.jsonl",
        send_retries=kw.pop("send_retries", 2),
        retry_backoff=0.0,
        **kw,
    )


def _append(path, *events):
    with path.open("a", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def _evt(event, **data):
    return {"ts": "2026-06-10T00:00:00Z", "strategy_id": data.pop("strategy_id", None),
            "event": event, "data": data}


# --- config from env ---------------------------------------------------------

def test_load_config_defaults_and_env(tmp_path):
    cfg = load_watcher_config({"POLYBOT_ROOT": str(tmp_path), "WATCHER_ALERT_RISK_BLOCK": "1"})
    assert cfg.outbox_path == tmp_path / "data" / "outbox.jsonl"
    assert cfg.alert_risk_block is True
    assert cfg.send_cmd == ""  # nothing hardcoded


# --- filtering ---------------------------------------------------------------

def test_should_alert_defaults(tmp_path):
    cfg = _cfg(tmp_path)
    for e in ("opportunity_detected", "arbitrage_detected", "paper_order_opened",
              "paper_order_closed", "error", "daily_report"):
        assert should_alert(e, cfg) is True
    assert should_alert("risk_block", cfg) is False          # ignored by default
    assert should_alert("something_else", cfg) is False


def test_risk_block_configurable(tmp_path):
    assert should_alert("risk_block", _cfg(tmp_path, alert_risk_block=True)) is True


# --- templates ---------------------------------------------------------------

def test_format_opportunity_with_strategy():
    e = _evt("opportunity_detected", strategy_id="aggressive", market="BTC > 100k?",
             suggested_action="BUY YES", edge=0.09, confidence=0.7,
             url="https://polymarket.com/market/btc")
    msg = format_message(e)
    assert "[aggressive]" in msg and "Oportunidad" in msg and "BUY YES" in msg
    assert "polymarket.com/market/btc" in msg


def test_format_arbitrage():
    msg = format_message(_evt("arbitrage_detected", market="X?", market_price=0.97, edge=0.02))
    assert "Arbitraje" in msg and "0.97" in msg


def test_format_paper_open_and_close():
    o = format_message(_evt("paper_order_opened", outcome="YES", market="X?",
                            price=0.46, notional_usd=25))
    assert "Paper abierta" in o and "YES" in o and "0.46" in o
    c = format_message(_evt("paper_order_closed", outcome="YES", market="X?",
                            price=0.6, realized_pnl=3.2))
    assert "Paper cerrada" in c and "PnL $3.2" in c


def test_format_error_priority_text():
    msg = format_message(_evt("error", stage="scan", error="timeout"))
    assert "ERROR" in msg and "scan" in msg and "timeout" in msg


def test_format_unknown_returns_none():
    assert format_message(_evt("mystery")) is None


def test_default_strategy_has_no_prefix():
    msg = format_message(_evt("opportunity_detected", strategy_id="default", market="X?"))
    assert "[default]" not in msg


# --- reading / cursor / no duplicates ---------------------------------------

def test_no_duplicate_alerts_across_runs(tmp_path):
    cfg = _cfg(tmp_path, from_start=True)
    _append(cfg.outbox_path, _evt("opportunity_detected", market="A"),
            _evt("paper_order_opened", market="B", outcome="YES", price=0.5, notional_usd=10))
    w = Watcher(cfg)
    assert w.run_once() == 2          # both alerted
    assert w.run_once() == 0          # nothing new -> no duplicates
    _append(cfg.outbox_path, _evt("error", stage="x", error="boom"))
    assert w.run_once() == 1          # only the new one


def test_partial_line_not_consumed(tmp_path):
    cfg = _cfg(tmp_path, from_start=True)
    # one full line + a half-written line (no trailing newline)
    with cfg.outbox_path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(_evt("opportunity_detected", market="A")) + "\n")
        fh.write('{"event": "opportunity_detected", "data": {"market": "B"')  # partial
    w = Watcher(cfg)
    assert w.run_once() == 1          # only the complete line
    # finish the partial line
    with cfg.outbox_path.open("a", encoding="utf-8") as fh:
        fh.write('}}\n')
    assert w.run_once() == 1          # now the second completes


def test_invalid_json_skipped(tmp_path):
    cfg = _cfg(tmp_path, from_start=True)
    with cfg.outbox_path.open("w", encoding="utf-8") as fh:
        fh.write("not json at all\n")
        fh.write(json.dumps(_evt("error", error="real")) + "\n")
    w = Watcher(cfg)
    assert w.run_once() == 1          # garbage skipped, good one alerted


def test_truncation_resets_cursor(tmp_path):
    cfg = _cfg(tmp_path, from_start=True)
    _append(cfg.outbox_path, _evt("opportunity_detected", market="A"))
    w = Watcher(cfg)
    assert w.run_once() == 1
    # truncate the outbox and write a fresh event
    cfg.outbox_path.write_text(json.dumps(_evt("error", error="after-truncate")) + "\n")
    assert w.run_once() == 1          # offset reset, picks up new content


def test_rotation_new_inode(tmp_path):
    cfg = _cfg(tmp_path, from_start=True)
    _append(cfg.outbox_path, _evt("opportunity_detected", market="A"))
    w = Watcher(cfg)
    w.run_once()
    # rotate: remove and recreate (new inode) with new content
    cfg.outbox_path.unlink()
    _append(cfg.outbox_path, _evt("error", error="rotated"))
    assert w.run_once() == 1


def test_missing_file_is_noop(tmp_path):
    cfg = _cfg(tmp_path)
    assert Watcher(cfg).run_once() == 0   # no file yet -> nothing, no crash


def test_first_run_skips_history_by_default(tmp_path):
    cfg = _cfg(tmp_path)  # from_start defaults False
    _append(cfg.outbox_path, _evt("opportunity_detected", market="old"))
    w = Watcher(cfg)
    assert w.run_once() == 0           # pre-existing history not replayed
    _append(cfg.outbox_path, _evt("opportunity_detected", market="new"))
    assert w.run_once() == 1           # only genuinely new events


def test_corrupt_state_recovers(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.state_path.write_text("{ this is : not json")
    assert load_state(cfg.state_path) is None   # corrupt -> None -> safe restart
    _append(cfg.outbox_path, _evt("error", error="x"))
    w = Watcher(cfg)            # starts at end (no replay) because state was corrupt
    assert w.run_once() == 0
    _append(cfg.outbox_path, _evt("error", error="y"))
    assert w.run_once() == 1


# --- delivery / retries / dead-letter ---------------------------------------

def test_sender_stdout_fallback(tmp_path):
    assert Sender(_cfg(tmp_path)).send("hello") is True   # no send_cmd -> logs, True


def test_sender_retries_then_fails_and_dead_letters(tmp_path):
    cfg = _cfg(tmp_path, send_cmd="sh -c 'exit 1'", send_retries=2, from_start=True)
    _append(cfg.outbox_path, _evt("error", error="undeliverable"))
    w = Watcher(cfg)
    assert w.run_once() == 1                      # attempted
    # the failed alert is dead-lettered, not lost
    assert cfg.failed_path.exists()
    rec = json.loads(cfg.failed_path.read_text().strip())
    assert rec["event"]["event"] == "error"


def test_sender_success_command(tmp_path):
    cfg = _cfg(tmp_path, send_cmd="cat >/dev/null", from_start=True)
    _append(cfg.outbox_path, _evt("opportunity_detected", market="A"))
    w = Watcher(cfg)
    assert w.run_once() == 1
    assert not cfg.failed_path.exists()           # delivered, nothing dead-lettered


def test_state_persisted_between_watchers(tmp_path):
    cfg = _cfg(tmp_path, from_start=True)
    _append(cfg.outbox_path, _evt("error", error="one"))
    Watcher(cfg).run_once()
    st = load_state(cfg.state_path)
    assert isinstance(st, CursorState) and st.offset > 0
    # a fresh Watcher (simulating a restart) must not replay
    assert Watcher(cfg).run_once() == 0
