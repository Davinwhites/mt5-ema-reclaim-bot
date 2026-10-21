#!/usr/bin/env python3
"""
EMA-Reclaim MT5 bridge.

Opens on a closed-bar signal and closes the moment price trades through
the stop or the target. Gold symbols are resolved by broker suffix, the
filling mode is negotiated, and a rejected stop does not leave a naked trade.

LIVE_TRADING defaults to false. Nothing is sent until that is set true.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import MetaTrader5 as mt5
import pandas as pd
from dotenv import load_dotenv

from risk import RiskConfig, judge_entry, pip_size
from strategy import STRATEGY_VERSION, Signal, evaluate_signal
from trade_logic import (
    MODIFY_OK_RETCODES,
    classify_send,
    deviation_points,
    effective_max_spread_pips,
    exit_reason,
    filling_modes,
    is_gold,
    min_stop_distance,
    normalize_price,
    protective_prices,
    symbol_candidates,
    volume_from_risk,
)

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("ema-reclaim")

MT5_PATH = os.getenv("MT5_PATH") or None
MT5_LOGIN = int(os.getenv("MT5_LOGIN", "0") or "0")
MT5_PASSWORD = os.getenv("MT5_PASSWORD", "")
MT5_SERVER = os.getenv("MT5_SERVER", "")

SYMBOL_REQUEST = os.getenv("SYMBOL", "XAUUSD").strip() or "XAUUSD"
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

_configured_spread = os.getenv("MAX_SPREAD_PIPS")
CONFIGURED_SPREAD = float(_configured_spread) if _configured_spread not in (None, "") else None

RISK_CFG = RiskConfig(
    risk_fraction=float(os.getenv("RISK_FRACTION", "0.0025")),
    daily_loss_fraction=float(os.getenv("DAILY_LOSS_FRACTION", "0.01")),
    max_drawdown_fraction=float(os.getenv("MAX_DRAWDOWN_FRACTION", "0.03")),
    max_positions=int(os.getenv("MAX_POSITIONS", "1")),
    max_spread_pips=effective_max_spread_pips(SYMBOL_REQUEST, CONFIGURED_SPREAD),
    stop_multiple=float(os.getenv("STOP_MULTIPLE", "1.5")),
    take_multiple=float(os.getenv("TAKE_MULTIPLE", "2.2")),
)

MAGIC = int(os.getenv("MAGIC", "20251020"))
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() in ("1", "true", "yes")
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
BARS_NEEDED = 120
MAX_SENDS_PER_BAR = 4
STATE_PATH = Path(__file__).with_name("runtime_state.json")

# Resolved after connect().
SYMBOL = SYMBOL_REQUEST


# ---------------------------------------------------------------------------
# Runtime state: day equity, and the SL/TP we intended for each ticket
# ---------------------------------------------------------------------------
def _blank_state() -> dict:
    return {"day": "", "day_start_equity": 0.0, "peak_equity": 0.0, "stops": {}}


def load_state() -> dict:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = _blank_state()
    data.setdefault("stops", {})
    return data


def save_state(state: dict) -> None:
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    tmp.replace(STATE_PATH)


def roll_equity(state: dict, equity: float) -> dict:
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("day") != today or not state.get("day_start_equity"):
        state["day"] = today
        state["day_start_equity"] = equity
        log.info("Day start equity set to %.2f (%s UTC)", equity, today)
    state["peak_equity"] = max(float(state.get("peak_equity") or equity), equity)
    save_state(state)
    return state


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def connect() -> bool:
    global SYMBOL
    init_kwargs = {}
    if MT5_PATH:
        init_kwargs["path"] = MT5_PATH
    if not mt5.initialize(**init_kwargs):
        log.error("mt5.initialize() failed: %s", mt5.last_error())
        return False

    if MT5_LOGIN and MT5_PASSWORD and MT5_SERVER:
        if not mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
            log.error("Login failed: %s", mt5.last_error())
            mt5.shutdown()
            return False
        log.info("Logged in to account %s @ %s", MT5_LOGIN, MT5_SERVER)
    else:
        info = mt5.account_info()
        if info is None:
            log.error("No account info — is the terminal logged in?")
            mt5.shutdown()
            return False
        log.info("Using existing terminal session: account %s", info.login)

    resolved = resolve_symbol(SYMBOL_REQUEST)
    if resolved is None:
        log.error("None of these symbols are in the terminal: %s", ", ".join(symbol_candidates(SYMBOL_REQUEST)))
        return False
    SYMBOL = resolved

    info = mt5.account_info()
    mode = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(info.trade_mode, str(info.trade_mode))
    log.info(
        "Connected | %s %s | Balance=%.2f Equity=%.2f | %s | live=%s | max spread=%.1f pips | poll=%.1fs",
        SYMBOL,
        TIMEFRAME_STR,
        info.balance,
        info.equity,
        mode,
        LIVE_TRADING,
        RISK_CFG.max_spread_pips,
        POLL_SECONDS,
    )
    if is_gold(SYMBOL) and CONFIGURED_SPREAD is not None and CONFIGURED_SPREAD <= 5:
        log.warning(
            "MAX_SPREAD_PIPS=%.1f is a forex cap. Using %.1f so gold quotes are not all rejected.",
            CONFIGURED_SPREAD,
            RISK_CFG.max_spread_pips,
        )
    if not is_gold(SYMBOL):
        log.warning("SYMBOL=%s is not gold. Set SYMBOL=XAUUSD (or your broker's gold name) to trade gold.", SYMBOL)
    if info.trade_mode == 2 and LIVE_TRADING:
        log.warning("!!! LIVE TRADING ENABLED ON A REAL ACCOUNT !!!")
    elif not LIVE_TRADING:
        log.warning("LIVE_TRADING=false — signals are tracked, orders are not sent.")
    return True


def resolve_symbol(requested: str) -> Optional[str]:
    for name in symbol_candidates(requested):
        info = mt5.symbol_info(name)
        if info is None:
            continue
        if not info.visible and not mt5.symbol_select(name, True):
            continue
        if mt5.symbol_info(name) is not None:
            if name != requested:
                log.info("Broker gold symbol is %s (requested %s)", name, requested)
            return name
    return None


def ensure_connected() -> bool:
    if mt5.terminal_info() is not None and mt5.account_info() is not None:
        return True
    log.warning("Terminal connection lost — reconnecting")
    try:
        mt5.shutdown()
    except Exception:
        pass
    return connect()


def symbol_info():
    info = mt5.symbol_info(SYMBOL)
    if info is not None and not info.visible:
        mt5.symbol_select(SYMBOL, True)
        info = mt5.symbol_info(SYMBOL)
    return info


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
def fetch_closed_candles(n: int = BARS_NEEDED) -> Optional[pd.DataFrame]:
    rates = mt5.copy_rates_from_pos(SYMBOL, TIMEFRAME, 0, n + 1)
    if rates is None or len(rates) < 60:
        log.warning("Not enough rates: %s", mt5.last_error())
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    # Drop the forming bar. Signals and the strategy only see closed candles.
    return df.iloc[:-1].tail(n).copy()


def spread_pips(info) -> float:
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None or info is None:
        return 999.0
    pip = pip_size(info)
    if pip <= 0:
        return 999.0
    return (tick.ask - tick.bid) / pip


def our_positions():
    positions = mt5.positions_get(symbol=SYMBOL)
    if positions is None:
        return []
    return [p for p in positions if p.magic == MAGIC]


_noted: set[str] = set()


def log_once(key: str, message: str) -> None:
    if key in _noted:
        return
    _noted.add(key)
    log.warning(message)


def side_of(position) -> str:
    return "BUY" if position.type == mt5.POSITION_TYPE_BUY else "SELL"


def loss_per_lot(info, side: str, entry: float, sl: float) -> float:
    tick_value = (
        info.trade_tick_value
        or getattr(info, "trade_tick_value_loss", 0.0)
        or getattr(info, "trade_tick_value_profit", 0.0)
        or 0.0
    )
    tick_size = info.trade_tick_size or 0.0
    if tick_value > 0 and tick_size > 0:
        return abs(entry - sl) / tick_size * tick_value
    order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    profit = mt5.order_calc_profit(order_type, SYMBOL, 1.0, entry, sl)
    if profit is None:
        return 0.0
    return abs(float(profit))


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------
def _send(request: dict):
    result = mt5.order_send(request)
    if result is None:
        log.error("order_send returned None: %s", mt5.last_error())
    return result


def attach_stops(ticket: int, sl: float, tp: float) -> bool:
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": SYMBOL,
        "position": ticket,
        "sl": sl,
        "tp": tp,
        "magic": MAGIC,
    }
    result = _send(request)
    if result is not None and result.retcode in MODIFY_OK_RETCODES:
        return True
    code = None if result is None else result.retcode
    comment = "" if result is None else result.comment
    log.error("Could not attach SL/TP on %s: retcode=%s %s", ticket, code, comment)
    return False


def remember_stops(state: dict, ticket: int, side: str, sl: float, tp: float) -> None:
    state["stops"][str(ticket)] = {"side": side, "sl": sl, "tp": tp}
    save_state(state)


def send_market(info, side: str, volume: float, sl: float, tp: float) -> str:
    """Return done | retry | reject. Tries filling modes, then a naked fill + stop modify."""
    modes = filling_modes(getattr(info, "filling_mode", 0) or 0)
    point = info.point or 10 ** (-info.digits)
    last_kind = "retry"

    for _attempt in range(3):
        if our_positions():
            return "done"
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            time.sleep(0.15)
            continue
        price = tick.ask if side == "BUY" else tick.bid
        price = normalize_price(price, info.digits, point)
        spread_pts = (tick.ask - tick.bid) / point if point else 0
        deviation = deviation_points(spread_pts, getattr(info, "trade_stops_level", 0) or 0)
        order_type = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL

        for filling in modes:
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": SYMBOL,
                "volume": volume,
                "type": order_type,
                "price": price,
                "sl": sl,
                "tp": tp,
                "deviation": deviation,
                "magic": MAGIC,
                "comment": "ema-reclaim",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }
            result = _send(request)
            code = None if result is None else result.retcode
            kind = classify_send(code)
            last_kind = kind
            if kind == "done":
                log.info(
                    "OPEN %s %s vol=%s entry=%s sl=%s tp=%s ticket=%s",
                    side,
                    SYMBOL,
                    volume,
                    price,
                    sl,
                    tp,
                    result.order,
                )
                _confirm_stops(sl, tp)
                return "done"
            if kind == "stops":
                naked = dict(request)
                naked["sl"] = 0.0
                naked["tp"] = 0.0
                naked_result = _send(naked)
                naked_code = None if naked_result is None else naked_result.retcode
                if classify_send(naked_code) == "done":
                    log.info(
                        "OPEN %s %s vol=%s entry=%s ticket=%s (stops attached after fill)",
                        side,
                        SYMBOL,
                        volume,
                        price,
                        naked_result.order,
                    )
                    _confirm_stops(sl, tp)
                    return "done"
                log.error("Order without stops failed: retcode=%s", naked_code)
                return "reject"
            if kind == "fill":
                continue
            comment = "" if result is None else result.comment
            if kind == "retry":
                log.warning("Order retryable retcode=%s %s", code, comment)
                break
            log.error("Order rejected: retcode=%s %s", code, comment)
            return "reject"
        time.sleep(0.2)
    return last_kind if last_kind == "reject" else "retry"


def _confirm_stops(sl: float, tp: float) -> None:
    """If the broker stripped the stops, put them back before the next poll."""
    for position in our_positions():
        if position.sl and position.tp:
            continue
        if attach_stops(position.ticket, sl, tp):
            log.info("Stops confirmed on %s sl=%s tp=%s", position.ticket, sl, tp)


def close_position(info, position, reason: str, sl: float, tp: float) -> str:
    side = side_of(position)
    modes = filling_modes(getattr(info, "filling_mode", 0) or 0)
    point = info.point or 10 ** (-info.digits)
    order_type = mt5.ORDER_TYPE_SELL if side == "BUY" else mt5.ORDER_TYPE_BUY

    for _attempt in range(3):
        tick = mt5.symbol_info_tick(SYMBOL)
        if tick is None:
            time.sleep(0.1)
            continue
        price = tick.bid if side == "BUY" else tick.ask
        price = normalize_price(price, info.digits, point)
        spread_pts = (tick.ask - tick.bid) / point if point else 0
        deviation = deviation_points(spread_pts, getattr(info, "trade_stops_level", 0) or 0)
        for filling in modes:
            request = {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": SYMBOL,
                "position": position.ticket,
                "volume": position.volume,
                "type": order_type,
                "price": price,
                "deviation": deviation,
                "magic": MAGIC,
                "comment": f"x-{reason}"[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": filling,
            }
            result = _send(request)
            code = None if result is None else result.retcode
            kind = classify_send(code)
            if kind in ("done", "closed"):
                log.info(
                    "CLOSE %s %s ticket=%s reason=%s px=%s sl=%s tp=%s",
                    side,
                    SYMBOL,
                    position.ticket,
                    reason,
                    price,
                    sl,
                    tp,
                )
                return "done"
            if kind == "fill":
                continue
            comment = "" if result is None else result.comment
            if kind == "retry":
                log.warning("Close retryable retcode=%s %s", code, comment)
                break
            log.error("Close rejected: retcode=%s %s", code, comment)
            return "reject"
        time.sleep(0.15)
    return "retry"


def levels_for(state: dict, position, info, atr: float) -> tuple[float, float]:
    """Broker stops if present, else the stops we stored, else a fresh ATR stop."""
    if position.sl and position.tp:
        return float(position.sl), float(position.tp)
    stored = state["stops"].get(str(position.ticket))
    if stored and stored.get("sl") and stored.get("tp"):
        sl, tp = float(stored["sl"]), float(stored["tp"])
        if not position.sl or not position.tp:
            attach_stops(position.ticket, sl, tp)
        return sl, tp
    point = info.point or 10 ** (-info.digits)
    floor = min_stop_distance(
        getattr(info, "trade_stops_level", 0) or 0,
        getattr(info, "trade_freeze_level", 0) or 0,
        point,
    )
    sl, tp, _ = protective_prices(
        side_of(position),
        float(position.price_open),
        atr if atr and atr > 0 else floor or point * 10,
        RISK_CFG.stop_multiple,
        RISK_CFG.take_multiple,
        floor,
        info.digits,
        point,
    )
    log.warning("Position %s had no stops — attaching sl=%s tp=%s", position.ticket, sl, tp)
    attach_stops(position.ticket, sl, tp)
    remember_stops(state, position.ticket, side_of(position), sl, tp)
    return sl, tp


# ---------------------------------------------------------------------------
# One pass
# ---------------------------------------------------------------------------
class Shadow:
    """Dry-run position so a stop/target still produces a close in the log."""

    def __init__(self, side: str, volume: float, entry: float, sl: float, tp: float):
        self.side = side
        self.volume = volume
        self.entry = entry
        self.sl = sl
        self.tp = tp


def manage_exits(state: dict, info, atr: float, shadow: Optional[Shadow]) -> Optional[Shadow]:
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None or info is None:
        return shadow
    point = info.point or 10 ** (-info.digits)

    if shadow is not None:
        reason = exit_reason(shadow.side, tick.bid, tick.ask, shadow.sl, shadow.tp, point)
        if reason:
            log.info(
                "CLOSE %s %s ticket=dry-run reason=%s px=%s sl=%s tp=%s",
                shadow.side,
                SYMBOL,
                reason,
                normalize_price(tick.bid if shadow.side == "BUY" else tick.ask, info.digits, point),
                shadow.sl,
                shadow.tp,
            )
            shadow = None

    live_tickets = set()
    for position in our_positions():
        live_tickets.add(str(position.ticket))
        sl, tp = levels_for(state, position, info, atr)
        reason = exit_reason(side_of(position), tick.bid, tick.ask, sl, tp, point)
        if not reason:
            continue
        if not LIVE_TRADING:
            log_once(
                f"dry-exit-{position.ticket}-{reason}",
                f"Exit {reason} on ticket {position.ticket}, but LIVE_TRADING=false so it was not sent.",
            )
            continue
        if close_position(info, position, reason, sl, tp) == "done":
            state["stops"].pop(str(position.ticket), None)
            save_state(state)

    # Drop stored stops for tickets the broker already closed.
    stale = [ticket for ticket in state["stops"] if ticket not in live_tickets]
    if stale:
        for ticket in stale:
            state["stops"].pop(ticket, None)
        save_state(state)
    return shadow


def place_order(state: dict, signal: Signal, info) -> tuple[str, Optional[Shadow]]:
    """Return (done|retry|reject|flat, shadow)."""
    tick = mt5.symbol_info_tick(SYMBOL)
    if tick is None or info is None:
        return "retry", None

    side = signal.action
    point = info.point or 10 ** (-info.digits)
    entry = normalize_price(tick.ask if side == "BUY" else tick.bid, info.digits, point)
    atr = float(signal.atr or 0.0)
    if atr <= 0:
        log.error("ATR missing — cannot size the trade")
        return "reject", None

    floor = min_stop_distance(
        getattr(info, "trade_stops_level", 0) or 0,
        getattr(info, "trade_freeze_level", 0) or 0,
        point,
    )
    sl, tp, widened = protective_prices(
        side,
        entry,
        atr,
        RISK_CFG.stop_multiple,
        RISK_CFG.take_multiple,
        floor,
        info.digits,
        point,
    )
    if widened:
        log.info("Stop widened to the broker minimum (stops level %s points)", info.trade_stops_level)

    per_lot = loss_per_lot(info, side, entry, sl)
    volume, size_reason = volume_from_risk(
        equity=mt5.account_info().equity,
        risk_fraction=RISK_CFG.risk_fraction,
        price_risk=abs(entry - sl),
        loss_per_lot=per_lot,
        volume_min=info.volume_min,
        volume_step=info.volume_step,
        volume_max=min(info.volume_max, 5.0),
    )
    if size_reason != "OK":
        log.info("Size: %s", size_reason)
    if volume <= 0:
        log.info("Entry rejected: %s", size_reason)
        return "reject", None

    account = mt5.account_info()
    equity = account.equity
    estimated_loss = per_lot * volume
    verdict = judge_entry(
        equity=equity,
        free_margin=account.margin_free,
        day_start_equity=state["day_start_equity"],
        peak_equity=state["peak_equity"],
        open_positions=len(our_positions()),
        spread_pips=spread_pips(info),
        cfg=RISK_CFG,
        live_trading=LIVE_TRADING,
        emergency_halt=False,
        volume=volume,
        estimated_loss=estimated_loss,
    )
    if not verdict.ok:
        log.info("Entry rejected: %s", verdict.reason)
        return "reject", None

    log.info(
        "SIGNAL %s %s | entry=%s sl=%s tp=%s vol=%s risk≈%.2f | %s",
        side,
        SYMBOL,
        entry,
        sl,
        tp,
        volume,
        estimated_loss,
        signal.reason,
    )

    if not LIVE_TRADING:
        log.warning("LIVE_TRADING=false → order NOT sent. Tracking the level so the close still prints.")
        return "done", Shadow(side, volume, entry, sl, tp)

    kind = send_market(info, side, volume, sl, tp)
    if kind == "done":
        for position in our_positions():
            remember_stops(state, position.ticket, side, sl, tp)
    return kind, None


def on_closed_bar(state: dict, df: pd.DataFrame, info) -> tuple[str, Optional[Shadow]]:
    signal = evaluate_signal(df)
    bar = df.iloc[-1]["time"].strftime("%Y-%m-%d %H:%M")
    log.info(
        "Bar %s | action=%-8s | ATR=%.5f EMA20=%.5f EMA50=%.5f | %s",
        bar,
        signal.action,
        signal.atr or 0,
        signal.ema_fast or 0,
        signal.ema_slow or 0,
        signal.reason,
    )
    if signal.action not in ("BUY", "SELL"):
        return "flat", None
    if our_positions():
        log.info("Already in a position — entry skipped.")
        return "flat", None
    return place_order(state, signal, info)


def latest_atr(df: Optional[pd.DataFrame]) -> float:
    if df is None:
        return 0.0
    signal = evaluate_signal(df)
    return float(signal.atr or 0.0)


def main():
    log.info(
        "Starting EMA-Reclaim %s | requested=%s tf=%s live=%s",
        STRATEGY_VERSION,
        SYMBOL_REQUEST,
        TIMEFRAME_STR,
        LIVE_TRADING,
    )
    if not connect():
        sys.exit(1)

    state = load_state()
    last_closed: Optional[int] = None
    pending: Optional[int] = None
    attempts = 0
    shadow: Optional[Shadow] = None
    log.info("Running. Entries on closed bars, exits on every poll. Ctrl+C to stop.")

    try:
        while True:
            started = time.monotonic()
            if not ensure_connected():
                time.sleep(2)
                continue

            info = symbol_info()
            account = mt5.account_info()
            if info is None or account is None:
                log.warning("Symbol or account unavailable")
                time.sleep(POLL_SECONDS)
                continue
            state = roll_equity(state, account.equity)

            df = fetch_closed_candles()
            atr = latest_atr(df)
            if shadow is not None or our_positions():
                shadow = manage_exits(state, info, atr, shadow)

            if df is not None and len(df) >= 60:
                bar_time = int(df.iloc[-1]["time"].timestamp())
                if bar_time != last_closed and bar_time != pending:
                    pending = bar_time
                    attempts = 0
                if pending is not None and shadow is None and not our_positions():
                    if attempts < MAX_SENDS_PER_BAR:
                        attempts += 1
                        outcome, new_shadow = on_closed_bar(state, df, symbol_info() or info)
                        if new_shadow is not None:
                            shadow = new_shadow
                        if outcome == "retry":
                            log.info("Entry not confirmed — retry %s/%s", attempts, MAX_SENDS_PER_BAR)
                        else:
                            last_closed = pending
                            pending = None
                    else:
                        log.error("Gave up on this bar after %s send attempts", MAX_SENDS_PER_BAR)
                        last_closed = pending
                        pending = None
                elif pending is not None:
                    log.info("In a position — bar noted, no second entry.")
                    last_closed = pending
                    pending = None
            else:
                time.sleep(POLL_SECONDS)
                continue

            # Exits are checked again after an entry so a fill is not left for a full poll.
            if shadow is not None or our_positions():
                shadow = manage_exits(state, symbol_info() or info, atr, shadow)

            remaining = POLL_SECONDS - (time.monotonic() - started)
            time.sleep(remaining if remaining > 0 else 0.05)
    except KeyboardInterrupt:
        log.info("Stopped by user.")
    finally:
        mt5.shutdown()
        log.info("MT5 connection closed.")


if __name__ == "__main__":
    main()
