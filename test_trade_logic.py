"""Decisions that decide whether a gold trade actually opens and closes."""

from trade_logic import (
    ORDER_FILLING_FOK,
    ORDER_FILLING_IOC,
    ORDER_FILLING_RETURN,
    classify_send,
    effective_max_spread_pips,
    exit_reason,
    filling_modes,
    is_gold,
    protective_prices,
    symbol_candidates,
    volume_from_risk,
)


def test_gold_symbol_detection():
    assert is_gold("XAUUSD")
    assert is_gold("XAUUSDm")
    assert is_gold("GOLD.m")
    assert not is_gold("EURUSD")
    assert symbol_candidates("XAUUSD.m")[0] == "XAUUSD.m"
    assert "XAUUSD" in symbol_candidates("GOLD")


def test_forex_spread_cap_does_not_block_gold():
    # 2.0 is the old EURUSD default. On gold that rejects every quote.
    assert effective_max_spread_pips("XAUUSD", 2.0) == 50.0
    assert effective_max_spread_pips("XAUUSD", None) == 50.0
    # An explicit gold cap is kept.
    assert effective_max_spread_pips("XAUUSD", 80.0) == 80.0
    assert effective_max_spread_pips("EURUSD", 2.0) == 2.0


def test_stops_widen_to_broker_minimum():
    sl, tp, widened = protective_prices(
        side="BUY",
        entry=2650.00,
        atr=2.0,
        stop_multiple=1.5,
        take_multiple=2.2,
        min_distance=0.50,  # wider than 1.5 * 2.0 = 3? no, 3.0 > 0.50
        digits=2,
        point=0.01,
    )
    assert widened is False
    assert sl == 2647.00
    assert tp == 2654.40

    sl, tp, widened = protective_prices(
        side="SELL",
        entry=2650.00,
        atr=0.20,
        stop_multiple=1.5,
        take_multiple=2.2,
        min_distance=0.50,
        digits=2,
        point=0.01,
    )
    assert widened is True
    assert sl == 2650.50
    assert tp == 2649.50


def test_exit_uses_the_price_we_can_actually_hit():
    # Buy stop is on the bid. Ask poking the stop must not close it early.
    assert exit_reason("BUY", bid=2647.01, ask=2647.20, sl=2647.00, tp=2654.40, point=0.01) is None
    assert exit_reason("BUY", bid=2647.00, ask=2647.20, sl=2647.00, tp=2654.40, point=0.01) == "stop"
    assert exit_reason("BUY", bid=2654.40, ask=2654.60, sl=2647.00, tp=2654.40, point=0.01) == "take"
    # Sell stop is on the ask.
    assert exit_reason("SELL", bid=2650.20, ask=2650.49, sl=2650.50, tp=2640.00, point=0.01) is None
    assert exit_reason("SELL", bid=2650.40, ask=2650.50, sl=2650.50, tp=2640.00, point=0.01) == "stop"
    assert exit_reason("SELL", bid=2639.80, ask=2640.00, sl=2650.50, tp=2640.00, point=0.01) == "take"
    # Stop wins if both somehow print.
    assert exit_reason("BUY", bid=1.0, ask=1.0, sl=2647.0, tp=2654.0, point=0.01) == "stop"


def test_filling_mode_always_includes_return():
    assert filling_modes(0)[0] == ORDER_FILLING_RETURN
    modes = filling_modes(2)  # IOC advertised
    assert modes[0] == ORDER_FILLING_IOC
    assert ORDER_FILLING_RETURN in modes
    assert ORDER_FILLING_FOK in modes


def test_volume_opens_min_lot_but_not_a_blowup():
    # Budget wants ~0.02. Step floors cleanly.
    volume, reason = volume_from_risk(
        equity=10_000,
        risk_fraction=0.0025,
        price_risk=9.0,
        loss_per_lot=900.0,  # $900 per 1.00 lot
        volume_min=0.01,
        volume_step=0.01,
        volume_max=5.0,
    )
    assert volume == 0.02
    assert reason == "OK"

    # Budget wants 0.004, min lot is 0.01 and costs less than 3x budget → still open.
    volume, reason = volume_from_risk(
        equity=2_000,
        risk_fraction=0.0025,  # $5 budget
        price_risk=6.0,
        loss_per_lot=600.0,  # 0.01 lot = $6
        volume_min=0.01,
        volume_step=0.01,
        volume_max=5.0,
    )
    assert volume == 0.01
    assert "minimum" in reason.lower()

    # Min lot would risk far past the account → do not open.
    volume, _ = volume_from_risk(
        equity=200,
        risk_fraction=0.0025,  # $0.50 budget
        price_risk=10.0,
        loss_per_lot=1000.0,  # 0.01 lot = $10
        volume_min=0.01,
        volume_step=0.01,
        volume_max=5.0,
    )
    assert volume == 0.0


def test_retcode_classes():
    assert classify_send(10009) == "done"
    assert classify_send(10030) == "fill"
    assert classify_send(10016) == "stops"
    assert classify_send(10004) == "retry"
    assert classify_send(10015) == "retry"
    assert classify_send(10020) == "retry"
    assert classify_send(None) == "retry"
    assert classify_send(10036) == "closed"
    assert classify_send(10019) == "reject"


if __name__ == "__main__":
    test_gold_symbol_detection()
    test_forex_spread_cap_does_not_block_gold()
    test_stops_widen_to_broker_minimum()
    test_exit_uses_the_price_we_can_actually_hit()
    test_filling_mode_always_includes_return()
    test_volume_opens_min_lot_but_not_a_blowup()
    test_retcode_classes()
    print("All trade-logic tests passed.")
