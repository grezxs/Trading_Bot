"""Performance metrics computed from a backtest's equity curve + trades.

Pure functions, stdlib only (no pandas/numpy needed). Annualisation uses the
bar timeframe so Sharpe is comparable across timeframes.
"""
from __future__ import annotations

import math
import statistics
from typing import TYPE_CHECKING, Any

from ..part1_data.connector import timeframe_seconds

if TYPE_CHECKING:  # avoid a circular import at runtime
    from .engine import BacktestResult


def compute_metrics(result: "BacktestResult") -> dict[str, Any]:
    eq = [v for _, v in result.equity_curve]
    start = result.starting_cash
    if not eq:
        return {"note": "no bars"}
    final = eq[-1]

    # --- max drawdown over the equity curve ---
    peak = eq[0]
    max_dd_abs = 0.0
    max_dd_pct = 0.0
    for v in eq:
        peak = max(peak, v)
        dd = peak - v
        max_dd_abs = max(max_dd_abs, dd)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, dd / peak)

    # --- annualised Sharpe from per-bar returns ---
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    sharpe = 0.0
    if len(rets) > 1:
        sd = statistics.pstdev(rets)
        if sd > 0:
            ppy = (365 * 24 * 3600) / max(1, timeframe_seconds(result.timeframe))
            sharpe = (statistics.mean(rets) / sd) * math.sqrt(ppy)

    # --- trade stats (win rate / profit factor on realised PnL) ---
    realized = [t.realized for t in result.trades if t.realized != 0.0]
    wins = [r for r in realized if r > 0]
    losses = [r for r in realized if r < 0]
    n_closing = len(wins) + len(losses)
    win_rate = (len(wins) / n_closing) if n_closing else 0.0
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else (
        float("inf") if gross_win > 0 else 0.0)

    # --- buy & hold benchmark ---
    bh = 0.0
    if result.candles and result.candles[0].close > 0:
        bh = result.candles[-1].close / result.candles[0].close - 1

    fees = sum(t.fee for t in result.trades)

    # --- Sortino ratio (downside deviation only) ---
    sortino = 0.0
    if len(rets) > 1:
        neg = [r for r in rets if r < 0]
        if neg:
            down_dev = math.sqrt(sum(r * r for r in neg) / len(rets))
            if down_dev > 0:
                ppy = (365 * 24 * 3600) / max(1, timeframe_seconds(result.timeframe))
                sortino = (statistics.mean(rets) / down_dev) * math.sqrt(ppy)

    # --- Calmar ratio (annualised return / max drawdown) ---
    calmar = 0.0
    if max_dd_pct > 0 and len(eq) > 1:
        total_bars = len(eq) - 1
        bar_sec = max(1, timeframe_seconds(result.timeframe))
        years = max(1e-9, total_bars * bar_sec / (365 * 24 * 3600))
        ann_ret = (final / start) ** (1.0 / years) - 1 if start > 0 else 0.0
        calmar = ann_ret / max_dd_pct

    # --- max consecutive losses ---
    max_consec_loss = 0
    cur_consec = 0
    for r in realized:
        if r < 0:
            cur_consec += 1
            max_consec_loss = max(max_consec_loss, cur_consec)
        else:
            cur_consec = 0

    # --- average holding period (seconds between paired entry/exit) ---
    avg_hold_sec = 0.0
    open_ts: dict[str, float] = {}
    hold_durations: list[float] = []
    for t in result.trades:
        key = t.side  # BUY opens, SELL closes (spot only)
        if t.side == "BUY":
            open_ts[t.reason] = t.ts
        elif t.side == "SELL" and open_ts:
            earliest = min(open_ts.values())
            hold_durations.append(t.ts - earliest)
            open_ts.clear()
    if hold_durations:
        avg_hold_sec = statistics.mean(hold_durations)

    # --- average win / average loss ---
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0

    return {
        "bars": len(eq),
        "final_equity": round(final, 2),
        "total_pnl": round(final - start, 2),
        "total_return_pct": round((final / start - 1) * 100, 2) if start else 0.0,
        "buy_hold_return_pct": round(bh * 100, 2),
        "max_drawdown_abs": round(max_dd_abs, 2),
        "max_drawdown_pct": round(max_dd_pct * 100, 2),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2),
        "num_trades": len(result.trades),
        "num_closing_trades": n_closing,
        "win_rate_pct": round(win_rate * 100, 1),
        "profit_factor": (round(profit_factor, 2)
                          if profit_factor != float("inf") else None),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "max_consec_losses": max_consec_loss,
        "avg_hold_seconds": round(avg_hold_sec, 1),
        "fees_paid": round(fees, 4),
        "halted_on_drawdown": result.halted,
    }
