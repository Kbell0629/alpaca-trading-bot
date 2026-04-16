#!/usr/bin/env python3
"""
Technical indicators for stock analysis.
All calculations use only stdlib — no numpy, pandas, or ta-lib needed.
"""


def sma(closes, period):
    """Simple Moving Average. Returns list of SMA values (None for first period-1 entries)."""
    if len(closes) < period:
        return [None] * len(closes)
    result = [None] * (period - 1)
    for i in range(period - 1, len(closes)):
        result.append(sum(closes[i - period + 1:i + 1]) / period)
    return result


def ema(closes, period):
    """Exponential Moving Average."""
    if not closes or len(closes) < period:
        return [None] * len(closes)
    k = 2 / (period + 1)
    result = [None] * (period - 1)
    # Seed with SMA
    first_ema = sum(closes[:period]) / period
    result.append(first_ema)
    for i in range(period, len(closes)):
        result.append(closes[i] * k + result[-1] * (1 - k))
    return result


def rsi(closes, period=14):
    """Relative Strength Index (0-100). Standard Wilder's RSI."""
    if len(closes) < period + 1:
        return [None] * len(closes)

    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    # First average
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    result = [None] * period
    if avg_loss == 0:
        result.append(100.0)
    else:
        rs = avg_gain / avg_loss
        result.append(100 - 100 / (1 + rs))

    # Subsequent values use Wilder's smoothing
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss == 0:
            result.append(100.0)
        else:
            rs = avg_gain / avg_loss
            result.append(100 - 100 / (1 + rs))

    return result


def macd(closes, fast=12, slow=26, signal=9):
    """MACD (Moving Average Convergence Divergence).
    Returns dict with keys: macd_line, signal_line, histogram (all lists)."""
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)

    macd_line = []
    for f, s in zip(fast_ema, slow_ema):
        if f is not None and s is not None:
            macd_line.append(f - s)
        else:
            macd_line.append(None)

    # Signal line is EMA of MACD line
    macd_values = [v for v in macd_line if v is not None]
    signal_line_raw = ema(macd_values, signal) if len(macd_values) >= signal else [None] * len(macd_values)

    # Align signal line with macd_line
    signal_line = [None] * (len(macd_line) - len(signal_line_raw)) + signal_line_raw

    # Histogram
    histogram = []
    for m, s in zip(macd_line, signal_line):
        if m is not None and s is not None:
            histogram.append(m - s)
        else:
            histogram.append(None)

    return {"macd_line": macd_line, "signal_line": signal_line, "histogram": histogram}


def bollinger_bands(closes, period=20, num_std=2):
    """Bollinger Bands. Returns dict with upper, middle, lower (all lists)."""
    middle = sma(closes, period)
    upper = []
    lower = []

    for i, m in enumerate(middle):
        if m is None:
            upper.append(None)
            lower.append(None)
        else:
            window = closes[max(0, i - period + 1):i + 1]
            std = (sum((x - m) ** 2 for x in window) / len(window)) ** 0.5
            upper.append(m + num_std * std)
            lower.append(m - num_std * std)

    return {"upper": upper, "middle": middle, "lower": lower}


def atr(bars, period=14):
    """Average True Range. Measures volatility."""
    if len(bars) < 2:
        return [None] * len(bars)

    true_ranges = [bars[0].get("h", 0) - bars[0].get("l", 0)]
    for i in range(1, len(bars)):
        h = bars[i].get("h", 0)
        l = bars[i].get("l", 0)
        prev_c = bars[i-1].get("c", 0)
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return [None] * len(bars)

    result = [None] * (period - 1)
    first_atr = sum(true_ranges[:period]) / period
    result.append(first_atr)

    for i in range(period, len(true_ranges)):
        result.append((result[-1] * (period - 1) + true_ranges[i]) / period)

    return result


def vwap(bars):
    """Volume Weighted Average Price (intraday)."""
    cum_vol = 0
    cum_vp = 0
    result = []
    for bar in bars:
        typical = (bar.get("h", 0) + bar.get("l", 0) + bar.get("c", 0)) / 3
        vol = bar.get("v", 0)
        cum_vp += typical * vol
        cum_vol += vol
        result.append(cum_vp / cum_vol if cum_vol > 0 else 0)
    return result


