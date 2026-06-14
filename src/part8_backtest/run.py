"""CLI entry for the backtester.

    python -m src.part8_backtest --symbol BTC/USDT --days 90 --source mainnet
    python -m src.part8_backtest --source mock --days 30      # offline, no network

Prints a performance report and (unless --no-plot) saves an interactive equity
curve to runtime/backtest_<symbol>.html. Fully standalone — does not touch the
live bot or the monitor.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# allow `python src/part8_backtest/run.py` as well as `-m src.part8_backtest`
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.core.config import load_config
from src.part8_backtest.data import load_history
from src.part8_backtest.engine import BacktestResult, MultiSymbolResult, run_backtest, run_multi_backtest

log = logging.getLogger("backtest.cli")

_LABELS = [
    ("bars", "Bars replayed"),
    ("final_equity", "Final equity"),
    ("total_pnl", "Total PnL"),
    ("total_return_pct", "Total return %"),
    ("buy_hold_return_pct", "Buy & hold %"),
    ("max_drawdown_abs", "Max drawdown"),
    ("max_drawdown_pct", "Max drawdown %"),
    ("sharpe", "Sharpe (annualised)"),
    ("sortino", "Sortino (annualised)"),
    ("calmar", "Calmar ratio"),
    ("num_trades", "Trades"),
    ("num_closing_trades", "Closing trades"),
    ("win_rate_pct", "Win rate %"),
    ("profit_factor", "Profit factor"),
    ("avg_win", "Avg win"),
    ("avg_loss", "Avg loss"),
    ("max_consec_losses", "Max consec losses"),
    ("avg_hold_seconds", "Avg hold (sec)"),
    ("fees_paid", "Fees paid"),
    ("halted_on_drawdown", "Halted on drawdown"),
]


def format_report(result: BacktestResult) -> str:
    m = result.metrics
    lines = [
        "",
        "=" * 48,
        f"  BACKTEST REPORT — {result.symbol} [{result.timeframe}]",
        "=" * 48,
        f"  Starting cash:   {result.starting_cash:,.2f}",
    ]
    for key, label in _LABELS:
        if key in m:
            val = m[key]
            if isinstance(val, float):
                val = f"{val:,.2f}"
            lines.append(f"  {label:<22} {val}")
    lines.append("=" * 48)
    return "\n".join(lines)


def save_equity_html(result: BacktestResult, path: Path) -> bool:
    try:
        import plotly.graph_objects as go
    except Exception:
        log.warning("plotly not installed — skipping chart")
        return False
    import datetime as dt
    xs = [dt.datetime.fromtimestamp(t) for t, _ in result.equity_curve]
    ys = [v for _, v in result.equity_curve]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xs, y=ys, name="equity", line=dict(color="#2563eb")))
    fig.add_hline(y=result.starting_cash, line_dash="dot", line_color="gray")
    fig.update_layout(title=f"Backtest equity — {result.symbol} [{result.timeframe}]",
                      height=480, margin=dict(l=10, r=10, t=40, b=10))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(path))
    return True


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level="INFO", format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    cfg = load_config()
    ap = argparse.ArgumentParser(description="Standalone strategy backtester (Part 8)")
    ap.add_argument("--symbol", default=cfg.symbols[0], help="e.g. BTC/USDT")
    ap.add_argument("--multi", action="store_true",
                    help="run all configured symbols through a shared portfolio")
    ap.add_argument("--days", type=float, default=30.0, help="history length in days")
    ap.add_argument("--timeframe", default=cfg.timeframe, help="bar size, e.g. 1m 5m 1h")
    ap.add_argument("--source", default=cfg.data_source, choices=["mainnet", "testnet", "mock"],
                    help="historical data source (mock = offline synthetic)")
    ap.add_argument("--cash", type=float, default=float(cfg.starting_cash))
    ap.add_argument("--out", default=None, help="equity-curve HTML path")
    ap.add_argument("--no-plot", action="store_true", help="skip the HTML chart")
    args = ap.parse_args(argv)

    symbols = cfg.symbols if args.multi else [args.symbol]

    if args.multi and len(symbols) > 1:
        log.info("multi-symbol backtest: %s, %s days @ %s from %s",
                 symbols, args.days, args.timeframe, args.source)
        symbol_candles = {}
        for sym in symbols:
            candles = load_history(sym, days=args.days, timeframe=args.timeframe,
                                   source=args.source)
            if len(candles) < 2:
                log.error("%s: not enough history (%d bars)", sym, len(candles))
                return 1
            symbol_candles[sym] = candles

        multi = run_multi_backtest(symbol_candles, cfg=cfg, starting_cash=args.cash,
                                   timeframe=args.timeframe)
        for sym, res in multi.results.items():
            print(format_report(res))
        print("\n" + "=" * 48)
        print("  COMBINED PORTFOLIO")
        print("=" * 48)
        for k, v in multi.metrics.items():
            if isinstance(v, float):
                v = f"{v:,.2f}"
            print(f"  {k:<22} {v}")
        print("=" * 48)

        if not args.no_plot:
            out = Path(args.out) if args.out else (Path("runtime") / "backtest_multi.html")
            dummy = BacktestResult(symbol="PORTFOLIO", timeframe=args.timeframe,
                                   starting_cash=args.cash, candles=[],
                                   equity_curve=multi.equity_curve)
            if save_equity_html(dummy, out):
                print(f"\n  equity curve -> {out}")
        return 0

    log.info("loading %s history: %s days @ %s from %s",
             args.symbol, args.days, args.timeframe, args.source)
    candles = load_history(args.symbol, days=args.days, timeframe=args.timeframe,
                           source=args.source)
    if len(candles) < 2:
        log.error("not enough history (%d bars) — try a longer --days or another source",
                  len(candles))
        return 1

    result = run_backtest(args.symbol, candles, cfg=cfg, starting_cash=args.cash,
                          timeframe=args.timeframe)
    print(format_report(result))

    if not args.no_plot:
        out = Path(args.out) if args.out else (
            Path("runtime") / f"backtest_{args.symbol.replace('/', '')}.html")
        if save_equity_html(result, out):
            print(f"\n  equity curve -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
