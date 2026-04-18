#!/usr/bin/env python3
"""
Rich notification email templates.

Design
------
Short push (ntfy) stays terse — it's a phone alert, not a manual.
Email gets the teaching-level context: what just happened, why the bot
did it, what might happen next, whether the user needs to act. Written
for someone who's heard the word "wheel" but hasn't traded one.

Each template returns (subject, body). Body is plain-text with
underlined section dividers — Gmail renders it cleanly, and there's
no HTML rendering variance across mail clients.

Call pattern (see cloud_scheduler.notify_rich):
    short = "Profit taken on AAPL at $198.50 (+12.3%)"
    subj, body = notification_templates.profit_target_hit(
        symbol="AAPL", strategy="breakout",
        entry_price=176.80, exit_price=198.50,
        shares=25, pnl=542.50, pnl_pct=12.3,
        hold_days=4,
    )
    notify_rich(user, short, "exit", rich_subject=subj, rich_body=body)
"""
from __future__ import annotations

import os
from typing import Any


DIVIDER = "━" * 44
DASH = "—" * 44


def _footer() -> str:
    dash_url = os.environ.get("DASHBOARD_URL") or os.environ.get("PUBLIC_BASE_URL", "")
    dash_url = dash_url.rstrip("/") if dash_url else ""
    lines = []
    if dash_url:
        lines.append(f"View full dashboard: {dash_url}")
    lines.append("Change these emails in Settings → Notifications.")
    return "\n".join(lines)


def _fmt_money(v):
    try:
        return f"${float(v):,.2f}"
    except (TypeError, ValueError):
        return "$—"


def _fmt_pct(v, decimals=2, signed=True):
    try:
        fmt = f"{{:+.{decimals}f}}%" if signed else f"{{:.{decimals}f}}%"
        return fmt.format(float(v))
    except (TypeError, ValueError):
        return "—"


def _fmt_signed_money(v):
    try:
        f = float(v)
        sign = "+" if f >= 0 else "−"
        return f"{sign}${abs(f):,.2f}"
    except (TypeError, ValueError):
        return "$—"


# ============================================================================
# Entry opened
# ============================================================================
def position_opened(symbol: str, strategy: str, shares: int,
                    entry_price: float, stop_price: float | None = None,
                    target_price: float | None = None,
                    reasoning: dict | None = None) -> tuple[str, str]:
    """A new position just opened via auto-deployer or manual deploy."""
    lines = []
    lines.append(f"📈 NEW POSITION — {symbol} ({strategy.replace('_',' ').title()})")
    lines.append(f"Bought {shares} shares of {symbol} at {_fmt_money(entry_price)}")
    lines.append("")

    lines.append(DIVIDER)
    lines.append("WHAT JUST HAPPENED")
    lines.append(DIVIDER)
    lines.append(_entry_what_happened(strategy, symbol, shares, entry_price))
    lines.append("")

    lines.append(DIVIDER)
    lines.append("HOW THIS STRATEGY WORKS")
    lines.append(DIVIDER)
    lines.append(_strategy_explainer(strategy))
    lines.append("")

    lines.append(DIVIDER)
    lines.append("POSITION DETAILS")
    lines.append(DIVIDER)
    lines.append(f"  Symbol:        {symbol}")
    lines.append(f"  Strategy:      {strategy.replace('_',' ').title()}")
    lines.append(f"  Shares bought: {shares}")
    lines.append(f"  Entry price:   {_fmt_money(entry_price)}")
    lines.append(f"  Cost basis:    {_fmt_money(entry_price * shares)}")
    if stop_price is not None:
        risk = (entry_price - stop_price) * shares
        stop_pct = ((stop_price / entry_price) - 1) * 100 if entry_price else 0
        lines.append(f"  Initial stop:  {_fmt_money(stop_price)} ({_fmt_pct(stop_pct)})")
        lines.append(f"  Max loss:      ~{_fmt_money(risk)} if the stop hits before any gain")
    if target_price is not None:
        gain = (target_price - entry_price) * shares
        target_pct = ((target_price / entry_price) - 1) * 100 if entry_price else 0
        lines.append(f"  Profit target: {_fmt_money(target_price)} ({_fmt_pct(target_pct)})")
        lines.append(f"  Target P&L:    ~{_fmt_money(gain)} if the target hits")
    if reasoning:
        if reasoning.get("best_score") is not None:
            lines.append(f"  Screener score:{reasoning.get('best_score')}")
        if reasoning.get("momentum_20d") is not None:
            lines.append(f"  20-day momentum: {_fmt_pct(reasoning.get('momentum_20d'))}")
    lines.append("")

    lines.append(DIVIDER)
    lines.append("WHAT COULD HAPPEN NEXT")
    lines.append(DIVIDER)
    lines.append(_entry_what_next(strategy))
    lines.append("")

    lines.append(DIVIDER)
    lines.append("DO YOU NEED TO DO ANYTHING?")
    lines.append(DIVIDER)
    lines.append(
        "No. The bot manages this position automatically — monitors\n"
        "the stop, raises the floor on gains, closes the position when\n"
        "conditions trigger. You'll get another email when the position\n"
        "closes (either at a profit, at the stop, or at a time-based\n"
        "exit).\n\n"
        "If you want to intervene: open the dashboard → Positions →\n"
        "Close Position, or use the emergency KILL switch to shut down\n"
        "all trading immediately."
    )
    lines.append("")

    lines.append(_footer())
    return f"[Trading Bot] Opened {symbol} — {strategy.replace('_',' ').title()}", "\n".join(lines)


