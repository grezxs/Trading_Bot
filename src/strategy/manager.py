"""StrategyManager: route MARKET events to the regime-appropriate strategy.

Responsibilities:
- Feed every strategy the price history (so a strategy is warm the moment its
  regime becomes active).
- Pick the strategy whose ``suitable_regimes`` matches the detector's current
  regime for the symbol.
- Throttle signal cadence: at most one signal per ``signal_interval`` seconds
  per symbol, even though MARKET events arrive much more often.

Registered as the LAST MARKET handler (after portfolio + detector), so the
regime it reads is already current for this tick.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from ..core.events import MarketEvent, SignalEvent
from ..core.models import Regime, SignalType
from ..part6_regime.detector import RegimeDetector
from .base import Strategy

log = logging.getLogger("strategy")


class StrategyManager:
    def __init__(
        self,
        detector: RegimeDetector,
        strategies: list[Strategy],
        signal_interval: float = 60.0,
    ) -> None:
        self.detector = detector
        self.strategies = strategies
        self.signal_interval = signal_interval
        self._last_signal_ts: dict[str, float] = {}

    def _select(self, regime: Regime) -> Optional[Strategy]:
        for strat in self.strategies:
            if regime in strat.suitable_regimes:
                return strat
        return None

    def on_market(self, event: MarketEvent) -> Optional[list[SignalEvent]]:
        # keep every strategy warm
        for strat in self.strategies:
            strat.observe(event)

        # cadence throttle (signals ~once/min even though data is ~10s)
        now = event.ts
        last = self._last_signal_ts.get(event.symbol, 0.0)
        if now - last < self.signal_interval:
            return None

        regime = self.detector.current_regime(event.symbol)
        strat = self._select(regime)
        if strat is None:
            return None

        signal = strat.generate(event.symbol, event.price)
        if signal is None or signal.signal is SignalType.HOLD:
            return None

        self._last_signal_ts[event.symbol] = now
        log.info(
            "%s %s signal %s mag=%.2f @ %.2f (%s)",
            event.symbol, strat.name, signal.signal.value,
            signal.magnitude, event.price, regime.value,
        )
        return [signal]
