# Day Trader Bot

An automated intraday trading bot for US stocks. It trades through the Alpaca API,
and all of its trading rules live in **`criteria.yaml`**, which you can edit at any time.
It starts in **paper** mode, which uses fake money with real market data.

> Educational tool, not financial advice. Most day traders lose money. Backtest first,
> then paper trade for weeks before you even think about `mode: live`.

## Setup (5 minutes)
1. Create a free account at alpaca.markets and generate **Paper Trading** API keys.
2. `cp .env.example .env` and paste the keys into `.env`.
3. `pip install -r requirements.txt`
4. `python bot.py check`, which validates your criteria.

## Commands
| Command | What it does |
|---|---|
| `python bot.py check` | Validate `criteria.yaml` and print the settings in effect |
| `python bot.py backtest` | Simulate the criteria on synthetic data (no keys needed) |
| `python bot.py backtest --source alpaca` | Simulate on real recent history (`sim.days`) |
| `python bot.py scan` | Show each watchlist symbol and which entry rules pass ✓ or fail ✗ |
| `python bot.py once` | Run one trading cycle: manage exits, then look for entries |
| `python bot.py run` | Loop every `poll_seconds` until the flatten time |
| `python bot.py status` | Account, day P&L, open bot positions, and today's closed trades |
| `python bot.py flatten` | Close all positions the bot opened and halt for the day |

Kill switch: `touch STOP` in this folder. The next cycle flattens everything and halts.

## How it trades (default criteria)
- **Entry (long):** fast EMA (9) has just crossed above the slow EMA (21) within the last 3 bars,
  RSI is between 50 and 70, price is above the session VWAP, and the latest bar's volume is at least 1.2× its average.
  Shorts work the same way in mirror image if you set `allow_short: true`.
- **Sizing:** each trade risks `risk_per_trade_pct` of equity at the stop, capped at `max_position_value`.
- **Exits:** the broker holds a hard stop and target as a bracket order, so these still work if the bot dies.
  The bot also manages a trailing stop, a max hold time, an EMA cross-back exit, and an RSI extreme exit.
- **Guardrails:** max open positions, max trades per day, a daily loss limit that flattens and halts,
  an optional daily profit target, a per-symbol cooldown, no entries in the first 15 minutes,
  and everything is flattened at 15:50 ET.
- The bot only touches positions it opened itself. Anything else in the account is left alone.

## Having Claude run it
Ask Claude things like:
- "Run the bot for today": Claude starts `python bot.py run` in the background and reports entries and exits.
- "Scan the watchlist": Claude runs `scan` and explains why each symbol is or isn't a trade.
- "Make it more conservative" or "add shorts" or "only trade SPY and QQQ": Claude edits `criteria.yaml`, runs `check`, and then runs `backtest`.
- "How did we do today?": Claude reads `status` and `journal.jsonl`.
- "Stop trading now": Claude runs `flatten`.

Note that a cloud Claude session stops after a while with no activity. For an unattended full day,
run `python bot.py run` on your own machine or a small always-on server.
Open positions stay protected by the broker's bracket orders even if the bot stops.

## Files
- `criteria.yaml`: your editable rules. The bot re-reads this file every cycle.
- `state.json`: today's bot positions and counters (generated).
- `journal.jsonl`: every entry, exit, and error, including the reasons (generated).

## PDT rule
US margin accounts under $25k are limited to 3 day trades in any 5 days.
`status` shows your current day-trade count.
