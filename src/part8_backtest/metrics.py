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

    return {
        "bars": len(eq),
        "final_equity": round(final, 2),
        "total_pnl": round(final - start, 2),
        "total_return_pct": round((final / start - 1) * 100, 2) if start else 0.0,
        "buy_hold_return_pct": round(bh * 100, 2),
        "max_drawdown_abs": round(max_dd_abs, 2),
        "max_drawdown_pct": round(max_dd_pct * 100, 2),
        "sharpe": round(sharpe, 2),
        "num_trades": len(result.trades),
        "num_closing_trades": n_closing,
        "win_rate_pct": round(win_rate * 100, 1),
        "profit_factor": (round(profit_factor, 2)
                          if profit_factor != float("inf") else None),
        "fees_paid": round(fees, 4),
        "halted_on_drawdown": result.halted,
    }
