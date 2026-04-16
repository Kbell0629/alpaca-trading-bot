# Alpaca Stock Trading Bot

Autonomous stock trading bot using Alpaca paper trading API, managed by Claude Code scheduled tasks. Self-learning, auto-deploying, with kill switch and capital management.

**GitHub:** https://github.com/Kbell0629/alpaca-trading-bot

## Quick Start

```bash
# Start the interactive dashboard
cd "/Users/kevinbell/Alpaca Trading"
python3 server.py
# Open http://localhost:8888

# Refresh stock screener data (runs automatically via scheduled tasks)
python3 update_dashboard.py
```

## Environment Variables

All secrets are in environment variables (not hardcoded). Local dev uses `.env` file (gitignored).

| Variable | Value | Where |
|----------|-------|-------|
| `ALPACA_API_KEY` | Paper trading key | `.env` locally, Railway vars in cloud |
| `ALPACA_API_SECRET` | Paper trading secret | `.env` locally, Railway vars in cloud |
| `ALPACA_ENDPOINT` | `https://paper-api.alpaca.markets/v2` | Change to `https://api.alpaca.markets/v2` for live |
| `ALPACA_DATA_ENDPOINT` | `https://data.alpaca.markets/v2` | Same for paper and live |
| `NTFY_TOPIC` | `alpaca-trading-bot-kevin` | Push notification topic |
| `NOTIFICATION_EMAIL` | `se2login@gmail.com` | Email alerts for important events |
| `PORT` | `8888` | Dashboard server port (Railway sets automatically) |

**Account:** PA3N3JCNBP02 ($100k paper cash, 2x margin)

## Files

| File | Purpose |
|------|---------|
| `server.py` | Web server + interactive dashboard (localhost:8888 or Railway) |
| `update_dashboard.py` | Full market screener (12k+ stocks), 10 scoring improvements, generates dashboard_data.json |
| `update_scorecard.py` | Performance metrics: win rate, Sharpe/Sortino ratios, readiness score |
| `error_recovery.py` | Finds orphan positions, missing stop-losses, stale strategies — auto-fixes |
| `capital_check.py` | Capital sustainability: can we afford more trades? Are we overextended? |
| `learn.py` | Self-learning engine: analyzes trade journal, adjusts strategy weights weekly |
| `notify.py` | Push notifications (ntfy.sh) + email queue (se2login@gmail.com) |
| `auto_deployer_config.json` | Auto-deployer ON/OFF toggle + risk settings |
| `guardrails.json` | Kill switch, daily loss limit, max drawdown, position limits |
| `scorecard.json` | Running performance metrics + paper-to-live readiness score |
| `trade_journal.json` | Every trade with full reasoning + daily portfolio snapshots |
| `learned_weights.json` | Strategy multipliers + signal boosts from self-learning |
| `capital_status.json` | Latest capital sustainability check results |
| `strategies/*.json` | Per-strategy state files (created by auto-deployer) |

## 5 Strategies

| # | Strategy | Signal | Stop-Loss | Best For |
|---|----------|--------|-----------|----------|
| 1 | **Trailing Stop** | Strong uptrend + momentum | 10%, trails 5% below peak | Riding trends |
| 2 | **Copy Trading** | Politician trade disclosures | 10% per position | Insider edge |
| 3 | **Wheel** | High volatility, $10-$50 price | Built into options | Premium income |
| 4 | **Mean Reversion** | 15%+ drop on no bad news | 10%, targets 20-day avg | Oversold bounces |
| 5 | **Breakout** | 20-day high on 2x+ volume | Tight 5% | Explosive moves |

## Scheduled Tasks

