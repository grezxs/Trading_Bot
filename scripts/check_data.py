"""Step-1 data-feed check: confirm the live Binance TESTNET feed works.

No API key needed — both order-book and kline data are public. Run:

    python scripts/check_data.py

It polls a few order books AND OHLCV klines from testnet.binance.vision and
prints them. On success it prints 'OK - data feed is working.' Any network /
ccxt failure is reported so you can fix it before building further.
"""
from __future__ import annotations

import sys
from pathlib import Path

# allow running directly: add repo root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.config import load_config
from src.part1_data.connector import CcxtConnector


def main() -> int:
    cfg = load_config()
    symbols = cfg.symbols
    use_sandbox = cfg.data_source != "mainnet"
    venue = "testnet (paper data)" if use_sandbox else "MAINNET (real market data)"
    print(f"Data source: Binance {venue}, polling: {symbols}")
    print(f"Kline timeframe: {cfg.timeframe} (limit {cfg.kline_limit}) | "
          f"book depth: {cfg.book_depth}\n")
    try:
        connector = CcxtConnector(symbols, market_type="spot",
                                  timeframe=cfg.timeframe, kline_limit=cfg.kline_limit,
                                  book_depth=cfg.book_depth, sandbox=use_sandbox)
    except Exception as exc:
        print(f"FAILED to create connector: {exc}")
        return 1

    ok = 0
    try:
        for symbol in symbols:
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
                n = len(event.candles)
                last = event.candles[-1]
                print(f"  {symbol} KLINES: {n} bars; latest O={last.open:.2f} "
                      f"H={last.high:.2f} L={last.low:.2f} C={last.close:.2f} "
                      f"V={last.volume:.3f}")
                ok += 1
            else:
                print(f"  {symbol} KLINES: none received")
    finally:
        connector.close()

    if ok == 0:
        print("\nFAILED - no order-book data received. Check network / ccxt / testnet status.")
        return 1
    print("\nOK - data feed is working.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
