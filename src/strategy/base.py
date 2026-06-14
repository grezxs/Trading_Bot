"""Strategy ABC — FROZEN interface (see brief).

A strategy keeps its own rolling price history (fed via ``observe``) and,
when asked to ``generate``, returns at most one SignalEvent for the symbol.
The StrategyManager owns *when* a strategy fires (cadence, regime gating);
strategies only decide *what* the signal is.

Do not change ``observe`` / ``generate`` signatures without coordinating —
the manager and both concrete strategies depend on them.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict, deque
from typing import Optional

from ..core.events import MarketEvent, SignalEvent
from ..core.models import SignalType


class Strategy(ABC):
    #: regimes this strategy is appropriate for (used by the manager)
    suitable_regimes: tuple = ()

    def __init__(self, name: str, history: int = 200) -> None:
        self.name = name
        self._history = history
        # tick-mode fallback history (used when no klines are supplied)
        self._prices: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=history))
        # kline-mode series (closes), replaced wholesale each observe()
        self._series: dict[str, list[float]] = {}

    def observe(self, event: MarketEvent) -> None:
        """Record a market snapshot into per-symbol history.

        Prefers the OHLCV **kline closes** as the indicator price series (the
        correct input for RSI/Bollinger/MACD/MA). The live mid (``price``) is
        appended as the most-recent point so signals react intra-bar. Falls
        back to a stream of mids if no candles are supplied (e.g. unit tests).
        """
        if event.candles:
            closes = [c.close for c in event.candles]
            # avoid double-counting if the last closed bar already ~= the mid
            if not closes or abs(closes[-1] - event.price) > 1e-12:
                closes.append(event.price)
            self._series[event.symbol] = closes
        else:
            self._prices[event.symbol].append(event.price)

    def prices(self, symbol: str) -> list[float]:
        if symbol in self._series:
            return self._series[symbol]
        return list(self._prices[symbol])

    @abstractmethod
    def generate(self, symbol: str, price: float) -> Optional[SignalEvent]:
        """Return a SignalEvent (BUY/SELL/HOLD) or None if not enough data."""
        ...

    # --- helper for subclasses ---
    def _signal(self, symbol: str, kind: SignalType, magnitude: float, price: float) -> SignalEvent:
        return SignalEvent(
            symbol=symbol,
            signal=kind,
            magnitude=max(0.0, min(1.0, magnitude)),
            price=price,
            strategy=self.name,
        )