def stochastic(bars, k_period=14, d_period=3):
    """Stochastic Oscillator (%K and %D)."""
    if len(bars) < k_period:
        return {"k": [None] * len(bars), "d": [None] * len(bars)}

    k_values = [None] * (k_period - 1)
    for i in range(k_period - 1, len(bars)):
        window = bars[i - k_period + 1:i + 1]
        high = max(b.get("h", 0) for b in window)
        low = min(b.get("l", 0) for b in window)
        close = bars[i].get("c", 0)
        if high == low:
            k_values.append(50.0)
        else:
            k_values.append((close - low) / (high - low) * 100)

    # %D is SMA of %K
    k_only = [v for v in k_values if v is not None]
    d_raw = sma(k_only, d_period) if len(k_only) >= d_period else [None] * len(k_only)
    d_values = [None] * (len(k_values) - len(d_raw)) + d_raw

    return {"k": k_values, "d": d_values}


def obv(bars):
    """On-Balance Volume. Cumulative volume flow."""
    if not bars:
        return []
    result = [bars[0].get("v", 0)]
    for i in range(1, len(bars)):
        if bars[i].get("c", 0) > bars[i-1].get("c", 0):
            result.append(result[-1] + bars[i].get("v", 0))
        elif bars[i].get("c", 0) < bars[i-1].get("c", 0):
            result.append(result[-1] - bars[i].get("v", 0))
        else:
            result.append(result[-1])
    return result


