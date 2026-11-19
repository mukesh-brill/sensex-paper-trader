# Sensex paper trader

Paper trades only. This bot never logs in to any broker and cannot place orders.

It runs on GitHub's servers (GitHub Actions), so it keeps working while your laptop is asleep or shut down. Every trading day from today until `END_DATE` (26 Nov 2026) it:

1. Watches Sensex 1-minute candles, RSI(14) and the previous day's high and low using Groww's public data.
2. Takes the version B signals from the backtest (entries before 1:00 PM, stops for the day after 2 losses, squares off at 3:15 PM).
3. Prices the ATM option of the nearest weekly expiry. On expiry day it uses the next expiry.
4. Runs three virtual books on the same signals. P&L is always the real ATM option premium:
   - **TRAIL (main)**: 75-point Sensex stop loss. At +100 points the stop moves to breakeven. At +150 points it locks in +100 and then trails 50 points behind the best level. There's no fixed target, so big moves can run until 3:15 PM.
   - **SLIDE**: your original sliding stop on premium (10% SL, 20% target, both moving up 10% for every +10%). Kept for comparison.
   - **BT75**: the fixed 75/150-point exit from the backtest. Kept for comparison.
5. Writes `reports/<date>.md` and an overall `REPORT.md` after 3:15 PM. It can also send them to Telegram.

## Setup (about 10 minutes)

1. Create a free account at github.com if you don't have one.
2. Create a new **public** repository, for example `sensex-paper-trader`. Public repos get unlimited free Actions minutes. A private repo's free 2,000 minutes a month would run out in about a week, because the bot runs about 6.5 hours a day.
3. Upload all files from this folder, keeping the `.github/workflows` folder structure. On the repo page, click "Add file" then "Upload files" and drag the whole folder in.
4. Go to the **Actions** tab and enable workflows if GitHub asks.
5. Click **Sensex paper trader**, then **Run workflow**, to start immediately. After that it starts automatically every weekday at 8:47 AM and 12:17 PM IST.

### Optional: Telegram alerts and daily report on your phone

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token.
2. Send any message to your new bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `chat` → `id` number.
3. In your GitHub repo, go to Settings → Secrets and variables → Actions → New repository secret. Add `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`.

## Where to see results

- `REPORT.md`: day-by-day summary of all three books.
- `reports/2026-11-19.md` and so on: every trade with entry, exit, premium %, Sensex points, reason and P&L.
- `trades.csv`: all trades, ready for Excel.
- The Actions tab: live log of each run.

## Notes

- GitHub can start scheduled runs 5 to 20 minutes late. The bot only acts on signals that complete after it starts.
- The morning run hands over to the afternoon run at 12:45 PM, which may cause a gap of a minute or so.
- Premiums are polled every 15 seconds. Real fills, especially stop-losses, can be worse than the paper fills.
- P&L is for 1 lot (20 qty) after ₹60 per trade in charges. You can change `LOTS` and `CHARGES_PER_TRADE` in the workflow `env`.
- After 26 Nov the bot does nothing. Disable the workflow under Actions → ⋯ → Disable.
- To check the signal logic against history, run `python bot.py replay` on any computer with Python.
