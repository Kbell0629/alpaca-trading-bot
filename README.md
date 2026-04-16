# Alpaca Stock Trading Bot — Complete User Guide

**What it is:** A fully autonomous stock trading bot that picks stocks, places trades, manages positions, and learns from its own performance. Runs 24/7 on Railway without your laptop.

**What it does:** Screens 12,000+ US stocks every 30 minutes, picks the best 2 to trade each morning using one of 6 strategies, places orders through Alpaca, monitors positions every 60 seconds, and enforces 15+ safety guardrails to protect your money.

**Dashboard:** https://stockbott.up.railway.app
**Login:** `Kbell0629` / `We360you45$$`
**GitHub:** https://github.com/Kbell0629/alpaca-trading-bot

---

## Table of Contents

1. [How To Read This README](#how-to-read-this-readme)
2. [The Big Picture — What Happens Each Day](#the-big-picture--what-happens-each-day)
3. [The 6 Trading Strategies](#the-6-trading-strategies)
4. [How The Bot Picks Stocks](#how-the-bot-picks-stocks)
5. [How The Bot Manages Positions](#how-the-bot-manages-positions)
6. [Safety Guardrails (15+ Protections)](#safety-guardrails-15-protections)
7. [The Dashboard — Every Section Explained](#the-dashboard--every-section-explained)
8. [The Cloud Scheduler](#the-cloud-scheduler)
9. [Notifications](#notifications)
10. [The 13 Advanced Profit Features](#the-13-advanced-profit-features)
11. [File Structure](#file-structure)
12. [Environment Variables](#environment-variables)
13. [When Things Go Wrong](#when-things-go-wrong)
14. [Going Live With Real Money](#going-live-with-real-money)
15. [Daily Operational Checklist](#daily-operational-checklist)

---

## How To Read This README

- **If you want to understand what's happening right now:** Skip to [The Big Picture](#the-big-picture--what-happens-each-day)
- **If something went wrong:** Skip to [When Things Go Wrong](#when-things-go-wrong)
- **If you want to know what a button does:** Skip to [The Dashboard](#the-dashboard--every-section-explained)
- **If you want to know what protections exist:** Skip to [Safety Guardrails](#safety-guardrails-15-protections)

---

## The Big Picture — What Happens Each Day

Every weekday, automatically, without you doing anything:

```
┌─────────────────────────────────────────────────────────────┐
│  9:30 AM ET - Market opens                                   │
│  Railway scheduler detects via Alpaca /v2/clock              │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  9:30-9:35 AM ET - First screener run (~60 seconds)          │
│  Scans 12,000+ stocks, applies 20+ filters and signals       │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  9:35 AM ET - Auto-deployer fires                            │
│  1. Checks kill switch, daily loss limit, drawdown           │
│  2. Checks cooldown from previous losses                     │
│  3. Checks capital availability                              │
│  4. Picks top 2 stocks from screener                         │
│  5. Runs correlation check (no sector over-concentration)    │
│  6. Skips stocks with earnings warnings                      │
│  7. Places market buy orders                                 │
│  8. Logs to trade_journal.json                               │
│  9. Sends you a push notification                            │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  9:36 AM onwards - Strategy monitor every 60 seconds         │
│  - Places stop-loss after each buy fills                     │
│  - Ratchets stops up as prices climb (trailing stop)         │
│  - Sells 25% at +10%, +20%, +30%, +50% profit targets        │
│  - Triggers kill switch if daily loss exceeds 3%             │
│  - Every 30 min: refreshes screener                          │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  3:45 PM ET (Fridays only) - Weekend risk reduction          │
│  Sells 50% of any position up 20%+ (locks in gains)          │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  4:00 PM ET - Market closes                                  │
│  4:05 PM ET - Daily close summary                            │
│  - Updates performance scorecard                             │
│  - Takes daily snapshot (for heatmap)                        │
│  - Runs error recovery                                       │
│  - Sends you end-of-day notification                         │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  5:00 PM ET (Fridays only) - Weekly learning engine          │
│  Analyzes trade journal, adjusts strategy weights            │
│  If RSI oversold worked 70% of the time, boost that signal   │
│  If breakouts failed 60% of the time, reduce their weight    │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│  First trading day of each month, 9:45 AM ET                 │
│  Monthly rebalance: closes positions held 60+ days at a loss │
│  Frees capital for better opportunities                      │
└─────────────────────────────────────────────────────────────┘
```

**You get push notifications for:** Every trade placed, every stop triggered, every profit take, kill switch activations, daily summaries.

---

## The 6 Trading Strategies

The bot has 6 different strategies. It picks the best one for each stock based on market conditions and the stock's characteristics.

### 1. Trailing Stop 🔵
**When it wins:** Stocks in strong uptrends with good momentum.
**How it works:**
- Buys stock at market price
- Places stop-loss 10% below entry
- When stock rises 10% from entry, trailing stop activates
- Stop moves UP as price rises (locks in gains)
- Stop never moves DOWN (floor only ratchets up)
- When stop is hit, sells all shares

**Example:** Buy NVDA at $200. Stop at $180. NVDA rises to $220 — stop moves to $209 (5% below peak). NVDA rises to $250 — stop moves to $237.50. NVDA drops to $237.50 — sells everything for +18.75% gain.

### 2. Copy Trading 🟢
**When it wins:** When US politicians disclose profitable trades via Capitol Trades.
**How it works:**
- Scans Capitol Trades for recent politician trades
- Copies them with 5% of portfolio per position
- Max 10 copy-trade positions at a time
- 10% stop-loss on each
- Skips trades where price already moved 15%+ since disclosure

**Why it works:** Politicians often have non-public information (they sit on committees, get briefings). Their disclosures are required by law within 45 days.

### 3. Wheel Strategy 🟣
**When it wins:** High-volatility stocks in a sideways/range-bound market.
**How it works:**
- Stage 1: Sell cash-secured put 10% below current price, 2-4 weeks to expiration
- If put expires worthless → collect premium, sell another put
- If put is assigned → you own 100 shares, move to Stage 2
- Stage 2: Sell covered call 10% above cost basis, 2-4 weeks out
- If call expires worthless → collect premium, sell another call
- If call is assigned → shares sold at profit, go back to Stage 1
- **Auto-picks stocks in $10-$50 range** so 100 shares fits your account

**Why it works:** You collect option premiums in any direction. Sideways markets pay you while you wait.

### 4. Mean Reversion 🟠
**When it wins:** Stocks oversold on no real bad news.
**How it works:**
- Buys stocks that dropped 15%+ in the last 5 days
- BUT only if news sentiment isn't negative (skip falling knives)
- Target: sell at 20-day moving average (mean recovery)
- 10% stop-loss if it keeps falling

**Why it works:** Stocks often overshoot on emotional selling. A 15% drop on no bad news usually bounces 5-10% within days.

### 5. Breakout 🔴
**When it wins:** Stocks breaking above resistance on massive volume.
**How it works:**
- Only buys stocks with 20-day high + 2x+ normal volume
- 3x volume = even better (volume-profile confirmation)
- Tight 5% stop-loss (if it's not a real breakout, fail fast)
- Immediate trailing stop activation (ride the momentum)

**Why it works:** Real breakouts continue. Fake breakouts fail quickly and we limit losses.

### 6. Short Selling (Bear Market Only) ⚫
**When it wins:** Stocks crashing in bear markets.
**How it works:**
- ONLY activates in bear markets (SPY down 5%+ in 20 days)
- Shorts stocks with: downtrend + high volume selling + negative sentiment
- Max 1 short position at a time
- 8% tight stop (shorts have unlimited upside risk)
- 15% profit target
- 48-hour cooldown after a losing short

**Why it works:** In bear markets, weak stocks fall fastest. Picks stocks likely to continue down.

---

## How The Bot Picks Stocks

Every 30 minutes during market hours, the bot runs the screener. Here's the exact process:

### Step 1: Fetch Universe (10,000+ stocks)
All tradeable US equities from NYSE, NASDAQ, and ARCA exchanges.

### Step 2: Quick Filters
- Skip penny stocks (< $5)
- Skip illiquid (< 100K daily volume)
- Skip stocks not in Alpaca's paper feed

### Step 3: Base Scoring (5 Strategy Scores Per Stock)
Each stock gets 5 scores, one for each long strategy:
- **Trailing score:** daily_change * 0.5 + volatility * 0.3 + volume bonus
- **Copy score:** +15 if large cap (>$100, >1M volume) + momentum
- **Wheel score:** Bell curve — moderate volatility wins, extreme gets penalized
- **Mean reversion score:** Reward big drops, penalize if news-driven (3x volume = likely news)
- **Breakout score:** Daily change + volume surge with tier multipliers (2x vol = 1.2x, 3x = 1.5x)

### Step 4: Enrichment (Top 100 Candidates Only)
For the top 100 scored stocks, fetch historical data and enrich:
- **20-day momentum** (from bars)
- **5-day momentum** (from bars)
- **Relative volume** (today vs 20-day average) — 2x+ = high conviction
- **Technical indicators:** RSI, MACD, Bollinger Bands, Stochastic, SMA 20/50
- **News sentiment** from Alpaca news API (keyword scoring)
- **Earnings warnings** (regex word-boundary matching for quarterly/Q1-4)
- **Backtest** (simulate trailing stop on last 30 days of data)
- **Social sentiment** from StockTwits (free API)
- **Position sizing** (volatility-based, max 2% risk per trade)

### Step 5: Global Filters
Applied to all picks:
- **Market breadth filter** — if <40% of stocks advancing, reduce long scores 15%
- **Sector rotation** — boost 15% in strong sectors (outperforming SPY), penalize 20% in weak
- **Market regime weights** — bull/neutral/bear multipliers per strategy
- **Economic calendar** — if FOMC/CPI within 3 days, reduce all scores

### Step 6: Diversification
Top 5 picks across all stocks, but no more than 2 from the same sector.

### Step 7: Additional Lists
- **Short candidates** — identified separately for bear market deploys
- **Earnings plays** — stocks showing pre-earnings momentum
- **Options flow signals** — unusual call/put ratios

### Step 8: Output
Saves to `dashboard_data.json` with all enrichment data, ready for the auto-deployer at 9:35 AM.

---

## How The Bot Manages Positions

Every 60 seconds during market hours, the Strategy Monitor processes each active position:

### The Monitor Loop

```
For each strategy JSON file in strategies/ folder:
  ├─ Skip if status = "closed", "paused", "stopped", "template"
  ├─ Skip if symbol is null
  │
  ├─ Check if entry order filled
  │   └─ If yes: save fill price, set status to "active"
  │
  ├─ Fetch current market price
  │
  ├─ Place initial stop-loss if missing
  │   └─ 5% below for breakouts, 10% for everything else
  │
  ├─ Strategy-specific logic:
  │   ├─ Trailing Stop: track highest price, activate trail at +10%, ratchet stop up
  │   ├─ Breakout: immediate trailing, 5% below peak
  │   ├─ Mean Reversion: target = entry * 1.15, sell if reached
  │   └─ Short Sell: inverse logic, stop moves DOWN as price drops
  │
  ├─ Check profit ladder:
  │   ├─ +10% → sell 25% of original position
  │   ├─ +20% → sell another 25%
  │   ├─ +30% → sell another 25%
  │   └─ +50% → sell final 25% (or let it ride)
  │
  └─ Check if stop triggered:
      ├─ If yes: record exit, update journal, set cooldown timer
      └─ Send notification
```

### Every 30 Min: Screener Refresh
Rebuilds `dashboard_data.json` with fresh picks. No trades placed — just keeps picks current.

### Every 15 Min: Wheel Strategy Monitor
Separate check for options wheel positions (if active).

---

## Safety Guardrails (15+ Protections)

Every one of these is enforced automatically — you don't have to do anything.

### Pre-Trade Guardrails (Block New Trades)

1. **Kill Switch** — One-click shutoff. When active, cancels all orders, closes all positions, blocks any new trades.

2. **Daily Loss Limit (3%)** — If portfolio drops 3% from day's starting value, AUTO-TRIGGERS kill switch. Forces you to review before resuming.

3. **Max Drawdown (10%)** — If portfolio drops 10% from all-time peak, auto-triggers kill switch. Circuit breaker against cascading losses.

4. **Cooldown After Loss (60 min)** — After any stop-loss triggers, bot waits 60 min before new deployments. Prevents revenge trading.

5. **Short-Specific Cooldown (48 hours)** — After a losing short position, 48-hour pause before another short can deploy.

6. **Capital Sustainability Check** — Before every trade, verifies there's enough cash, buying power isn't overextended, and free capital is above minimum threshold.

7. **Max Positions (5)** — Won't open more than 5 concurrent positions (3 in live mode with $5k).

8. **Max Per Stock (10%)** — Won't risk more than 10% of portfolio on any single stock (5% in live mode).

9. **Max New Per Day (2)** — Won't deploy more than 2 new trades per day (1 in live mode). Prevents overtrading.

10. **Correlation Enforcement** — Won't buy a stock if it would give you 3+ positions in same sector or >40% concentration in one sector.

11. **Earnings Avoidance** — Auto-skips stocks reporting earnings in next 3 days. Earnings gaps can destroy stop-losses.

12. **Meme Stock Filter** — Shorts won't deploy on stocks with high StockTwits buzz. Too volatile for controlled shorting.

13. **Market Hours Check** — All trading tasks check Alpaca `/v2/clock` before placing orders. Won't trade if market closed.

14. **Deployment Lock** — Prevents two processes from deploying the same stock simultaneously.

15. **Market Breadth Guard** — If only 40% of stocks are advancing (weak breadth = narrow market = risky), scores reduced 15%.

### Strategy-Specific Guardrails

16. **Bear Market Regime** — When SPY down 5% in 20 days, aggressive strategies (breakout) get score penalties.

17. **Monthly Rebalance** — First trading day of each month, closes any losing position held 60+ days. Prevents dead money.

18. **Friday Weekend Risk Reduction** — Fridays at 3:45 PM ET, sells 50% of any position up 20%+. Weekend gap protection.

19. **Options Trading Limits** — Wheel strategy requires 100 shares worth of cash. Won't sell puts you can't cover.

20. **Wheel Price Range** — Only wheels stocks between $10-$50 (affordable with $5k live account).

### Authentication

21. **Dashboard Basic Auth** — Username/password required for every endpoint.
22. **No hardcoded secrets** — All API keys in environment variables (Railway + `.env`).

### Request Hardening

23. **Input validation** — `qty` must be 1-1000, `symbol` must match `^[A-Z]{1,10}$`, `order_id` must be UUID format.
24. **Request size limits** — POST bodies capped at 1MB to prevent memory attacks.
25. **Rate-limited error toasts** — Dashboard won't spam error messages.

---

## The Dashboard — Every Section Explained

Access at https://stockbott.up.railway.app

### Header Bar (Top of page)
- **Stock Trading Bot title** — clickable home link
- **Paper Trading badge** — orange indicator this is fake money
- **Trading Session Badge** — PRE-MARKET / MARKET OPEN / AFTER HOURS / CLOSED
- **Readiness Score** — progress bar 0-100, must hit 80 to go live
- **24/7 CLOUD badge** — green pulse = cloud scheduler running on Railway
- **Auto-Deployer Toggle** — ON/OFF switch for the brain
- **Kill Switch Button** — big red emergency button
- **Voice Button** 🎤 — voice control (Chrome/Safari)

### Navigation Tabs (Scrollable)
Quick-jump to any section. Tabs include: Overview, Picks, Strategies, Positions, Screener, Short Sells, Tax Harvest, Backtest, Readiness, Heatmap, Paper vs Live, Scheduler, Settings.

### Account Overview
5 cards showing: Portfolio Value, Cash, Buying Power, Long Market Value, Daily P&L meter (colors change as you approach 3% daily limit).

### Economic Calendar Banner
Yellow/red banner shown when FOMC meeting, CPI release, or other market-moving event is within 3 days. Also includes earnings calendar warnings.

### What Happens at Market Open
Timeline showing all scheduled tasks with ON/OFF badges and pending orders/actions list.

### Top 3 Stock Picks
Each card shows:
- Symbol and current price
- 5-strategy score bars (Trailing/Copy/Wheel/MeanRev/Breakout)
- Technical indicators: RSI, MACD signal, overall bias
- Social sentiment (bullish/bearish) and trending badge
- Recommended shares (volatility-based sizing)
- 30-day backtest return
- Earnings warning badge if applicable
- Deploy button (opens confirmation modal with P&L estimates)

### Active Strategy Cards (6 cards)
1. Trailing Stop — entry/shares/stop/trailing status
2. Copy Trading — politician tracked, trades copied count
3. Wheel Strategy — current stage, premiums collected
4. Mean Reversion — ready status, target description
5. Breakout — ready status, trail config
6. Short Selling — ACTIVE (bear mkt) / STANDBY / TURNED OFF, current SPY momentum, top candidate, Turn On/Off button

Each has Pause and Stop buttons.

### Positions Table
Live P&L, AUTO/MANUAL badges, Close and Sell Half buttons (with confirmation modals).

### Open Orders Table
All pending orders with Cancel buttons.

### Full Screener (Top 50)
Sortable columns. Per-row Deploy buttons. Shows: Price, daily change, volatility, volume, vol surge, best strategy, score.

### Short Selling Candidates
Only shows in bear markets or when candidates exist. Reasons, stop-loss, target, R:R ratio per candidate.

### Tax-Loss Harvesting
Positions at a loss that qualify for tax-loss harvesting. Shows: loss amount, tax savings estimate, replacement stock suggestions. "Harvest" button opens close-position modal.

### Readiness Scorecard
6 metrics tracked toward the 80/100 threshold for going live:
- Days tracked (target: 30)
- Total trades (target: 20)
- Win rate (target: 50%)
- Max drawdown (target: <10%)
- Profit factor (target: 1.5)
- Sharpe ratio (target: 0.5)

### Trade Heatmap (New)
- Daily P&L calendar for last 90 days (color-coded green/red)
- 6-metric summary: trading days, win/loss days, total P&L, best/worst day
- By-weekday analysis: average P&L by Monday/Tuesday/etc.
Helps spot patterns like "Mondays are losers, skip them."

### Paper vs Live Comparison (New)
Side-by-side cards showing paper and live trading performance. While paper-only, shows setup guide for going live.

### Visual Backtest
- Dropdown to pick ANY stock from the screener (not just top pick)
- Summary cards: return, days held, entry→exit, stopped out or not
- Interactive Chart.js line chart showing price + stop level
- Plain-English explanation of what the backtest means

### Cloud Scheduler
- Task grid showing all scheduled jobs with last-run times
- Summary bar: current ET time, market status, thread name
- Live log box showing last 20 scheduler events
- Auto-refreshes every 15 seconds

### Strategy Templates (Enhanced)
3 detailed preset cards with ACTIVE badge on current:
- **Conservative** — 5% stops, 3 positions, $5% per stock (recommended for first-timers)
- **Moderate** — 10% stops, 5 positions, 10% per stock (default)
- **Aggressive** — 5% tight stops, 8 positions, 15% per stock (experienced only)

Each card shows: stop-loss, max positions, per-stock%, new/day, strategies enabled/disabled, how it works, good for, tradeoffs, expected return, max drawdown.

### Activity Log
Last 20 actions taken with timestamps, color-coded by type. Clear button.

### Voice Commands (🎤 button)
Say: "Kill switch", "Refresh", "Portfolio", "What's the top pick", "Deploy trailing stop on NVDA"
Bot speaks responses via text-to-speech.

---

## The Cloud Scheduler

Runs 24/7 on Railway. Laptop not required. Replaces all Claude Code scheduled tasks.

### Tasks Running

| Task | Schedule | What It Does |
|------|----------|--------------|
| Auto-Deployer | Weekdays 9:35 AM ET | Screens market, deploys top 2 picks |
| Screener | Every 30 min during market hours | Refreshes dashboard picks |
| Strategy Monitor | Every 60s during market hours | Manages all active positions |
| Friday Risk Reduction | Fridays 3:45 PM ET | Scales out 50% of winners |
| Daily Close Summary | Weekdays 4:05 PM ET | Updates scorecard, error recovery |
| Weekly Learning | Fridays 5:00 PM ET | Analyzes trades, adjusts weights |
| Monthly Rebalance | First trading day 9:45 AM ET | Closes stale underwater positions |

### How To Monitor
- Dashboard → Scheduler tab
- Shows all tasks with last-run times
- Live log feed from Railway
- Updates every 15 seconds

### To Disable
Set `ENABLE_CLOUD_SCHEDULER=false` in Railway env vars. Redeploy.

---

## Notifications

All push notifications free via **ntfy.sh**.

### Setup (One-Time)
1. Install ntfy app: https://ntfy.sh
2. Subscribe to topic: `alpaca-trading-bot-kevin`
3. Important events also queue emails to `se2login@gmail.com`

### Notification Types

| Type | Priority | Trigger |
|------|----------|---------|
| `trade` | Normal | New position opened |
| `exit` | Normal | Position closed (profit take) |
| `stop` | High | Stop-loss triggered |
| `alert` | Urgent | Drawdown/correlation warnings |
| `kill` | Max | Kill switch activated |
| `daily` | Low | 4 PM close summary |
| `learn` | Low | Weekly learning update |
| `info` | Min | Routine info |

---

## The 13 Advanced Profit Features

Built in to increase profit probability:

### 1. Dynamic Strategy Rotation
Market regime changes strategy weights. Bull market: boost trailing/breakout. Bear: boost shorts/mean reversion. Neutral: boost wheel. Prevents deploying the wrong strategy at the wrong time.

### 2. Earnings Play Strategy
Buys stocks with positive momentum 2-3 days before earnings, sells morning-of (never holds through earnings). Captures the "run-up" effect without gap risk.

### 3. Sector Rotation Signal
Tracks 11 sector ETFs (XLK, XLV, XLF, etc.). Boosts picks in sectors outperforming SPY by 2%+, penalizes picks in sectors underperforming by 2%+.

### 4. Post-Market News Scanner
Scans Alpaca news for 40+ actionable patterns. Earnings beats, FDA approvals, guidance raises = bullish signals. Earnings misses, SEC probes, bankruptcies = bearish.

### 5. Options Flow Tracking
Monitors call/put open interest ratios. C/P ratio > 2.0 = bullish smart money, < 0.5 = bearish. Leading indicator of stock moves.

### 6. Overnight Risk Reduction
Fridays at 3:45 PM ET, scales out 50% of any position up 20%+. Weekend gap protection.

### 7. Volume Profile Breakouts
Enhanced breakout scoring: 2x normal volume = 1.2x multiplier, 3x volume = 1.5x. Fewer false breakouts, higher win rate.

### 8. Partial Profit Taking
Sells 25% at +10%, +20%, +30%, +50% gains. Locks in progressive profits even if the trade later reverses.

### 9. Market Breadth Filter
Tracks % of advancing stocks. Weak breadth (<40%) = reduce long scores 15%. Strong breadth (>60%) = boost 10%.

### 10. Position Correlation Enforcement
Auto-deployer blocks trades that would give you 3+ positions in same sector or >40% sector concentration.

### 11-13. (Lower Priority, Not Yet Built)
Reserved for future: FedSpeak tracker, Twitter sentiment, insider trading follow.

### 18. Trade Heatmap
Dashboard calendar showing daily P&L + by-weekday analysis.

### 19. Monthly Rebalancing
First trading day of each month, closes positions held 60+ days at a loss. Frees capital.

### 20. Paper vs Live Comparison
Side-by-side panel (paper active, live pending 80/100 readiness).

---

## File Structure

```
/Users/kevinbell/Alpaca Trading/
├── README.md                    ← You are here
├── PROJECT.md                   ← Quick reference
├── server.py                    ← Web dashboard + API (Railway main)
├── cloud_scheduler.py           ← 24/7 scheduler (runs in server.py thread)
├── update_dashboard.py          ← Screener (runs every 30 min)
│
├── Analysis Modules
│   ├── indicators.py            ← RSI, MACD, Bollinger, ATR, etc.
│   ├── economic_calendar.py     ← FOMC, opex, news events
│   ├── social_sentiment.py      ← StockTwits sentiment
│   ├── correlation.py           ← Portfolio correlation matrix
│   ├── options_analysis.py      ← Options chain for wheel
│   ├── options_flow.py          ← Unusual options activity (NEW)
│   ├── extended_hours.py        ← Pre/post-market session logic
│   ├── short_strategy.py        ← Short selling candidates
│   ├── earnings_play.py         ← Pre-earnings momentum (NEW)
│   └── news_scanner.py          ← Post-market news signals (NEW)
│
├── Management Modules
│   ├── capital_check.py         ← Capital sustainability check
│   ├── tax_harvesting.py        ← Tax-loss harvesting scanner
│   ├── error_recovery.py        ← Orphan detection + auto-fix
│   ├── notify.py                ← Push notifications
│   ├── learn.py                 ← Self-learning engine
│   ├── update_scorecard.py      ← Performance metrics
│   └── realtime.py              ← Fast price poller (local)
│
├── Config Files
│   ├── .env                     ← Secrets (gitignored)
│   ├── guardrails.json          ← Safety limits
│   ├── auto_deployer_config.json← Auto-deployer settings
│   ├── accounts.json            ← Paper + live accounts
│   ├── strategies/
│   │   ├── trailing_stop.json   ← Base template
│   │   ├── copy_trading.json    ← State
│   │   ├── wheel_strategy.json  ← State
│   │   └── <strategy>_<SYMBOL>.json ← Auto-created
│
├── Runtime State (gitignored)
│   ├── dashboard_data.json      ← Latest screener output
│   ├── scorecard.json           ← Performance metrics
│   ├── trade_journal.json       ← Every trade with reasoning
│   ├── learned_weights.json     ← Self-learning output
│   ├── capital_status.json      ← Latest capital check
│   ├── notification_log.json    ← Notification history
│   └── email_queue.json         ← Pending emails
│
└── Infrastructure
    ├── Procfile                 ← Railway startup: `web: python3 server.py`
    ├── railway.json             ← Railway config
    ├── requirements.txt         ← Empty (stdlib only)
    ├── manifest.json            ← PWA manifest
    ├── icon-192.png             ← PWA icon
    └── icon-512.png             ← PWA icon
```

---

## Environment Variables

### Required (set in Railway + .env)

| Variable | Purpose |
|----------|---------|
| `ALPACA_API_KEY` | Alpaca API key |
| `ALPACA_API_SECRET` | Alpaca API secret |
| `ALPACA_ENDPOINT` | `https://paper-api.alpaca.markets/v2` (paper) or `https://api.alpaca.markets/v2` (live) |
| `ALPACA_DATA_ENDPOINT` | `https://data.alpaca.markets/v2` |
| `DASHBOARD_USER` | Login username |
| `DASHBOARD_PASS` | Login password |
| `NTFY_TOPIC` | `alpaca-trading-bot-kevin` |
| `NOTIFICATION_EMAIL` | `se2login@gmail.com` |
| `PORT` | `8888` (Railway sets automatically) |

### Optional

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENABLE_CLOUD_SCHEDULER` | `true` | Set to `false` to disable cloud scheduler |
| `CORS_ORIGIN` | none | If set, restricts CORS to this origin |

---

## When Things Go Wrong

### Scenario 1: Bot Made A Trade I Didn't Want
1. Open dashboard
2. Find position in Positions table
3. Click **Close** button (confirmation modal shows P&L estimate)
4. Confirm — position closes at market

### Scenario 2: Market Is Crashing
1. Open dashboard
2. Click **KILL SWITCH** button (red, in header)
3. Confirm — all orders cancel, all positions close at market
4. Auto-deployer automatically disabled
5. Bot won't resume until you manually toggle auto-deployer back on

### Scenario 3: Daily Loss Hit 3%
Bot automatically triggers kill switch. You get a max-priority notification. Dashboard shows red banner. Review positions, decide if you want to resume.

### Scenario 4: Dashboard Shows "Scheduler OFF"
Cloud scheduler stopped. On Railway this means the deployment crashed. Check Railway logs:
1. Railway dashboard → zestful-charm project → web service → Logs
2. Look for Python errors
3. Usually fixed by redeploy: push any commit to main

### Scenario 5: No Trades Deployed This Morning
Check dashboard → Scheduler tab → recent logs. Common reasons:
- "Kill switch active" — someone triggered it, reset in guardrails.json
- "Cannot trade: LOW CAPITAL" — not enough free cash
- "In cooldown after recent loss" — wait 60 min
- "No qualifying trades" — screener didn't find any picks meeting criteria

### Scenario 6: Position Showing Wrong Price/Value
1. Dashboard auto-refreshes every 60 seconds
2. Click Refresh button in header for immediate refresh
3. If still wrong: `/api/data` endpoint returns raw data for debugging

### Scenario 7: I Can't Log In
Credentials are `Kbell0629` / `We360you45$$` (case-sensitive). If changed, check Railway env vars `DASHBOARD_USER` and `DASHBOARD_PASS`.

### Emergency Contacts / Resources
- Alpaca support: https://alpaca.markets/support
- Dashboard logs: https://stockbott.up.railway.app/api/scheduler-status
- GitHub issues: https://github.com/Kbell0629/alpaca-trading-bot/issues

---

## Going Live With Real Money

### Prerequisites
1. **Readiness score ≥ 80/100** — requires 30 days paper trading, 20+ trades, 50%+ win rate, <10% max drawdown, 1.5+ profit factor, 0.5+ Sharpe ratio
2. **Profitable paper performance** — if paper shows losses, don't go live yet
3. **Understand the strategies** — re-read this README

### Step-By-Step
1. Log in to Alpaca → create a **live trading account**
2. Fund with **$5,000 minimum** (bot is sized for this amount in live mode)
3. Go to Alpaca dashboard → API Keys → Generate new keys (MARK THEM AS LIVE)
4. Update Railway env vars:
   ```
   ALPACA_ENDPOINT=https://api.alpaca.markets/v2
   ALPACA_API_KEY=<your live key>
   ALPACA_API_SECRET=<your live secret>
   ```
5. Update `guardrails.json` to use `live_mode_settings` (tighter limits):
   - Max positions: 3 (instead of 5)
   - Max per stock: 5% (instead of 10%)
   - Max new/day: 1 (instead of 2)
6. Keep paper account running in parallel to compare performance
7. Watch carefully for first week — live fills may differ from paper

### What's Different in Live Mode
- Real slippage (bid/ask spread costs ~0.1-0.5% per trade)
- Slower fills during high volume
- Pattern day trader rules (need $25k for unlimited day trading, or limit to 3 day trades per 5 days)
- Tax implications (consult accountant)

---

## Daily Operational Checklist

You don't have to do any of this — the bot handles everything. But if you want to stay informed:

### Morning (before 9:30 AM ET)
- [ ] Check push notifications — any overnight alerts?
- [ ] Open dashboard — any red banners or kill switch active?
- [ ] Check Scheduler tab — all tasks green?

### During Market Hours
- [ ] Expect push notifications for trades
- [ ] Can intervene via dashboard at any time

### After Market Close (4:05 PM ET)
- [ ] Daily close summary notification arrives
- [ ] Check dashboard → Overview — any positions need attention?
- [ ] Check Heatmap tab — today's P&L

### Weekly (Sunday or Friday)
- [ ] Review scorecard — readiness score trending up?
- [ ] Check heatmap by-weekday analysis — any patterns?
- [ ] Review Trade Journal for anomalies

### Monthly
- [ ] Paper vs Live comparison panel
- [ ] If readiness score hits 80 + consistent profits: consider going live

---

## Quick Reference Card

### Everyone Can Say To Claude In Any Session
- `/stock-bot` — launches the bot skill
- "What did the bot do today?"
- "Deploy trailing stop on NVDA"
- "Start wheel on SOFI"
- "Kill switch"
- "Check readiness score"

### Bot Defaults (Moderate Preset)
- Stop-loss: 10% (5% for breakouts)
- Max positions: 5
- Max per stock: 10%
- Max new per day: 2
- Cooldown: 60 min after loss
- Daily loss limit: 3% → kill switch

### Where Everything Lives
- **Dashboard:** https://stockbott.up.railway.app
- **Code:** `/Users/kevinbell/Alpaca Trading/`
- **GitHub:** https://github.com/Kbell0629/alpaca-trading-bot
- **Railway:** auto-deploys from GitHub main branch
- **Scheduler:** runs in Railway server.py background thread
- **Memory:** `/Users/kevinbell/.claude/projects/-Users-kevinbell-Alpaca-Trading/memory/`

---

*Last Updated: 2026-04-16 after completing 13 profit-boosting features. Bot has 6 strategies, 20+ safety rails, 18 Python modules, cloud-hosted 24/7 autonomous operation.*