def _entry_what_happened(strategy: str, symbol: str, shares: int, entry: float) -> str:
    base = (f"The bot's auto-deployer found {symbol} as a top pick in today's\n"
            f"screen of ~12,000 US stocks and opened a long position: {shares}\n"
            f"shares at {_fmt_money(entry)}.")
    extra = {
        "breakout": (" This was a Breakout signal — the stock broke above its 20-day\n"
                     "high on heavy volume, which historically continues for several\n"
                     "days before fading."),
        "mean_reversion": (" This was a Mean Reversion signal — the stock dropped sharply\n"
                           "on modest volume (no real bad news), and historically\n"
                           "bounces back toward its 20-day average within 5-10 days."),
        "pead": (" This was a PEAD signal — the stock just reported quarterly\n"
                 "earnings and beat analyst estimates by 5%+. The Post-Earnings\n"
                 "Announcement Drift anomaly (Bernard & Thomas, 1989) shows\n"
                 "strong beats tend to drift upward for 30-60 days as\n"
                 "institutions gradually build positions."),
        "trailing_stop": (" This is a Trailing Stop entry — a long position protected\n"
                          "by a stop that moves up as the price rises (never down)."),
    }
    return base + extra.get(strategy, "")


def _entry_what_next(strategy: str) -> str:
    common = (
        "Three things can happen from here:\n\n"
        "  1. The stock goes up. The trailing stop follows it up (only\n"
        "     moves UP, never down), locking in gains. When the stock\n"
        "     eventually reverses and hits the trailing floor, the bot\n"
        "     closes the position at the floor price.\n\n"
        "  2. The stock goes down. The initial stop-loss limits your\n"
        "     max loss. The bot closes at the stop and you eat the\n"
        "     defined risk — this is by design, and the math only\n"
        "     works if you take the small losses cleanly.\n\n"
        "  3. The stock goes sideways. The bot holds, the monitor keeps\n"
        "     checking every 60 seconds, and the position waits for one\n"
        "     of the above.\n"
    )
    pead_extra = (
        "\nFor PEAD specifically, there's a fourth path: the 60-day drift\n"
        "window expires. If neither the trailing stop nor a pre-earnings\n"
        "close has triggered by then, the bot closes the position at\n"
        "market. And if the NEXT earnings announcement is within 5 days,\n"
        "the bot closes early — you don't want to ride one surprise into\n"
        "another."
    )
    mr_extra = (
        "\nMean Reversion also has a +15% profit target — if the stock\n"
        "bounces back quickly, the bot takes the win and moves on\n"
        "rather than hoping for more."
    )
    if strategy == "pead":
        return common + pead_extra
    if strategy == "mean_reversion":
        # Round-11 audit: mean_reversion has NO trailing stop — it
        # has a hard 10% stop and a fixed +15% target. The generic
        # `common` block talks about "trailing stop follows it up"
        # which is false for MR. Return a MR-specific explainer.
        return (
            "Three things can happen from here:\n\n"
            "  1. The stock bounces back to the 20-day average. At a\n"
            "     +15% gain from entry, the bot sells at market and\n"
            "     books the win. Mean Reversion's edge is speed —\n"
            "     typical hold is 3-7 days.\n\n"
            "  2. The stock keeps falling. At a 10% loss from entry,\n"
            "     the hard stop triggers and the position closes.\n"
            "     Mean Reversion has no trailing stop — you either\n"
            "     get the bounce or you eat the defined loss.\n\n"
            "  3. The stock chops sideways. The bot holds and the\n"
            "     monitor keeps checking every 60s. No time-based\n"
            "     exit — MR waits for either the target or the stop.\n"
        )
    return common