def analyze_stock(bars):
    """Run all indicators on a set of bars and return a summary dict with signals."""
    if not bars or len(bars) < 26:
        return {"error": "Need at least 26 bars for full analysis"}

    closes = [b.get("c", 0) for b in bars]

    # Calculate all indicators
    rsi_vals = rsi(closes, 14)
    macd_data = macd(closes)
    bb = bollinger_bands(closes, 20, 2)
    atr_vals = atr(bars, 14)
    stoch = stochastic(bars, 14, 3)
    sma_20 = sma(closes, 20)
    sma_50 = sma(closes, 50) if len(closes) >= 50 else [None] * len(closes)

    # Current values (last non-None)
    current = closes[-1]
    current_rsi = rsi_vals[-1] if rsi_vals[-1] is not None else 50
    current_macd = macd_data["histogram"][-1] if macd_data["histogram"][-1] is not None else 0
    current_bb_upper = bb["upper"][-1]
    current_bb_lower = bb["lower"][-1]
    current_bb_middle = bb["middle"][-1]
    current_atr = atr_vals[-1] if atr_vals[-1] is not None else 0
    current_stoch_k = stoch["k"][-1] if stoch["k"][-1] is not None else 50
    current_sma_20 = sma_20[-1]
    current_sma_50 = sma_50[-1] if sma_50[-1] is not None else None

    # Generate signals
    signals = []
    score_adjustment = 0

    # RSI signals
    if current_rsi < 30:
        signals.append({"indicator": "RSI", "signal": "oversold", "value": round(current_rsi, 1), "bias": "bullish"})
        score_adjustment += 5  # Oversold = potential bounce
    elif current_rsi > 70:
        signals.append({"indicator": "RSI", "signal": "overbought", "value": round(current_rsi, 1), "bias": "bearish"})
        score_adjustment -= 3  # Overbought = potential pullback
    else:
        signals.append({"indicator": "RSI", "signal": "neutral", "value": round(current_rsi, 1), "bias": "neutral"})

    # MACD signals
    prev_macd = macd_data["histogram"][-2] if len(macd_data["histogram"]) > 1 and macd_data["histogram"][-2] is not None else 0
    if current_macd > 0 and prev_macd <= 0:
        signals.append({"indicator": "MACD", "signal": "bullish_crossover", "value": round(current_macd, 4), "bias": "bullish"})
        score_adjustment += 5
    elif current_macd < 0 and prev_macd >= 0:
        signals.append({"indicator": "MACD", "signal": "bearish_crossover", "value": round(current_macd, 4), "bias": "bearish"})
        score_adjustment -= 5
    elif current_macd > 0:
        signals.append({"indicator": "MACD", "signal": "bullish", "value": round(current_macd, 4), "bias": "bullish"})
        score_adjustment += 2
    else:
        signals.append({"indicator": "MACD", "signal": "bearish", "value": round(current_macd, 4), "bias": "bearish"})
        score_adjustment -= 2

    # Bollinger Band signals
    if current_bb_lower and current <= current_bb_lower:
        signals.append({"indicator": "Bollinger", "signal": "below_lower", "value": round(current, 2), "bias": "bullish"})
        score_adjustment += 3  # Below lower band = oversold
    elif current_bb_upper and current >= current_bb_upper:
        signals.append({"indicator": "Bollinger", "signal": "above_upper", "value": round(current, 2), "bias": "bearish"})
        score_adjustment -= 2

    # SMA crossover
    if current_sma_20 and current_sma_50:
        if current_sma_20 > current_sma_50:
            signals.append({"indicator": "SMA", "signal": "golden_cross", "value": f"SMA20={round(current_sma_20,2)} > SMA50={round(current_sma_50,2)}", "bias": "bullish"})
            score_adjustment += 3
        else:
            signals.append({"indicator": "SMA", "signal": "death_cross", "value": f"SMA20={round(current_sma_20,2)} < SMA50={round(current_sma_50,2)}", "bias": "bearish"})
            score_adjustment -= 3

    # Price vs SMA20
    if current_sma_20:
        if current > current_sma_20 * 1.02:
            signals.append({"indicator": "Trend", "signal": "above_sma20", "bias": "bullish"})
            score_adjustment += 2
        elif current < current_sma_20 * 0.98:
            signals.append({"indicator": "Trend", "signal": "below_sma20", "bias": "bearish"})
            score_adjustment -= 2

    # Stochastic
    if current_stoch_k < 20:
        signals.append({"indicator": "Stochastic", "signal": "oversold", "value": round(current_stoch_k, 1), "bias": "bullish"})
        score_adjustment += 2
    elif current_stoch_k > 80:
        signals.append({"indicator": "Stochastic", "signal": "overbought", "value": round(current_stoch_k, 1), "bias": "bearish"})
        score_adjustment -= 2

    # Overall bias
    bullish_count = sum(1 for s in signals if s.get("bias") == "bullish")
    bearish_count = sum(1 for s in signals if s.get("bias") == "bearish")

    if bullish_count > bearish_count + 1:
        overall_bias = "bullish"
    elif bearish_count > bullish_count + 1:
        overall_bias = "bearish"
    else:
        overall_bias = "neutral"

    # Strategy recommendations based on indicators
    strategy_adjustments = {
        "trailing_stop": 0,
        "mean_reversion": 0,
        "breakout": 0,
        "wheel": 0,
        "copy_trading": 0,
    }

    if current_rsi < 30:
        strategy_adjustments["mean_reversion"] += 10  # Oversold = great for mean reversion
    if current_rsi > 70:
        strategy_adjustments["trailing_stop"] -= 5  # Overbought = bad entry for trailing
    if current_macd > 0 and prev_macd <= 0:
        strategy_adjustments["breakout"] += 8  # MACD crossover = breakout confirmation
        strategy_adjustments["trailing_stop"] += 5
    if current_bb_lower and current <= current_bb_lower:
        strategy_adjustments["mean_reversion"] += 8  # Below BB = mean reversion setup
    if 30 < current_rsi < 70 and abs(current_macd) < 0.5:
        strategy_adjustments["wheel"] += 5  # Ranging = good for wheel

    return {
        "rsi": round(current_rsi, 1),
        "macd_histogram": round(current_macd, 4),
        "bollinger_position": "below" if (current_bb_lower and current <= current_bb_lower) else "above" if (current_bb_upper and current >= current_bb_upper) else "middle",
        "atr": round(current_atr, 2) if current_atr else 0,
        "stochastic_k": round(current_stoch_k, 1),
        "sma_20": round(current_sma_20, 2) if current_sma_20 else None,
        "sma_50": round(current_sma_50, 2) if current_sma_50 else None,
        "signals": signals,
        "overall_bias": overall_bias,
        "score_adjustment": score_adjustment,
        "strategy_adjustments": strategy_adjustments,
        "bullish_signals": bullish_count,
        "bearish_signals": bearish_count,
    }
