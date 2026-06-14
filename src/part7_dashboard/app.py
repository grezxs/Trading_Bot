"""Streamlit dashboard — LIVE monitor + control panel for the running bot.

Run with:  streamlit run src/part7_dashboard/app.py

Two channels with the bot (separate process):
- bot -> dashboard:  ``runtime/state.json`` (StatePublisher) — equity/PnL/
  drawdown, per-symbol regime + position + klines + strategy analytics, alerts.
- dashboard -> bot:  ``runtime/control.json`` (control.py) — the three buttons
  Enable Trading / Stop Trading / Kill Switch.

The body auto-refreshes every 2s via ``st.fragment(run_every=...)``. A view
selector switches between an Overview and each symbol (kline + analytics).

TESTNET / PAPER ONLY.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# `streamlit run src/part7_dashboard/app.py` executes this file as a bare script with
# no parent package AND without its own directory on sys.path, so BOTH relative
# (`from .control`) and sibling-absolute (`from control`) imports fail. Make it
# robust: put this file's directory on sys.path, then import the siblings
# absolutely. This also works for `from src.part7_dashboard.app import render`.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from control import DEFAULT_CONTROL_PATH, read_control, write_control  # noqa: E402
from state import DEFAULT_STATE_PATH  # noqa: E402

_STALE_AFTER = 15.0  # seconds without an update => consider the bot stopped


def _load_state(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


# ----------------------------------------------------------------------------
# control buttons (write runtime/control.json — the bot polls it each tick)
# ----------------------------------------------------------------------------
def _render_controls(st) -> None:
    cmd = read_control(DEFAULT_CONTROL_PATH) or {"trading_enabled": True, "kill_switch": False}
    trading_enabled = bool(cmd.get("trading_enabled", True))
    killed = bool(cmd.get("kill_switch", False))

    st.subheader("Controls")
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    if c1.button("▶ Enable Trading", use_container_width=True,
                 disabled=killed, type="primary" if not trading_enabled else "secondary"):
        write_control(True, killed, DEFAULT_CONTROL_PATH)
        st.rerun()
    if c2.button("⏸ Stop Trading", use_container_width=True,
                 disabled=killed, type="primary" if trading_enabled else "secondary"):
        write_control(False, killed, DEFAULT_CONTROL_PATH)
        st.rerun()
    if c3.button("🛑 Kill Switch", use_container_width=True, disabled=killed):
        write_control(False, True, DEFAULT_CONTROL_PATH)
        st.rerun()

    if killed:
        c4.error("KILLED — restart the bot to resume")
    elif trading_enabled:
        c4.success("Trading ENABLED (entries on)")
    else:
        c4.warning("Trading STOPPED (entries off · exits still fire)")


# ----------------------------------------------------------------------------
# overview vs per-symbol views
# ----------------------------------------------------------------------------
def _metrics_row(st, state: dict) -> None:
    a = st.columns(3)
    a[0].metric("Equity", f"{state.get('equity', 0):,.2f}")
    a[1].metric("Total PnL", f"{state.get('total_pnl', 0):,.2f}")
    a[2].metric("Cash", f"{state.get('cash', 0):,.2f}")
    b = st.columns(4)
    b[0].metric("Realized PnL", f"{state.get('realized_pnl', 0):,.2f}")
    b[1].metric("Unrealized PnL", f"{state.get('unrealized_pnl', 0):,.2f}")
    b[2].metric("Max Drawdown", f"{state.get('max_drawdown_abs', 0):,.2f}")
    b[3].metric("Max Drawdown %", f"{state.get('max_drawdown_pct', 0):.2f}%",
                delta=f"now {state.get('drawdown_pct', 0):.2f}%", delta_color="inverse")


def _render_overview(st, state: dict) -> None:
    _metrics_row(st, state)

    st.subheader("Equity curve")
    curve = state.get("equity_curve") or []
    if len(curve) >= 2:
        st.line_chart({"equity": [p[1] for p in curve]})
        st.caption(f"{len(curve)} points · span {int(curve[-1][0] - curve[0][0])}s "
                   f"· peak {state.get('peak_equity', 0):,.2f} · fees {state.get('fees_paid', 0):,.4f}")
    else:
        st.info("Collecting equity points…")

    left, right = st.columns(2)
    with left:
        st.subheader("Positions / inventory")
        positions = state.get("positions") or {}
        if positions:
            rows = [{"symbol": s, "qty": p.get("qty"), "avg": p.get("avg"),
                     "last": p.get("last"),
                     "notional": round((p.get("qty", 0) or 0) * (p.get("last", 0) or 0), 2)}
                    for s, p in positions.items()]
            st.dataframe(rows, use_container_width=True, hide_index=True)
        else:
            st.info("Flat — no open positions.")
    with right:
        st.subheader("Regime per symbol")
        regimes = state.get("regimes") or {}
        if regimes:
            badge = {"TRENDING": "🟦", "RANGING": "🟨", "HIGH_VOL": "🟥"}
            for sym, reg in regimes.items():
                st.write(f"{badge.get(reg, '⬜')} **{sym}** — {reg or 'warming up…'}")
        else:
            st.info("No regime yet.")


def _render_kline(st, sym: str, candles: list) -> None:
    st.subheader(f"Kline — {sym}")
    if not candles or len(candles) < 2:
        st.info("Waiting for klines…")
        return
    try:
        import pandas as pd
        import plotly.graph_objects as go
        df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume"])
        df["t"] = pd.to_datetime(df["ts"], unit="s")
        fig = go.Figure(data=[go.Candlestick(
            x=df["t"], open=df["open"], high=df["high"],
            low=df["low"], close=df["close"], name=sym)])
        fig.update_layout(height=420, margin=dict(l=0, r=0, t=10, b=0),
                          xaxis_rangeslider_visible=False)
        st.plotly_chart(fig, use_container_width=True)
    except Exception:  # plotly/pandas missing -> fall back to a close line
        st.line_chart({"close": [c[4] for c in candles]})


def _render_symbol(st, state: dict, sym: str) -> None:
    per = (state.get("per_symbol") or {}).get(sym) or {}
    pos = per.get("position") or {}
    regime = per.get("regime")
    badge = {"TRENDING": "🟦", "RANGING": "🟨", "HIGH_VOL": "🟥"}.get(regime, "⬜")

    top = st.columns(4)
    top[0].metric("Price", f"{per.get('price', 0):,.2f}")
    top[1].metric("Position qty", f"{pos.get('qty', 0):.6f}")
    top[2].metric("Realized PnL", f"{pos.get('realized', 0):,.2f}")
    top[3].metric("Unrealized PnL", f"{pos.get('unrealized', 0):,.2f}")

    _render_kline(st, sym, per.get("candles") or [])

    st.subheader("Strategy analytics")
    left, right = st.columns(2)
    with left:
        st.write(f"**Regime:** {badge} {regime or 'warming up…'}")
        sigs = per.get("signals") or {}
        st.write(f"**Signals:** 🟢 BUY {sigs.get('BUY', 0)} · "
                 f"🔴 SELL {sigs.get('SELL', 0)} · ⚪ HOLD {sigs.get('HOLD', 0)}")
        st.write(f"**Fills:** {per.get('fills', 0)}")
        st.write(f"**Avg entry:** {pos.get('avg', 0):,.2f} · "
                 f"**Notional:** {pos.get('notional', 0):,.2f}")
    with right:
        last = per.get("last_signal")
        if last:
            st.write("**Last signal**")
            st.json({
                "time": last.get("time"),
                "signal": last.get("signal"),
                "strategy": last.get("strategy"),
                "magnitude": last.get("magnitude"),
                "price": last.get("price"),
            })
        else:
            st.info("No signal emitted yet for this symbol.")


# ----------------------------------------------------------------------------
# body (auto-refreshed) + page shell
# ----------------------------------------------------------------------------
def _render_body(st) -> None:
    state = _load_state(DEFAULT_STATE_PATH)
    if state is None:
        st.warning(
            "No live state yet. Start the bot in another terminal:\n\n"
            "`BOT_MODE=testnet python main.py`  (or `BOT_MODE=mock python main.py`)"
        )
        return

    age = time.time() - float(state.get("updated", 0))
    fresh = age <= _STALE_AFTER
    status = f"🟢 live · updated {state.get('updated_str', '—')}" if fresh \
        else f"🔴 stale ({int(age)}s ago) · bot may be stopped"
    ctl = state.get("control") or {}
    flag = "KILLED" if ctl.get("killed") else ("ENABLED" if ctl.get("trading_enabled", True) else "STOPPED")
    st.caption(f"TESTNET ONLY · {status} · trading: {flag}")

    view = st.session_state.get("view", "Overview")
    if view == "Overview":
        _render_overview(st, state)
    else:
        _render_symbol(st, state, view)

    st.subheader("Alerts")
    alerts = state.get("alerts") or []
    if alerts:
        for a in alerts[:40]:
            line = f"`{a.get('time','')}` **{a.get('level','')}** {a.get('name','')}: {a.get('msg','')}"
            if a.get("level") in ("WARNING", "ERROR", "CRITICAL"):
                st.warning(line)
            else:
                st.write(line)
    else:
        st.info("No alerts yet (orders / TP / SL / kills / drawdown will appear here).")


def render() -> None:
    import streamlit as st  # imported lazily; dashboard deps are optional

    st.set_page_config(page_title="Crypto Bot — Paper Monitor", layout="wide")
    st.title("Crypto Trading Bot — Paper (Testnet) Monitor")

    # controls + view selector live OUTSIDE the auto-refresh fragment so button
    # clicks drive a full rerun; the fragment only redraws live data.
    _render_controls(st)

    state = _load_state(DEFAULT_STATE_PATH) or {}
    symbols = state.get("symbols") or list((state.get("per_symbol") or {}).keys())
    options = ["Overview"] + symbols
    st.radio("View", options, horizontal=True, key="view")

    st.divider()
    if hasattr(st, "fragment"):
        @st.fragment(run_every="2s")
        def _body() -> None:
            _render_body(st)
        _body()
    else:  # older streamlit: manual rerun loop
        _render_body(st)
        time.sleep(2)
        st.rerun()


if __name__ == "__main__":
    render()