# ============================================================================
# Profit target hit / trailing-stop exit / ladder sell
# ============================================================================
def profit_target_hit(symbol: str, strategy: str,
                       entry_price: float, exit_price: float,
                       shares: int, pnl: float, pnl_pct: float,
                       hold_days: int | None = None,
                       reason: str = "target_hit") -> tuple[str, str]:
    direction = "profit" if pnl >= 0 else "loss"
    lines = []
    lines.append(f"💰 POSITION CLOSED — {symbol} ({strategy.replace('_',' ').title()})")
    lines.append(f"Closed at {_fmt_money(exit_price)} — "
                 f"P&L {_fmt_signed_money(pnl)} ({_fmt_pct(pnl_pct)})")
    lines.append("")

    lines.append(DIVIDER)
    lines.append("WHAT JUST HAPPENED")
    lines.append(DIVIDER)
    reason_text = {
        "target_hit": ("The stock hit the profit target. The bot sold the\n"
                       "entire position at market and locked in the gain."),
        "trail_exit": ("The trailing stop triggered — the stock pulled back\n"
                       "from its high by the trail distance, and the bot\n"
                       "sold at the trailing floor. The gains above the\n"
                       "entry are yours."),
        "pead_window_complete": ("The PEAD 60-day drift window closed. "
                                 "Per Bernard & Thomas, post-earnings drift\n"
                                 "fades after ~60 days, so the bot exits\n"
                                 "on a timer rather than hoping for more."),
        "pre_earnings_exit": ("The next earnings announcement is within 5\n"
                              "days. The bot closed the position to avoid\n"
                              "re-rolling the dice on a fresh surprise\n"
                              "(which could go either way)."),
    }.get(reason, "The position closed.")
    lines.append(reason_text)
    lines.append("")

    lines.append(DIVIDER)
    lines.append("THE NUMBERS")
    lines.append(DIVIDER)
    lines.append(f"  Symbol:      {symbol}")
    lines.append(f"  Strategy:    {strategy.replace('_',' ').title()}")
    lines.append(f"  Entry:       {_fmt_money(entry_price)}")
    lines.append(f"  Exit:        {_fmt_money(exit_price)}")
    lines.append(f"  Shares:      {shares}")
    lines.append(f"  Gross P&L:   {_fmt_signed_money(pnl)}  ({_fmt_pct(pnl_pct)})")
    if hold_days is not None:
        lines.append(f"  Held for:    {hold_days} day{'s' if hold_days != 1 else ''}")
        if hold_days > 0 and pnl_pct is not None:
            annualized = (pnl_pct / hold_days) * 365
            lines.append(f"  Annualized:  {_fmt_pct(annualized)} (extrapolated from this single trade)")
    lines.append(f"  Exit reason: {reason}")
    lines.append("")

    lines.append(DIVIDER)
    lines.append("WHAT THIS MEANS FOR THE ACCOUNT")
    lines.append(DIVIDER)
    if pnl >= 0:
        lines.append(
            f"This {direction} adds to your realized P&L for the day. Capital\n"
            f"is now freed up — the bot's auto-deployer will consider new\n"
            f"picks in the next scan cycle (every 30 minutes during market\n"
            f"hours).\n\n"
            f"The monitor doesn't 'chase' — if {symbol} keeps climbing after\n"
            f"this exit, that's fine. Our edge is the disciplined entry and\n"
            f"exit rules; over many trades, sticking to the rules beats\n"
            f"second-guessing each individual winner."
        )
    else:
        lines.append(
            f"This loss is within the expected risk budget — the initial\n"
            f"stop was set at deploy time precisely so this outcome caps\n"
            f"downside. The strategy works over MANY trades, not any\n"
            f"single one, and taking small losses quickly is what keeps\n"
            f"bigger ones from happening.\n\n"
            f"Capital from the close is now freed up for the next cycle's\n"
            f"picks."
        )
    lines.append("")

    lines.append(DIVIDER)
    lines.append("DO YOU NEED TO DO ANYTHING?")
    lines.append(DIVIDER)
    lines.append(
        "No action needed — the trade is closed and booked.\n\n"
        "The daily close email (4:05 PM ET) will roll this up with every\n"
        "other position into the day's scorecard. The weekly learning\n"
        "engine runs Fridays and adjusts strategy weights based on what's\n"
        "been working."
    )
    lines.append("")

    lines.append(_footer())
    return (f"[Trading Bot] Closed {symbol} {_fmt_signed_money(pnl)} "
            f"({_fmt_pct(pnl_pct)})"), "\n".join(lines)


