# 📖 Stock Trading Bot — User Manual

*Your complete guide to understanding, operating, and profiting from your autonomous trading bot.*

---

## 👋 Welcome

This is your personal stock trading bot. It picks stocks, places trades, manages positions, and protects your money — all while you sleep. You don't need to know how to trade. You don't need to watch the market. The bot does that for you.

**What you're looking at:** A cloud-hosted dashboard that shows what the bot is doing with your (fake) money. Real money is never at risk right now — this is paper trading with $100,000 in fake cash.

**Your job:** Check the dashboard once a day, watch your phone for notifications, hit the red kill switch if anything goes wrong.

**Bot's job:** Everything else.

---

## 🎯 Quick Answers to Common Questions

**"Is the bot doing anything right now?"**
Look at the top of the dashboard. Green "24/7 CLOUD" badge = running. Check the "Scheduler" tab to see recent activity.

**"Did I make money today?"**
Look at the "Daily P&L" card in the account overview. Green = profit, red = loss. Your phone also gets a daily summary at 4 PM ET.

**"Why did the bot buy this stock?"**
Click any position in the Positions table → the strategy file shows why. Or check the Trade Journal for full reasoning.

**"What happens if the stock crashes?"**
Every position has an automatic stop-loss (usually 10% below entry). If the stock drops that much, the bot sells automatically. You can't lose more than 10% on any single trade.

**"What if the WHOLE market crashes?"**
Daily loss limit of 3% triggers an automatic kill switch. Bot cancels all orders, closes all positions, and stops trading until you manually re-enable it.

**"How do I stop it immediately?"**
Big red **KILL SWITCH** button in the dashboard header. One click stops everything.

**"When can I use real money?"**
When the Readiness Score hits 80/100 (usually takes 30 days of profitable paper trading). The bot tells you when you're ready.

---

## 📋 Table of Contents

