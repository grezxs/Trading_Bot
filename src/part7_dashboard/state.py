"""Live state bridge between the running bot and the Streamlit dashboard.

The bot (``python main.py``) and the dashboard (``streamlit run
src/part7_dashboard/app.py``) are SEPARATE processes, so they share state through a
small JSON snapshot file (no Redis needed). ``StatePublisher`` registers as the
LAST MARKET handler: on every tick it reads the portfolio + regime detector and
writes ``runtime/state.json`` (throttled). It also observes SIGNAL and FILL
events to build per-symbol strategy analytics, and keeps each symbol's recent
klines for the dashboard candlestick chart.

It also taps the logging stream to collect a rolling ALERTS feed (orders,
TP/SL, kills, funding caps, drawdown halt).

Observer only — every handler returns None and emits no events, so the
event-driven contract is untouched.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque, defaultdict
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_STATE_PATH = _REPO_ROOT / "runtime" / "state.json"

# loggers whose INFO lines are worth surfacing as "alerts" (plus anything
# WARNING+ from anywhere). These are the trade/risk lifecycle loggers.
_ALERT_INFO_LOGGERS = {"risk", "oms", "broker", "control"}
_MAX_CANDLES = 120  # bars kept per symbol for the dashboard kline


class _AlertCollector(logging.Handler):
    """Captures interesting log records into a bounded ring buffer."""

    def __init__(self, buf: deque) -> None:
        super().__init__(level=logging.INFO)
        self._buf = buf

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.WARNING and record.name not in _ALERT_INFO_LOGGERS:
                return
            self._buf.append({
                "ts": record.created,
                "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level": record.levelname,
                "name": record.name,
                "msg": record.getMessage(),
            })
        except Exception:  # logging must never raise
            pass


class StatePublisher:
    """Snapshots portfolio + regime + analytics to JSON for the dashboard."""

    def __init__(self, portfolio, detector, symbols: list[str],
                 out_path: Optional[Path] = None, *, control: Any = None,
                 write_every: float = 0.5, max_points: int = 1000,
                 max_alerts: int = 120) -> None:
        self.portfolio = portfolio
        self.detector = detector
        self.symbols = list(symbols)
        self.control = control
        self.out_path = Path(out_path) if out_path else DEFAULT_STATE_PATH
        self.write_every = write_every
        self._equity_curve: deque = deque(maxlen=max_points)
        self._alerts: deque = deque(maxlen=max_alerts)
        self._candles: dict[str, list] = {}        # symbol -> [[ts,o,h,l,c,v], ...]
        self._sig_counts: dict[str, dict[str, int]] = defaultdict(
            lambda: {"BUY": 0, "SELL": 0, "HOLD": 0})
        self._last_signal: dict[str, dict] = {}    # symbol -> {signal, strategy, magnitude, price, time}
        self._fill_counts: dict[str, int] = defaultdict(int)
        # drawdown tracking (current + running max, abs + pct)
        start_eq = float(getattr(portfolio, "equity", 0.0) or 0.0)
        self._peak_equity = start_eq
        self._max_dd_abs = 0.0
        self._max_dd_pct = 0.0
        self._last_write = 0.0
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        logging.getLogger().addHandler(_AlertCollector(self._alerts))

    # --- event observers (all return None) ---
    def on_signal(self, event) -> None:
        sym = getattr(event, "symbol", None)
        sig = getattr(event, "signal", None)
        if sym is None or sig is None:
            return None
        name = getattr(sig, "name", str(sig))
        self._sig_counts[sym][name] = self._sig_counts[sym].get(name, 0) + 1
        self._last_signal[sym] = {
            "signal": name,
            "strategy": getattr(event, "strategy", "") or "",
            "magnitude": round(float(getattr(event, "magnitude", 0.0) or 0.0), 3),
            "price": round(float(getattr(event, "price", 0.0) or 0.0), 2),
            "time": time.strftime("%H:%M:%S"),
        }
        return None

    def on_fill(self, event) -> None:
        sym = getattr(event, "symbol", None)
        if sym is not None:
            self._fill_counts[sym] += 1
        return None

    def on_market(self, event) -> None:
        # cache the latest klines for this symbol (for the candlestick chart)
        candles = getattr(event, "candles", None)
        if candles:
            self._candles[event.symbol] = [
                [round(float(c.ts), 2), float(c.open), float(c.high),
                 float(c.low), float(c.close), float(getattr(c, "volume", 0.0) or 0.0)]
                for c in candles[-_MAX_CANDLES:]
            ]
        now = time.time()
        if (now - self._last_write) >= self.write_every:
            self._last_write = now
            self._write(now)
        return None

    # --- snapshot building ---
    def _update_drawdown(self, equity: float) -> tuple[float, float]:
        self._peak_equity = max(self._peak_equity, equity)
        dd_abs = self._peak_equity - equity
        dd_pct = (dd_abs / self._peak_equity) if self._peak_equity > 0 else 0.0
        self._max_dd_abs = max(self._max_dd_abs, dd_abs)
        self._max_dd_pct = max(self._max_dd_pct, dd_pct)
        return dd_abs, dd_pct

    def _per_symbol(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        last = getattr(self.portfolio, "_last_price", {})
        for s in self.symbols:
            try:
                r = self.detector.current_regime(s)
                regime = r.name if r is not None else None
            except Exception:
                regime = None
            pos = self.portfolio.positions.get(s)
            price = float(last.get(s, getattr(pos, "last_price", 0.0) if pos else 0.0) or 0.0)
            qty = float(pos.qty) if pos else 0.0
            position = {
                "qty": round(qty, 6),
                "avg": round(float(pos.avg_price), 2) if pos else 0.0,
                "last": round(price, 2),
                "notional": round(qty * price, 2),
                "realized": round(float(pos.realized_pnl), 2) if pos else 0.0,
                "unrealized": round(float(pos.unrealized_pnl), 2) if pos else 0.0,
            }
            out[s] = {
                "regime": regime,
                "price": round(price, 2),
                "position": position,
                "candles": self._candles.get(s, []),
                "signals": dict(self._sig_counts.get(s, {"BUY": 0, "SELL": 0, "HOLD": 0})),
                "last_signal": self._last_signal.get(s),
                "fills": int(self._fill_counts.get(s, 0)),
            }
        return out

    def _snapshot(self, now: float) -> dict[str, Any]:
        pf = self.portfolio
        equity = float(pf.equity)
        dd_abs, dd_pct = self._update_drawdown(equity)
        snap = pf.snapshot()
        control = {
            "trading_enabled": bool(getattr(self.control, "trading_enabled", True)),
            "killed": bool(getattr(self.control, "killed", False)),
        } if self.control is not None else {"trading_enabled": True, "killed": False}
        return {
            "updated": now,
            "updated_str": time.strftime("%H:%M:%S", time.localtime(now)),
            "equity": round(equity, 2),
            "cash": round(float(pf.cash), 2),
            "realized_pnl": snap.get("realized_pnl", 0.0),
            "unrealized_pnl": snap.get("unrealized_pnl", 0.0),
            "total_pnl": snap.get("total_pnl", 0.0),
            "fees_paid": snap.get("fees_paid", 0.0),
            "peak_equity": round(self._peak_equity, 2),
            "drawdown_abs": round(dd_abs, 2),
            "drawdown_pct": round(dd_pct * 100, 2),
            "max_drawdown_abs": round(self._max_dd_abs, 2),
            "max_drawdown_pct": round(self._max_dd_pct * 100, 2),
            "positions": snap.get("positions", {}),
            "regimes": {},      # filled in _write (after _per_symbol is built)
            "per_symbol": {},   # filled in _write
            "symbols": self.symbols,
            "equity_curve": list(self._equity_curve),
            "control": control,
            "alerts": list(self._alerts)[-60:][::-1],  # newest first
        }

    def _write(self, now: float) -> None:
        self._equity_curve.append([round(now, 2), round(float(self.portfolio.equity), 2)])
        data = self._snapshot(now)
        per_symbol = self._per_symbol()
        data["per_symbol"] = per_symbol
        data["regimes"] = {s: d["regime"] for s, d in per_symbol.items()}
        tmp = self.out_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(data))
            os.replace(tmp, self.out_path)  # atomic swap so readers never see a half file
        except Exception:
            logging.getLogger("dashboard").debug("state write failed", exc_info=True)

    def flush(self) -> None:
        """Force a final write (e.g. at shutdown)."""
        self._write(time.time())
