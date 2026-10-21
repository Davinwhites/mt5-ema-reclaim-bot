"""Pure open/close decisions. No MetaTrader5 import, so this runs in tests."""

from __future__ import annotations

import math
from typing import Optional

# Stable MetaTrader 5 numeric values.
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

DONE_RETCODES = {10009, 10008, 10010}  # DONE, PLACED, DONE_PARTIAL
RETRY_RETCODES = {10004, 10006, 10007, 10012, 10015, 10020, 10021, 10024, 10031}
INVALID_FILL_RETCODE = 10030
INVALID_STOPS_RETCODE = 10016
ALREADY_CLOSED_RETCODES = {10036, 10039}
MODIFY_OK_RETCODES = DONE_RETCODES | {10025}  # NO_CHANGES

# 50 pips on gold is 0.50 price — wide enough for a normal XAU quote, tight enough to skip news.
GOLD_SPREAD_PIPS = 50.0
FOREX_SPREAD_PIPS = 2.0


def is_gold(symbol: str) -> bool:
    compact = "".join(ch for ch in symbol.upper() if ch.isalnum())
    return "XAU" in compact or "GOLD" in compact


def symbol_candidates(symbol: str) -> list[str]:
    raw = (symbol or "").strip() or "XAUUSD"
    names = [raw]
    if is_gold(raw):
        names.extend(
            [
                "XAUUSD",
                "GOLD",
                "XAUUSDm",
                "XAUUSD.m",
                "XAUUSD.pro",
                "XAUUSD.r",
                "XAUUSD.",
                "XAUUSD.i",
                "GOLD.m",
            ]
        )
    seen: set[str] = set()
    out: list[str] = []
    for name in names:
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def effective_max_spread_pips(symbol: str, configured: Optional[float]) -> float:
    """A forex cap of 2 pips rejects every gold quote. Lift it unless the user set a real gold cap."""
    if is_gold(symbol):
        if configured is None or configured <= 5:
            return GOLD_SPREAD_PIPS
        return float(configured)
    if configured is None:
        return FOREX_SPREAD_PIPS
    return float(configured)


def normalize_price(price: float, digits: int, point: float) -> float:
    if point and point > 0:
        return round(round(price / point) * point, digits)
    return round(price, digits)


def min_stop_distance(stops_level_points: int, freeze_level_points: int, point: float) -> float:
    points = max(int(stops_level_points or 0), int(freeze_level_points or 0))
    if point <= 0 or points <= 0:
        return 0.0
    return points * point


def protective_prices(
    side: str,
    entry: float,
    atr: float,
    stop_multiple: float,
    take_multiple: float,
    min_distance: float,
    digits: int,
    point: float,
) -> tuple[float, float, bool]:
    """SL/TP from ATR, widened only when the broker stop level is further out."""
    raw_stop = atr * stop_multiple
    raw_take = atr * take_multiple
    stop_dist = max(raw_stop, min_distance)
    take_dist = max(raw_take, min_distance)
    tol = (point or 0.0) * 0.5
    widened = stop_dist > raw_stop + tol or take_dist > raw_take + tol
    if side == "BUY":
        sl = entry - stop_dist
        tp = entry + take_dist
    else:
        sl = entry + stop_dist
        tp = entry - take_dist
    return (
        normalize_price(sl, digits, point),
        normalize_price(tp, digits, point),
        widened,
    )


def exit_reason(
    side: str,
    bid: float,
    ask: float,
    sl: float,
    tp: float,
    point: float = 0.0,
) -> Optional[str]:
    """Close check. A buy is sold at the bid; a sell is covered at the ask.

    `point` is only used as a dust tolerance so a float like 2647.0000001
    still counts as a touch. It does not close a point early.
    """
    dust = (point * 1e-4) if point and point > 0 else 1e-9
    if side == "BUY":
        if sl and bid <= sl + dust:
            return "stop"
        if tp and bid >= tp - dust:
            return "take"
    elif side == "SELL":
        if sl and ask >= sl - dust:
            return "stop"
        if tp and ask <= tp + dust:
            return "take"
    return None


def filling_modes(filling_mode_mask: int) -> list[int]:
    """Use what the symbol advertises, and always keep RETURN as a fallback.

    Gold CFDs often reject IOC/FOK with retcode 10030. RETURN is the mode that fills.
    """
    modes: list[int] = []
    mask = int(filling_mode_mask or 0)
    if mask & SYMBOL_FILLING_FOK:
        modes.append(ORDER_FILLING_FOK)
    if mask & SYMBOL_FILLING_IOC:
        modes.append(ORDER_FILLING_IOC)
    if ORDER_FILLING_RETURN not in modes:
        modes.append(ORDER_FILLING_RETURN)
    for mode in (ORDER_FILLING_IOC, ORDER_FILLING_FOK):
        if mode not in modes:
            modes.append(mode)
    return modes


def deviation_points(spread_points: float, stops_level: int) -> int:
    return int(max(50, int(stops_level or 0), math.ceil((spread_points or 0) * 3)))


def _decimals(step: float) -> int:
    if step <= 0 or step >= 1:
        return 2
    text = f"{step:.8f}".rstrip("0")
    if "." not in text:
        return 0
    return min(8, len(text.split(".")[1]))


def volume_from_risk(
    equity: float,
    risk_fraction: float,
    price_risk: float,
    loss_per_lot: float,
    volume_min: float,
    volume_step: float,
    volume_max: float,
    max_risk_multiple: float = 3.0,
) -> tuple[float, str]:
    """Size a full stop to about `risk_fraction` of equity.

    If the broker minimum is larger than that budget but still within
    `max_risk_multiple`, take the minimum so a valid gold signal still opens.
    """
    if equity <= 0 or risk_fraction <= 0 or price_risk <= 0 or loss_per_lot <= 0:
        return 0.0, "Cannot size: missing equity, stop distance, or tick value."
    if volume_step <= 0 or volume_min <= 0 or volume_max <= 0:
        return 0.0, "Broker volume limits are invalid."

    risk_money = equity * risk_fraction
    raw = risk_money / loss_per_lot
    decimals = _decimals(volume_step)
    steps = math.floor((raw / volume_step) + 1e-9)
    volume = round(steps * volume_step, decimals)
    max_steps = math.floor((volume_max / volume_step) + 1e-9)
    capped = round(max_steps * volume_step, decimals)
    if volume > capped:
        volume = capped

    if volume + 1e-12 >= volume_min:
        return volume, "OK"

    min_loss = volume_min * loss_per_lot
    if min_loss <= risk_money * max_risk_multiple:
        return round(volume_min, decimals), (
            f"Broker minimum {volume_min:g} exceeds the risk budget; using it "
            f"(about {min_loss:.2f} vs budget {risk_money:.2f})."
        )
    return 0.0, (
        f"Minimum lot risks {min_loss:.2f}, more than {max_risk_multiple:.0f}x "
        f"the {risk_money:.2f} budget."
    )


def classify_send(retcode: Optional[int]) -> str:
    if retcode in DONE_RETCODES:
        return "done"
    if retcode == INVALID_FILL_RETCODE:
        return "fill"
    if retcode == INVALID_STOPS_RETCODE:
        return "stops"
    if retcode in ALREADY_CLOSED_RETCODES:
        return "closed"
    if retcode in RETRY_RETCODES or retcode is None:
        return "retry"
    return "reject"