def stop_loss_triggered(symbol: str, strategy: str,
                         entry_price: float, exit_price: float,
                         shares: int, pnl: float, pnl_pct: float,
                         hold_days: int | None = None) -> tuple[str, str]:
    """Hard stop-loss hit (vs target_hit / trailing exit above — this is
    the loss scenario specifically)."""
    subj, _body = profit_target_hit(
        symbol=symbol, strategy=strategy,
        entry_price=entry_price, exit_price=exit_price,
        shares=shares, pnl=pnl, pnl_pct=pnl_pct,
        hold_days=hold_days, reason="stop_triggered",
    )
    # Override the "what happened" with stop-specific copy
    lines = _body.split("\n")
    # Rewrite subject to make clear this is a stop
    subj = f"[Trading Bot] STOP hit on {symbol}: {_fmt_signed_money(pnl)} ({_fmt_pct(pnl_pct)})"
    # Replace the first what-happened block with stop-specific copy
    try:
        start = lines.index("WHAT JUST HAPPENED") + 2  # skip header + divider
        end = lines.index("", start)
        lines[start:end] = [
            ("The stop-loss triggered. The stock fell to your stop price\n"
             "and the bot sold automatically — that's the entire point of\n"
             "the stop: cap the downside before it becomes a bigger\n"
             "problem.\n\n"
             "This is a normal, expected outcome of the strategy. Setting\n"
             "a stop is how the bot keeps any single trade from blowing up\n"
             "the account. Over many trades, some will hit stops. The math\n"
             "works because the winners are bigger than the stopped losers.")
        ]
    except ValueError:
        pass
    return subj, "\n".join(lines)


