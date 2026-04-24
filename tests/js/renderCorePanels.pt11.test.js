// Round-61 pt.11 — regression pin for the "100% drawdown" false alarm.
//
// User screenshot: Alpaca /account returned no data → all metric
// cards rendered $0.00. The drawdown computation against the stored
// peak (e.g. $103,000) yielded "100% / 10% LIMIT — Approaching max
// drawdown limit!" — terrifying for the user, who reasonably read it
// as "my account just got liquidated".
//
// Fix in dashboard_render_core.js: when portfolioValue <= 0, skip the
// drawdown alert + render "—" in place of the false percentage.
// The dashboard layer also adds a top-of-page banner explaining the
// API error, but THIS pin guards the in-meter behavior so the alarm
// can't return on its own.

import { describe, it, expect, beforeAll } from 'vitest';
import { loadRenderCore } from './loadRenderCore.js';

let core;
beforeAll(() => { core = loadRenderCore(); });

describe('buildGuardrailMeters — missing-data state (pt.11 regression)', () => {
  it('does NOT raise the "Approaching max drawdown limit" alarm when portfolioValue is 0', () => {
    const html = core.buildGuardrailMeters(0, 0, 0, {
      peak_portfolio_value: 103000,
      max_drawdown_pct: 0.10,
    });
    // The alarm copy must NOT appear when portfolio value is missing
    expect(html).not.toContain('Approaching max drawdown limit');
    expect(html).not.toContain('Over 50% of max drawdown limit used');
  });

  it('renders "—" instead of "100.0% / 10% LIMIT" in the drawdown label', () => {
    const html = core.buildGuardrailMeters(0, 0, 0, {
      peak_portfolio_value: 103000,
      max_drawdown_pct: 0.10,
    });
    expect(html).toContain('— / 10% limit');
    expect(html).not.toContain('100.0% / 10% limit');
  });

  it('keeps the meter-fill at 0 width (not a full red bar) when portfolio is missing', () => {
    const html = core.buildGuardrailMeters(0, 0, 0, {
      peak_portfolio_value: 103000,
      max_drawdown_pct: 0.10,
    });
    // The drawdown meter-fill width should be 0% (no false 100%)
    expect(html).toContain('width:0%');
  });

  it('still triggers the alarm when portfolio IS present and drawdown is real', () => {
    // Real drawdown: peak 100k, current 88k → 12% drawdown vs 10% limit
    const html = core.buildGuardrailMeters(0, 88000, 100000, {
      peak_portfolio_value: 100000,
      max_drawdown_pct: 0.10,
    });
    expect(html).toContain('Approaching max drawdown limit');
  });
});
