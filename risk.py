"""
Risk management helpers — ported and adapted from the simulation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class RiskConfig:
    risk_fraction: float = 0.0025          # 0.25% of equity per trade
    daily_loss_fraction: float = 0.01      # 1% daily loss limit
    max_drawdown_fraction: float = 0.03    # 3% from peak
    max_positions: int = 1
    max_spread_pips: float = 2.0
    stop_multiple: float = 1.5
    take_multiple: float = 2.2


@dataclass
class EntryVerdict:
    ok: bool
    volume: float = 0.0
    reason: str = ""
    risk_budget: float = 0.0
    estimated_loss: float = 0.0


def pip_size(symbol_info) -> float:
    """Return the pip size for the symbol (handles JPY and metals)."""
    digits = symbol_info.digits
    point = symbol_info.point
    # Most brokers: 5-digit forex → pip = 10 * point; 3-digit JPY → pip = 10 * point
    if digits in (3, 5):
        return point * 10
    return point


def calculate_volume(
    equity: float,
    risk_fraction: float,
    entry: float,
    stop: float,
    tick_value: float,
    tick_size: float,
    volume_min: float,
    volume_step: float,
    volume_max: float,
) -> float:
    """
    Position size so that a full stop-loss ≈ risk_fraction * equity.
    Uses broker tick_value / tick_size for accurate money risk.
    """
    if entry <= 0 or stop <= 0 or tick_size <= 0 or tick_value <= 0:
        return 0.0

    price_risk = abs(entry - stop)
    if price_risk <= 0:
        return 0.0

    risk_money = equity * risk_fraction
    # money lost per 1.0 lot for this price move
    loss_per_lot = (price_risk / tick_size) * tick_value
    if loss_per_lot <= 0:
        return 0.0

    raw = risk_money / loss_per_lot
    # round down to volume_step
    steps = int(raw / volume_step)
    volume = max(volume_min, steps * volume_step)
    volume = min(volume, volume_max)
    # final safety: never below min
    if volume < volume_min:
        return 0.0
    return round(volume, 2)


def judge_entry(
    *,
    equity: float,
    free_margin: float,
    day_start_equity: float,
    peak_equity: float,
    open_positions: int,
    spread_pips: float,
    cfg: RiskConfig,
    live_trading: bool,
    emergency_halt: bool,
    volume: float,
    estimated_loss: float,
) -> EntryVerdict:
    """Final go / no-go before sending an order."""

    if emergency_halt:
        return EntryVerdict(False, reason="Emergency halt is active. New entries blocked.")

    if not live_trading:
        # Still allow the call so the bot can log what it *would* do,
        # but the main loop must respect LIVE_TRADING=false.
        pass

    if open_positions >= cfg.max_positions:
        return EntryVerdict(False, reason=f"Open-position limit reached ({cfg.max_positions}).")

    if spread_pips > cfg.max_spread_pips:
        return EntryVerdict(
            False,
            reason=f"Spread {spread_pips:.1f} pips > max {cfg.max_spread_pips:.1f}.",
        )

    day_pnl = equity - day_start_equity
    day_limit = day_start_equity * cfg.daily_loss_fraction
    if day_pnl <= -day_limit:
        return EntryVerdict(False, reason=f"Daily loss limit hit ({day_pnl:.2f} <= -{day_limit:.2f}).")

    dd = (peak_equity - equity) / peak_equity if peak_equity > 0 else 0.0
    if dd >= cfg.max_drawdown_fraction:
        return EntryVerdict(False, reason=f"Max drawdown reached ({dd*100:.1f}%).")

    if volume <= 0:
        return EntryVerdict(False, reason="Calculated volume is zero or invalid.")

    # rough free-margin check (broker will enforce exact)
    if free_margin < estimated_loss * 1.5:
        return EntryVerdict(False, reason="Insufficient free margin for safety buffer.")

    return EntryVerdict(
        ok=True,
        volume=volume,
        reason="OK",
        risk_budget=equity * cfg.risk_fraction,
        estimated_loss=estimated_loss,
    )