# ============================================================================
# Wheel-specific — sold put, assignment, sold call, bought-to-close
# ============================================================================
def wheel_put_sold(symbol: str, strike: float, premium: float,
                    expiration: str, contracts: int = 1) -> tuple[str, str]:
    cash_commitment = strike * 100 * contracts
    max_return = (premium * 100 * contracts)
    breakeven = strike - premium
    days_to_exp = None  # optional — caller can set separately
    lines = []
    lines.append(f"🎯 WHEEL — Sold cash-secured put on {symbol}")
    lines.append(f"Strike ${strike:.2f}, collected {_fmt_money(premium * 100 * contracts)} in premium")
    lines.append("")

    lines.append(DIVIDER)
    lines.append("WHAT JUST HAPPENED")
    lines.append(DIVIDER)
    lines.append(
        f"The bot sold {contracts} cash-secured put on {symbol} at a strike\n"
        f"of {_fmt_money(strike)}, expiring {expiration}. That means:\n\n"
        f"  • The buyer paid us {_fmt_money(premium)} per share\n"
        f"    ({_fmt_money(premium * 100 * contracts)} total, called the 'premium').\n"
        f"  • We're obligated to BUY {100 * contracts} shares of {symbol} at\n"
        f"    {_fmt_money(strike)} each IF the buyer chooses to exercise\n"
        f"    by {expiration}.\n"
        f"  • We have {_fmt_money(cash_commitment)} of cash set aside as\n"
        f"    collateral (the 'cash-secured' part — Alpaca holds it).\n"
    )
    lines.append("")

    lines.append(DIVIDER)
    lines.append("THE WHEEL STRATEGY — HOW IT WORKS")
    lines.append(DIVIDER)
    lines.append(
        "The Wheel is a premium-collection strategy. You rotate through:\n\n"
        "  STAGE 1: Sell a put → collect premium.\n"
        "           If the stock stays above strike, the put expires\n"
        "           worthless — we keep the entire premium as profit\n"
        "           and the cash collateral is released.\n"
        "           If the stock drops below strike, we're assigned —\n"
        "           we buy 100 shares at the strike price, but our\n"
        "           effective cost is (strike − premium), below the\n"
        "           strike.\n\n"
        "  STAGE 2: If assigned, sell a call against the shares.\n"
        "           Collect more premium. If called away (stock > call\n"
        "           strike), we sell the shares at a profit; if not,\n"
        "           we keep the shares AND the call premium.\n\n"
        "  STAGE 3: Repeat. The Wheel prints income in sideways/\n"
        "           mildly-bullish markets. It gets hurt in strong\n"
        "           trends (missing upside) or big crashes (owning\n"
        "           assigned shares as they keep falling).\n"
    )
    lines.append("")

    lines.append(DIVIDER)
    lines.append("THE NUMBERS ON THIS CONTRACT")
    lines.append(DIVIDER)
    lines.append(f"  Symbol:            {symbol}")
    lines.append(f"  Strike:            {_fmt_money(strike)}")
    lines.append(f"  Expiration:        {expiration}")
    lines.append(f"  Contracts sold:    {contracts}")
    lines.append(f"  Premium collected: {_fmt_money(max_return)} (our max profit on this leg)")
    lines.append(f"  Cash held:         {_fmt_money(cash_commitment)} (collateral, released on expiry or assignment)")
    lines.append(f"  Breakeven:         {_fmt_money(breakeven)} — below this, we start losing on assignment")
    roc_monthly = (max_return / cash_commitment * 100) if cash_commitment else 0
    lines.append(f"  Return on collat:  {_fmt_pct(roc_monthly, decimals=2)} on this one leg")
    lines.append("")

    lines.append(DIVIDER)
    lines.append("WHAT COULD HAPPEN NEXT")
    lines.append(DIVIDER)
    lines.append(
        f"  A. STOCK STAYS ABOVE {_fmt_money(strike)} — most common outcome.\n"
        f"     The put expires worthless on {expiration}. We keep the\n"
        f"     {_fmt_money(max_return)} premium and our cash frees up.\n"
        f"     The bot may re-sell another put in the next wheel cycle.\n\n"
        f"  B. STOCK DROPS BELOW {_fmt_money(strike)} — we get assigned.\n"
        f"     The bot buys 100 × {contracts} shares at {_fmt_money(strike)}.\n"
        f"     Effective cost basis = {_fmt_money(breakeven)}. The bot\n"
        f"     transitions to Stage 2 and sells covered calls against\n"
        f"     those shares. You'll get a follow-up email.\n\n"
        f"  C. BOT BUYS TO CLOSE EARLY — if the put loses 50% of its\n"
        f"     original premium (i.e., we can buy it back cheap), the\n"
        f"     bot closes the position early to lock in the partial\n"
        f"     gain and free up cash. This is the 'Close at 50% profit'\n"
        f"     rule in the wheel config."
    )
    lines.append("")

    lines.append(DIVIDER)
    lines.append("DO YOU NEED TO DO ANYTHING?")
    lines.append(DIVIDER)
    lines.append(
        "No action needed. The Wheel Monitor runs every 15 minutes during\n"
        "market hours and handles every transition automatically —\n"
        "early buy-to-close at 50% profit, assignment into Stage 2,\n"
        "covered-call rollover, etc.\n\n"
        "Heads-up: if you were planning to USE the cash collateral\n"
        f"({_fmt_money(cash_commitment)}) for something else, it's locked until\n"
        "the put expires, is bought back, or gets assigned. Alpaca holds\n"
        "the cash — you can see it in the Buying Power breakdown."
    )
    lines.append("")

    lines.append(_footer())
    return (f"[Trading Bot] Wheel: sold {symbol} "
            f"{_fmt_money(strike)} put @ {_fmt_money(premium)} premium"), "\n".join(lines)


