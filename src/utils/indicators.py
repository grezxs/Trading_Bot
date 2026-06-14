"""Hand-rolled technical indicators (numpy only).

Each function takes a 1-D price series (list or ndarray) and returns the
indicator's *latest* value (a float), or a small tuple for multi-line
indicators. They return NaN when there is not enough history so callers can
guard with ``math.isnan``. Can later be swapped for pandas-ta / TA-Lib
behind the same signatures.
"""
from __future__ import annotations

import math
from typing import Sequence

import numpy as np


def _arr(prices: Sequence[float]) -> np.ndarray:
    return np.asarray(prices, dtype=float)


def moving_average(prices: Sequence[float], window: int) -> float:
    p = _arr(prices)
    if p.size < window or window <= 0:
        return math.nan
    return float(p[-window:].mean())


def zscore(prices: Sequence[float], window: int) -> float:
    """Z-score of the last price against a trailing window."""
    p = _arr(prices)
    if p.size < window or window <= 1:
        return math.nan
    w = p[-window:]
    sd = w.std(ddof=1)
    if sd == 0:
        return 0.0
    return float((w[-1] - w.mean()) / sd)


def rsi(prices: Sequence[float], window: int = 14) -> float:
    """Wilder's RSI in [0, 100]."""
    p = _arr(prices)
    if p.size < window + 1:
        return math.nan
    delta = np.diff(p[-(window + 1):])
    gains = np.clip(delta, 0, None)
    losses = -np.clip(delta, None, 0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100.0 - 100.0 / (1.0 + rs))


def bollinger(prices: Sequence[float], window: int = 20, num_std: float = 2.0):
    """Return (lower, mid, upper) Bollinger bands for the latest bar."""
    p = _arr(prices)
    if p.size < window:
        return (math.nan, math.nan, math.nan)
    w = p[-window:]
    mid = w.mean()
    sd = w.std(ddof=1)
    return (float(mid - num_std * sd), float(mid), float(mid + num_std * sd))


def _ema(p: np.ndarray, span: int) -> np.ndarray:
    """Exponential moving average over the whole series."""
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(p)
    out[0] = p[0]
    for i in range(1, p.size):
        out[i] = alpha * p[i] + (1 - alpha) * out[i - 1]
    return out


def macd(prices: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    """Return (macd_line, signal_line, histogram) for the latest bar."""
    p = _arr(prices)
    if p.size < slow + signal:
        return (math.nan, math.nan, math.nan)
    macd_line = _ema(p, fast) - _ema(p, slow)
    signal_line = _ema(macd_line, signal)
    hist = macd_line - signal_line
    return (float(macd_line[-1]), float(signal_line[-1]), float(hist[-1]))


def realized_vol(prices: Sequence[float], window: int) -> float:
    """Std-dev of log returns over the window (per-bar volatility)."""
    p = _arr(prices)
    if p.size < window + 1:
        return math.nan
    rets = np.diff(np.log(p[-(window + 1):]))
    return float(rets.std(ddof=1))
