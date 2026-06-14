"""Momentum strategy: MACD + moving-average crossover.

Fires in the TRENDING regime. Goes long when the fast MA is above the slow
MA and MACD histogram is positive; short the mirror case. Requiring both to
agree filters whipsaws. Conviction scales with histogram magnitude relative
to price.
"""
from __future__ import annotations

import math
from typing import Optional

from ..core.events import SignalEvent
from ..core.models import Regime, SignalType
from ..utils import indicators as ind
from .base import Strategy


class MomentumStrategy(Strategy):
    suitable_regimes = (Regime.TRENDING,)

    def __init__(self, fast_ma: int = 10, slow_ma: int = 30) -> None:
        super().__init__(name="momentum")
        self.fast_ma = fast_ma
        self.slow_ma = slow_ma

    def generate(self, symbol: str, price: float) -> Optional[SignalEvent]:
        prices = self.prices(symbol)
        fast = ind.moving_average(prices, self.fast_ma)
        slow = ind.moving_average(prices, self.slow_ma)
        macd_line, signal_line, hist = ind.macd(prices)
        if math.isnan(fast) or math.isnan(slow) or math.isnan(hist):
            return None

        mag = min(abs(hist) / (price * 1e-3 + 1e-12), 1.0)  # ~0.1% move => full conviction
        if fast > slow and hist > 0:
            return self._signal(symbol, SignalType.BUY, 0.4 + 0.6 * mag, price)
        if fast < slow and hist < 0:
            return self._signal(symbol, SignalType.SELL, 0.4 + 0.6 * mag, price)
        return self._signal(symbol, SignalType.HOLD, 0.0, price)