def wheel_assigned(symbol: str, strike: float, shares: int,
                    original_premium: float) -> tuple[str, str]:
    cost_basis = strike - original_premium
    lines = []
    lines.append(f"📦 WHEEL — Assigned {shares} shares of {symbol} at {_fmt_money(strike)}")
    lines.append(f"Effective cost basis: {_fmt_money(cost_basis)} (strike minus premium collected)")
    lines.append("")

    lines.append(DIVIDER)
    lines.append("WHAT JUST HAPPENED — STAGE 1 → STAGE 2")
    lines.append(DIVIDER)
    lines.append(
        f"{symbol} fell below your put strike by expiration. The put\n"
        f"buyer exercised their right to sell you the shares at\n"
        f"{_fmt_money(strike)}. Your cash collateral was used to buy\n"
        f"{shares} shares at that price.\n\n"
        f"This is a NORMAL and PLANNED outcome of the wheel — not a\n"
        f"loss. Your effective cost is {_fmt_money(cost_basis)} because\n"
        f"you already collected {_fmt_money(original_premium)} per share\n"
        f"in premium when you sold the put.\n"
    )
    lines.append("")

    lines.append(DIVIDER)
    lines.append("WHAT'S NEXT — STAGE 2: SELL COVERED CALLS")
    lines.append(DIVIDER)
    lines.append(
        f"Now that you own the shares, the bot will sell a call option\n"
        f"against them at a strike above your cost basis. Same concept,\n"
        f"flipped: you collect another premium; if the stock climbs\n"
        f"above that strike, your shares get 'called away' (sold) at\n"
        f"that price + you keep the premium; if the stock stays below,\n"
        f"you keep the shares AND the premium, and the bot sells\n"
        f"another call next cycle.\n\n"
        f"Two outcomes:\n"
        f"  • Stock climbs above the call strike → shares sold at a\n"
        f"    profit. Combined P&L = (call strike − cost basis) +\n"
        f"    premium. Wheel cycle complete; back to Stage 1.\n"
        f"  • Stock stays below → we keep shares + keep premium. Roll\n"
        f"    into another call next expiration."
    )
    lines.append("")

    lines.append(DIVIDER)
    lines.append("WHEN DOES THIS GO BAD?")
    lines.append(DIVIDER)
    lines.append(
        "The wheel gets hurt when you're assigned into a stock that\n"
        "KEEPS falling. You then own shares with a shrinking mark-to-\n"
        "market value. The covered-call premium helps cushion, but\n"
        "if the stock falls 20%+ below your cost basis, the bot may\n"
        "switch from selling calls to closing the position at a loss —\n"
        "same logic as a trailing-stop exit on any long position.\n\n"
        f"Your breakeven on these {shares} shares is {_fmt_money(cost_basis)}.\n"
        f"Below that, you're underwater on the wheel leg."
    )
    lines.append("")

    lines.append(_footer())
    return (f"[Trading Bot] Wheel assigned: {shares} {symbol} @ "
            f"{_fmt_money(strike)}"), "\n".join(lines)


# ============================================================================
# Kill switch
# ============================================================================
def kill_switch(reason: str, portfolio_value: float,
                 daily_pnl: float | None = None) -> tuple[str, str]:
    lines = []
    lines.append(f"🛑 KILL SWITCH ACTIVATED")
    lines.append(f"Reason: {reason}")
    lines.append("")

    lines.append(DIVIDER)
    lines.append("WHAT JUST HAPPENED")
    lines.append(DIVIDER)
    lines.append(
        "The kill switch activated — either manually by you or\n"
        "automatically by one of the bot's hard guardrails (daily loss\n"
        "limit, max drawdown, or critical error).\n\n"
        "All trading is now halted:\n"
        "  • No new positions will be opened.\n"
        "  • The Auto-Deployer will skip its next scheduled run.\n"
        "  • Existing open positions are NOT auto-closed — their\n"
        "    individual trailing stops still protect them as before.\n"
        f"    You still hold whatever was in the portfolio.\n\n"
        f"Reason given: {reason}"
    )
    lines.append("")

    lines.append(DIVIDER)
    lines.append("STATE SNAPSHOT")
    lines.append(DIVIDER)
    lines.append(f"  Portfolio value: {_fmt_money(portfolio_value)}")
    if daily_pnl is not None:
        lines.append(f"  Today's P&L:     {_fmt_signed_money(daily_pnl)}")
    lines.append("")

    lines.append(DIVIDER)
    lines.append("WHAT TO DO")
    lines.append(DIVIDER)
    lines.append(
        "1. Open the dashboard. Review what triggered this — usually\n"
        "   the top banner tells you (daily loss limit, max drawdown,\n"
        "   scheduler error, etc.).\n\n"
        "2. Decide if you want to close positions manually. The bot\n"
        "   isn't doing that for you.\n\n"
        "3. When you're ready to trade again, use the 'Deactivate'\n"
        "   button on the kill-switch banner. The bot resumes trading\n"
        "   on the next scheduler tick.\n\n"
        "If an automated guardrail tripped this, consider whether the\n"
        "thresholds need tuning — or whether market conditions warrant\n"
        "staying offline for the day."
    )
    lines.append("")

    lines.append(_footer())
    return "[Trading Bot] 🛑 KILL SWITCH ACTIVATED", "\n".join(lines)


