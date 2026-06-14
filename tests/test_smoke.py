"""Smoke tests: the pieces import, accounting is sane, mock pipeline runs."""
from __future__ import annotations

import asyncio

from src.core.config import load_config
from src.core.models import Position, Side
from src.utils import indicators as ind

import main as bot


def test_position_accounting():
    pos = Position("BTC/USDT")
    pos.apply_fill(Side.BUY, 1.0, 100.0)
    pos.mark(110.0)
    assert pos.qty == 1.0
    assert abs(pos.unrealized_pnl - 10.0) < 1e-9
    realized = pos.apply_fill(Side.SELL, 1.0, 110.0)
    assert abs(realized - 10.0) < 1e-9
    assert pos.qty == 0.0
    assert abs(pos.realized_pnl - 10.0) < 1e-9


def test_indicators_warmup_returns_nan():
    import math
    assert math.isnan(ind.rsi([1, 2, 3], 14))
    assert math.isnan(ind.moving_average([1, 2], 20))


def test_mock_pipeline_runs_end_to_end():
    cfg = load_config()
    cfg.raw.setdefault("mock", {})
    cfg.raw["mock"]["ticks"] = 120        # keep the test fast
    cfg.raw["mock"]["tick_sleep"] = 0.0
    portfolio = asyncio.run(bot.run("mock", cfg))
    snap = portfolio.snapshot()
    # equity is finite and accounting keys exist
    assert "equity" in snap and "total_pnl" in snap
    assert isinstance(snap["equity"], float)
    # cash never went absurdly negative (funding check held)
    assert portfolio.cash > -1.0
    # the MARKET path actually ran (catches a crashed/dead data producer)
    assert portfolio._last_price, "no market data processed — producer may have crashed"
    assert set(portfolio._last_price) == set(cfg.symbols)
