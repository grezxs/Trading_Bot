"""Part 8 — standalone backtest web page.

    streamlit run src/part8_backtest/app.py

Independent of the live monitor (Part 7) and the bot (main.py). Pick a symbol /
timeframe / history length / data source, click "Run backtest", and get a
performance report + candlestick with trade markers + equity & drawdown curves
+ a trades table. The heavy lifting is shared with the CLI via
``engine.run_backtest`` / ``data.load_history``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# `streamlit run src/part8_backtest/app.py` runs this as a bare script with the
# repo root NOT on sys.path, so absolute `src.*` imports fail. Put the repo root
# on the path, then import the shared backtest code absolutely.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.core.config import load_config  # noqa: E402
from src.part8_backtest.data import load_history  # noqa: E402
from src.part8_backtest.engine import run_backtest  # noqa: E402


def _metric_grid(st, m: dict) -> None:
    a = st.columns(4)
    a[0].metric("Total return", f"{m.get('total_return_pct', 0):.2f}%",
                delta=f"B&H {m.get('buy_hold_return_pct', 0):.2f}%")
    a[1].metric("Total PnL", f"{m.get('total_pnl', 0):,.2f}")
    a[2].metric("Final equity", f"{m.get('final_equity', 0):,.2f}")
    a[3].metric("Sharpe", f"{m.get('sharpe', 0):.2f}")
    b = st.columns(4)
    b[0].metric("Max drawdown %", f"{m.get('max_drawdown_pct', 0):.2f}%")
    b[1].metric("Win rate", f"{m.get('win_rate_pct', 0):.1f}%")
    pf = m.get("profit_factor")
    b[2].metric("Profit factor", "∞" if pf is None else f"{pf:.2f}")
    b[3].metric("Trades", f"{m.get('num_trades', 0)}")


def _charts(st, result) -> None:
    import datetime as dt
    import pandas as pd
    import plotly.graph_objects as go

    df = pd.DataFrame([(c.ts, c.open, c.high, c.low, c.close) for c in result.candles],
                      columns=["ts", "open", "high", "low", "close"])
    df["t"] = pd.to_datetime(df["ts"], unit="s")

    st.subheader("Price + trades")
    fig = go.Figure(data=[go.Candlestick(
        x=df["t"], open=df["open"], high=df["high"], low=df["low"],
        close=df["close"], name=result.symbol)])
    buys = [(dt.datetime.fromtimestamp(t.ts), t.price) for t in result.trades if t.side == "BUY"]
    sells = [(dt.datetime.fromtimestamp(t.ts), t.price) for t in result.trades if t.side == "SELL"]
    if buys:
        fig.add_trace(go.Scatter(x=[b[0] for b in buys], y=[b[1] for b in buys],
                                 mode="markers", name="BUY",
                                 marker=dict(symbol="triangle-up", size=9, color="#16a34a")))
    if sells:
        fig.add_trace(go.Scatter(x=[s[0] for s in sells], y=[s[1] for s in sells],
                                 mode="markers", name="SELL",
                                 marker=dict(symbol="triangle-down", size=9, color="#dc2626")))
    fig.update_layout(height=460, margin=dict(l=10, r=10, t=10, b=10),
                      xaxis_rangeslider_visible=False)
    st.plotly_chart(fig, use_container_width=True)

    left, right = st.columns(2)
    eq = pd.DataFrame(result.equity_curve, columns=["ts", "equity"])
    eq["t"] = pd.to_datetime(eq["ts"], unit="s")
    with left:
        st.subheader("Equity curve")
        ef = go.Figure([go.Scatter(x=eq["t"], y=eq["equity"], name="equity",
                                   line=dict(color="#2563eb"))])
        ef.add_hline(y=result.starting_cash, line_dash="dot", line_color="gray")
        ef.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(ef, use_container_width=True)
    with right:
        st.subheader("Drawdown")
        peak = eq["equity"].cummax()
        dd = (eq["equity"] - peak) / peak * 100
        df_dd = go.Figure([go.Scatter(x=eq["t"], y=dd, name="drawdown %",
                                      fill="tozeroy", line=dict(color="#dc2626"))])
        df_dd.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10))
        st.plotly_chart(df_dd, use_container_width=True)


def render() -> None:
    import streamlit as st

    st.set_page_config(page_title="Backtester — Part 8", layout="wide")
    st.title("Part 8 — Strategy Backtester")
    st.caption("Standalone · replays historical klines through the live strategy/risk stack. "
               "Independent of the bot and the live monitor.")

    cfg = load_config()
    with st.sidebar:
        st.header("Backtest settings")
        symbol = st.selectbox("Symbol", cfg.symbols, index=0)
        timeframe = st.selectbox("Timeframe", ["1m", "5m", "15m", "1h"],
                                 index=["1m", "5m", "15m", "1h"].index(cfg.timeframe)
                                 if cfg.timeframe in ["1m", "5m", "15m", "1h"] else 0)
        days = st.slider("History (days)", 1, 180, 30)
        source = st.radio("Data source", ["mainnet", "testnet", "mock"],
                          index=["mainnet", "testnet", "mock"].index(cfg.data_source)
                          if cfg.data_source in ["mainnet", "testnet", "mock"] else 0,
                          help="mock = offline synthetic data (no network)")
        cash = st.number_input("Starting cash", value=float(cfg.starting_cash), step=1000.0)
        run = st.button("▶ Run backtest", use_container_width=True, type="primary")

    if not run:
        st.info("Set parameters on the left, then click **Run backtest**.")
        return

    with st.spinner(f"Loading {symbol} · {days}d @ {timeframe} from {source}…"):
        try:
            candles = load_history(symbol, days=days, timeframe=timeframe, source=source)
        except Exception as exc:
            st.error(f"Failed to load history: {exc}")
            return
    if len(candles) < 2:
        st.error(f"Not enough history ({len(candles)} bars). Try more days or another source.")
        return

    with st.spinner(f"Replaying {len(candles)} bars…"):
        result = run_backtest(symbol, candles, cfg=cfg, starting_cash=cash, timeframe=timeframe)

    if result.metrics.get("halted_on_drawdown"):
        st.warning("Risk drawdown halt triggered during the backtest — trading stopped early.")
    _metric_grid(st, result.metrics)
    _charts(st, result)

    st.subheader("Trades")
    if result.trades:
        rows = [{"time": __import__("datetime").datetime.fromtimestamp(t.ts).strftime("%Y-%m-%d %H:%M"),
                 "side": t.side, "qty": round(t.qty, 6), "price": round(t.price, 2),
                 "fee": round(t.fee, 4), "realized": round(t.realized, 2), "reason": t.reason}
                for t in result.trades]
        st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.info("No trades were generated (strategy stayed flat over this window).")


if __name__ == "__main__":
    render()
