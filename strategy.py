"""
EMA-Reclaim strategy — exact port of the TypeScript simulation (ema-reclaim-v1).

Signals use CLOSED candles only. The currently forming bar must never be passed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd

STRATEGY_VERSION = "ema-reclaim-v1"


@dataclass
class Signal:
    action: str  # "BUY" | "SELL" | "NO_TRADE"
    reason: str
    atr: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    stop_multiple: float = 1.5
    take_multiple: float = 2.2


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    """Simple EMA matching the JS implementation (k = 2/(span+1))."""
    k = 2.0 / (span + 1)
    out = np.empty_like(values, dtype=float)
    prev = float(values[0]) if len(values) else 0.0
    for i, v in enumerate(values):
        prev = float(v) if i == 0 else float(v) * k + prev * (1 - k)
        out[i] = prev
    return out


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> np.ndarray:
    """Average True Range (SMA of TR), matching the simulation."""
    n = len(close)
    tr = np.zeros(n)
    tr[0] = high[0] - low[0]
    for i in range(1, n):
        tr[i] = max(
            high[i] - low[i],
            abs(high[i] - close[i - 1]),
            abs(low[i] - close[i - 1]),
        )
    out = np.full(n, np.nan)
    if n < period:
        return out
    # rolling SMA
    cs = np.cumsum(tr)
    out[period - 1] = cs[period - 1] / period
    for i in range(period, n):
        out[i] = (cs[i] - cs[i - period]) / period
    return out


def evaluate_signal(df: pd.DataFrame) -> Signal:
    """
    Evaluate the EMA-reclaim signal on a DataFrame of CLOSED candles.

    Required columns: open, high, low, close
    (column names are case-insensitive; we normalise them)
    """
    if df is None or len(df) < 60:
        return Signal("NO_TRADE", "Not enough closed candles.")

    # Normalise column names
    cols = {c.lower(): c for c in df.columns}
    o = df[cols.get("open", "open")].to_numpy(dtype=float)
    h = df[cols.get("high", "high")].to_numpy(dtype=float)
    l = df[cols.get("low", "low")].to_numpy(dtype=float)
    c = df[cols.get("close", "close")].to_numpy(dtype=float)

    fast = _ema(c, 20)
    slow = _ema(c, 50)
    ranges = _atr(h, l, c, 14)

    i = len(c) - 1
    bar_o, bar_h, bar_l, bar_c = o[i], h[i], l[i], c[i]
    prev_c = c[i - 1]
    ema_fast = float(fast[i])
    ema_slow = float(slow[i])
    prev_fast = float(fast[i - 1])
    a = float(ranges[i])

    if not np.isfinite(a) or a <= 0:
        return Signal("NO_TRADE", "ATR is unavailable.", atr=None, ema_fast=ema_fast, ema_slow=ema_slow)

    separation = abs(ema_fast - ema_slow)
    if separation < a * 0.12:
        return Signal(
            "NO_TRADE",
            "No trade. The averages are tangled inside 0.12 ATR.",
            atr=a,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
        )

    bar_range = bar_h - bar_l
    if bar_range > a * 3.2:
        return Signal(
            "NO_TRADE",
            "No trade. The last closed bar is a spike.",
            atr=a,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
        )

    long_trend = ema_fast > ema_slow and bar_c > ema_slow
    short_trend = ema_fast < ema_slow and bar_c < ema_slow
    long_cross = long_trend and prev_c <= prev_fast and bar_c > ema_fast and bar_c > bar_o
    short_cross = short_trend and prev_c >= prev_fast and bar_c < ema_fast and bar_c < bar_o

    if long_cross:
        return Signal(
            "BUY",
            "Bullish stack. Price closed back above the fast average.",
            atr=a,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            stop_multiple=1.5,
            take_multiple=2.2,
        )

    if short_cross:
        return Signal(
            "SELL",
            "Bearish stack. Price closed back below the fast average.",
            atr=a,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            stop_multiple=1.5,
            take_multiple=2.2,
        )

    reason = (
        "No trade. Waiting for a close back through the fast average."
        if (long_trend or short_trend)
        else "No trade. No aligned trend."
    )
    return Signal("NO_TRADE", reason, atr=a, ema_fast=ema_fast, ema_slow=ema_slow)
