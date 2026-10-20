# EMA-Reclaim MT5 Live Bridge

Real-market Python service that:

- Connects to a running **MetaTrader 5** terminal via the official `MetaTrader5` package
- Runs the exact **EMA-Reclaim v1** strategy from the simulation (EMA 20/50 + ATR filters)
- Calculates position size from risk %, sets SL/TP from ATR multiples
- Places market orders (or dry-runs them) with a unique magic number
- Is designed to run 24/7 on a cheap VPS

---

## Strategy summary (identical to the TypeScript simulation)

| Rule | Detail |
|------|--------|
| Indicators | EMA(20), EMA(50), ATR(14) |
| Min history | 60 closed candles |
| Filter | EMA separation ≥ 0.12 × ATR |
| Filter | Last bar range ≤ 3.2 × ATR (spike filter) |
| BUY | Fast > Slow, close > Slow, previous close ≤ prev Fast, close > Fast, bullish candle |
| SELL | Fast < Slow, close < Slow, previous close ≥ prev Fast, close < Fast, bearish candle |
| Stop | 1.5 × ATR |
| Take | 2.2 × ATR |
| Risk | 0.25 % of equity per trade (configurable) |
| Limits | Max 1 position, daily loss 1 %, max DD 3 %, max spread filter |

Signals are evaluated **only on closed bars**.

---

## Requirements

1. **Windows machine or Windows VPS** (MetaTrader 5 Python package is officially Windows-only).
   - Oracle Cloud free tier → create a Windows Server instance (or use a cheap Windows VPS from Contabo / ForexVPS / etc.).
   - Linux users: possible via Wine + MT5, but not officially supported and more fragile.
2. MetaTrader 5 terminal installed and logged into a **demo** account first.
3. Python 3.10+ 

---

## Quick start (demo)

```bash
# 1. Clone / copy this folder to the machine that has MT5 installed
cd mt5-ema-reclaim-bot

# 2. Create virtual environment
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # Linux (if using Wine setup)

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
copy .env.example .env
# Edit .env with your demo login, password, server, symbol, etc.
# Keep LIVE_TRADING=false

# 5. Make sure MT5 terminal is running and logged in

# 6. Start the bot
python bot.py
```

You should see logs like:

```
2026-10-20 20:45:01 | INFO    | Connected | Balance=10000.00 Equity=10000.00 | Server=YourBroker-Demo | Trade mode=DEMO
2026-10-20 20:45:05 | INFO    | Bot running. Waiting for new closed bars…
2026-10-20 20:50:01 | INFO    | Bar 2026-10-20 20:45 | action=NO_TRADE | … Waiting for a close back through the fast average.
```

When a real signal appears and `LIVE_TRADING=false`, the bot logs the would-be order but does **not** send it.

---

## Going live (real account)

1. Run on demo for at least several weeks / hundreds of bars.
2. Verify the logs match what you expect.
3. Switch the MT5 terminal to your **real** account (or change credentials in `.env`).
4. Set `LIVE_TRADING=true` in `.env`.
5. Restart the bot.
6. Monitor closely the first few days.

**You are fully responsible for any losses.** This is educational/production-ready scaffolding, not financial advice.

---

## Hosting options (always-on)

| Provider | Notes |
|----------|-------|
| **Oracle Cloud Free Tier** | Can create a Windows Server VM (limited). Good free option if you can get capacity. |
| **Contabo / Hetzner / DigitalOcean** | Cheap Windows or Linux VPS (~$5–12/mo). |
| **Specialized Forex VPS** | ForexVPS, Beeks, etc. — already optimised for MT5 latency. |
| Railway / Render / Netlify | **Not suitable** — they sleep or are not designed for persistent MT5 connections. |

Recommended minimal setup:
- Windows Server VPS
- MT5 terminal installed + auto-login
- This Python bot running as a Windows Service or under `nssm` / Task Scheduler so it restarts on reboot

---

## Phone monitoring

- Install the official **MetaTrader 5** mobile app (iOS/Android).
- Log into the same account.
- You can see open positions, history, and even close trades manually from the phone.
- The bot itself must keep running on the VPS/PC; the phone is for monitoring and emergency intervention only.

---

## Configuration reference (`.env`)

| Variable | Default | Meaning |
|----------|---------|---------|
| `MT5_LOGIN` / `PASSWORD` / `SERVER` | — | Account credentials |
| `SYMBOL` | EURUSD | Instrument |
| `TIMEFRAME` | M15 | Bar size |
| `RISK_FRACTION` | 0.0025 | 0.25 % equity risk per trade |
| `DAILY_LOSS_FRACTION` | 0.01 | 1 % daily loss circuit-breaker |
| `MAX_DRAWDOWN_FRACTION` | 0.03 | 3 % from peak |
| `MAX_POSITIONS` | 1 | Concurrent trades |
| `MAX_SPREAD_PIPS` | 2.0 | Spread filter |
| `STOP_MULTIPLE` / `TAKE_MULTIPLE` | 1.5 / 2.2 | ATR multiples |
| `MAGIC` | 20251020 | Identifies the bot’s trades |
| `LIVE_TRADING` | false | **Must be true to send real orders** |
| `POLL_SECONDS` | 5 | How often to check for a new bar |

---

## Safety features built in

- Magic-number isolation (only manages its own trades)
- Spread filter
- Daily loss limit
- Max drawdown limit
- Position count limit
- Volume calculated from risk % (never fixed lots)
- Dry-run mode when `LIVE_TRADING=false`
- Clear logging of every decision

---

## Next improvements you may want

- Persist day-start equity and peak equity to a small SQLite file
- Telegram / Discord notifications on entries & exits
- Multi-symbol support
- Proper Windows service wrapper
- Back-test harness using the same `strategy.py` on historical data

---

## Disclaimer

Trading forex and CFDs involves substantial risk of loss. Past performance of a strategy (even a carefully ported one) does not guarantee future results. Always test thoroughly on a demo account before risking real capital. You alone are responsible for the configuration and the capital you put at risk.
