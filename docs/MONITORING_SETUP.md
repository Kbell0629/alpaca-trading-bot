# Monitoring Setup Guide

Round-11 expansion items 18-20. All three services have free tiers — you never have to pay. Each is independently optional; the bot works fine without any of them.

---

## 1. Sentry (free — 5K errors/month)

Create a free Sentry account + project at https://sentry.io/signup/, then
grab the DSN from Project Settings → Client Keys (DSN). Format looks like:

```
https://YOUR_PUBLIC_KEY@oYOUR_ORG_ID.ingest.us.sentry.io/YOUR_PROJECT_ID
```

> **NOTE**: Do not commit real DSNs to source control. Earlier revisions of
> this file embedded a live DSN; if you deployed against that DSN, rotate it
> in Sentry (Project Settings → Client Keys → disable old key).

### What you need to do

Add one env var on Railway:

```
SENTRY_DSN=https://YOUR_PUBLIC_KEY@oYOUR_ORG_ID.ingest.us.sentry.io/YOUR_PROJECT_ID
```

1. Railway dashboard → your bot project → Variables
2. New Variable → Name: `SENTRY_DSN` → Value: (paste the DSN above)
3. Save. Railway auto-redeploys.

That's it. On the next deploy, `observability.py` auto-detects the DSN and starts capturing exceptions.

### Verify it's working

Visit `https://se2-events-inc.sentry.io/issues/?project=alpaca-trading-bot` in a day or two — if no issues show up, that's good (means zero errors). You can also trigger a test event:

```
railway run python3 observability.py test
```

---

## 2. UptimeRobot (free forever — 50 monitors / 5-min interval)

Independent check that pings your dashboard every 5 minutes. Free tier is enough (we only need 1 monitor).

### 2-minute setup

1. Go to https://uptimerobot.com/signUp
2. Sign up with any email (free tier, no credit card)
3. After signup → **"+ Create new monitor"**
4. Settings:
   - **Monitor Type:** HTTP(s)
   - **Friendly Name:** Alpaca Trading Bot
   - **URL:** `https://stockbott.up.railway.app/healthz`
   - **Monitoring Interval:** 5 minutes (free tier minimum)
5. **Alert Contacts:** add your email
6. **Create Monitor**

### What it does

- Hits `/healthz` every 5 min
- If 2 consecutive checks fail (= 10 min down), emails you
- Independent of Railway's own monitoring — catches Railway-level outages

**Optional:** UptimeRobot can also push to ntfy.sh, but your ntfy topic is already wired to the bot — the email alert is sufficient.

---

## 3. PagerDuty — SKIPPED

Marketed as free, but the free tier requires credit card + has 14-day trial restrictions on features that matter for trading bots (SMS, phone calls). We already have critical alerts via **ntfy.sh** (push notifications to your phone) which is genuinely free.

### What's already wired for critical alerts

The bot's `critical_alert(title, body, ...)` helper already fires:

| Channel | When it fires | How to configure |
|---|---|---|
| **Sentry** | Error-level event appears in Sentry issues feed | Set SENTRY_DSN (step 1 above) |
| **ntfy.sh push** | Instant phone push notification | Already wired — your `ntfy_topic` user setting |
| **Email** | Queued via Gmail SMTP | Already wired — your `notification_email` setting |

Events that trigger `critical_alert`:
- Kill switch auto-triggered (daily loss, max drawdown)
- Scheduler thread hung >5 min
- Circuit breaker open on yfinance

Adding new critical-alert call sites is trivial — see `observability.critical_alert()` signature.

---

## Quick health check after setup

1. **Sentry**: issues list should be empty + project marked "Connected"
2. **UptimeRobot**: dashboard shows the monitor in green "Up" state within 5 min
3. **ntfy.sh**: you already get trade notifications — no new setup

If Sentry shows errors after deploy: **good news, it's catching real issues**. Check the stack trace and either fix or mark the issue as "Ignored" if it's benign (e.g. client disconnects).

Total ongoing cost: **$0/month** for all three services at this volume.
