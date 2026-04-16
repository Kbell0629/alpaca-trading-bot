# Alpaca Stock Trading Bot

Autonomous stock trading bot using Alpaca paper trading API, managed by Claude Code scheduled tasks.

## Quick Start

```bash
# Start the interactive dashboard
cd "/Users/kevinbell/Alpaca Trading"
python3 server.py
# Open http://localhost:8888

# Refresh stock screener data (runs automatically via scheduled tasks)
python3 update_dashboard.py
```

## API Credentials (Paper Trading)

- **Endpoint:** `https://paper-api.alpaca.markets/v2`
- **Data:** `https://data.alpaca.markets/v2` (requires `&feed=iex` on free tier)
- **Key/Secret:** Set via environment variables (`ALPACA_API_KEY`, `ALPACA_API_SECRET`). See `.env` file for local dev.
- **Account:** PA3N3JCNBP02 ($100k paper cash, 2x margin)

## Files

| File | Purpose |
|------|---------|
| `server.py` | Local web server + interactive dashboard at localhost:8888 |
| `update_dashboard.py` | Full market screener (12k+ stocks), scoring engine, generates dashboard_data.json |
| `dashboard_data.json` | Latest screener output (auto-generated, don't edit) |
| `dashboard.html` | Static HTML dashboard (auto-generated fallback) |
| `auto_deployer_config.json` | Auto-deployer ON/OFF toggle + risk settings |
| `strategies/trailing_stop.json` | Active trailing stop strategy state |
| `strategies/copy_trading.json` | Copy trading strategy state |
| `strategies/wheel_strategy.json` | Wheel strategy state |
| `strategies/*_{SYMBOL}.json` | Per-stock strategy files (created by auto-deployer) |

## 5 Strategies

| # | Strategy | Signal | Stop-Loss | Best For |
|---|----------|--------|-----------|----------|
| 1 | **Trailing Stop** | Strong uptrend + momentum | 10%, trails 5% below peak | Riding trends |
| 2 | **Copy Trading** | Politician trade disclosures | 10% per position | Insider edge |
| 3 | **Wheel** | High volatility, range-bound, $10-$50 price | Built into options | Premium income |
| 4 | **Mean Reversion** | 15%+ drop on no bad news | 10%, targets 20-day avg | Oversold bounces |
| 5 | **Breakout** | 20-day high on 2x+ volume | Tight 5% | Explosive moves |

## Scheduled Tasks (Active)

| Task ID | Schedule | What It Does |
|---------|----------|-------------|
| `auto-deployer` | 9:35 AM ET weekdays | Brain: screens market, picks top 2 stocks, auto-deploys |
| `strategy-monitor` | Every 5 min, market hours | Universal monitor: manages ALL active trailing stop, mean reversion, and breakout positions across any stock |
| `copy-trading-monitor` | 10 AM ET weekdays | Scans Capitol Trades, copies politician moves |
| `wheel-strategy-monitor` | Every 15 min, market hours | Manages put/call selling cycle |

Disabled tasks: `tsla-morning-check`, `tsla-midmorning-check`, `tsla-midday-check`, `tsla-afternoon-check`, `tsla-close-check` (replaced by `tsla-strategy-monitor`).

## 10 Scoring Improvements

The screener in `update_dashboard.py` applies these filters/enhancements to every stock:

1. Multi-timeframe momentum (5d + 20d bars for top 100 candidates)
2. Relative volume (today vs 20-day average)
3. Sector diversification (max 2 per sector in top 5)
4. Earnings date avoidance (news keyword scan, -10 penalty)
5. Dynamic position sizing (volatility-based, max 2% risk, max 10% portfolio)
6. Profit-taking ladder (sell 25% at +10%, +20%, +30%, +50%)
7. SPY market regime (bull/neutral/bear adjusts strategy scores)
8. News sentiment (positive/negative keyword analysis from Alpaca news API)
9. Daily P&L tracking (alerts if portfolio drops >3%)
10. Backtesting (simulates trailing stop on 30-day history for top 10)

## Dashboard Features

- **Account overview** with portfolio value, cash, buying power
- **Auto-deployer toggle** (ON/OFF) in header
- **Top 3 stock picks** with all 5 strategy score bars + deploy buttons
- **Active strategy cards** for all 5 strategies with status
- **Positions table** with Close / Sell Half buttons
- **Orders table** with Cancel buttons
- **Full screener** (top 50, sortable columns) with per-row Deploy buttons
- **Activity log** with timestamped color-coded entries
- **Confirmation modals** on every financial action showing plain-English P&L estimates
- **Auto-refresh** every 60 seconds

## Safety Rules

- Max 2 new positions per day
- Max 10% of portfolio per stock
- Every position gets a stop-loss
- No trading in bear market (except wheel)
- No trading stocks with earnings warnings
- No deployment if capital < $1,000
- All financial actions require user confirmation via modal

## Common Commands

```
# Tell Claude in any session:
/stock-bot                          # Launch bot skill, see picks
"Run trailing stop on NVDA"         # Deploy trailing stop
"Start wheel on SOFI"               # Deploy wheel strategy
"Set up copy trading"               # Start copy trading
"Show me the dashboard"             # Open dashboard
"Pause the auto-deployer"           # Turn off auto mode
"What's my P&L?"                    # Check positions
```

## Architecture

```
Auto-deployer (daily 9:35 AM)
  -> Runs update_dashboard.py (screens 12k+ stocks, ~47s)
  -> Reads dashboard_data.json (top picks with scores)
  -> Deploys top 2 via Alpaca API
  -> Writes strategy files to strategies/

Strategy monitors (every 5-15 min)
  -> Read their strategy JSON files
  -> Check prices via Alpaca data API
  -> Manage stops, ladders, profit takes
  -> Update strategy JSON files

Dashboard server (localhost:8888)
  -> Serves interactive HTML with JS
  -> Proxies Alpaca API calls
  -> Handles deploy/cancel/close via POST endpoints
  -> Reads dashboard_data.json for screener data
```

## Push Notifications (ntfy.sh)

Free push notifications to your phone — no account needed.

**Setup:** Install the [ntfy app](https://ntfy.sh) on your phone, subscribe to topic: `alpaca-trading-bot-kevin`

**Notification types:**
| Type | Priority | When |
|------|----------|------|
| trade | Normal | New trade placed |
| exit | Normal | Position closed with profit |
| stop | High | Stop-loss triggered |
| alert | Urgent | Warnings (drawdown limit, correlation) |
| kill | Max | Kill switch activated |
| daily | Low | Daily close summary |
| learn | Low | Weekly learning update |

## Cloud Hosting (Railway)

The dashboard server runs on Railway for 24/7 access.

**GitHub repo:** https://github.com/Kbell0629/alpaca-trading-bot

**Auto-deploy:** Railway auto-deploys from the `main` branch on every push.

**Environment variables** are set on Railway (not in code):
- `ALPACA_API_KEY`, `ALPACA_API_SECRET`
- `ALPACA_ENDPOINT`, `ALPACA_DATA_ENDPOINT`
- `NTFY_TOPIC`, `NOTIFICATION_EMAIL`

**Local dev:** Uses `.env` file (not tracked in git). Run `python3 server.py` on localhost:8888.

**Deploy manually:**
```bash
cd "/Users/kevinbell/Alpaca Trading"
railway up
```

**Railway setup (one-time):**
```bash
railway login          # Opens browser for auth
railway link           # Link this directory to your Railway project
railway variables set ALPACA_API_KEY=<key> ALPACA_API_SECRET=<secret> ...
```

**Note:** Scheduled tasks (auto-deployer, strategy-monitor, etc.) still run locally via Claude Code. Only the dashboard/API server is on Railway. The server reads from local strategy files and dashboard_data.json, so after Railway deployment you'll need to set up a way to sync data (e.g., use the Alpaca API directly from Railway instead of reading local files).
