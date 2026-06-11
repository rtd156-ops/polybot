"""Local, LLM-free outbox watcher core (stdlib only).

Tails polybot's append-only ``data/outbox.jsonl``, filters for events worth a
ping, and renders short fixed-template messages -- no AI, no tokens. A delivery
command (WhatsApp/OpenClaw gateway) is configured via environment so no secret
ever lives in code or in the repo.

Robustness goals (all covered by tests):
  * persistent byte-offset cursor keyed by inode -> no duplicate alerts
  * survives missing file, invalid JSON lines, partial (half-written) lines,
    in-place truncation, and file rotation (new inode)
  * corrupt/missing cursor recovers safely (starts at end, never replays history)
  * delivery failures retry, then dead-letter to disk instead of being lost

The logic here is import-clean and side-effect free except where noted, so it is
unit-tested without a VPS. ``scripts/local_watcher.py`` is the thin CLI wrapper.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("polybot.watcher")

# Events that trigger an alert by default. risk_block is intentionally excluded
# (configurable). Unknown event types are ignored.
DEFAULT_ALERTABLE = frozenset({
    "opportunity_detected",
    "arbitrage_detected",
    "paper_order_opened",
    "paper_order_closed",
    "error",
    "daily_report",
})


@dataclass
class WatcherConfig:
    outbox_path: Path
    state_path: Path
    failed_path: Path
    poll_seconds: float = 5.0
    send_cmd: str = ""            # shell command; message delivered on stdin
    send_retries: int = 3
    retry_backoff: float = 2.0
    send_timeout: float = 30.0
    alert_risk_block: bool = False
    from_start: bool = False      # replay whole file on first run (default: no)


def _b(v: str, default: bool) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def load_watcher_config(env: Optional[dict] = None) -> WatcherConfig:
    """Build config from environment, with /opt/polybot defaults. No secrets here."""
    env = dict(os.environ if env is None else env)
    root = env.get("POLYBOT_ROOT", "/opt/polybot")
    data = Path(env.get("WATCHER_DATA_DIR", str(Path(root) / "data")))
    return WatcherConfig(
        outbox_path=Path(env.get("WATCHER_OUTBOX", str(data / "outbox.jsonl"))),
        state_path=Path(env.get("WATCHER_STATE", str(data / "local_watcher.state.json"))),
        failed_path=Path(env.get("WATCHER_FAILED", str(data / "local_watcher.failed.jsonl"))),
        poll_seconds=float(env.get("WATCHER_POLL_SECONDS", "5")),
        send_cmd=env.get("WATCHER_SEND_CMD", "") or "",
        send_retries=int(env.get("WATCHER_SEND_RETRIES", "3")),
        retry_backoff=float(env.get("WATCHER_RETRY_BACKOFF", "2")),
        send_timeout=float(env.get("WATCHER_SEND_TIMEOUT", "30")),
        alert_risk_block=_b(env.get("WATCHER_ALERT_RISK_BLOCK"), False),
        from_start=_b(env.get("WATCHER_FROM_START"), False),
    )


# --- cursor state ------------------------------------------------------------

@dataclass
class CursorState:
    inode: int
    offset: int


def load_state(path: Path) -> Optional[CursorState]:
    """Load cursor; return None if missing or corrupt (caller restarts safely)."""
    try:
        raw = path.read_text(encoding="utf-8")
        d = json.loads(raw)
        return CursorState(int(d["inode"]), int(d["offset"]))
    except (FileNotFoundError, ValueError, KeyError, TypeError, OSError):
        return None


def save_state(path: Path, state: CursorState) -> None:
    """Atomically persist the cursor (temp file + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"inode": state.inode, "offset": state.offset}),
                   encoding="utf-8")
    os.replace(tmp, path)


# --- reading new events ------------------------------------------------------

