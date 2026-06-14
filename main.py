"""Single entry point. BOT_MODE selects the data/execution backend.

    BOT_MODE=mock     (default) offline synthetic data + MockBroker. No keys.
    BOT_MODE=observe  Step-2 dry run: real market data -> regime + strategy,
                      EXECUTION DISABLED. Replays recent real klines instantly
                      and logs the regimes + signals. Places no orders.
                      For a LIVE stream + buy/sell points, run the standalone
                      recorder instead:  python src/part2_observe/recorder.py
    BOT_MODE=testnet  Live paper trading: data feed (mainnet or testnet per
                      config) + CcxtBroker on a PAPER account. PAPER_VENUE picks
                      the venue: "demo" (main-site Demo Trading) or "testnet"
                      (testnet.binance.vision, default).

BOT_MODE and PAPER_VENUE can be set in a gitignored .env so a bare
``python main.py`` runs the whole stack live on the paper venue. Trading is
PAPER ONLY — the broker is hard-locked (sandbox testnet or demo host; it refuses
the production endpoint). The data feed may read real mainnet data (read-only,
no keys). Credentials come from env only.

Handler registration order is deliberate and load-bearing:
    MARKET:  portfolio.on_market -> detector.on_market -> strategy.on_market
             (mark first, then regime, then generate signals)
    SIGNAL:  risk.on_signal
    ORDER:   oms.on_order
    FILL:    portfolio.on_fill
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

from src.core.config import (
    Config,
    Credentials,
    load_config,
    load_credentials,
    load_local_env,
)
from src.core.engine import Engine
from src.core.events import EventType, MarketEvent
from src.core.models import Regime
from src.part7_dashboard.control import BotControl, ControlBridge
from src.part7_dashboard.state import StatePublisher
from src.part1_data.connector import BaseConnector, CcxtConnector, MockConnector
from src.part3_execution.broker import BaseBroker, CcxtBroker, MockBroker
from src.part3_execution.oms import OMS
from src.portfolio.portfolio import Portfolio
from src.part6_regime.detector import RegimeDetector
from src.part5_risk.risk_manager import RiskManager
from src.strategy.manager import StrategyManager
from src.strategy.mean_reversion import MeanReversionStrategy
from src.strategy.momentum import MomentumStrategy

log = logging.getLogger("main")


def _creds_for_venue(venue: str, market_type: str) -> Credentials:
    """Pick the paper-venue keys. ``demo`` = main-site Demo Trading
    (demo-api.binance.com); anything else = testnet.binance.vision. Keys come
    from env only (auto-loaded from a gitignored ``.env`` if present)."""
    if venue == "demo":
        return Credentials(
            api_key=os.environ.get("BINANCE_DEMO_API_KEY"),
            secret=os.environ.get("BINANCE_DEMO_SECRET"),
        )
    return load_credentials(market_type)


def build_modules(cfg: Config, mode: str):
    risk_cfg = cfg.risk
    portfolio = Portfolio(starting_cash=cfg.starting_cash)
    engine = Engine()
    detector = RegimeDetector(
        window=cfg.regime_window,
        trend_z=float(cfg.get("regime", "trend_z", default=1.0)),
        highvol_mult=float(cfg.get("regime", "highvol_mult", default=2.0)),
    )
    # momentum first so TRENDING matches it before mean-reversion's HIGH_VOL/RANGING
    strategies = [MomentumStrategy(), MeanReversionStrategy()]
    manager = StrategyManager(detector, strategies, signal_interval=cfg.signal_interval)
    # dashboard control: seed the runtime enable/stop/kill flag from config
    control = BotControl(trading_enabled=bool(cfg.order.get("trading_enabled", True)))
    risk = RiskManager(
        portfolio, engine,
        max_position_value=float(risk_cfg.get("max_position_value", 200)),
        max_position_qty=float(risk_cfg.get("max_position_qty", 1.0)),
        max_drawdown=float(risk_cfg.get("max_drawdown", 0.2)),
        var_window=int(risk_cfg.get("var_window", 60)),
        order_policy=cfg.order,  # sizing / TP-SL / throttles (config.yaml order:)
        control=control,         # dashboard buttons override trading_enabled
    )

    taker_fee = float(cfg.get("execution", "taker_fee", default=0.001))
    connector: BaseConnector
    broker: BaseBroker
    if mode == "testnet":
        market_type = cfg.get("execution", "market_type", default="spot")
        venue = os.environ.get("PAPER_VENUE", "testnet").lower()  # "demo" | "testnet"
        creds: Credentials = _creds_for_venue(venue, market_type)
        # DATA source is independent of the trading venue. Orders ALWAYS go to a
        # PAPER venue (demo/testnet broker below); only the data feed may read
        # real mainnet data.
        data_sandbox = cfg.data_source != "mainnet"
        connector = CcxtConnector(cfg.symbols, market_type=market_type,
                                  timeframe=cfg.timeframe, kline_limit=cfg.kline_limit,
                                  book_depth=cfg.book_depth, sandbox=data_sandbox)
        log.info("data feed: %s | orders: %s (paper)",
                 "MAINNET (real, read-only)" if not data_sandbox else "testnet", venue)
        if creds.present:
            broker = CcxtBroker(creds, market_type=market_type, venue=venue)
            log.info("testnet mode: live CcxtBroker venue=%s "
                     "(orders WILL be placed on the %s paper account)", venue, venue)
        else:
            broker = MockBroker(cfg.starting_cash, taker_fee)
            log.warning("paper DATA only: no %s API keys -> orders go to MockBroker", venue)
    else:
        m = cfg.mock
        connector = MockConnector(cfg.symbols, vol=float(m.get("vol", 0.002)),
                                  seed=m.get("seed", 42), interval=cfg.market_interval,
                                  timeframe=cfg.timeframe, kline_limit=cfg.kline_limit,
                                  book_depth=cfg.book_depth)
        broker = MockBroker(cfg.starting_cash, taker_fee)

    oms = OMS(broker,
              default_ttl=float(cfg.order.get("ttl_sec", 5)),
              cancel_unfilled=bool(cfg.order.get("cancel_unfilled", True)),
              engine=engine)  # lets the OMS cancel-all on a hard halt

    # dashboard -> bot command bridge (Enable/Stop/Kill). FIRST so a toggle
    # applies on the same tick. Seed the command file from config defaults.
    bridge = ControlBridge(control, engine, oms)
    bridge.seed_file()
    publisher = StatePublisher(portfolio, detector, cfg.symbols, control=control)

    # registration order is the contract — do not reorder casually
    engine.register(EventType.MARKET, bridge.on_market)  # pull dashboard commands
    engine.register(EventType.MARKET, portfolio.on_market)
    engine.register(EventType.MARKET, risk.on_market)  # TP/SL exits (after the mark)
    engine.register(EventType.MARKET, detector.on_market)
    engine.register(EventType.MARKET, manager.on_market)
    engine.register(EventType.MARKET, oms.on_market)  # tracks ref price + TTL sweep
    engine.register(EventType.MARKET, publisher.on_market)  # LAST: publish state
    engine.register(EventType.SIGNAL, risk.on_signal)
    engine.register(EventType.SIGNAL, publisher.on_signal)  # observe for analytics
    engine.register(EventType.ORDER, oms.on_order)
    engine.register(EventType.FILL, portfolio.on_fill)
    engine.register(EventType.FILL, publisher.on_fill)  # observe fill counts

    return engine, connector, broker, portfolio, publisher


def make_producer(engine: Engine, connector: BaseConnector, symbols: list[str],
                  *, ticks: int | None, sleep_sec: float):
    """One producer that polls all symbols each cycle.

    ticks=None  -> run forever (testnet) until cancelled.
    ticks=N     -> emit N cycles then return (mock), so the engine drains+stops.
    """
    async def producer() -> None:
        n = 0
        while ticks is None or n < ticks:
            for symbol in symbols:
                event = await asyncio.to_thread(connector.poll, symbol)
                if event is not None:
                    await engine.put(event)
            n += 1
            if sleep_sec > 0:
                await asyncio.sleep(sleep_sec)
            elif ticks is None:
                await asyncio.sleep(0)  # yield
    return producer()


def run_observe(cfg: Config) -> None:
    """Step-2 dry run. Real market data -> regime + strategy, NO execution.

    Pulls the recent real kline window once per symbol, then replays it bar by
    bar through portfolio -> detector -> manager (the same MARKET handler chain
    as live), logging regime evolution and every non-HOLD signal. No risk, no
    OMS, no broker — nothing can place an order. Replaying history lets us see
    real results immediately instead of waiting ~30 min for live bars.
    """
    data_sandbox = cfg.data_source != "mainnet"
    venue = "MAINNET (real, read-only)" if not data_sandbox else "testnet (paper)"
    log.info("observe mode: data=%s | EXECUTION DISABLED (no broker)", venue)

    connector = CcxtConnector(cfg.symbols, market_type="spot",
                              timeframe=cfg.timeframe, kline_limit=cfg.kline_limit,
                              book_depth=cfg.book_depth, sandbox=data_sandbox)
    detector = RegimeDetector(
        window=cfg.regime_window,
        trend_z=float(cfg.get("regime", "trend_z", default=1.0)),
        highvol_mult=float(cfg.get("regime", "highvol_mult", default=2.0)),
    )
    strategies = [MomentumStrategy(), MeanReversionStrategy()]
    manager = StrategyManager(detector, strategies, signal_interval=cfg.signal_interval)
    portfolio = Portfolio(starting_cash=cfg.starting_cash)

    try:
        for symbol in cfg.symbols:
            event = connector.poll(symbol)
            if event is None or not event.candles:
                log.warning("observe: no klines for %s — skipping", symbol)
                continue
            candles = event.candles
            log.info("observe %s: replaying %d real %s bars",
                     symbol, len(candles), cfg.timeframe)
            regime_counts: dict[Regime, int] = {}
            sig_counts: dict[str, int] = {}
            n_signals = 0
            prev_regime: Regime | None = None
            for i in range(len(candles)):
                c = candles[i]
                me = MarketEvent(symbol=symbol, price=c.close,
                                 candles=candles[: i + 1], ts=c.ts)
                portfolio.on_market(me)
                detector.on_market(me)
                out = manager.on_market(me)
                regime = detector.current_regime(symbol)
                if regime is not None:
                    regime_counts[regime] = regime_counts.get(regime, 0) + 1
                    if regime != prev_regime:
                        log.info("  regime %s bar %d: %s -> %s", symbol, i,
                                 prev_regime.name if prev_regime else "(none)",
                                 regime.name)
                        prev_regime = regime
                for ev in out or []:
                    sig = getattr(ev, "signal", None)
                    name = sig.name if sig else str(ev)
                    sig_counts[name] = sig_counts.get(name, 0) + 1
                    n_signals += 1
            dist = ", ".join(f"{r.name}={n}" for r, n in regime_counts.items())
            tally = ", ".join(f"{k}={v}" for k, v in sig_counts.items())
            log.info("observe %s: regime distribution -> %s", symbol, dist or "(none)")
            log.info("observe %s: %d signal(s) -> %s", symbol, n_signals, tally or "(none)")
    finally:
        connector.close()

    print("\n=== OBSERVE SUMMARY ===")
    print(f"  data source: {venue}")
    print(f"  symbols replayed: {cfg.symbols}")
    print("  execution: DISABLED (no orders placed)")


async def run(mode: str, cfg: Config) -> Portfolio:
    engine, connector, broker, portfolio, publisher = build_modules(cfg, mode)
    if mode == "testnet":
        producer = make_producer(engine, connector, cfg.symbols,
                                 ticks=None, sleep_sec=cfg.market_interval)
        log.info("testnet running — Ctrl-C to stop")
    else:
        m = cfg.mock
        # env overrides let the launcher run a CONTINUOUS, watchable mock for the
        # live dashboard (otherwise mock emits its 300 ticks instantly and exits).
        ticks = int(os.environ.get("MOCK_TICKS") or m.get("ticks", 300))
        sleep = float(os.environ.get("MOCK_TICK_SLEEP") or m.get("tick_sleep", 0.0))
        producer = make_producer(engine, connector, cfg.symbols,
                                 ticks=ticks, sleep_sec=sleep)
    try:
        await engine.run([producer])
    finally:
        publisher.flush()  # final snapshot so the dashboard shows the end state
        connector.close()
        broker.close()
    return portfolio


def _launch_dashboard() -> "subprocess.Popen | None":
    """Spawn the Streamlit monitor as a child process so a single
    ``python main.py`` brings up BOTH the bot and the web UI (one command, one
    Ctrl-C stops everything). Disable with ``DASHBOARD=0``. Returns the child
    process handle, or None if disabled / Streamlit isn't installed.
    """
    if os.environ.get("DASHBOARD", "1") == "0":
        return None
    here = os.path.dirname(os.path.abspath(__file__))
    app = os.path.join(here, "src", "part7_dashboard", "app.py")
    # skip Streamlit's first-run interactive "Email:" prompt (only if unset)
    cred = os.path.expanduser("~/.streamlit/credentials.toml")
    if not os.path.exists(cred):
        try:
            os.makedirs(os.path.dirname(cred), exist_ok=True)
            with open(cred, "w") as f:
                f.write('[general]\nemail = ""\n')
        except Exception:
            pass
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "streamlit", "run", app,
             "--browser.gatherUsageStats", "false"],
        )
        log.info("dashboard: Streamlit monitor launching -> http://localhost:8501 "
                 "(set DASHBOARD=0 to disable)")
        return proc
    except Exception:
        log.warning("dashboard: failed to launch Streamlit (is it installed?)",
                    exc_info=True)
        return None


def _stop_dashboard(proc: "subprocess.Popen | None") -> None:
    if proc is None:
        return
    log.info("dashboard: stopping Streamlit monitor")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except Exception:
        proc.kill()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    load_local_env()  # pull gitignored .env so BOT_MODE / PAPER_VENUE / keys apply
    mode = os.environ.get("BOT_MODE", "mock").lower()
    if mode not in ("mock", "observe", "testnet"):
        log.error("BOT_MODE must be 'mock', 'observe' or 'testnet' (got %r)", mode)
        sys.exit(2)

    cfg = load_config()
    log.info("starting in %s mode | symbols=%s", mode, cfg.symbols)

    if mode == "observe":
        # Historical replay (instant). For the LIVE stream + buy/sell points,
        # run the standalone recorder:  python src/part2_observe/recorder.py
        run_observe(cfg)
        return

    # one-command launch: bring up the Streamlit monitor alongside the bot
    dash = _launch_dashboard()
    portfolio = None
    try:
        portfolio = asyncio.run(run(mode, cfg))
    except KeyboardInterrupt:
        log.info("interrupted")
    finally:
        _stop_dashboard(dash)

    if portfolio is None:
        return
    snap = portfolio.snapshot()
    print("\n=== FINAL PORTFOLIO ===")
    for k, v in snap.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
