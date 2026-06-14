"""Regime detection.

Current implementation is a lightweight z-score / volatility heuristic over
a rolling window (~30 bars). It classifies each symbol as TRENDING, RANGING
or HIGH_VOL and flags the high-volatility regime explicitly.

``on_market`` is a MARKET handler. It is registered AFTER the portfolio and
BEFORE the strategy manager, so by the time the strategy runs the detector's
per-symbol regime is already up to date. It also emits a RegimeEvent for the
dashboard / logging.

TODO (ShiYi): replace the heuristic with GaussianHMM (hmmlearn) + an entropy
feature. Keep ``current_regime`` / ``on_market`` signatures stable so the
StrategyManager does not need to change.
"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Optional

from ..core.events import MarketEvent, RegimeEvent
from ..core.models import Regime
from ..utils import indicators as ind

log = logging.getLogger("regime")


class RegimeDetector:
    def __init__(
        self,
        window: int = 30,
        trend_z: float = 1.0,
        highvol_mult: float = 2.0,
    ) -> None:
        self.window = window
        self.trend_z = trend_z          # |z| above this => trending
        self.highvol_mult = highvol_mult  # vol above mult*baseline => high vol
        self._prices: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=window))
        self._vol_baseline: dict[str, float] = {}
        self._regime: dict[str, Regime] = {}

    def current_regime(self, symbol: str) -> Regime:
        """Latest regime for a symbol; defaults to RANGING before warmup."""
        return self._regime.get(symbol, Regime.RANGING)

    def on_market(self, event: MarketEvent) -> Optional[list[RegimeEvent]]:
        prices = self._prices[event.symbol]
        prices.append(event.price)
        if len(prices) < self.window:
            return None

        series = list(prices)
        z = ind.zscore(series, self.window)
        vol = ind.realized_vol(series, self.window - 1)

        # slow EWMA baseline of vol so HIGH_VOL means "unusually volatile now"
        base = self._vol_baseline.get(event.symbol, vol)
        base = 0.9 * base + 0.1 * vol
        self._vol_baseline[event.symbol] = base

        if vol > self.highvol_mult * base:
            regime, conf = Regime.HIGH_VOL, min(vol / (base + 1e-12) / self.highvol_mult, 2.0)
        elif abs(z) >= self.trend_z:
            regime, conf = Regime.TRENDING, min(abs(z) / self.trend_z, 2.0)
        else:
            regime, conf = Regime.RANGING, 1.0 - min(abs(z) / self.trend_z, 1.0)

        prev = self._regime.get(event.symbol)
        self._regime[event.symbol] = regime
        if regime != prev:
            log.info("%s regime -> %s (z=%.2f vol=%.4f)", event.symbol, regime.value, z, vol)
        return [RegimeEvent(symbol=event.symbol, regime=regime, confidence=float(conf))]