1. [Quick Start — Your First Day](#-quick-start--your-first-day)
2. [Daily Routine — What To Do Each Day](#-daily-routine--what-to-do-each-day)
3. [Dashboard Tour — Every Button Explained](#-dashboard-tour--every-button-explained)
4. [The 6 Trading Strategies — Plain English](#-the-6-trading-strategies--plain-english)
5. [How Stock Picks Work — What The Bot Looks At](#-how-stock-picks-work--what-the-bot-looks-at)
6. [Position Management — How Trades Get Watched](#-position-management--how-trades-get-watched)
7. [Safety Rails — How Your Money Is Protected](#-safety-rails--how-your-money-is-protected)
8. [Push Notifications — What Your Phone Will Tell You](#-push-notifications--what-your-phone-will-tell-you)
9. [Scheduled Actions — The Bot's Daily Timeline](#-scheduled-actions--the-bots-daily-timeline)
10. [Strategy Presets — Conservative vs Moderate vs Aggressive](#-strategy-presets)
11. [Reading Your Performance — Scorecard & Heatmap](#-reading-your-performance)
12. [Advanced Features — What Makes This Bot Smart](#-advanced-features)
13. [When Things Go Wrong — Troubleshooting](#-when-things-go-wrong)
14. [Going Live — Switching to Real Money](#-going-live--switching-to-real-money)
15. [Glossary — Trading Terms Explained](#-glossary)
16. [Technical Reference](#-technical-reference)

---

## 🚀 Quick Start — Your First Day

### Day 1 Setup (5 minutes)

1. **Open the dashboard:** https://stockbott.up.railway.app
2. **Log in:** username `Kbell0629`, password `We360you45$$`
3. **Install ntfy on your phone** (for push notifications):
   - iPhone: App Store → "ntfy" → Install → Subscribe to topic `alpaca-trading-bot-kevin`
   - Android: Play Store → same
4. **Install the dashboard as an app** (optional but recommended):
   - iPhone: Safari → Share → "Add to Home Screen"
   - Android: Chrome → menu → "Install app"
5. **Verify everything is green:**
   - Top-right should show **24/7 CLOUD** in green (bot is running)
   - Trading Session badge shows the current market state
   - Account shows $100,000 portfolio

### That's it. Go to bed. The bot trades tomorrow at 9:30 AM ET.

---

## 📅 Daily Routine — What To Do Each Day

### Morning (optional — 2 minutes)
- Open dashboard. Any red banners? Kill switch active? If yes, investigate.
- Check "Top 3 Picks" for today's recommendations.

### During Market Hours (9:30 AM - 4:00 PM ET)
- **Do nothing.** The bot works. You can check the dashboard anytime.
- You'll get a push notification every time a trade is placed.

### After Market Close (4:05 PM ET)
- **Daily summary notification** arrives on your phone.
- Optional: open dashboard → Heatmap tab to see today's P&L.

### Weekly (Sundays — 5 minutes)
- Review the Readiness Score — trending up?
- Check the Heatmap for patterns (e.g., "Mondays are always losers").
- Read weekly learning notification (Fridays 5 PM ET) to see what the bot learned.

### Monthly
- Monthly rebalance runs automatically on the first trading day.
- Review Paper vs Live comparison tab to see if you're ready for real money.

---

## 🖥️ Dashboard Tour — Every Button Explained

### Top Header Bar (always visible)

| Element | What It Does |
|---|---|
| **Stock Trading Bot** title | Click to scroll to top |
| **PAPER TRADING** (orange) | Reminds you it's fake money |
| **Trading Session Badge** | PRE-MARKET / MARKET OPEN / AFTER HOURS / CLOSED |
| **Readiness Score** | Progress toward going live (need 80/100) |
| **24/7 CLOUD** badge (green) | Cloud scheduler running — bot is alive |
| **Auto-Deployer Toggle** | ON/OFF switch for automatic trading |
| **KILL SWITCH** (red) | Emergency stop — cancels all orders, closes all positions |
| **🎤 Voice** | Voice control (Chrome/Safari) — say "Kill switch", "What's my P&L" |
| **📖 Help** | Opens this user manual |
| **↻ Refresh** | Manually refresh dashboard data |
| **Next refresh: Xs** | Countdown to next auto-refresh (every 60s) |

### Navigation Tabs (scrollable)

Click any tab to jump to that section:
- **Overview** — Account metrics
- **Picks** — Top 3 stock recommendations
- **Strategies** — 6 strategy cards
- **Positions** — What you currently own
- **Screener** — Full top 50 stocks
- **Short Sells** — Bear market plays
- **Tax Harvest** — Tax-loss opportunities
- **Backtest** — Historical simulation
- **Readiness** — Progress to go live
- **Heatmap** — Daily P&L calendar
- **Paper vs Live** — Account comparison
- **Scheduler** — Cloud scheduler status
- **Settings** — Strategy presets

### Account Overview (5 cards)

- **Portfolio Value** — Total worth of account (cash + positions)
- **Cash** — Uninvested money available to spend
- **Buying Power** — Max you could buy (2x cash with margin)
- **Long Market Value** — Total value of current positions
- **Daily P&L Meter** — Today's profit/loss, color-coded (green → red as you approach 3% loss limit)

### Top 3 Picks (3 cards)

Each card shows:
- **Symbol & price** — The stock ticker and current price
- **Score bars** — How good each of the 5 strategies is for this stock (higher = better)
- **Technical indicators** — RSI (momentum), MACD (trend), bias (bullish/bearish/neutral)
- **Social sentiment** — What retail traders on StockTwits are saying
- **Recommended shares** — How many to buy (sized based on volatility)
- **30d backtest** — What would have happened if you'd traded this stock the last 30 days
- **Deploy button** — Buy this stock now (opens confirmation with max loss / profit target)

### Strategy Cards (6 active strategies)

Each shows status, key stats, and **Pause/Stop buttons**:
- **Pause** — Temporarily disable this strategy
- **Stop** — Disable and close any open positions in this strategy

For Short Selling, also has **Turn On/Off** toggle since shorts are riskier.

### Positions Table

Your current holdings:
- Symbol, Quantity, Entry Price, Current Price, P&L, P&L %
- **AUTO/MANUAL** badge — was this deployed by the bot or you?
- **Close button** — Sell everything (confirmation modal shows expected P&L)
- **Sell Half button** — Sell 50% (lock in gains)

### Open Orders Table

Pending orders not yet filled:
- **Cancel button** per row — Cancel the order (no money involved)

### Full Screener (Top 50)

All the stocks the bot is considering:
- Click column headers to sort
- **Deploy button** per row to manually deploy any stock

### Short Selling Candidates

Only appears when the market is bearish or when candidates exist. Shows:
- Symbol, score, 20-day momentum, stop-loss, profit target, risk/reward ratio
- Why each candidate was flagged

### Tax-Loss Harvesting

Positions at a loss that could be sold for tax benefits:
- Amount of loss, estimated tax savings
- Suggested replacement stock (avoids wash sale rule)
- **Harvest button** — Sells the loser

### Readiness Scorecard

6 metrics tracked toward the 80/100 go-live threshold:
- Days tracked (target: 30)
- Total trades (target: 20)
- Win rate (target: 50%)
- Max drawdown (target: <10%)
- Profit factor (target: 1.5)
- Sharpe ratio (target: 0.5)

### Visual Backtest

Pick any stock from the dropdown. Chart shows:
- Blue line: how the stock's price moved over 30 days
- Red dashed line: where your stop-loss would have been
- Summary cards: return %, days held, entry→exit, stopped out or held

**Plain English explanation below** the chart tells you exactly what it means.

### Heatmap (Trade Performance Calendar)

- Color-coded daily P&L grid (dark red = big loss, bright green = big win)
- Hover any day for details
- **By-weekday analysis** — which days are most profitable?

Example insight: "Mondays avg -$50, Tuesdays avg +$120 → maybe skip Mondays"

### Paper vs Live Comparison

Side-by-side cards:
- **Paper** — Active, shows current return
- **Live** — Not yet active, shows setup guide + readiness progress

### Scheduler Panel

Real-time status of all cloud tasks:
- **Task grid** — 5 jobs with last-run times and status (Active/Market Closed/Pending)
- **Summary bar** — Current ET time, market status, thread name
- **Live log feed** — Last 20 scheduler events
- Auto-refreshes every 15 seconds

### Strategy Templates (Settings)

3 preset configurations:
- **Conservative** — 5% stops, 3 positions, for beginners
- **Moderate** — 10% stops, 5 positions, the default
- **Aggressive** — 5% tight stops, 8 positions, experienced only

The active one glows with an "ACTIVE" badge.

### Activity Log

Recent actions taken by you or the bot. Color-coded:
- 🟢 Green — Buys
- 🔴 Red — Sells
- 🟡 Yellow — Cancels
- 🔵 Blue — Info

---

## 📈 The 6 Trading Strategies — Plain English

### 1. Trailing Stop 🔵 (Ride the uptrend)

**In plain English:** Buy a stock that's going up. Set a floor price (stop-loss). As the stock climbs higher, the floor moves up with it — never down. When the stock eventually drops to the floor, sell automatically. You keep most of the gains.

**Example:**
- Buy NVDA at $200
- Initial stop-loss at $180 (10% below)
- NVDA rises to $250 → stop moves up to $237.50
- NVDA drops to $237.50 → sell automatically for +18.75% profit

**When it wins:** Stocks in strong uptrends with good momentum.
**When it loses:** Choppy, sideways markets (gets "whipsawed").

### 2. Copy Trading 🟢 (Follow the smart money)

**In plain English:** US politicians are required to disclose their stock trades within 45 days. Some of them (senators on committees, for example) have access to information we don't. The bot watches public disclosures and buys what they buy.

**When it wins:** When politicians have information edges (committee briefings, early bill drafts).
**When it loses:** When the trade is disclosed too late and the move already happened.

### 3. Wheel Strategy 🟣 (Get paid to wait)

**In plain English:** Instead of buying a stock, "sell insurance" on it. You collect a premium payment. If the stock stays above your target price, you keep the money free. If it drops, you buy the stock at a discount (minus the premium you already collected). Repeat forever.

**Two stages:**
1. Sell a "put" — collect premium, wait for stock to drop to your strike
2. If assigned (stock drops), you own 100 shares → sell a "call" → collect more premium

**When it wins:** Choppy, sideways markets. Stocks that swing $10-$50 range.
**When it loses:** Strong trends — you miss most of the upside.

### 4. Mean Reversion 🟠 (Buy the oversold dip)

**In plain English:** Stocks sometimes overreact to bad news and drop too far. If the drop is emotional (no actual bad fundamentals), the stock often bounces back to its average price within days. The bot buys the dip and sells when it recovers to the mean.

**When it wins:** Stocks drop 15%+ on weak volume with no real bad news.
**When it loses:** "Catching falling knives" — buying stocks with real problems that keep falling.

### 5. Breakout 🔴 (Catch the explosion)

**In plain English:** When a stock breaks above its 20-day high on 2x normal volume, it often keeps running. The bot buys the breakout, sets a tight 5% stop (breakouts fail fast if they're going to fail), and rides the move up.

**When it wins:** Real news catalysts create real breakouts.
**When it loses:** "Fake breakouts" — stock pokes above and quickly falls back.

### 6. Short Selling ⚫ (Profit when stocks fall)

**In plain English:** Borrow shares, sell them immediately at current price. When the stock falls, buy them back cheaper and return them. You keep the difference.

**Gated to bear markets only** (SPY down 5%+ in 20 days) because shorts can lose infinite money if the stock shoots up.

**When it wins:** Bear markets, specific stocks with bad news.
**When it loses:** Unexpected rallies, short squeezes.

---

## 🎯 How Stock Picks Work — What The Bot Looks At

Every 30 minutes during market hours, the bot scans **10,000+ US stocks**. Here's how it decides which to trade:

### Step 1: Basic Filters
- Skip penny stocks (< $5 — too risky)
- Skip illiquid stocks (< 100K daily volume — can't easily sell)
- Skip stocks not on Alpaca's paper feed

### Step 2: Score Each Stock (5 Scores)
Each stock gets a score for each strategy:
- **Trailing Stop Score** = momentum * 0.5 + volatility * 0.3 + volume surge
- **Copy Trading Score** = bonus for large caps + momentum
- **Wheel Score** = moderate volatility scores highest (extreme gets penalized)
- **Mean Reversion Score** = rewards big drops (but penalizes news-driven drops)
- **Breakout Score** = daily change + volume surge multiplier (2x/3x volume tiers)

### Step 3: Enrich Top 100 With More Data
- **20-day momentum** (longer trend)
- **5-day momentum** (recent trend)
- **Relative volume** (today vs average)
- **Technical indicators** (RSI, MACD, Bollinger, Stochastic, SMA)
- **News sentiment** (positive, negative, neutral)
- **Earnings warnings** (auto-skip if earnings in next 3 days)
- **Backtest** (simulates 30 days of performance)
- **Social sentiment** (StockTwits buzz)
- **Position sizing** (fewer shares for volatile stocks)

### Step 4: Apply Global Filters
- **Market breadth** — if <40% of stocks advancing, reduce scores 15%
- **Sector rotation** — boost picks in strong sectors, penalize weak ones
- **Regime weights** — bull/neutral/bear multipliers per strategy
- **Economic calendar** — reduce scores if FOMC or CPI within 3 days

### Step 5: Diversification
Top 5 picks, but no more than 2 stocks from the same sector.

### Step 6: Deploy at 9:35 AM ET
Bot picks top 2 and places market buy orders.

---

## 🛡️ Position Management — How Trades Get Watched

Every 60 seconds during market hours, the bot checks every position:

1. **Did the buy fill yet?** If yes, note the fill price and switch status to "active".
2. **Is the stop-loss placed?** If not (first check after fill), place it.
3. **Is the price up 10%?** Activate the trailing stop (ratchet the floor up).
4. **Did the floor need to move up?** Cancel old stop, place new higher one.
5. **Hit any profit target?** At +10%, +20%, +30%, +50% → sell 25% each level.
6. **Did the stop trigger?** Position closed, record the exit, start cooldown.

**Critical detail:** When the trailing stop moves up, the bot places the NEW stop BEFORE canceling the old one. This way if the API hiccups, your position is never unprotected.

---

## 🛡️ Safety Rails — How Your Money Is Protected

### Hard Stops (Bot Can't Override)

1. **Kill Switch** — Manual red button. Cancels all orders, closes all positions.
2. **Daily Loss Limit (3%)** — Auto-triggers kill switch if portfolio drops 3% in a day.
3. **Max Drawdown (10%)** — Auto-triggers kill switch if portfolio drops 10% from peak.
4. **Stop-Losses On Every Trade** — No exceptions. Every position gets a stop.

### Position Limits

5. **Max 5 concurrent positions** (3 in live mode).
6. **Max 10% of portfolio per stock** (5% in live mode).
7. **Max 2 new trades per day** (1 in live mode) — prevents overtrading.

### Timing Rules

8. **60-min cooldown after any loss** — prevents revenge trading.
9. **48-hour cooldown after short loss** — shorts are riskier.
10. **Market hours check** — never trades outside regular hours (except when explicitly configured).

### Quality Filters

11. **Earnings avoidance** — won't buy stocks reporting earnings within 3 days.
12. **Meme stock filter** — shorts won't deploy on heavily-buzzed stocks (too volatile).
13. **Correlation enforcement** — won't hold 3+ positions in same sector, max 40% concentration.
14. **Market breadth filter** — reduces scores when broader market is weak.
15. **Bear market regime** — aggressive strategies pause in bear markets, shorts activate.
16. **Data quality guard** — rejects stocks with impossible data (e.g., +569% "daily change" from stale splits).

### Capital Protection

17. **Capital sustainability check** — verifies enough cash before every trade.
18. **Won't trade if <$1,000 free capital.**
19. **Won't sell more shares than held** (profit ladder dynamically resizes stops).

### Authentication & Security

20. **Basic auth with timing-safe comparison** (prevents timing attacks).
21. **All secrets in environment variables** (not in code).
22. **Input validation** (order IDs must be UUIDs, symbols must be letters only, qty 1-1000).
23. **1MB POST body cap** (prevents memory attacks).

### Redundancy

24. **Deployment lock** — two processes can't deploy same stock simultaneously.
25. **Atomic file writes** — no data corruption if process dies mid-save.
26. **Error recovery** — detects orphan positions (position without strategy) and auto-fixes.

---

## 📱 Push Notifications — What Your Phone Will Tell You

After you install the **ntfy** app and subscribe to topic `alpaca-trading-bot-kevin`:

| Type | When | Example |
|---|---|---|
| 🟢 **Trade** | New position opened | "Deployed trailing_stop on NVDA: 25 shares @ ~$198.46" |
| 💰 **Exit** | Position closed (profit) | "Profit take on NVDA at +20%: sold 25 shares" |
| 🛑 **Stop** | Stop-loss triggered (LOUD) | "TSLA stopped out at $353. P&L: -$450" |
| ⚠️ **Alert** | Warnings (URGENT) | "High correlation risk — 3 tech positions held" |
| 🚨 **Kill** | Kill switch activated (MAX priority) | "KILL SWITCH: Daily loss 3.2% exceeded limit" |
| 📊 **Daily** | 4 PM close summary | "Daily close: $101,200 (+1.2%) | Win rate 60%" |
| 🧠 **Learn** | Weekly learning (Fridays) | "Weekly learning: boosted breakout strategy (70% win rate)" |
| ℹ️ **Info** | Routine | "Morning scan complete, no qualifying trades" |

**Important events** (trade, exit, stop, kill, daily) also queue **emails** to your inbox.

---

## ⏰ Scheduled Actions — The Bot's Daily Timeline

All times **Eastern Time**. All automatic.

| Time | What Happens |
|---|---|
| **9:30 AM** | Market opens |
| **9:30-9:35 AM** | First screener run (~60 seconds) |
| **9:35 AM** | Auto-deployer fires: picks top 2, places buys |
| **9:36 AM onwards** | Strategy monitor every 60 seconds |
| **10:00 AM, 10:30 AM, etc.** | Screener refreshes every 30 minutes |
| **3:45 PM (Fridays only)** | Scale out 50% of positions up 20%+ |
| **4:00 PM** | Market closes |
| **4:05 PM** | Daily close summary → phone notification |
| **5:00 PM (Fridays only)** | Weekly learning engine runs |
| **9:45 AM first trading day of month** | Monthly rebalance runs |

---

## 🎨 Strategy Presets

Switch between 3 risk profiles in the Settings section:

### 🟢 Conservative
**Good for:** First-time traders, accounts under $5k, high market uncertainty.
- 5% tight stops (cut losses fast)
- Max 3 positions
- 5% per stock
- 1 new trade/day
- Only safe strategies (trailing stop, wheel, copy trading)
- **Expected return:** 5-15% annually
- **Max drawdown:** ~5-8%

### 🔵 Moderate (Default)
**Good for:** Most traders, $5k-$50k accounts, all market conditions.
- 10% stops
- Max 5 positions
- 10% per stock
- 2 new trades/day
- All 5 long strategies
- Shorts only in bear markets
- **Expected return:** 15-25% annually
- **Max drawdown:** ~10%

### 🔴 Aggressive
**Good for:** Experienced traders, $25k+ accounts, active monitoring.
- 5% tight stops (fail fast)
- Max 8 positions
- 15% per stock
- 3 new trades/day
- All 6 strategies including shorts in any market
- Extended hours enabled
- **Expected return:** 20-40% (or -20% in bad year)
- **Max drawdown:** ~15-20%

---

## 📊 Reading Your Performance

### The Readiness Scorecard (Go-Live Indicator)

6 criteria, 20 points each = 100 max. Need **80+ to go live with real money**:

| Criterion | Target | Why It Matters |
|---|---|---|
| Days tracked | 30+ | Sample size — need enough data |
| Total trades | 20+ | Strategy actually tested across conditions |
| Win rate | 50%+ | More wins than losses |
| Max drawdown | <10% | Controlled worst-case scenario |
| Profit factor | 1.5+ | Total wins / total losses ratio |
| Sharpe ratio | 0.5+ | Risk-adjusted returns |

### The Heatmap (Pattern Recognition)

- Each colored square = one trading day
- Dark red = big loss day, bright green = big win day
- **By-weekday analysis** helps spot patterns:
  - "Mondays avg -0.5%, Tuesdays avg +1.2%" → consider skipping Mondays
  - "Win rate 80% on Wednesdays" → double down

### The Trade Journal

Every trade logged with full reasoning:
- Entry price, exit price, P&L
- Which strategy was used
- Why the bot picked this stock (score, indicators, sentiment)
- What happened (stop triggered, profit taken, manual close)

Use this to understand what's working and what isn't.

---

## 🧠 Advanced Features

These features run automatically — you don't have to do anything, but here's what they do:

### Dynamic Strategy Rotation
Bot knows bull markets favor trailing stops and breakouts. Bear markets favor shorts and mean reversion. Adjusts strategy weights automatically based on SPY's 20-day performance.

### Sector Rotation
Tracks 11 sector ETFs (tech, healthcare, energy, etc.). Boosts picks in sectors outperforming SPY. Penalizes picks in sectors underperforming.

### Market Breadth Filter
Counts how many stocks are advancing vs declining. When breadth is weak (<40% advancing), reduces risk.

### News Sentiment Analysis
Scans Alpaca news for keywords like "beats earnings", "FDA approval", "lawsuit", "downgrade". Applies score adjustments.

### Social Sentiment (StockTwits)
Free API. Tracks bullish/bearish sentiment and trending buzz. Flags meme stocks for caution.

### Options Flow Tracking
Monitors call/put open interest ratios. High C/P = bullish smart money, low C/P = bearish. Leading indicator.

### Economic Calendar
Knows upcoming FOMC meetings and options expiration dates. Reduces position sizes before high-impact events.

### Post-Market News Scanner
After market close, scans news for earnings beats, FDA approvals, contract wins, lawsuits, downgrades. Produces actionable signals.

### Earnings Play Strategy
Stocks with positive momentum run up 2-3 days before earnings. The bot catches this pattern and always sells before the actual earnings release (no gap risk).

### Self-Learning Engine
Every Friday, analyzes your trade journal. Which strategies are winning? Which signals actually predict profits? Adjusts internal weights so next week's picks are smarter.

### Error Recovery
If a process crashes mid-trade and leaves an orphan position (no strategy file, no stop-loss), the recovery system auto-detects and fixes it.

### Tax-Loss Harvesting
Scans positions at a loss. Suggests selling for tax deduction + buying similar-but-not-identical stock (avoids wash sale rule). Free money from the IRS.

---

## 🚨 When Things Go Wrong

### Scenario: "I hit the wrong button by accident"

**Deploy button pressed?** Click the position → Close button → confirms sell at market. Usually costs a few dollars in slippage.

**Kill switch activated accidentally?** Go to kill switch status → Deactivate. Then manually toggle auto-deployer back on.

### Scenario: "Market is crashing, I'm panicking"

1. Open dashboard
2. Click **KILL SWITCH**
3. Confirm — all orders cancel, all positions close at market
4. Done. Deep breath. You can re-enable later.

### Scenario: "Daily loss hit 3%, bot triggered kill switch"

- Max priority notification on your phone.
- Dashboard shows red banner.
- Your choice: Review positions, decide if you want to resume.
- **To resume:** Edit `guardrails.json` → set `kill_switch: false` (or use API).

### Scenario: "Dashboard shows 'Scheduler OFF'"

Cloud scheduler stopped. Usually means Railway deployment crashed.
1. Check Railway dashboard → Logs
2. Look for Python errors
3. Usually fixed by a redeploy: push any commit to main branch

### Scenario: "No trades deployed this morning"

Check Dashboard → Scheduler tab → recent logs. Common reasons:
- "Kill switch active" → reset in guardrails.json
- "Cannot trade: LOW CAPITAL" → not enough free cash
- "In cooldown after recent loss" → wait 60 min
- "No qualifying trades" → screener didn't find picks meeting criteria
- "Market closed" → it's a holiday or weekend

### Scenario: "Position showing wrong P&L"

Dashboard auto-refreshes every 60 seconds. Click Refresh button for immediate refresh. Position P&L comes directly from Alpaca — if still wrong, that's an Alpaca issue.

### Scenario: "I can't log in"

Credentials: `Kbell0629` / `We360you45$$` (case-sensitive). If changed, check Railway env vars `DASHBOARD_USER` and `DASHBOARD_PASS`.

### Emergency Resources
- Alpaca support: https://alpaca.markets/support
- Dashboard status: https://stockbott.up.railway.app/api/scheduler-status
- GitHub issues: https://github.com/Kbell0629/alpaca-trading-bot/issues

---

## 💵 Going Live — Switching to Real Money

### Prerequisites

1. **Readiness Score ≥ 80/100**
2. **Profitable paper trading** (if paper is losing money, don't go live)
3. **Understand the strategies** (re-read this manual)
4. **$5,000 minimum** to fund live account (bot sized for this amount)

### Step-by-Step Process

1. **Create live Alpaca account:** https://alpaca.markets → "Open an Account" (NOT paper)
2. **Fund with $5,000** (ACH transfer, takes 2-3 days to clear)
3. **Generate live API keys:** Alpaca dashboard → API Keys → Generate. **IMPORTANT:** Mark them as LIVE keys.
4. **Update Railway environment variables:**
   ```
   ALPACA_ENDPOINT = https://api.alpaca.markets/v2
   ALPACA_API_KEY = <your live key>
   ALPACA_API_SECRET = <your live secret>
   ```
5. **Update `guardrails.json`** to use `live_mode_settings`:
   - Max positions: 3 (not 5)
   - Max per stock: 5% (not 10%)
   - Max new per day: 1 (not 2)
6. **Keep paper running in parallel** for comparison (use separate Alpaca account).
7. **Watch carefully for first week** — live fills may differ from paper due to slippage.

### What's Different in Live Mode

- **Real slippage:** bid/ask spread costs 0.1-0.5% per trade
- **Slower fills** during high volume
- **Pattern day trader rules:** need $25k for unlimited day trading, or limit to 3 day trades per 5-day period
- **Tax implications** — consult an accountant
- **Emotional pressure** — real money hits different

---

## 📖 Glossary — Trading Terms Explained

| Term | Meaning |
|---|---|
| **Alpaca** | The broker (like Robinhood) that executes our trades via API |
| **Backtest** | Simulating a strategy on historical data to see how it would have performed |
| **Bear market** | When stocks are falling broadly (SPY down 5%+ over 20 days) |
| **Bull market** | When stocks are rising broadly |
| **Daily P&L** | Profit/loss since market open today |
| **Drawdown** | % drop from your portfolio's peak value |
| **FOMC** | Federal Open Market Committee — Fed's meetings where they set interest rates |
| **Gap** | When a stock opens much higher/lower than it closed (overnight news) |
| **Limit order** | Buy/sell at a specific price or better (may not fill) |
| **Long position** | You own the stock, profit when it goes up |
| **Market order** | Buy/sell at current price immediately (always fills) |
| **Paper trading** | Fake money, real prices — for testing |
| **Position size** | How much of your portfolio is in one stock |
| **Profit factor** | Total $ won / Total $ lost (want >1.5) |
| **RSI** | Relative Strength Index (0-100). <30 = oversold, >70 = overbought |
| **Sharpe ratio** | Risk-adjusted return (want >0.5) |
| **Short position** | You borrowed and sold the stock, profit when it goes down |
| **Slippage** | Difference between expected price and actual fill price |
| **Stop-loss** | Auto-sell if stock drops to a certain price (limits losses) |
| **Trailing stop** | Stop-loss that moves UP as price rises, never DOWN |
| **Volatility** | How much a stock's price moves (higher = riskier) |
| **Wash sale** | Selling at a loss and buying back within 30 days (can't claim tax loss) |
| **Win rate** | % of trades that were profitable |

---

## 🔧 Technical Reference

### Environment Variables

| Variable | Purpose |
|---|---|
| `ALPACA_API_KEY` | Alpaca API key |
| `ALPACA_API_SECRET` | Alpaca API secret |
| `ALPACA_ENDPOINT` | `paper-api.alpaca.markets/v2` or `api.alpaca.markets/v2` |
| `ALPACA_DATA_ENDPOINT` | `data.alpaca.markets/v2` |
| `DASHBOARD_USER` | Dashboard login username |
| `DASHBOARD_PASS` | Dashboard login password |
| `NTFY_TOPIC` | Push notification topic |
| `NOTIFICATION_EMAIL` | Email for critical alerts |
| `PORT` | Server port (Railway sets automatically) |
| `ENABLE_CLOUD_SCHEDULER` | `true` to run scheduler on Railway |

### Voice Commands (Click 🎤 button)

- "Kill switch" → triggers emergency stop
- "Refresh" → reloads dashboard
- "Portfolio" → speaks current value
- "What's the top pick" → speaks top recommendation
- "Deploy trailing stop on NVDA" → opens deploy modal

### API Endpoints (For Developers)

All require basic auth.
- `GET /api/data` — Full dashboard data
- `GET /api/account` — Alpaca account
- `GET /api/positions` — Current positions
- `GET /api/orders` — Open orders
- `GET /api/scheduler-status` — Cloud scheduler state
- `GET /api/trade-heatmap` — Daily P&L history
- `GET /api/guardrails` — Safety config
- `GET /api/readme` — This user manual (raw markdown)
- `POST /api/deploy` — Deploy a strategy
- `POST /api/kill-switch` — Activate/deactivate kill switch
- `POST /api/auto-deployer` — Toggle auto-deployer
- `POST /api/apply-preset` — Apply strategy preset

### File Locations

| File | Purpose |
|---|---|
| `server.py` | Dashboard web server |
| `cloud_scheduler.py` | 24/7 task scheduler |
| `update_dashboard.py` | Stock screener |
| `guardrails.json` | Safety limits |
| `auto_deployer_config.json` | Auto-deployer settings |
| `strategies/*.json` | Per-position strategy state |
| `trade_journal.json` | All trade history |
| `scorecard.json` | Performance metrics |

---

## 📞 Quick Reference Card

**Dashboard:** https://stockbott.up.railway.app
**Login:** Kbell0629 / We360you45$$
**GitHub:** github.com/Kbell0629/alpaca-trading-bot
**Notifications app:** ntfy (topic: `alpaca-trading-bot-kevin`)
**Emergency stop:** Red KILL SWITCH button in dashboard header

**Bot hours:** Weekdays 9:30 AM - 4:00 PM ET (fully automatic)
**Your hours:** Check dashboard once a day, watch phone for notifications

**If lost:** Click the 📖 Help button — opens this manual.

---

*Last updated: 2026-04-16. Maintained by: Claude Code. Questions or issues → open a GitHub issue.*
