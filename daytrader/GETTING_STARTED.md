# Getting started with Day Trader

## 1. Install Python (one time)
Download it from https://www.python.org/downloads/ and run the installer.
- **Windows:** on the first installer screen, tick **"Add python.exe to PATH"**.
- **Mac:** after installing, also double-click **Install Certificates.command**
  in Applications → Python 3.x.

## 2. Start the app
Open the `daytrader` folder, then:
- **Windows:** double-click **Start Day Trader.bat**.
  If Windows shows "Windows protected your PC", click **More info**, then **Run anyway**.
- **Mac:** right-click **Start Day Trader.command** and choose **Open**, then click **Open** again.
  You only need to do this the first time. After that, a normal double-click works.

The first launch takes about a minute while it sets itself up. Then your browser opens
http://localhost:8000. **Keep the black terminal window open while you trade.** Closing it quits the app.

## 3. Try the demo market (no account needed)
1. Click the **Connection** tab, then **Start demo market**.
2. Click **▶ Start**. The simulated market runs fast, so you'll see the bot buy and sell within a minute.
3. Click any stock in the watchlist to see its chart and which entry rules pass or fail.

## 4. Connect to the real market (paper trading, fake money)
1. Sign up free at https://alpaca.markets.
2. In the Alpaca dashboard, make sure the account switcher at the top left says **Paper**.
3. Find **API Keys** on the right side of the home page and click **Generate New Keys**.
   Copy the **Key** and the **Secret**. The secret is only shown once.
4. In the app, go to **Connection**, paste both keys, and click **Connect to Alpaca**.
   Your paper account starts with about $100k of fake money.
5. During market hours (9:30–4:00 ET, Mon–Fri), click **▶ Start**.
   The bot checks the market every 60 seconds. It makes no new trades in the first 15 minutes
   or after 3:30, and it closes everything at 3:50.

## 5. Make it yours
- **Choose stocks:** type a ticker in the watchlist box and click **Add**. Hover over a stock and click **×** to remove it.
- **Change rules:** use the **Criteria** tab to set risk per trade, stop loss, profit target,
  daily loss limit, the indicators, and more. Click **Save**.
- **Test first:** in the **Backtest** tab, choose "Real history from Alpaca" and click **Run**.

## Emergency stop
Click **Flatten all** to close every position the bot opened and stop trading for the day.
Stop-loss and profit-target orders sit with Alpaca, so they still protect you if the app is closed.

## Troubleshooting
| Problem | Fix |
|---|---|
| "Python is not installed" | Install Python (step 1). On Windows, tick "Add to PATH", then restart your computer. |
| Browser didn't open | Go to http://localhost:8000 yourself. |
| "Address already in use" | The app is already running in another window. Use that one, or close it first. |
| "Alpaca rejected the connection" | Use **Paper** keys while `mode` is paper. Regenerate the keys if you're unsure. |
| Market shows Closed | It's outside 9:30–4:00 ET or a market holiday. Use the demo market instead. |
| No trades happening | Click a stock and read the ✓/✗ checklist. All rules must pass. Loosen them in Criteria if needed. |

## Real money (only after weeks of paper trading)
1. Generate **Live** keys in Alpaca.
2. Open the `.env` file in the `daytrader` folder with a text editor and add the line `DAYTRADER_ALLOW_LIVE=yes`.
3. Set `mode` to `live` in Criteria and reconnect with your live keys.
4. Under US rules, a margin account with less than $25,000 can make only 3 day trades in any 5 days.
