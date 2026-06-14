"""SignalRecorder — observe-only live validation: real data in, buy/sell out.

Two things live here:

1. ``SignalRecorder`` — a terminal SIGNAL sink. It records every BUY/SELL point
   the strategies generate and returns **nothing**: no OrderEvent is ever
   produced, so by construction observe mode cannot place an order.

2. A standalone live runner (``run_live`` + ``__main__``) so the team can just::

       python src/part2_observe/recorder.py            # ~30 min, real data
       OBSERVE_MINUTES=5 python src/part2_observe/recorder.py
       OBSERVE_MINUTES=0 python src/part2_observe/recorder.py   # until Ctrl-C

   It streams real market data through the SAME MARKET chain as live trading
   (portfolio -> detector -> manager), prints each live tick (price / regime /
   book imbalance), and prints a POINT line whenever a buy/sell signal fires.
   There is NO broker, NO risk, NO OMS — nothing can trade.

Data source follows ``config/config.yaml`` (``data.source: mainnet`` = real
read-only feed; ``testnet`` = paper data). Points are appended to
``logs/observe_signals.jsonl`` for later review.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# allow running directly as a script: python src/part2_observe/recorder.py
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.events import SignalEvent
from src.core.models import SignalType

log = logging.getLogger("observe")


@dataclass
class SignalPoint:
    """One buy/sell decision captured from the live stream."""

    ts: float
    iso: str
    symbol: str
    side: str          # "BUY" or "SELL"
    price: float
    magnitude: float
    strategy: str


class SignalRecorder:
    """Collects buy/sell points from SIGNAL events (a no-op terminal handler).

    ``on_signal`` returns ``None`` on purpose: it is the end of the event chain
    in observe mode. Nothing downstream runs, nothing trades.
    """

    def __init__(self, out_path: Optional[str] = None) -> None:
        self.points: list[SignalPoint] = []
        self.counts: dict[str, dict[str, int]] = {}  # symbol -> {BUY: n, SELL: n}
        self._out_path = out_path
        self._fh = None
        if out_path:
            try:
                self._fh = open(out_path, "a", encoding="utf-8")
            except OSError as exc:
                log.warning("cannot open %s (%s) — points go to stdout only",
                            out_path, exc)

    def on_signal(self, event: SignalEvent) -> None:
        # HOLD never reaches here (the manager drops it), but guard anyway.
        if event.signal is SignalType.HOLD:
            return None
        side = event.signal.value  # "BUY" / "SELL"
        point = SignalPoint(
            ts=event.ts,
            iso=datetime.fromtimestamp(event.ts, tz=timezone.utc).isoformat(),
            symbol=event.symbol,
            side=side,
            price=event.price,
            magnitude=event.magnitude,
            strategy=event.strategy,
        )
        self.points.append(point)
        bucket = self.counts.setdefault(event.symbol, {"BUY": 0, "SELL": 0})
        bucket[side] = bucket.get(side, 0) + 1

        log.info(
            "POINT  %-4s %-9s @ %.2f  mag=%.2f  via %s",
            side, point.symbol, point.price, point.magnitude, point.strategy,
        )
        if self._fh is not None:
            self._fh.write(json.dumps(asdict(point)) + "\n")
            self._fh.flush()
        return None

    def summary(self) -> dict[str, dict[str, int]]:
        return self.counts

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def run_live(minutes: float = 30.0) -> SignalRecorder:
    """Stream real data -> regime + strategy, recording buy/sell points.

    No broker / risk / OMS is constructed, so nothing can place an order.
    Runs ``minutes`` real minutes (``minutes<=0`` => until Ctrl-C).
    """
    # local imports so importing SignalRecorder stays cheap (no ccxt pull-in)
    from src.core.config import load_config
    from src.core.events import MarketEvent
    from src.part1_data.connector import CcxtConnector
    from src.portfolio.portfolio import Portfolio
    from src.part6_regime.detector import RegimeDetector
    from src.strategy.manager import StrategyManager
    from src.strategy.mean_reversion import MeanReversionStrategy
    from src.strategy.momentum import MomentumStrategy

    cfg = load_config()
    data_sandbox = cfg.data_source != "mainnet"
    venue = "MAINNET (real, read-only)" if not data_sandbox else "testnet (paper)"

    connector = CcxtConnector(cfg.symbols, market_type="spot",
                              timeframe=cfg.timeframe, kline_limit=cfg.kline_limit,
                              book_depth=cfg.book_depth, sandbox=data_sandbox)
    portfolio = Portfolio(starting_cash=cfg.starting_cash)
    detector = RegimeDetector(
        window=cfg.regime_window,
        trend_z=float(cfg.get("regime", "trend_z", default=1.0)),
        highvol_mult=float(cfg.get("regime", "highvol_mult", default=2.0)),
    )
    strategies = [MomentumStrategy(), MeanReversionStrategy()]
    manager = StrategyManager(detector, strategies, signal_interval=cfg.signal_interval)

    # anchor the log dir to the repo root (not the CWD, which may be read-only)
    repo_root = Path(__file__).resolve().parents[2]
    out_path: Optional[str] = str(repo_root / "logs" / "observe_signals.jsonl")
    try:
        os.makedirs(repo_root / "logs", exist_ok=True)
    except OSError as exc:
        log.warning("cannot create logs dir (%s) — points go to stdout only", exc)
        out_path = None
    recorder = SignalRecorder(out_path=out_path)

    interval = cfg.market_interval
    deadline = time.time() + minutes * 60.0 if minutes > 0 else None
    horizon = f"~{minutes:g} min" if deadline else "until Ctrl-C"
    log.info("LIVE observe | data=%s | poll every %.0fs | %s | EXECUTION DISABLED",
             venue, interval, horizon)

    # Warm the detector with each symbol's historical klines so the regime is
    # meaningful from the very first live tick (it needs `window` prices first).
    # Silence the detector's per-change log during this backfill — those are
    # historical transitions, not live ones.
    reg_log = logging.getLogger("regime")
    prev_level = reg_log.level
    reg_log.setLevel(logging.WARNING)
    for symbol in cfg.symbols:
        ev = connector.poll(symbol)
        if ev is None or not ev.candles:
            continue
        for c in ev.candles:
            detector.on_market(MarketEvent(symbol=symbol, price=c.close, ts=c.ts))
        for s in strategies:
            s.observe(ev)
    reg_log.setLevel(prev_level)
    log.info("warmup done — streaming live now")

    try:
        while deadline is None or time.time() < deadline:
            for symbol in cfg.symbols:
                event = connector.poll(symbol)
                if event is None:
                    continue
                # same MARKET handler order as live trading, minus execution
                portfolio.on_market(event)
                detector.on_market(event)
                signals = manager.on_market(event) or []
                regime = detector.current_regime(symbol)
                imb = event.book.depth_imbalance(cfg.book_depth) if event.book else float("nan")
                log.info("DATA  %-9s px=%.2f  regime=%-9s  bookImb=%+.3f",
                         symbol, event.price, regime.value, imb)
                for sig in signals:
                    recorder.on_signal(sig)
            if interval > 0:
                time.sleep(interval)
    except KeyboardInterrupt:
        log.info("interrupted — stopping live observe")
    finally:
        connector.close()
        recorder.close()
    return recorder


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    minutes = float(os.environ.get("OBSERVE_MINUTES", "30"))
    recorder = run_live(minutes)
    print("\n=== LIVE OBSERVE SUMMARY ===")
    print(f"  buy/sell points recorded: {len(recorder.points)}")
    for sym, c in recorder.summary().items():
        print(f"  {sym}: BUY={c.get('BUY', 0)} SELL={c.get('SELL', 0)}")
    print("  log: logs/observe_signals.jsonl | execution: DISABLED (no orders)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
