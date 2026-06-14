"""Market-data connectors.

A connector turns a venue (or a synthetic source) into a stream of
``MarketEvent``s. The Engine drives the cadence; each connector exposes
``poll(symbol) -> Optional[MarketEvent]`` returning the latest snapshot.

Each snapshot carries BOTH data sources the bot uses:
- the top-of-book **order book** (microstructure / regime / execution ref), and
- a rolling window of **OHLCV klines** (the indicator price series).

- ``MockConnector``  — offline random walk; synthesises both, pre-seeded with
  kline history so strategies are warm from the first tick. No network, no keys.
- ``CcxtConnector``  — real Binance *testnet* via ccxt REST polling of
  ``fetch_order_book`` + ``fetch_ohlcv``. Public data, no key needed for the
  feed. Sandbox mode is forced on.
"""
from __future__ import annotations

import logging
import math
import random
import sys
import time
from collections import deque
from pathlib import Path
from typing import Optional

# allow running directly as a script: python src/part1_data/connector.py
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.events import MarketEvent
from src.core.models import Candle, OrderBook

log = logging.getLogger("data")

# map ccxt-style timeframe strings to seconds (for the mock clock)
_TF_SECONDS = {
    "1s": 1, "5s": 5, "10s": 10, "15s": 15, "30s": 30,
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "1h": 3600,
}


def timeframe_seconds(tf: str) -> int:
    return _TF_SECONDS.get(tf, 60)


class BaseConnector:
    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols

    def poll(self, symbol: str) -> Optional[MarketEvent]:
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - nothing to clean up by default
        pass


class MockConnector(BaseConnector):
    """Synthetic price source: independent GBM-ish walk per symbol."""

    def __init__(
        self,
        symbols: list[str],
        start_prices: Optional[dict[str, float]] = None,
        vol: float = 0.002,
        spread_bps: float = 2.0,
        seed: Optional[int] = None,
        interval: float = 10.0,
        timeframe: str = "1m",
        kline_limit: int = 100,
        book_depth: int = 10,
    ) -> None:
        super().__init__(symbols)
        defaults = {"BTC/USDT": 60_000.0, "ETH/USDT": 3_000.0}
        self._px = {s: (start_prices or {}).get(s, defaults.get(s, 100.0)) for s in symbols}
        self._vol = vol
        self._spread = spread_bps / 10_000.0
        self._book_depth = book_depth
        self._rng = random.Random(seed)
        # simulated clock: each tick advances ts by `interval` so time-based
        # logic (e.g. the signal cadence throttle) behaves as in real time even
        # when mock runs at full speed.
        self._interval = interval
        self._t0 = time.time()
        self._tick: dict[str, int] = {s: 0 for s in symbols}

        # --- kline synthesis ---
        self._timeframe = timeframe
        self._tf_sec = timeframe_seconds(timeframe)
        self._kline_limit = kline_limit
        self._ticks_per_candle = max(1, int(round(self._tf_sec / self._interval)))
        self._candles: dict[str, deque[Candle]] = {}
        self._forming: dict[str, dict] = {}
        for s in symbols:
            self._seed_candles(s)

    def _seed_candles(self, symbol: str) -> None:
        """Pre-generate `kline_limit` historical bars so indicators are warm."""
        dq: deque[Candle] = deque(maxlen=self._kline_limit)
        p = self._px[symbol]
        candle_vol = self._vol * math.sqrt(self._ticks_per_candle)
        for i in range(self._kline_limit):
            o = p
            p *= math.exp(self._rng.gauss(0.0, candle_vol))
            c = p
            hi = max(o, c) * (1 + abs(self._rng.gauss(0.0, self._vol)))
            lo = min(o, c) * (1 - abs(self._rng.gauss(0.0, self._vol)))
            ts = self._t0 - (self._kline_limit - i) * self._tf_sec
            dq.append(Candle(ts=ts, open=o, high=hi, low=lo, close=c,
                             volume=self._rng.uniform(1.0, 10.0)))
        self._px[symbol] = p
        self._candles[symbol] = dq
        self._forming[symbol] = {"open": p, "high": p, "low": p, "close": p, "volume": 0.0}

    def poll(self, symbol: str) -> Optional[MarketEvent]:
        last = self._px[symbol]
        # occasional volatility burst so the regime detector has something to do
        shock = self._vol * (5 if self._rng.random() < 0.05 else 1)
        last *= math.exp(self._rng.gauss(0.0, shock))
        self._px[symbol] = last
        # synthesize a `book_depth`-level book: levels step away from the mid by
        # ~half-spread, with random sizes that grow a little deeper in the book.
        half = last * self._spread / 2.0
        tick = max(half, last * 1e-5)
        bids: list[tuple[float, float]] = []
        asks: list[tuple[float, float]] = []
        for i in range(self._book_depth):
            level_scale = 1.0 + 0.15 * i  # deeper levels tend to be larger
            bids.append((last - half - i * tick, self._rng.uniform(0.5, 2.0) * level_scale))
            asks.append((last + half + i * tick, self._rng.uniform(0.5, 2.0) * level_scale))
        book = OrderBook(symbol=symbol, bids=bids, asks=asks)

        tick = self._tick[symbol]
        ts = self._t0 + tick * self._interval

        # roll the forming candle; close it on a timeframe boundary
        tick_volume = bids[0][1] + asks[0][1]
        f = self._forming[symbol]
        f["high"] = max(f["high"], last)
        f["low"] = min(f["low"], last)
        f["close"] = last
        f["volume"] += tick_volume
        if tick > 0 and tick % self._ticks_per_candle == 0:
            candle_ts = self._t0 + (tick - self._ticks_per_candle) * self._interval
            self._candles[symbol].append(Candle(
                ts=candle_ts, open=f["open"], high=f["high"],
                low=f["low"], close=f["close"], volume=f["volume"]))
            self._forming[symbol] = {"open": last, "high": last, "low": last,
                                     "close": last, "volume": 0.0}

        self._tick[symbol] += 1
        return MarketEvent(symbol=symbol, price=book.mid or last, book=book,
                           candles=list(self._candles[symbol]), ts=ts)