def read_new_events(
    cfg: WatcherConfig, state: Optional[CursorState]
) -> tuple[list[dict], Optional[CursorState], str]:
    """Return (events, new_state, status).

    status in {"ok", "missing"}. Handles first-run, rotation, truncation and
    partial trailing lines. Never raises on a malformed line -- it is skipped.
    """
    try:
        st = os.stat(cfg.outbox_path)
    except FileNotFoundError:
        return [], state, "missing"

    if state is None:
        # First run / recovered cursor: start at end unless asked to replay.
        offset = 0 if cfg.from_start else st.st_size
        state = CursorState(st.st_ino, offset)
    elif state.inode != st.st_ino:
        state = CursorState(st.st_ino, 0)          # rotated: fresh file
    elif st.st_size < state.offset:
        state = CursorState(st.st_ino, 0)          # truncated in place

    with open(cfg.outbox_path, "rb") as fh:
        fh.seek(state.offset)
        data = fh.read()

    parts = data.split(b"\n")
    partial = parts.pop()                          # last chunk: b'' or half line
    consumed = len(data) - len(partial)            # only advance past full lines

    events: list[dict] = []
    for raw in parts:
        raw = raw.strip()
        if not raw:
            continue
        try:
            events.append(json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            log.warning("skipping malformed outbox line (%d bytes)", len(raw))

    new_state = CursorState(state.inode, state.offset + consumed)
    return events, new_state, "ok"


# --- message formatting (fixed templates, no AI) -----------------------------

def _short(text, n: int = 55) -> str:
    s = str(text or "?")
    return s if len(s) <= n else s[: n - 1] + "…"


def _strategy_prefix(evt: dict) -> str:
    sid = evt.get("strategy_id")
    if not sid:
        data = evt.get("data") or {}
        sid = data.get("strategy_id") if isinstance(data, dict) else None
    return f"[{sid}] " if sid and sid != "default" else ""


def _url(d: dict) -> str:
    u = d.get("url")
    return f" {u}" if u else ""


def should_alert(event_type: Optional[str], cfg: WatcherConfig) -> bool:
    if event_type == "risk_block":
        return cfg.alert_risk_block
    return event_type in DEFAULT_ALERTABLE


def format_message(evt: dict) -> Optional[str]:
    """Render a short fixed-template message. Returns None if no template."""
    etype = evt.get("event")
    d = evt.get("data") or {}
    if not isinstance(d, dict):
        d = {}
    pfx = _strategy_prefix(evt)

    if etype == "opportunity_detected":
        return (f"🎯 {pfx}Oportunidad: {_short(d.get('market'))} → "
                f"{d.get('suggested_action', '?')} | edge {d.get('edge')} "
                f"conf {d.get('confidence')}{_url(d)}")
    if etype == "arbitrage_detected":
        return (f"💰 {pfx}Arbitraje: {_short(d.get('market'))} | "
                f"costo {d.get('market_price')} edge {d.get('edge')}{_url(d)}")
    if etype == "paper_order_opened":
        return (f"📈 {pfx}Paper abierta: {d.get('outcome', '?')} "
                f"{_short(d.get('market'))} @ {d.get('price')} "
                f"(${d.get('notional_usd')})")
    if etype == "paper_order_closed":
        pnl = d.get("realized_pnl", d.get("pnl"))
        tail = f" | PnL ${pnl}" if pnl is not None else ""
        return (f"📉 {pfx}Paper cerrada: {d.get('outcome', '?')} "
                f"{_short(d.get('market'))} @ {d.get('price')}{tail}")
    if etype == "error":
        stage = f" en {d.get('stage')}" if d.get("stage") else ""
        return f"🚨 {pfx}polybot ERROR{stage}: {d.get('error', '(sin detalle)')}"
    if etype == "daily_report":
        return f"📊 {pfx}Reporte diario: {_daily_summary(d)}"
    if etype == "risk_block":
        return (f"⛔ {pfx}Bloqueado: {_short(d.get('market'))} "
                f"({d.get('risk_reason', '?')})")
    return None


def _daily_summary(d: dict) -> str:
    keys = ("opportunities", "signals_accepted", "signals_rejected",
            "paper_orders", "realized_pnl", "equity")
    bits = [f"{k}={d[k]}" for k in keys if k in d]
    return ", ".join(bits) if bits else "(ver paper_report)"


# --- delivery ----------------------------------------------------------------

class Sender:
    """Delivers a message via a configured shell command (message on stdin).

    If no command is configured, logs to stdout/journald instead -- the watcher
    is fully functional for testing without WhatsApp wired. No secret is ever
    logged; the delivery command supplies its own credentials from its env.
    """

    def __init__(self, cfg: WatcherConfig) -> None:
        self.cfg = cfg

    def send(self, message: str, priority: bool = False) -> bool:
        if not self.cfg.send_cmd:
            log.info("ALERT%s: %s", " [PRIORITY]" if priority else "", message)
            return True
        for attempt in range(1, self.cfg.send_retries + 1):
            try:
                proc = subprocess.run(
                    self.cfg.send_cmd, shell=True, input=message.encode("utf-8"),
                    capture_output=True, timeout=self.cfg.send_timeout,
                )
                if proc.returncode == 0:
                    return True
                log.warning("send cmd exit=%d (attempt %d/%d)",
                            proc.returncode, attempt, self.cfg.send_retries)
            except (subprocess.SubprocessError, OSError) as exc:
                log.warning("send cmd failed: %s (attempt %d/%d)",
                            type(exc).__name__, attempt, self.cfg.send_retries)
            if attempt < self.cfg.send_retries:
                time.sleep(self.cfg.retry_backoff * (2 ** (attempt - 1)))
        return False


# --- watcher orchestration ---------------------------------------------------

class Watcher:
    def __init__(self, cfg: WatcherConfig, sender: Optional[Sender] = None) -> None:
        self.cfg = cfg
        self.sender = sender or Sender(cfg)
        self.state = load_state(cfg.state_path)
        self._stop = False

    def run_once(self) -> int:
        """Process all currently-available new events. Returns count alerted."""
        events, new_state, status = read_new_events(self.cfg, self.state)
        if status == "missing":
            return 0
        alerted = 0
        for evt in events:
            etype = evt.get("event")
            if not should_alert(etype, self.cfg):
                continue
            msg = format_message(evt)
            if msg is None:
                continue
            ok = self.sender.send(msg, priority=(etype == "error"))
            if not ok:
                self._dead_letter(evt)
            alerted += 1
        self.state = new_state
        if new_state is not None:
            save_state(self.cfg.state_path, new_state)
        return alerted

    def _dead_letter(self, evt: dict) -> None:
        """Persist an undeliverable alert instead of losing it."""
        try:
            self.cfg.failed_path.parent.mkdir(parents=True, exist_ok=True)
            with self.cfg.failed_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"failed_at": time.time(), "event": evt}) + "\n")
            log.error("delivery failed; dead-lettered to %s", self.cfg.failed_path)
        except OSError:
            log.error("delivery failed AND could not dead-letter event")

    def request_stop(self, *_a) -> None:
        self._stop = True

    def run_forever(self) -> None:
        log.info("watcher started: outbox=%s poll=%.1fs send=%s",
                 self.cfg.outbox_path, self.cfg.poll_seconds,
                 "cmd" if self.cfg.send_cmd else "stdout")
        while not self._stop:
            try:
                self.run_once()
            except Exception:  # never let the daemon die on a transient error
                log.exception("watcher cycle failed; continuing")
            # Interruptible-ish sleep in small steps so SIGTERM stops promptly.
            slept = 0.0
            while slept < self.cfg.poll_seconds and not self._stop:
                time.sleep(min(0.5, self.cfg.poll_seconds - slept))
                slept += 0.5
        log.info("watcher stopped cleanly")
