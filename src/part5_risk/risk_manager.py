"""Risk manager: turn approved SIGNALs into sized ORDERs, and gate trading.

Implemented (verified in mock):
- Drawdown halt: if equity falls more than ``max_drawdown`` below the peak,
  halt the engine (hard stop, human intervention required) and stop emitting
  orders.
- Funding check: never let a BUY exceed available cash.

Part 4 (rough) — the order-policy knobs in ``config.yaml`` (``order:``) are now
wired through here. Passed in as ``order_policy`` (a plain dict, ``Config.order``):
- ``trading_enabled`` master kill-switch (entries off; protective exits still fire).
- Position sizing modes: notional / base / pct_equity / allin.
- ``max_open_positions`` — cap how many symbols can be held at once (entries).
- ``max_position_value_per_symbol`` — cap TOTAL marked notional per symbol; a BUY
  is shrunk (or skipped) so accumulated holdings never exceed it.
- ``cooldown_sec`` — min seconds between *entry* orders on the same symbol.
- ``min_notional_usdt`` — skip dust orders.
- Spot has no shorting, so a SELL is capped at the current long quantity.
- Order ``type``: MARKET, or LIMIT priced ``limit_offset_bps`` inside the mid
  with a ``ttl_sec`` for the OMS to auto-cancel if unfilled.
- ``take_profit_pct`` / ``stop_loss_pct`` — position-aware exits, checked every
  MARKET tick via ``on_market`` (these bypass the kill-switch/cooldown so a
  position can always be protected).
- ``inventory_balance`` — when on, ``on_market`` trims an over-weight long back
  toward ``inventory_target_usdt`` once it drifts past the tolerance band
  (SELL-only; spot can't short to rebalance). Runs after TP/SL.

Still TODO (Gilbert / Grace): historical VaR; trailing stop; per-symbol total
position cap; daily-loss halt; slippage abort on MARKET fills.
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Any, Optional

from ..core.engine import Engine
from ..core.events import OrderEvent, SignalEvent
from ..core.events import MarketEvent
from ..core.models import Order, OrderType, Side, SignalType
from ..portfolio.portfolio import Portfolio

log = logging.getLogger("risk")


class RiskManager:
    def __init__(
        self,
        portfolio: Portfolio,
        engine: Engine,
        max_position_value: float = 200.0,
        max_position_qty: float = 1.0,
        max_drawdown: float = 0.2,
        var_window: int = 60,
        order_policy: Optional[dict[str, Any]] = None,
        control: Any = None,
    ) -> None:
        self.portfolio = portfolio
        self.engine = engine
        self.max_position_value = max_position_value
        self.max_position_qty = max_position_qty
        self.max_drawdown = max_drawdown
        self.var_window = var_window
        self.policy: dict[str, Any] = order_policy or {}
        # optional dashboard control (BotControl): runtime enable/stop/kill.
        # When present it OVERRIDES the static config ``trading_enabled``.
        self.control = control
        self._peak_equity = portfolio.equity
        self._equity_hist: deque[float] = deque(maxlen=max(var_window, 2))
        self._last_order_ts: dict[str, float] = {}  # per-symbol entry cooldown

    def _p(self, key: str, default: Any) -> Any:
        return self.policy.get(key, default)

    # --- drawdown / halt ---
    def _update_drawdown(self) -> bool:
        eq = self.portfolio.equity
        self._equity_hist.append(eq)
        self._peak_equity = max(self._peak_equity, eq)
        if self._peak_equity <= 0:
            return False
        dd = (self._peak_equity - eq) / self._peak_equity
        if dd >= self.max_drawdown:
            log.critical("DRAWDOWN BREACH %.1f%% (peak=%.2f now=%.2f) — halting",
                         dd * 100, self._peak_equity, eq)
            self.engine.halt()
            return True
        return False

    # --- sizing ---
    def _size(self, price: float) -> float:
        """Position size for ONE order, from the configured sizing mode, then
        clamped by the hard per-order caps (max_position_value/qty)."""
        mode = str(self._p("sizing", "notional")).lower()
        if mode == "base":
            qty = float(self._p("base_qty", 0.0001))
        elif mode == "pct_equity":
            qty = float(self._p("pct_equity", 0.10)) * self.portfolio.equity / price
        elif mode == "allin":
            buf = float(self._p("allin_buffer_pct", 0.01))
            qty = max(0.0, self.portfolio.cash) * (1.0 - buf) / price
        else:  # "notional"
            qty = float(self._p("notional_usdt", 100.0)) / price
        # hard caps (defense in depth, independent of the sizing mode)
        notional_cap_qty = self.max_position_value / price
        return max(0.0, min(qty, notional_cap_qty, self.max_position_qty))

    def _held_symbols(self) -> set[str]:
        return {s for s, p in self.portfolio.positions.items() if p.qty != 0}

    def _build_order(self, symbol: str, side: Side, qty: float, ref: float) -> Order:
        """MARKET, or a LIMIT placed passively inside the mid with a TTL."""
        is_limit = str(self._p("type", "market")).lower() == "limit"
        if not is_limit:
            return Order(symbol=symbol, side=side, qty=qty, order_type=OrderType.MARKET)
        off = float(self._p("limit_offset_bps", 0.0)) / 10_000.0
        # passive: buy below mid, sell above mid
        limit_price = ref * (1.0 - off) if side is Side.BUY else ref * (1.0 + off)
        ttl = float(self._p("ttl_sec", 5.0)) if bool(self._p("cancel_unfilled", True)) else None
        return Order(symbol=symbol, side=side, qty=qty, order_type=OrderType.LIMIT,
                     limit_price=limit_price, ttl=ttl)

    def on_signal(self, event: SignalEvent) -> Optional[list[OrderEvent]]:
        if self._update_drawdown() or self.engine.halted:
            return None
        if self.control is not None and getattr(self.control, "killed", False):
            return None
        if event.signal is SignalType.HOLD:
            return None
        # dashboard Stop button (control) overrides the static config flag
        enabled = bool(self._p("trading_enabled", True))
        if self.control is not None:
            enabled = bool(self.control.trading_enabled)
        if not enabled:
            log.info("trading disabled — %s %s logged, no order placed",
                     event.symbol, event.signal.value)
            return None

        price = event.price or 0.0
        if price <= 0:
            return None

        # entry cooldown (per symbol)
        cooldown = float(self._p("cooldown_sec", 0.0))
        last = self._last_order_ts.get(event.symbol)
        if last is not None and (event.ts - last) < cooldown:
            return None

        side = Side.BUY if event.signal is SignalType.BUY else Side.SELL

        # cap concurrent symbols held (only blocks opening a NEW symbol)
        if side is Side.BUY:
            cap = int(self._p("max_open_positions", 10_000))
            held = self._held_symbols()
            if event.symbol not in held and len(held) >= cap:
                log.info("max_open_positions=%d reached — skip new BUY %s", cap, event.symbol)
                return None

        qty = self._size(price)
        if qty <= 0:
            return None

        # spot can't short: a SELL only reduces an existing long
        if side is Side.SELL:
            pos = self.portfolio.positions.get(event.symbol)
            held_qty = pos.qty if pos else 0.0
            if held_qty <= 0:
                log.info("no long %s to sell (spot, no shorting) — skip", event.symbol)
                return None
            qty = min(qty, held_qty)

        # per-symbol TOTAL position cap: shrink (or skip) a BUY that would push
        # this symbol's marked holdings above the cap. This is the dimension the
        # per-ORDER caps + cooldown can't enforce — it stops slow accumulation
        # into a single symbol across many small orders.
        if side is Side.BUY:
            cap_sym = float(self._p("max_position_value_per_symbol", 0.0) or 0.0)
            if cap_sym > 0:
                pos = self.portfolio.positions.get(event.symbol)
                held_notional = (pos.qty * price) if pos else 0.0
                room = cap_sym - held_notional
                if room <= 0:
                    log.info("per-symbol cap %.2f USDT reached for %s (held=%.2f) — skip BUY",
                             cap_sym, event.symbol, held_notional)
                    return None
                max_qty = room / price
                if qty > max_qty:
                    log.info("per-symbol cap %s: %.6f -> %.6f (held=%.2f, cap=%.2f)",
                             event.symbol, qty, max_qty, held_notional, cap_sym)
                    qty = max_qty

        # funding check on buys (spot can't spend cash it doesn't have)
        if side is Side.BUY:
            cost = qty * price
            if cost > self.portfolio.cash:
                affordable = self.portfolio.cash / price
                log.warning("funding cap %s: %.6f -> %.6f", event.symbol, qty, affordable)
                qty = affordable
            if qty <= 0:
                log.warning("no funding for %s BUY — skipped", event.symbol)
                return None

        # skip dust below the exchange min-notional
        if qty * price < float(self._p("min_notional_usdt", 0.0)):
            log.info("below min_notional (%.2f USDT) — skip %s", qty * price, event.symbol)
            return None

        order = self._build_order(event.symbol, side, qty, price)
        self._last_order_ts[event.symbol] = event.ts
        log.info("ORDER %s %s %.6f %s @ ~%.2f", event.symbol, side.value, qty,
                 order.order_type.value, price)
        return [OrderEvent(order=order)]

    # --- take-profit / stop-loss (position-aware exits) ---
    def on_market(self, event: MarketEvent) -> Optional[list[OrderEvent]]:
        """Checked every MARKET tick: close a position that breaches TP/SL.

        Bypasses the kill-switch and cooldown — a held position must always be
        protectable. Registered AFTER ``portfolio.on_market`` so the mark is
        current.
        """
        if self.engine.halted:
            return None
        pos = self.portfolio.positions.get(event.symbol)
        if pos is None or pos.qty == 0 or pos.avg_price <= 0:
            return None

        tp = float(self._p("take_profit_pct", 0.0) or 0.0)
        sl = float(self._p("stop_loss_pct", 0.0) or 0.0)
        if tp > 0 or sl > 0:
            # signed return in the position's favour (works long or short)
            raw_ret = (event.price - pos.avg_price) / pos.avg_price
            pnl_ret = raw_ret * (1 if pos.qty > 0 else -1)
            reason = None
            if tp > 0 and pnl_ret >= tp:
                reason = "TAKE-PROFIT"
            elif sl > 0 and pnl_ret <= -sl:
                reason = "STOP-LOSS"
            if reason is not None:
                close_side = Side.SELL if pos.qty > 0 else Side.BUY
                qty = abs(pos.qty)
                log.info("%s %s hit (ret=%+.2f%%) — closing %.6f @ ~%.2f",
                         event.symbol, reason, pnl_ret * 100, qty, event.price)
                order = Order(symbol=event.symbol, side=close_side, qty=qty,
                              order_type=OrderType.MARKET)
                return [OrderEvent(order=order)]

        # inventory balancing: if no TP/SL exit fired, trim an over-weight long
        # back toward the target band. SELL-only (spot can't short to balance).
        return self._inventory_trim(event, pos)

    # --- inventory balancing (trim an over-weight position to target) ---
    def _inventory_trim(self, event: MarketEvent, pos) -> Optional[list[OrderEvent]]:
        if not bool(self._p("inventory_balance", False)):
            return None
        if pos.qty <= 0 or event.price <= 0:  # spot: only trim longs
            return None
        target = float(self._p("inventory_target_usdt", 0.0) or 0.0)
        if target <= 0:
            return None
        tol = float(self._p("inventory_tolerance_pct", 0.0) or 0.0)
        held_notional = pos.qty * event.price
        upper = target * (1.0 + tol)
        if held_notional <= upper:
            return None
        # trim the excess over TARGET (not just over the band) back to target
        excess_notional = held_notional - target
        qty = min(excess_notional / event.price, pos.qty)
        if qty <= 0:
            return None
        log.info("%s inventory %.2f > band %.2f — trimming %.6f (~%.2f USDT) to target %.2f",
                 event.symbol, held_notional, upper, qty, qty * event.price, target)
        order = Order(symbol=event.symbol, side=Side.SELL, qty=qty,
                      order_type=OrderType.MARKET)
        return [OrderEvent(order=order)]

    # --- TODO: historical VaR gate ---
    def check_tp_sl(self, symbol: str) -> Optional[list[OrderEvent]]:
        """Deprecated stub kept for compatibility — TP/SL now lives in
        ``on_market`` so it runs on every mark. Left as a no-op."""
        return None
