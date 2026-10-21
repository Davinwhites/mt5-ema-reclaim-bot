# EMA-Reclaim MT5 Live Bridge

Real-market Python service that:

- Connects to a running **MetaTrader 5** terminal via the official `MetaTrader5` package
- Runs the exact **EMA-Reclaim v1** strategy (EMA 20/50 + ATR filters) on **gold**
- Opens a market order when a closed bar signals, and closes it the moment price trades through the stop or the target
- Sizes the position from risk %, with the broker's minimum lot used only when it is still close to that risk
- Is meant to run 24/7 on a Windows VPS next to the MT5 terminal

---

## Strategy summary

| Rule | Detail |
|------|--------|
| Instrument | Gold. `SYMBOL=XAUUSD`, with broker suffixes tried automatically (`XAUUSDm`, `GOLD`, `XAUUSD.m`, …) |
| Indicators | EMA(20), EMA(50), ATR(14) |
| Min history | 60 closed candles |
| Filter | EMA separation ≥ 0.12 × ATR |
| Filter | Last bar range ≤ 3.2 × ATR (spike filter) |
| BUY | Fast > Slow, close > Slow, previous close ≤ prev Fast, close > Fast, bullish candle |
| SELL | Fast < Slow, close < Slow, previous close ≥ prev Fast, close < Fast, bearish candle |
| Stop | 1.5 × ATR, widened only if the broker stop level is further |
| Take | 2.2 × ATR |
| Risk | 0.25 % of equity per trade (configurable) |
| Limits | Max 1 position, daily loss 1 %, max DD 3 %, gold spread cap |

Signals are evaluated **only on closed bars**. Exits are checked on every poll (default 1 second) against the price the close would actually fill: bid for a buy, ask for a sell.

---

## What makes the order actually go through

Gold brokers reject the old forex defaults. The bot now handles that:

- **Spread.** A 2-pip forex cap blocks every XAU quote. Gold defaults to 50 pips (about 0.50 of price). A configured cap of 5 or below is lifted automatically on gold.
- **Filling mode.** The symbol's advertised mode is tried first, then `RETURN`, which is what most gold CFDs accept. A `10030` invalid-fill answer tries the next mode instead of dropping the signal.
- **Stops.** If the broker stop level is wider than 1.5 × ATR, the stop is pushed out and the size is recalculated. If the order is still rejected for stops (`10016`), the bot fills without stops and attaches them immediately after.
- **Requotes.** Price-changed / requote / off-quotes are retried with a fresh tick, up to 4 times on that bar. The bar is not marked done until the order is filled or permanently rejected.
- **Closes.** Every poll, an open position is closed at market when the stop or target is touched. A missing stop is put back on the position, including after a restart (`runtime_state.json`). A broker stop can still close it; a second close is treated as already done.
- **Naked tickets.** A position with no stop gets an ATR stop attached so it cannot sit unprotected.

`LIVE_TRADING=false` still sends nothing. In that mode the bot tracks the would-be position and logs the close when the level is hit, so you can see both sides before going live.

---

## Requirements

1. **Windows machine or Windows VPS** (the MetaTrader 5 Python package is officially Windows-only).
2. MetaTrader 5 installed and logged into a **demo** account first. Gold (`XAUUSD` or your broker's name) must be available.
3. Python 3.10+

---

## Quick start (demo)

```bash
cd mt5-ema-reclaim-bot
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env`: demo login, password, server, and `SYMBOL` if your broker names gold something other than `XAUUSD`. Leave `LIVE_TRADING=false`.

Start the terminal, log in, then:

```bash
python bot.py
```

Check the decision logic without MT5:

```bash
python test_trade_logic.py
python test_strategy.py
```

---

## Going live

1. Run on demo until the logged opens and closes match the levels you expect.
2. Confirm the symbol in the log is your gold symbol, not a fallback you didn't want.
3. Set `LIVE_TRADING=true`.
4. Restart the bot and watch the first fills.

**You are fully responsible for any losses.** This is not financial advice.

---

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `SYMBOL` | XAUUSD | Gold symbol. Suffixes are tried if this exact name is missing |
| `TIMEFRAME` | M15 | Bar size. Entries only on close |
| `RISK_FRACTION` | 0.0025 | Equity risk per trade |
| `DAILY_LOSS_FRACTION` | 0.01 | Blocks new entries after a 1% down day |
| `MAX_DRAWDOWN_FRACTION` | 0.03 | Blocks new entries 3% off the peak |
| `MAX_POSITIONS` | 1 | One gold position at a time |
| `MAX_SPREAD_PIPS` | 50 | Gold spread filter. Do not set this to 2 |
| `STOP_MULTIPLE` / `TAKE_MULTIPLE` | 1.5 / 2.2 | ATR multiples |
| `MAGIC` | 20251020 | Only these tickets are managed |
| `LIVE_TRADING` | false | Must be true to send orders |
| `POLL_SECONDS` | 1 | Exit check interval |

Day-start equity, peak equity, and intended stops are stored in `runtime_state.json` next to the bot (not committed).

---

## Hosting

Run it on a Windows VPS beside a logged-in MT5 terminal (Task Scheduler or NSSM so it restarts on reboot). The phone app is only for watching the same account. Railway / Render / Netlify cannot host this.

---

## Disclaimer

Trading CFDs and gold involves substantial risk of loss. Test on a demo account before using real capital. You alone are responsible for the configuration and the money at risk.
