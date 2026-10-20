#!/usr/bin/env python3
"""
EMA-Reclaim MT5 Bridge Bot
--------------------------
Connects to a running MetaTrader 5 terminal, evaluates the same strategy
used in the simulation, and (optionally) places real orders.

IMPORTANT SAFETY
- Start with LIVE_TRADING=false and a DEMO account.
- Only set LIVE_TRADING=true after extensive forward testing on demo.
- The bot only manages positions that carry its MAGIC number.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

import MetaTrader5 as mt5
import pandas as pd
from dotenv import load_dotenv

from risk import RiskConfig, calculate_volume, judge_entry, pip_size
from strategy import STRATEGY_VERSION, Signal, evaluate_signal

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ema-reclaim")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
MT5_PATH = os.getenv("MT5_PATH") or None
MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")

SYMBOL = os.getenv("SYMBOL", "EURUSD")
TIMEFRAME_STR = os.getenv("TIMEFRAME", "M15").upper()
TIMEFRAME_MAP = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}
TIMEFRAME = TIMEFRAME_MAP.get(TIMEFRAME_STR, mt5.TIMEFRAME_M15)

RISK_CFG = RiskConfig(
    risk_fraction=float(os.getenv("RISK_FRACTION", "0.0025")),
    daily_loss_fraction=float(os.getenv("DAILY_LOSS_FRACTION", "0.01")),
    max_drawdown_fraction=float(os.getenv("MAX_DRAWDOWN_FRACTION", "0.03")),
    max_positions=int(os.getenv("MAX_POSITIONS", "1")),
    max_spread_pips=float(os.getenv("MAX_SPREAD_PIPS", "2.0")),
    stop_multiple=float(os.getenv("STOP_MULTIPLE", "1.5")),
    take_multiple=float(os.getenv("TAKE_MULTIPLE", "2.2")),
)

MAGIC = int(os.getenv("MAGIC", "20251020"))
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() in ("1", "true", "yes")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "5"))

# Bars needed for the strategy
BARS_NEEDED = 120


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def connect() -> bool:
    """Initialize and log in to MT5."""
    init_kwargs = {}
    if MT5_PATH:
        init_kwargs["path"] = MT5_PATH
    if not mt5.initialize(**init_kwargs):
        log.error("mt5.initialize() failed: %s", mt5.last_error())
        return False

    if MT5_LOGIN and MT5_PASSWORD and MT5_SERVER:
        authorized = mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER)
        if not authorized:
            log.error("Login failed: %s", mt5.last_error())
            mt5.shutdown()
            return False
        log.info("Logged in to account %s @ %s", MT5_LOGIN, MT5_SERVER)
    else:
        # Use the already-open terminal session
        info = mt5.account_info()
        if info is None:
            log.error("No account info — is the terminal logged in?")
            mt5.shutdown()
            return False
        log.info("Using existing terminal session: account %s", info.login)

    # Ensure symbol is visible
    if not mt5.symbol_select(SYMBOL, True):
        log.error("Failed to select symbol %s", SYMBOL)
        return False

    info = mt5.account_info()
    log.info(
        "Connected | Balance=%.2f Equity=%.2f | Server=%s | Trade mode=%s",
        info.balance,
        info.equity,
        info.server,
        "DEMO" if info.trade_mode == 0 else "REAL" if info.trade_mode == 2 else str(info.trade_mode),
    )
    if info.trade_mode == 2 and not LIVE_TRADING:
        log.warning("REAL account detected but LIVE_TRADING=false — orders will be simulated only.")
    if info.trade_mode == 2 and LIVE_TRADING:
        log.warning("!!! LIVE TRADING ENABLED ON A REAL ACCOUNT !!!")
    return True


def fetch_closed_candles(n: int = BARS_NEEDED) -> Optional[pd.DataFrame]:
    """Return the last `n` *closed* candles (excludes the currently forming bar)."""
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, n + 1)
    if rates is None or len(rates) < n:
        log.warning("Not enough rates: %s", mt5.last_error())
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    # Drop the last (forming) bar so strategy only sees closed candles
    df = df.iloc[:-1].copy()
    df = df.rename(columns={"open": "open", "high": "high", "low": "low", "close": "close"})
    return df.tail(n)


def current_spread_pips(symbol_info) -> float:
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        return 999.0
    pip = pip_size(symbol_info)
    return (tick.ask - tick.bid) / pip


def our_positions():
    """Positions opened by this bot (matching MAGIC)."""
    positions = mt5.positions_get(symbol=SYMBOL)
    if positions is None:
        return []
    return [p for p in positions if p.magic == MAGIC]


def day_start_equity() -> float:
    """Approximate day-start equity from account (simple version)."""
    info = mt5.account_info()
    if info is None:
        return 0.0
    # For production you may want to persist this in a file or DB at midnight
    return info.balance  # fallback; refine with deal history if needed


def place_order(signal: Signal, symbol_info) -> bool:
    """Calculate size, SL/TP and send a market order (or log it if not live)."""
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None:
        log.error("No tick data")
        return False

    side = signal.action
    entry = tick.ask if side == "BUY" else tick.bid
    atr = signal.atr or 0.0
    if atr <= 0:
        log.error("ATR missing — cannot size trade")
        return False

    stop_dist = atr * RISK_CFG.stop_multiple
    take_dist = atr * RISK_CFG.take_multiple

    if side == "BUY":
        sl = entry - stop_dist
        tp = entry + take_dist
        order_type = mt5.ORDER_TYPE_BUY
    else:
        sl = entry + stop_dist
        tp = entry - take_dist
        order_type = mt5.ORDER_TYPE_SELL

    # Round to tick size / digits
    digits = symbol_info.digits
    entry = round(entry, digits)
    sl = round(sl, digits)
    tp = round(tp, digits)

    info = mt5.account_info()
    equity = info.equity
    free_margin = info.margin_free

    volume = calculate_volume(
        equity=equity,
        risk_fraction=RISK_CFG.risk_fraction,
        entry=entry,
        stop=sl,
        tick_value=symbol_info.trade_tick_value,
        tick_size=symbol_info.trade_tick_size,
        volume_min=symbol_info.volume_min,
        volume_step=symbol_info.volume_step,
        volume_max=min(symbol_info.volume_max, 5.0),
    )

    estimated_loss = equity * RISK_CFG.risk_fraction
    spread = current_spread_pips(symbol_info)

    verdict = judge_entry(
        equity=equity,
        free_margin=free_margin,
        day_start_equity=day_start_equity(),
        peak_equity=max(equity, info.balance),  # simple peak
        open_positions=len(our_positions()),
        spread_pips=spread,
        cfg=RISK_CFG,
        live_trading=LIVE_TRADING,
        emergency_halt=False,
        volume=volume,
        estimated_loss=estimated_loss,
    )

    if not verdict.ok:
        log.info("Entry rejected: %s", verdict.reason)
        return False

    log.info(
        "SIGNAL %s | entry=%.5f SL=%.5f TP=%.5f | vol=%.2f | risk≈%.2f | %s",
        side,
        entry,
        sl,
        tp,
        volume,
        estimated_loss,
        signal.reason,
    )

    if not LIVE_TRADING:
        log.warning("LIVE_TRADING=false → order NOT sent (dry-run).")
        return True

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": volume,
        "type": order_type,
        "price": entry,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": MAGIC,
        "comment": f"ema-reclaim-{STRATEGY_VERSION}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None:
        log.error("order_send returned None: %s", mt5.last_error())
        return False
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error("Order failed: retcode=%s  comment=%s", result.retcode, result.comment)
        return False

    log.info("Order placed successfully | ticket=%s  volume=%.2f", result.order, volume)
    return True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
def main():
    log.info("Starting EMA-Reclaim bot  strategy=%s  symbol=%s  tf=%s  live=%s",
             STRATEGY_VERSION, SYMBOL, TIMEFRAME_STR, LIVE_TRADING)

    if not connect():
        sys.exit(1)

    symbol_info = mt5.symbol_info(SYMBOL)
    if symbol_info is None:
        log.error("Symbol info unavailable for %s", SYMBOL)
        mt5.shutdown()
        sys.exit(1)

    last_bar_time: Optional[int] = None
    log.info("Bot running. Waiting for new closed bars… (Ctrl+C to stop)")

    try:
        while True:
            df = fetch_closed_candles()
            if df is None or len(df) < 60:
                time.sleep(POLL_SECONDS)
                continue

            # Detect a newly closed bar
            current_bar_time = int(df.iloc[-1]["time"].timestamp())
            if last_bar_time is not None and current_bar_time == last_bar_time:
                time.sleep(POLL_SECONDS)
                continue

            last_bar_time = current_bar_time
            bar_time_str = df.iloc[-1]["time"].strftime("%Y-%m-%d %H:%M")

            signal = evaluate_signal(df)
            log.info(
                "Bar %s | action=%-8s | ATR=%.5f  EMA20=%.5f  EMA50=%.5f | %s",
                bar_time_str,
                signal.action,
                signal.atr or 0,
                signal.ema_fast or 0,
                signal.ema_slow or 0,
                signal.reason,
            )

            if signal.action in ("BUY", "SELL"):
                # Only enter if we have no open position for this magic
                if our_positions():
                    log.info("Already in a position — skipping new signal.")
                else:
                    place_order(signal, symbol_info)

            time.sleep(POLL_SECONDS)

    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        mt5.shutdown()
        log.info("MT5 connection closed.")


if __name__ == "__main__":
    main()
