# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Non-negotiable rules
- **TESTNET ONLY.** No real-money account, no real orders. `CcxtBroker` is hard-locked to sandbox/demo mode — keep it that way.
- **Keys from env only** — never hard-code or commit credentials. `.env` is gitignored; credentials are loaded via `load_local_env()` at startup.
- **Do not change the event-driven architecture** or break the frozen interfaces in `src/core/events.py` and `src/strategy/base.py`. Every module depends on them.
- **Keep mock mode working at all times** (`python main.py`) — it's how the team tests offline.
- **Handler registration order in `main.py` is a contract.** `MARKET: bridge → portfolio → risk → detector → manager → oms → publisher`. Do not reorder without understanding the data-dependency chain (mark before regime before signal).
- After completing a step, summarize what changed and what to verify, then wait.

## Commands

```bash
# Install
pip install -r requirements.txt

# Offline mock — no keys, runs 300 ticks, prints final portfolio
python main.py

# Step-1 data-feed check (public order book + klines, no key needed)
python scripts/check_data.py

# Step-2 observe-only — real market data → regime + strategy, NO orders
BOT_MODE=observe python main.py

# Live paper trading (keys from https://testnet.binance.vision)
export BINANCE_TESTNET_API_KEY=...
export BINANCE_TESTNET_SECRET=...
BOT_MODE=testnet python main.py

# Run all tests
python -m pytest -q

# Run a single test
python -m pytest tests/test_smoke.py::test_mock_pipeline_runs_end_to_end -v

# Backtester (CLI — saves runtime/backtest_<symbol>.html)
python -m src.part8_backtest --symbol BTC/USDT --days 90 --source mainnet

# Backtester web UI
streamlit run src/part8_backtest/app.py
```

## Architecture

### Event-driven pipeline

One `asyncio.Queue` in `Engine`. Modules never call each other — each handler consumes one event and returns a list of new events; the Engine routes them. Event chain:

```
MARKET → (REGIME) → SIGNAL → ORDER → FILL
```

The `Engine` (`src/core/engine.py`) dispatches events in handler-registration order, isolating exceptions per handler. `Engine.halt()` drops all future `ORDER` events — it is the hard risk stop.

### Data vs. trading venue separation

Market data (`CcxtConnector`) and the trading broker (`CcxtBroker`) are **independent clients**. The data feed can read real Binance mainnet order books + klines (public, no key, `data.source: mainnet` in config) while orders always go to a paper testnet/demo account. The broker is built without an API credential for the data endpoint and refuses the production trading endpoint.

### Frozen interfaces

Two files are interface contracts — every module depends on them:
- `src/core/events.py` — `MarketEvent`, `SignalEvent`, `OrderEvent`, `FillEvent`, `RegimeEvent`. Change these and you break every handler.
- `src/strategy/base.py` — `Strategy.observe(event)` / `Strategy.generate(symbol, price)`. The `StrategyManager` and both concrete strategies depend on these signatures.

### Key module interactions

- **`MockConnector`** pre-seeds `kline_limit` historical candles so indicators (RSI/Bollinger/MACD/MA) are warm from tick 0 — no cold-start period in mock mode.
- **`CcxtConnector`** caches klines per symbol and refreshes at most once per timeframe, since the order book is polled far more often than a new candle forms.
- **`RegimeDetector`** runs a z-score/volatility heuristic and writes `_regime[symbol]` in its `on_market`. The `StrategyManager` reads this dict in its own `on_market` (registered next) — so the regime is always current for each signal tick. HMM upgrade is a ShiYi TODO.
- **`StrategyManager`** feeds price history to *all* strategies every tick (so they are warm), then calls `generate` only on the regime-appropriate one. Signal cadence is throttled to `signal_interval` (default 60 s) per symbol.
- **`RiskManager.on_signal`** sizes the order (notional/base/pct_equity/allin modes), enforces drawdown halt, kill-switch, cooldown, position caps, and funding check before emitting `OrderEvent`. **`RiskManager.on_market`** fires take-profit / stop-loss exits every tick, bypassing the kill-switch, so a held position is always protectable.
- **`OMS.on_market`** sweeps expired LIMIT orders (5 s TTL by default) and cancels everything on engine halt.
- **`Portfolio`** must be the first `MARKET` handler so MTM and equity are current before `RiskManager` checks drawdown.

### Config as single source of truth

`config/config.yaml` (`order:` block) is the only place to tune sizing, TP/SL, limit TTL, cooldown, and position caps — no code changes needed. `Config.order` merges this block over hardcoded defaults, so all keys are safe to read even if absent from the YAML.

### BOT_MODE

| Value | Data | Broker | Orders |
|---|---|---|---|
| `mock` (default) | `MockConnector` synthetic GBM | `MockBroker` | Simulated immediately |
| `observe` | `CcxtConnector` (mainnet or testnet) | None | Never placed |
| `testnet` | `CcxtConnector` | `CcxtBroker` (paper) | Testnet/demo account |

`PAPER_VENUE=demo` switches from `testnet.binance.vision` to the main-site Demo Trading endpoint. Both are paper-only.

## Module ownership

| Module | Owner(s) |
|---|---|
| Data (`part1_data`), Strategies (`strategy/`), Dashboard (`part7_dashboard`) | Cheng, Gilbert |
| Regime (`part6_regime`) | ShiYi |
| Portfolio (`portfolio/`), Risk (`part5_risk`) | Gilbert, Grace |
| Execution / OMS (`part3_execution`) | sookoon |

## Step-by-step plan — DO IN ORDER, stop and report after each

1. **Confirm the data feed** ← current step. `python scripts/check_data.py` should print live BTC/ETH books + `OK - data feed is working.` (no key needed).
2. **Observe-only validation.** Live testnet data → regime + strategy, execution disabled. Log regimes/signals ~30 min; place no orders.
3. **First live order.** Enable OMS with `CcxtBroker`; one small manual market order; confirm fill + Portfolio + testnet balance update.
4. **Order lifecycle.** Limit orders, 5s TTL cancel, kill orders, partial fills, inventory balancing. (TODOs in `src/part3_execution/oms.py`.)
5. **Risk hardening.** Historical VaR (10-min), stop-loss/take-profit, drawdown alerts + halt. (TODOs in `src/part5_risk/risk_manager.py`.)
6. **Regime upgrade.** GaussianHMM (`hmmlearn`) + entropy feature. (`src/part6_regime/detector.py`.)
7. **Dashboard.** Wire Streamlit to live state. (`src/part7_dashboard/app.py`.)
8. **Backtester.**
9. **Class diagram + 5-min pitch.**
