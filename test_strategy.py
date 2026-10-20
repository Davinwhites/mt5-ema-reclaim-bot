"""Quick unit test that the Python strategy matches the expected behaviour."""

import numpy as np
import pandas as pd
from strategy import evaluate_signal


def make_df(closes, highs=None, lows=None, opens=None):
    n = len(closes)
    if highs is None:
        highs = [c + 0.0005 for c in closes]
    if lows is None:
        lows = [c - 0.0005 for c in closes]
    if opens is None:
        opens = closes[:]
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes})


def test_not_enough_bars():
    df = make_df([1.10] * 30)
    s = evaluate_signal(df)
    assert s.action == "NO_TRADE"
    assert "Not enough" in s.reason


def test_runs_without_crash():
    # Synthetic trending series with enough bars
    rng = np.random.default_rng(42)
    closes = 1.1000 + np.cumsum(rng.normal(0.00005, 0.0003, 120))
    df = make_df(closes.tolist())
    s = evaluate_signal(df)
    assert s.action in ("BUY", "SELL", "NO_TRADE")
    assert s.atr is not None or s.action == "NO_TRADE"
    print(f"Sample signal: {s.action} | {s.reason}")
    print(f"  ATR={s.atr}  EMA20={s.ema_fast}  EMA50={s.ema_slow}")


if __name__ == "__main__":
    test_not_enough_bars()
    test_runs_without_crash()
    print("All strategy tests passed.")