| Task ID | Schedule | What It Does |
|---------|----------|-------------|
| `auto-deployer` | 9:35 AM ET weekdays | Brain: capital check → screen 12k stocks → pick top 2 → deploy → log to journal → notify |
| `strategy-monitor` | Every 5 min, market hours | Universal: manages ALL active positions (stops, ladders, profit takes) across any stock |
| `copy-trading-monitor` | 9:35 AM ET weekdays | Scans Capitol Trades, copies politician trades |
| `wheel-strategy-monitor` | Every 15 min, market hours | Auto-picks affordable stocks, manages put/call selling |
| `daily-close-summary` | 4:05 PM ET weekdays | Updates scorecard, takes daily snapshot, runs error recovery, sends summary |
| `weekly-learning` | Friday 2 PM PT | Self-learning: analyzes trade journal, adjusts strategy weights |

## 10 Scoring Improvements

1. Multi-timeframe momentum (5d + 20d)
2. Relative volume (today vs 20-day average)
3. Sector diversification (max 2 per sector in top 5)
4. Earnings date avoidance (-10 penalty)
5. Dynamic position sizing (volatility-based, max 2% risk)
6. Profit-taking ladder (sell 25% at +10/+20/+30/+50%)
7. SPY market regime (bull/neutral/bear adjusts scores)
8. News sentiment (Alpaca news API keyword analysis)
9. Daily P&L tracking (alerts if >3% loss)
10. Backtesting (30-day trailing stop simulation)

## 10 Production Readiness Features

1. **Performance Scorecard** — win rate, avg win/loss, profit factor, Sharpe/Sortino ratios
2. **Push + Email Notifications** — ntfy.sh to phone, email to se2login@gmail.com
3. **Cloud Hosting** — Railway auto-deploys from GitHub main branch
4. **Paper-to-Live Readiness Score** — 0-100, must hit 80+ before going live (30 days, 50% win rate, <10% drawdown, 1.5 profit factor, 0.5 Sharpe)
5. **Trade Journal** — every trade logged with full reasoning and scores
6. **Error Recovery** — auto-fixes orphan positions, missing stops, stale strategies
7. **A/B Testing** — compares strategy performance head-to-head in scorecard
8. **Risk-Adjusted Metrics** — Sharpe ratio, Sortino ratio, max drawdown tracking
9. **Correlation Guard** — warns if 3+ positions in same sector
10. **Market Hours Awareness** — won't waste API calls outside trading hours

## Safety & Guard Rails

| Guard Rail | Setting | What It Does |
|-----------|---------|-------------|
| Kill Switch | Dashboard button | Cancels all orders, closes all positions, halts everything |
| Daily Loss Limit | 3% | Auto-triggers kill switch if breached |
| Max Drawdown | 10% | Circuit breaker from peak portfolio value |
| Max Positions | 5 | Won't open more until one closes |
| Max Per Stock | 10% | Limits single-stock exposure |
| Capital Check | Before every deploy | Won't trade if insufficient free cash |
| Cooldown | 60 min after loss | Prevents revenge trading |
| Earnings Filter | Auto-skip | Won't trade stocks near earnings |
| Bear Market | Pauses aggressive strategies | Only wheel runs in bear markets |

## Dashboard Features

- Account overview with P&L meters
- Kill switch button (red, with confirmation modal)
- Auto-deployer toggle (ON/OFF)
- "What Happens at Market Open" timeline
- Top 3 stock picks with 5 strategy score bars + Deploy buttons
- 5 active strategy cards with Pause/Stop buttons
- Positions table with Close / Sell Half buttons
- Orders table with Cancel buttons
- Full screener (top 50, sortable) with per-row Deploy
- Activity log with timestamps and color coding
- Guard rail meters (daily loss, drawdown)
- Confirmation modals on ALL financial actions showing plain-English P&L estimates
- Auto-refresh every 60 seconds

## Notifications

| Type | Push (Phone) | Email | When |
|------|-------------|-------|------|
| Trade placed | Yes | Yes | New position opened |
| Position closed | Yes | Yes | Stop hit or profit taken |
| Stop triggered | Yes (high priority) | Yes | Stop-loss fired |
| Kill switch | Yes (max priority) | Yes | Emergency halt |
| Daily summary | Yes | Yes | Market close recap |
| Learning update | Yes | No | Weekly weight adjustment |
| Info | Yes | No | Routine updates |

