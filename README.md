# Crypto Trading Bot (BTC/USDT, ETH/USDT) — Testnet

Event-driven algorithmic trading bot for a 5-person team project. Runs on a
**paper-trading testnet only** — there is no real-money path anywhere, and
ccxt sandbox mode is forced on in every client.

> **Hard rule: TESTNET ONLY for trading.** API keys come from environment
> variables only — never hard-coded, never committed.
>
> **Data vs. trading venue are separate.** The market-data feed can read
> *real Binance mainnet* order books + klines (`data.source: mainnet`, read-only,
> no API key), while **orders always go to the testnet paper account** — the
> broker is hard-locked to sandbox mode. This gives realistic signals without
> any real-money risk. Set `data.source: testnet` to use paper data instead.

## Quick start

```bash
pip install -r requirements.txt

# Offline mock (no keys, runs now):
python main.py

# Step-1 data-feed check (public order books, no key needed):
python scripts/check_data.py

# Paper-trading testnet (keys from https://testnet.binance.vision):
export BINANCE_TESTNET_API_KEY=...
export BINANCE_TESTNET_SECRET=...
BOT_MODE=testnet python main.py
```

`python main.py` runs the full pipeline on synthetic data for `mock.ticks`
ticks, then prints the final portfolio. Tune everything in `config/config.yaml`.

## Architecture (event-driven — do not refactor)

One `asyncio.Queue`. Modules never call each other directly: each consumes an
event and returns a list of new events; the `Engine` routes them. Handlers run
in **registration order** within an event type.

```
MARKET -> (REGIME) -> SIGNAL -> ORDER -> FILL
```

Each `MarketEvent` carries **both** data sources: a multi-level **order book**
(regime / microstructure / execution reference / mark-to-market) and a rolling
window of **OHLCV klines** (the price series for the strategy indicators
RSI / Bollinger / MACD / MA). Set under `data:` in the config: `timeframe` +
`kline_limit` (default 1m / 100 bars) and `book_depth` (default 10 levels per
side). `OrderBook.depth_imbalance(n)` gives the depth-weighted bid/ask
imbalance over the top `n` levels.

MARKET handler order is load-bearing:
`portfolio.on_market` → `detector.on_market` → `manager.on_market` → `oms.on_market`
(mark first, then regime, then generate signals, then track ref price).

**Frozen interfaces:** `src/core/events.py` and `src/strategy/base.py`. Everything
depends on them — change deliberately.

## Layout

| Path | What |
|---|---|
| `main.py` | `BOT_MODE=mock|testnet` switch, wires modules, runs the loop |
| `src/core/events.py` | Event classes (frozen interface) |
| `src/core/engine.py` | Async event loop + handler registry |
| `src/core/models.py` | Order / Position / OrderBook + enums |
| `src/core/config.py` | YAML config + env-var credentials |
| `src/part1_data/connector.py` | MockConnector + CcxtConnector: order book + OHLCV klines |
| `src/part6_regime/detector.py` | z-score/vol heuristic (HMM = TODO) |
| `src/strategy/` | base ABC, mean_reversion (RSI+BB), momentum (MACD+MA), manager |
| `src/portfolio/portfolio.py` | cash, positions, realised/unrealised PnL, MTM |
| `src/part5_risk/risk_manager.py` | sizing, funding check, drawdown halt (VaR/SL/TP = TODO) |
| `src/part3_execution/oms.py` | order lifecycle (limit TTL/kill/partial = TODO) |
| `src/part3_execution/broker.py` | MockBroker + CcxtBroker (testnet) |
| `src/part7_dashboard/app.py` | Streamlit live monitor (controls + metrics + kline + analytics) |
| `src/part8_backtest/` | standalone backtester: CLI + web page, shares the live strategy/risk stack |
| `src/utils/indicators.py` | rsi, bollinger, macd, zscore, moving_average |
| `scripts/check_data.py` | step-1 data-feed test |

### Part 8 — Backtester (standalone)

Replays historical klines through the SAME strategy / regime / portfolio / risk
modules the live bot uses (fills via `MockBroker`). Independent of `main.py` and
the live monitor.

```bash
# CLI report (+ saves runtime/backtest_<symbol>.html)
python -m src.part8_backtest --symbol BTC/USDT --days 90 --source mainnet
python -m src.part8_backtest --source mock --days 30          # offline, no network

# standalone web page: pick symbol/timeframe/days/source, click "Run backtest"
streamlit run src/part8_backtest/app.py
```

Report covers total/buy&hold return, max drawdown, annualised Sharpe, win rate,
profit factor, trade count and fees, plus an equity curve, drawdown curve and a
candlestick with buy/sell markers (web).

## Status

**Done & verified (mock):** full pipeline (data→regime→strategy→risk→OMS→
portfolio), indicators, both strategies + regime manager, portfolio accounting,
risk sizing/funding/drawdown-halt. Testnet code (`CcxtConnector`, `CcxtBroker`,
env creds, `BOT_MODE` switch) is written and imports cleanly.

**Not yet verified / TODO:** live testnet data feed (step 1), live orders, HMM+
entropy regime, limit-order 5s TTL / kill / partial fills / inventory balancing,
historical VaR + SL/TP execution, dashboard live wiring, backtester, shorting
(needs USD-M futures testnet). See module docstrings for owner-tagged TODOs.

## Tests

```bash
python -m pytest -q
```
