"""Event definitions — the FROZEN interface that every module depends on.

The whole bot is event-driven: modules never call each other directly. A
handler consumes one event and returns a list of new events; the Engine
routes them. Changing these classes ripples through every module, so treat
this file as an interface contract and edit it deliberately.

Event chain: MARKET -> (REGIME) -> SIGNAL -> ORDER -> FILL
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .models import Candle, OrderBook, Order, Regime, Side, SignalType


class EventType(str, Enum):
    MARKET = "MARKET"
    REGIME = "REGIME"
    SIGNAL = "SIGNAL"
    ORDER = "ORDER"
    FILL = "FILL"


# kw_only avoids the "non-default argument follows default argument" trap
# that bites dataclass inheritance when a base field has a default.
@dataclass(kw_only=True)
class Event:
    type: EventType
    ts: float = field(default_factory=time.time)


@dataclass(kw_only=True)
class MarketEvent(Event):
    """A new market snapshot for one symbol.

    Carries both data sources the strategies/risk layers need:
    - ``book``    — top-of-book order book (microstructure, regime, exec ref).
    - ``candles`` — recent OHLCV bars (the indicator price series). The most
      recent element may be the still-forming bar; ``price`` (mid) is the live
      mark. ``candles`` is optional so older callers / tests still work.
    """

    type: EventType = EventType.MARKET
    symbol: str
    price: float                 # mid price — the canonical mark
    book: Optional[OrderBook] = None
    candles: Optional[list[Candle]] = None


@dataclass(kw_only=True)
class RegimeEvent(Event):
    """Emitted by the detector after it updates a symbol's regime.

    Informational (dashboard / logging): the StrategyManager reads the
    detector's state directly thanks to MARKET handler ordering.
    """

    type: EventType = EventType.REGIME
    symbol: str
    regime: Regime
    confidence: float = 0.0


@dataclass(kw_only=True)
class SignalEvent(Event):
    """A trading intention with a conviction magnitude in [0, 1]."""

    type: EventType = EventType.SIGNAL
    symbol: str
    signal: SignalType
    magnitude: float = 0.0
    price: float = 0.0
    strategy: str = ""


@dataclass(kw_only=True)
class OrderEvent(Event):
    """A risk-approved order on its way to the OMS."""

    type: EventType = EventType.ORDER
    order: Order


@dataclass(kw_only=True)
class FillEvent(Event):
    """A (partial or full) fill reported by the OMS."""

    type: EventType = EventType.FILL
    symbol: str
    side: Side
    qty: float
    price: float
    order_id: int
    fee: float = 0.0