class CcxtConnector(BaseConnector):
    """Binance market-data polling via ccxt.

    Each poll fetches both the order book (``fetch_order_book``) and recent
    OHLCV klines (``fetch_ohlcv``). Klines are cached per symbol and refreshed
    at most once per timeframe to stay well under rate limits, since the book
    is polled far more often than a new candle forms.

    ``sandbox`` selects the data source:
      True  -> Binance testnet paper data.
      False -> Binance *mainnet* real market data (read-only; the client is
               built with NO API credentials, so it cannot place orders —
               trading stays on the separate testnet broker).
    Both order book and klines are public, so no key is needed either way.
    """

    def __init__(
        self,
        symbols: list[str],
        market_type: str = "spot",
        timeframe: str = "1m",
        kline_limit: int = 100,
        book_depth: int = 10,
        sandbox: bool = True,
    ) -> None:
        super().__init__(symbols)
        import ccxt  # imported lazily so mock mode needs no ccxt

        options = {"defaultType": market_type}  # "future" enables shorting
        # no apiKey/secret here — data is public and this client must never trade
        self._ex = ccxt.binance({"enableRateLimit": True, "options": options})
        self._ex.set_sandbox_mode(sandbox)  # False => real mainnet data feed
        self.sandbox = sandbox
        self._book_depth = book_depth
        self._timeframe = timeframe
        self._tf_sec = timeframe_seconds(timeframe)
        self._kline_limit = kline_limit
        self._candle_cache: dict[str, list[Candle]] = {}
        self._candle_ts: dict[str, float] = {}

    def _fetch_klines(self, symbol: str) -> Optional[list[Candle]]:
        now = time.time()
        # refresh at most once per timeframe (book polls are more frequent)
        if symbol in self._candle_cache and now - self._candle_ts.get(symbol, 0) < self._tf_sec:
            return self._candle_cache[symbol]
        try:
            raw = self._ex.fetch_ohlcv(symbol, timeframe=self._timeframe, limit=self._kline_limit)
        except Exception as exc:
            log.warning("fetch_ohlcv(%s) failed: %s", symbol, exc)
            return self._candle_cache.get(symbol)  # fall back to last good window
        candles = [
            Candle(ts=row[0] / 1000.0, open=float(row[1]), high=float(row[2]),
                   low=float(row[3]), close=float(row[4]), volume=float(row[5]))
            for row in raw
        ]
        self._candle_cache[symbol] = candles
        self._candle_ts[symbol] = now
        return candles

    def poll(self, symbol: str) -> Optional[MarketEvent]:
        try:
            ob = self._ex.fetch_order_book(symbol, limit=self._book_depth)
        except Exception as exc:  # log and skip — never crash the loop
            log.warning("fetch_order_book(%s) failed: %s", symbol, exc)
            return None
        bids = [(float(p), float(q)) for p, q in ob.get("bids", [])][:self._book_depth]
        asks = [(float(p), float(q)) for p, q in ob.get("asks", [])][:self._book_depth]
        if not bids or not asks:
            log.warning("empty book for %s", symbol)
            return None
        book = OrderBook(symbol=symbol, bids=bids, asks=asks)
        candles = self._fetch_klines(symbol)
        return MarketEvent(symbol=symbol, price=book.mid or bids[0][0],
                           book=book, candles=candles)

    def close(self) -> None:
        try:
            self._ex.close()
        except Exception:
            pass


