"""Portfolio: cash, positions, realised / unrealised PnL, mark-to-market.

Two handlers:
- ``on_market`` (FIRST MARKET handler) — marks every position to the latest
  price so unrealised PnL and equity are current before anything else runs.
- ``on_fill``  (FILL handler) — moves cash and updates the position with
  average-cost accounting, accumulating realised PnL.

Equity = cash + unrealised PnL of open positions, valued at last mark. This
includes mark-to-market of imbalanced inventory, per the brief.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Optional

from ..core.events import FillEvent, MarketEvent
from ..core.models import Position, Side

log = logging.getLogger("portfolio")


class Portfolio:
    def __init__(self, starting_cash: float = 10_000.0) -> None:
        self.starting_cash = starting_cash
        self.cash = starting_cash
        self.positions: dict[str, Position] = defaultdict(lambda: Position(""))
        self._last_price: dict[str, float] = {}
        self.fees_paid = 0.0

    def _pos(self, symbol: str) -> Position:
        pos = self.positions[symbol]
        if not pos.symbol:
            pos.symbol = symbol
        return pos

    def on_market(self, event: MarketEvent) -> None:
        self._last_price[event.symbol] = event.price
        if event.symbol in self.positions:
            self.positions[event.symbol].mark(event.price)
        return None  # observer: emits no events

    def on_fill(self, event: FillEvent) -> None:
        pos = self._pos(event.symbol)
        notional = event.price * event.qty
        # cash out on buys, in on sells; fees always reduce cash
        self.cash -= event.side.sign * notional
        self.cash -= event.fee
        self.fees_paid += event.fee
        realized = pos.apply_fill(event.side, event.qty, event.price)
        log.info(
            "FILL %s %s %.6f @ %.2f | cash=%.2f pos=%.6f realized=%.2f",
            event.symbol, event.side.value, event.qty, event.price,
            self.cash, pos.qty, realized,
        )
        return None

    # --- reporting ---
    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.positions.values())

    @property
    def realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self.positions.values())

    @property
    def position_value(self) -> float:
        return sum(p.qty * self._last_price.get(s, p.last_price)
                   for s, p in self.positions.items())

    @property
    def equity(self) -> float:
        """Cash + marked value of holdings."""
        return self.cash + self.position_value

    @property
    def total_pnl(self) -> float:
        return self.equity - self.starting_cash

    def snapshot(self) -> dict:
        return {
            "cash": round(self.cash, 2),
            "equity": round(self.equity, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "total_pnl": round(self.total_pnl, 2),
            "fees_paid": round(self.fees_paid, 4),
            "positions": {
                s: {"qty": round(p.qty, 6), "avg": round(p.avg_price, 2),
                    "last": round(p.last_price, 2)}
                for s, p in self.positions.items() if p.qty != 0
            },
        }