# ============================================================================
# Strategy explainer (used in position_opened + fallback)
# ============================================================================
def _strategy_explainer(strategy: str) -> str:
    explainers = {
        "breakout": (
            "BREAKOUT plays stocks that punch above their 20-day high on\n"
            "2x+ normal volume. The thesis: a clean breakout often keeps\n"
            "going for several days as momentum traders pile in. The risk:\n"
            "'fake breakouts' that reverse immediately — we handle that\n"
            "with a tight 5% stop-loss and an immediate trailing stop,\n"
            "so fake breakouts cost us little and real ones pay well.\n\n"
            "Holding period: typically 3-10 days.\n"
            "Best regime: bull markets with strong breadth.\n"
        ),
        "mean_reversion": (
            "MEAN REVERSION plays stocks that dropped 15%+ on modest\n"
            "volume (not panic selling, not news-driven). The thesis:\n"
            "short-term overshoots revert to the 20-day average within\n"
            "a few days. The risk: 'catching a falling knife' — a stock\n"
            "with real problems that keeps falling. We hedge by SKIPPING\n"
            "drops driven by bearish news, and using a 10% stop-loss.\n\n"
            "Holding period: typically 3-7 days.\n"
            "Best regime: choppy or range-bound markets.\n"
        ),
        "pead": (
            "PEAD (Post-Earnings Announcement Drift) is the most\n"
            "academically-validated stock market anomaly. When a company\n"
            "beats earnings by 5%+, the stock price tends to drift upward\n"
            "for 30-60 days as institutional investors slowly build\n"
            "positions (they can't buy a billion-dollar stake in one day).\n"
            "Reference: Bernard & Thomas, 1989 — has survived 35+ years\n"
            "of out-of-sample tests.\n\n"
            "The bot holds up to 60 days per position, with an 8% trailing\n"
            "stop. It closes early if the next earnings event is within\n"
            "5 days (to avoid rolling into fresh surprise risk).\n\n"
            "Best regime: any — the anomaly works across bull and bear.\n"
        ),
        "wheel": (
            "WHEEL STRATEGY sells cash-secured puts to collect premium.\n"
            "If the put expires worthless, we pocket the premium. If the\n"
            "stock drops to the strike, we get assigned shares at a\n"
            "discount (effective cost = strike − premium) and then sell\n"
            "covered calls on those shares to collect more premium.\n\n"
            "Best regime: sideways / mildly bullish.\n"
            "Worst regime: strong trends (missing upside on the stock).\n"
        ),
        "trailing_stop": (
            "TRAILING STOP is a long position protected by a stop that\n"
            "ratchets up as the price climbs (never down). Losses capped,\n"
            "gains ride. This is now the universal exit policy on every\n"
            "non-Wheel position — not a standalone entry.\n"
        ),
    }
    return explainers.get(strategy,
        "An entry position opened via the screener's best-strategy selector.")


# ============================================================================
# Round-11 LIVE-BATCH 4: daily scorecard email digest
# ============================================================================