**Phone setup:** Install [ntfy app](https://ntfy.sh), subscribe to topic `alpaca-trading-bot-kevin`

## Self-Learning Engine

Runs weekly (Fridays). Analyzes the trade journal and adjusts strategy scoring:
- **Strategy multipliers:** Boosts strategies with >60% win rate (1.2x), reduces <40% (0.7x)
- **Signal analysis:** Identifies which entry signals (momentum, volume, sentiment) correlate with wins
- **Price range preferences:** Finds optimal price range per strategy
- **Holding period:** Finds optimal trade duration per strategy
- **Confidence:** Low (<20 trades) → Medium (20-50) → High (50+)

## Cloud Hosting (Railway)

**GitHub:** https://github.com/Kbell0629/alpaca-trading-bot
**Auto-deploy:** Push to `main` → Railway deploys automatically

### Railway Setup (one-time, in Terminal)
```bash
cd "/Users/kevinbell/Alpaca Trading"
railway login                    # Opens browser for auth
railway init                     # Create project "alpaca-trading-bot"
railway variables set ALPACA_API_KEY=<your-api-key>
railway variables set ALPACA_API_SECRET=<your-api-secret>
railway variables set ALPACA_ENDPOINT=https://paper-api.alpaca.markets/v2
railway variables set ALPACA_DATA_ENDPOINT=https://data.alpaca.markets/v2
railway variables set NTFY_TOPIC=alpaca-trading-bot-kevin
railway variables set NOTIFICATION_EMAIL=se2login@gmail.com
railway up                       # Deploy
```

Then connect GitHub: Railway dashboard → Project → Settings → Connect Repo → `Kbell0629/alpaca-trading-bot`

**Note:** Scheduled tasks run locally via Claude Code (they need Claude). The dashboard server runs on Railway for 24/7 access.

## Going Live (Future)

When the readiness score hits 80+ after 30 days of paper trading:

1. Create a live Alpaca account and fund with $5k
2. Generate live API keys (endpoint: `api.alpaca.markets` instead of `paper-api.alpaca.markets`)
3. Update environment variables:
   - `ALPACA_API_KEY` → live key
   - `ALPACA_API_SECRET` → live secret
   - `ALPACA_ENDPOINT` → `https://api.alpaca.markets/v2`
4. Update guardrails for $5k:
   - Max positions: 3
   - Max per stock: 5% ($250)
   - Max new per day: 1
   - Wheel: stocks under $50 only
5. Keep paper account running in parallel to compare

## Architecture

```
Morning (9:35 AM ET):
  capital_check.py → Can we trade?
  auto-deployer → update_dashboard.py (screen 12k stocks, ~47s)
                 → Pick top 2, deploy via Alpaca API
                 → Log to trade_journal.json
                 → notify.py (push + email)
                 → update_scorecard.py
                 → error_recovery.py

During Market Hours:
  strategy-monitor (5 min) → Manage all active positions
  wheel-strategy-monitor (15 min) → Manage options wheel
  
Market Close (4:05 PM ET):
  daily-close-summary → Snapshot portfolio
                      → Update scorecard
                      → Check readiness
                      → Send daily summary

Weekly (Friday 2 PM):
  learn.py → Analyze journal → Adjust weights → Better picks next week
```

## Common Commands

```
/stock-bot                          # Launch bot skill, see picks
"Run trailing stop on NVDA"         # Deploy trailing stop
"Start wheel on SOFI"               # Deploy wheel strategy
"Set up copy trading"               # Start copy trading
"Show me the dashboard"             # Open dashboard
"Pause the auto-deployer"           # Turn off auto mode
"What's my P&L?"                    # Check positions
"Check readiness score"             # Am I ready for live?
"Run the learning engine"           # Force a learning cycle
```
