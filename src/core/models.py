"""Domain models and enums shared across the bot.

These are plain dataclasses / enums with no behaviour beyond simple
accounting helpers. They are imported almost everywhere, so keep them
dependency-free (stdlib only).
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for BUY, -1 for SELL — handy for signed inventory maths."""
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


class Regime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    HIGH_VOL = "HIGH_VOL"


class SignalType(str, Enum):
    HOLD = "HOLD"
    BUY = "BUY"
    SELL = "SELL"


_order_ids = itertools.count(1)


@dataclass
class Order:
    """A single order request / live order.

    ``magnitude`` carries the strategy conviction through to sizing; it is
    not part of the exchange order itself.
    """

    symbol: str
    side: Side
    qty: float
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    status: OrderStatus = OrderStatus.NEW
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    order_id: int = field(default_factory=lambda: next(_order_ids))
    exchange_id: Optional[str] = None  # id returned by the venue
    created_ts: float = field(default_factory=time.time)
    ttl: Optional[float] = None  # seconds; LIMIT lifecycle (TODO in OMS)

    @property
    def remaining(self) -> float:
        return max(self.qty - self.filled_qty, 0.0)

    @property
    def is_open(self) -> bool:
        return self.status in (OrderStatus.NEW, OrderStatus.PARTIAL)


@dataclass
class Position:
    """Net position in one symbol with average-cost accounting."""

    symbol: str
    qty: float = 0.0          # signed: +long / -short
    avg_price: float = 0.0    # average entry price of the open qty
    realized_pnl: float = 0.0
    last_price: float = 0.0   # most recent mark, for MTM

    @property
    def unrealized_pnl(self) -> float:
        if self.qty == 0.0:
            return 0.0
        return (self.last_price - self.avg_price) * self.qty

    def mark(self, price: float) -> None:
        self.last_price = price

    def apply_fill(self, side: Side, qty: float, price: float) -> float:
        """Apply a fill and return the realized PnL it generated.

        Uses signed average-cost: adding in the same direction averages the
        entry price; reducing/flipping realizes PnL against ``avg_price``.
        """
        signed = side.sign * qty
        realized = 0.0
        if self.qty == 0 or (self.qty > 0) == (signed > 0):
            # opening or increasing in the same direction
            new_qty = self.qty + signed
            if new_qty != 0:
                self.avg_price = (
                    self.avg_price * abs(self.qty) + price * qty
                ) / abs(new_qty)
            self.qty = new_qty
        else:
            # reducing or flipping
            closing = min(qty, abs(self.qty))
            realized = (price - self.avg_price) * closing * (1 if self.qty > 0 else -1)
            self.realized_pnl += realized
            new_qty = self.qty + signed
            if (self.qty > 0) != (new_qty > 0) and new_qty != 0:
                # flipped through zero: remainder opens a fresh position
                self.avg_price = price
            self.qty = new_qty
            if self.qty == 0:
                self.avg_price = 0.0
        self.last_price = price
        return realized


@dataclass
class Candle:
    """One OHLCV bar. ``ts`` is the bar's open time (epoch seconds)."""

    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class OrderBook:
    """Top-of-book snapshot. ``bids``/``asks`` are [price, size] levels."""

    symbol: str
    bids: list[tuple[float, float]] = field(default_factory=list)
    asks: list[tuple[float, float]] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def depth(self) -> int:
        """Number of levels available (min of the two sides)."""
        return min(len(self.bids), len(self.asks))

    @property
    def imbalance(self) -> float:
        """(bid_vol - ask_vol) / total over the top level. 0 if empty."""
        bv = self.bids[0][1] if self.bids else 0.0
        av = self.asks[0][1] if self.asks else 0.0
        tot = bv + av
        return (bv - av) / tot if tot else 0.0

    def depth_imbalance(self, levels: int = 10) -> float:
        """(bid_vol - ask_vol) / total summed over the top ``levels`` levels.

        A depth-aware version of ``imbalance`` — uses the full 10-level book
        rather than just top-of-book. Returns 0 on an empty book.
        """
        bv = sum(q for _, q in self.bids[:levels])
        av = sum(q for _, q in self.asks[:levels])
        tot = bv + av
        return (bv - av) / tot if tot else 0.0