def scorecard_digest(username, scorecard, today_trades, positions, account):
    """Daily 4:30 PM ET scorecard digest email.
    Returns (subject, body) tuple. Plain text for Gmail SMTP simplicity.

    Shows:
      - Today's P&L (from account.equity - account.last_equity)
      - Today's trades (opens + closes)
      - Open positions snapshot
      - Scorecard stats (win rate, Sharpe, max drawdown)
      - Readiness score + go-live gate
    """
    from datetime import datetime
    try:
        from et_time import now_et
        today = now_et().strftime("%A, %B %d, %Y")
    except ImportError:
        today = datetime.now().strftime("%A, %B %d, %Y")

    acct = account or {}
    pv = float(acct.get("portfolio_value") or 0)
    last_eq = float(acct.get("last_equity") or pv)
    day_pnl = pv - last_eq
    day_pnl_pct = (day_pnl / last_eq * 100) if last_eq > 0 else 0
    day_arrow = "▲" if day_pnl >= 0 else "▼"

    sc = scorecard or {}
    lines = []
    lines.append(f"DAILY SCORECARD — {today}")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"TODAY")
    lines.append(f"  Portfolio value:  ${pv:,.2f}")
    lines.append(f"  Today's P&L:      {day_arrow} {_fmt_signed_money(day_pnl)}  ({day_pnl_pct:+.2f}%)")
    lines.append(f"  Cash available:   ${float(acct.get('cash') or 0):,.2f}")
    lines.append(f"  Open positions:   {len(positions or [])}")
    lines.append("")

    # Today's trades
    if today_trades:
        lines.append(f"TODAY'S ACTIVITY ({len(today_trades)} events)")
        for t in today_trades[:20]:
            side = (t.get("side") or "?").upper()
            qty = t.get("qty", "?")
            sym = t.get("symbol", "?")
            px = t.get("price", 0)
            strat = t.get("strategy", "")
            pnl = t.get("pnl")
            line = f"  {side} {qty} {sym} @ ${float(px or 0):.2f} ({strat})"
            if pnl is not None:
                line += f"  →  {_fmt_signed_money(pnl)}"
            lines.append(line)
        if len(today_trades) > 20:
            lines.append(f"  ... and {len(today_trades) - 20} more")
        lines.append("")

    # Open positions
    if positions:
        lines.append("OPEN POSITIONS")
        for p in positions[:15]:
            sym = p.get("symbol", "?")
            qty = p.get("qty", "?")
            entry = float(p.get("avg_entry_price") or 0)
            cur = float(p.get("current_price") or 0)
            pnl = float(p.get("unrealized_pl") or 0)
            pnl_pct = float(p.get("unrealized_plpc") or 0) * 100
            lines.append(f"  {sym:8} {qty:>6}  entry ${entry:>7.2f}  now ${cur:>7.2f}  "
                         f"{_fmt_signed_money(pnl):>10}  ({pnl_pct:+.1f}%)")
        lines.append("")

    # Scorecard
    lines.append("ALL-TIME SCORECARD")
    lines.append(f"  Total trades:      {sc.get('total_trades', 0)}")
    lines.append(f"  Win rate:          {sc.get('win_rate', 0):.1f}%"
                 if sc.get('win_rate') is not None else "  Win rate:          —")
    lines.append(f"  Total P&L:         {_fmt_signed_money(sc.get('total_pnl', 0))}")
    lines.append(f"  Sharpe ratio:      {float(sc.get('sharpe_ratio') or 0):.2f}")
    lines.append(f"  Max drawdown:      {float(sc.get('max_drawdown_pct') or 0):.1f}%")
    lines.append(f"  Profit factor:     {float(sc.get('profit_factor') or 0):.2f}")
    lines.append("")

    # Readiness
    readiness = int(sc.get("readiness_score") or 0)
    ready_bar = "█" * (readiness // 10) + "░" * (10 - readiness // 10)
    lines.append(f"GO-LIVE READINESS")
    lines.append(f"  Score: {readiness}/100  [{ready_bar}]")
    if readiness >= 80:
        lines.append(f"  Status: READY — meeting criteria to enable live trading.")
    elif readiness >= 50:
        lines.append(f"  Status: Building — keep trading and the score will climb.")
    else:
        lines.append(f"  Status: Early days — need more trades to judge.")
    lines.append("")

    # Strategy breakdown (top 3 by P&L)
    breakdown = sc.get("strategy_breakdown", {}) or {}
    ranked = sorted(
        [(k, v) for k, v in breakdown.items() if int(v.get("trades", 0) or 0) > 0],
        key=lambda kv: float(kv[1].get("pnl", 0) or 0),
        reverse=True,
    )
    if ranked:
        lines.append("STRATEGY LEADERBOARD")
        for strat, s in ranked[:6]:
            trades_n = int(s.get("trades", 0) or 0)
            wins_n = int(s.get("wins", 0) or 0)
            pnl = float(s.get("pnl", 0) or 0)
            wr = (wins_n / trades_n * 100) if trades_n else 0
            lines.append(f"  {strat:18} {trades_n:>3} trades  {wr:>5.1f}% win  {_fmt_signed_money(pnl):>10}")
        lines.append("")

    lines.append(_footer())

    if day_pnl >= 0:
        subj = f"✅ Scorecard — {today} — {_fmt_signed_money(day_pnl)} ({day_pnl_pct:+.2f}%)"
    else:
        subj = f"⚠️ Scorecard — {today} — {_fmt_signed_money(day_pnl)} ({day_pnl_pct:+.2f}%)"
    return subj, "\n".join(lines)
