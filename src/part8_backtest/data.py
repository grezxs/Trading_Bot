"""Historical kline loader for the backtester.

Three sources, all returning a plain ``list[Candle]`` (oldest first):

- ``mainnet``  — real Binance market data via ccxt (read-only, NO keys). The
  client is built without credentials and can only read public OHLCV.
- ``testnet``  — Binance testnet paper data (ccxt sandbox).
- ``mock``     — synthetic GBM series generated offline. No network, no ccxt;
  keeps the backtester usable with zero setup (mirrors the bot's mock mode).

``fetch_ohlcv`` is paginated so we can pull many days of bars past the
per-request limit.
"""
from __future__ import annotations

import logging
import math
import random
import time
from typing import Optional

from ..core.models import Candle
from ..part1_data.connector import timeframe_seconds

log = logging.getLogger("backtest.data")


def load_history(
    symbol: str,
    *,
    days: float = 90.0,
    timeframe: str = "1m",
    source: str = "mainnet",
    market_type: str = "spot",
    mock_seed: Optional[int] = 42,
    mock_vol: float = 0.002,
    mock_start: Optional[float] = None,
) -> list[Candle]:
    """Return ``list[Candle]`` (oldest first) covering ~``days`` of history."""
    tf_sec = timeframe_seconds(timeframe)
    n_bars = max(2, int(round(days * 86400 / tf_sec)))
    src = (source or "mainnet").lower()
    if src == "mock":
        return _mock_history(symbol, n_bars, tf_sec, mock_seed, mock_vol, mock_start)
    return _ccxt_history(symbol, n_bars, timeframe, tf_sec, src, market_type)


def _mock_history(symbol: str, n_bars: int, tf_sec: int,
                  seed: Optional[int], vol: float, start: Optional[float]) -> list[Candle]:
    rng = random.Random(seed)
    defaults = {"BTC/USDT": 60_000.0, "ETH/USDT": 3_000.0}
    p = start if start is not None else defaults.get(symbol, 100.0)
    t0 = time.time() - n_bars * tf_sec
    out: list[Candle] = []
    for i in range(n_bars):
        o = p
        p *= math.exp(rng.gauss(0.0, vol) + (vol * vol * 4 if rng.random() < 0.05 else 0.0))
        c = p
        hi = max(o, c) * (1 + abs(rng.gauss(0.0, vol)))
        lo = min(o, c) * (1 - abs(rng.gauss(0.0, vol)))
        out.append(Candle(ts=t0 + i * tf_sec, open=o, high=hi, low=lo, close=c,
                          volume=rng.uniform(1.0, 10.0)))
    log.info("mock history %s: %d synthetic %ds bars", symbol, len(out), tf_sec)
    return out


def _ccxt_history(symbol: str, n_bars: int, timeframe: str, tf_sec: int,
                  source: str, market_type: str) -> list[Candle]:
    import ccxt  # lazy: mock source needs no ccxt

    ex = ccxt.binance({"enableRateLimit": True, "options": {"defaultType": market_type}})
    ex.set_sandbox_mode(source == "testnet")  # False => real mainnet data
    venue = "testnet (paper data)" if source == "testnet" else "MAINNET (real data)"
    limit = 1000  # Binance per-request cap
    since = ex.milliseconds() - n_bars * tf_sec * 1000
    rows: list[list] = []
    try:
        while True:
            batch = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
            if not batch:
                break
            rows += batch
            since = batch[-1][0] + tf_sec * 1000
            if len(batch) < limit or since >= ex.milliseconds():
                break
    finally:
        try:
            ex.close()
        except Exception:
            pass
    # de-dup by timestamp, keep last n_bars
    seen: dict[int, list] = {}
    for r in rows:
        seen[int(r[0])] = r
    ordered = [seen[k] for k in sorted(seen)][-n_bars:]
    candles = [Candle(ts=r[0] / 1000.0, open=float(r[1]), high=float(r[2]),
                      low=float(r[3]), close=float(r[4]), volume=float(r[5]))
               for r in ordered]
    log.info("ccxt history %s [%s]: %d %s bars", symbol, venue, len(candles), timeframe)
    return candles
