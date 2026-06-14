"""Backtest engine — replay historical bars through the live trading stack.

The bot is event-driven, so a backtest is the live MARKET chain driven by a
finite, historical producer instead of a real-time connector, with fills booked
by an instant ``MockBroker``. We reuse the REAL modules unchanged:

    Portfolio · RegimeDetector · StrategyManager(+strategies) · RiskManager

so a backtest exercises exactly the code that trades live. Per bar we mirror
``main.py``'s MARKET handler order:

    portfolio.on_market  (mark)
 -> risk.on_market        (TP/SL + inventory exits)
 -> detector.on_market    (update regime)
 -> manager.on_market     (emit signals)
 -> risk.on_signal        (size -> ORDER)  -> MockBroker fill -> portfolio.on_fill

Orders fill at the bar close (MARKET) or the limit price (LIMIT) — a standard
bar-level approximation; intrabar TTL/partials are out of scope here.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..core.config import load_config
from ..core.engine import Engine
from ..core.events import FillEvent, MarketEvent, OrderEvent
from ..core.models import Candle, Order
from ..part3_execution.broker import MockBroker
from ..part5_risk.risk_manager import RiskManager
from ..part6_regime.detector import RegimeDetector
from ..portfolio.portfolio import Portfolio
from ..strategy.manager import StrategyManager
from ..strategy.mean_reversion import MeanReversionStrategy
from ..strategy.momentum import MomentumStrategy
from .metrics import compute_metrics

log = logging.getLogger("backtest")


@dataclass
class Trade:
    ts: float
    side: str
    qty: float
    price: float
    fee: float
    realized: float
    reason: str = ""  # ENTRY / EXIT / TP-SL / INVENTORY (best-effort label)


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    starting_cash: float
    candles: list[Candle]
    equity_curve: list[tuple[float, float]] = field(default_factory=list)
    regime_series: list[tuple[float, Optional[str]]] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    halted: bool = False

    @property
    def final_equity(self) -> float:
        return self.equity_curve[-1][1] if self.equity_curve else self.starting_cash


def build_modules(cfg, starting_cash: float, order_policy: Optional[dict] = None):
    """Construct the real trading modules wired for a backtest (no dashboard,
    no live broker). ``order_policy`` defaults to ``config.yaml``'s ``order:``."""
    portfolio = Portfolio(starting_cash=starting_cash)
    engine = Engine()  # only used for the drawdown halt flag
    detector = RegimeDetector(
        window=cfg.regime_window,
        trend_z=float(cfg.get("regime", "trend_z", default=1.0)),
        highvol_mult=float(cfg.get("regime", "highvol_mult", default=2.0)),
    )
    strategies = [MomentumStrategy(), MeanReversionStrategy()]
    manager = StrategyManager(detector, strategies, signal_interval=cfg.signal_interval)
    policy = dict(order_policy if order_policy is not None else cfg.order)
    policy["trading_enabled"] = True  # a backtest always trades (no kill switch)
    risk_cfg = cfg.risk
    risk = RiskManager(
        portfolio, engine,
        max_position_value=float(risk_cfg.get("max_position_value", 200)),
        max_position_qty=float(risk_cfg.get("max_position_qty", 1.0)),
        max_drawdown=float(risk_cfg.get("max_drawdown", 0.2)),
        var_window=int(risk_cfg.get("var_window", 60)),
        order_policy=policy,
        control=None,
    )
    return portfolio, engine, detector, manager, risk


def run_backtest(
    symbol: str,
    candles: list[Candle],
    *,
    cfg=None,
    starting_cash: Optional[float] = None,
    timeframe: str = "1m",
    taker_fee: Optional[float] = None,
    order_policy: Optional[dict] = None,
) -> BacktestResult:
    """Replay ``candles`` for ``symbol`` and return a populated result."""
    if cfg is None:
        cfg = load_config()
    if starting_cash is None:
        starting_cash = float(cfg.starting_cash)
    if taker_fee is None:
        taker_fee = float(cfg.get("execution", "taker_fee", default=0.001))

    portfolio, engine, detector, manager, risk = build_modules(cfg, starting_cash, order_policy)
    broker = MockBroker(starting_cash, taker_fee)
    result = BacktestResult(symbol=symbol, timeframe=timeframe,
                            starting_cash=starting_cash, candles=candles)
    # match LIVE behaviour: the connector only ever hands strategies the last
    # `kline_limit` bars, so feed the same bounded trailing window here. This is
    # both faithful (strategies see what they'd see live) and keeps the replay
    # O(n * window) instead of O(n^2).
    window = max(int(cfg.kline_limit), cfg.regime_window + 2)

    def _execute(order: Order, ref: float, ts: float, reason: str) -> None:
        fill = broker.place(order, ref)
        if not fill or fill.get("qty", 0) <= 0:
            return
        pos = portfolio.positions.get(order.symbol)
        realized_before = pos.realized_pnl if pos else 0.0
        portfolio.on_fill(FillEvent(
            symbol=order.symbol, side=order.side, qty=float(fill["qty"]),
            price=float(fill["price"]), order_id=order.order_id, fee=float(fill["fee"]),
        ))
        realized_after = portfolio.positions[order.symbol].realized_pnl
        result.trades.append(Trade(
            ts=ts, side=order.side.value, qty=float(fill["qty"]),
            price=float(fill["price"]), fee=float(fill["fee"]),
            realized=realized_after - realized_before, reason=reason,
        ))

    for i, c in enumerate(candles):
        lo = max(0, i + 1 - window)
        me = MarketEvent(symbol=symbol, price=c.close, candles=candles[lo: i + 1], ts=c.ts)
        portfolio.on_market(me)
        # protective exits (TP/SL) + inventory trim — run before new entries
        for ev in (risk.on_market(me) or []):
            if isinstance(ev, OrderEvent):
                _execute(ev.order, c.close, c.ts, "EXIT")
        detector.on_market(me)
        # new entries from regime-appropriate strategy
        for sig in (manager.on_market(me) or []):
            for oe in (risk.on_signal(sig) or []):
                if isinstance(oe, OrderEvent):
                    _execute(oe.order, c.close, c.ts, "ENTRY")
        reg = detector.current_regime(symbol)
        result.regime_series.append((c.ts, reg.value if reg is not None else None))
        result.equity_curve.append((c.ts, portfolio.equity))
        if engine.halted:
            result.halted = True

    result.metrics = compute_metrics(result)
    return result