def _demo() -> int:
    """Run the connector standalone and print the crawled data.

        python src/part1_data/connector.py

    Polls each configured symbol once via CcxtConnector and prints the full
    order book (all levels) + the latest kline. Data source follows
    ``config/config.yaml`` (``data.source``). No API key needed — public data.
    """
    from src.core.config import load_config

    cfg = load_config()
    use_sandbox = cfg.data_source != "mainnet"
    venue = "testnet (paper data)" if use_sandbox else "MAINNET (real market data)"
    print(f"Data source: Binance {venue} | symbols: {cfg.symbols}")
    print(f"Kline: {cfg.timeframe} x{cfg.kline_limit} | book depth: {cfg.book_depth}\n")

    connector = CcxtConnector(cfg.symbols, market_type="spot",
                              timeframe=cfg.timeframe, kline_limit=cfg.kline_limit,
                              book_depth=cfg.book_depth, sandbox=use_sandbox)
    ok = 0
    try:
        for symbol in cfg.symbols:
            event = connector.poll(symbol)
            if event is None or event.book is None:
                print(f"  {symbol}: no order-book data")
                continue
            book = event.book
            print(f"  {symbol} ORDER BOOK: {book.depth} levels/side | mid {event.price:.2f} "
                  f"| L1 imb {book.imbalance:+.3f} | {cfg.book_depth}-lvl imb "
                  f"{book.depth_imbalance(cfg.book_depth):+.3f}")
            for i in range(book.depth):
                bp, bq = book.bids[i]
                ap, aq = book.asks[i]
                print(f"      L{i + 1:>2}  bid {bp:>12.2f} x {bq:<10.4f}   "
                      f"ask {ap:>12.2f} x {aq:<10.4f}")
            if event.candles:
                last = event.candles[-1]
                print(f"  {symbol} KLINES: {len(event.candles)} bars; latest "
                      f"O={last.open:.2f} H={last.high:.2f} L={last.low:.2f} "
                      f"C={last.close:.2f} V={last.volume:.3f}\n")
                ok += 1
            else:
                print(f"  {symbol} KLINES: none received\n")
    finally:
        connector.close()

    if ok == 0:
        print("FAILED - no data received. Check network / ccxt / testnet status.")
        return 1
    print("OK - data feed is working.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_demo())
