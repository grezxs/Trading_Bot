"""Mean-reversion strategy: RSI + Bollinger Bands.

Fires in RANGING and HIGH_VOL regimes. Buys when price is stretched below
the lower band and RSI is oversold; sells the mirror case. Conviction scales
with how far RSI is past the threshold.
"""
from __future__ import annotations

import math
from typing import Optional

from ..core.events import SignalEvent
from ..core.models import Regime, SignalType
from ..utils import indicators as ind
from .base import Strategy


class MeanReversionStrategy(Strategy):
    suitable_regimes = (Regime.RANGING, Regime.HIGH_VOL)

    def __init__(
        self,
        rsi_window: int = 14,
        bb_window: int = 20,
        rsi_low: float = 30.0,
        rsi_high: float = 70.0,
    ) -> None:
        super().__init__(name="mean_reversion")
        self.rsi_window = rsi_window
        self.bb_window = bb_window
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high

    def generate(self, symbol: str, price: float) -> Optional[SignalEvent]:
        prices = self.prices(symbol)
        rsi = ind.rsi(prices, self.rsi_window)
        lower, mid, upper = ind.bollinger(prices, self.bb_window)
        if math.isnan(rsi) or math.isnan(mid):
            return None

        if price <= lower and rsi <= self.rsi_low:
            mag = (self.rsi_low - rsi) / self.rsi_low
            return self._signal(symbol, SignalType.BUY, 0.4 + 0.6 * mag, price)
        if price >= upper and rsi >= self.rsi_high:
            mag = (rsi - self.rsi_high) / (100.0 - self.rsi_high)
            return self._signal(symbol, SignalType.SELL, 0.4 + 0.6 * mag, price)
        return self._signal(symbol, SignalType.HOLD, 0.0, price)
