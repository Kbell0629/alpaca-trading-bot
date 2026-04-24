// Round-61 pt.8 batch-8 — buildComparisonPanel tests.
//
// Renders the Paper vs Live comparison panel. pt.8 kickoff fixed this to
// derive win-rate reliability from `closed_trades < 5` directly (defensive
// against a missing API field). These tests pin both the ACTIVE paper
// path and the NOT ACTIVE live-setup path.

import { describe, it, expect, beforeAll } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('buildComparisonPanel — Paper card', () => {
  it('renders portfolio value with 2dp + comma grouping', () => {
    const html = api.buildComparisonPanel({
      account: { portfolio_value: 103191.86 },
      scorecard: { starting_capital: 100000 },
    });
    expect(html).toContain('$103,191.86');
  });

  it('positive return → + prefix + positive class', () => {
    const html = api.buildComparisonPanel({
      account: { portfolio_value: 103000 },
      scorecard: { starting_capital: 100000 },
    });
    expect(html).toContain('+3.00%');
    expect(html).toContain('class="positive"');
  });

  it('negative return → minus + negative class', () => {
    const html = api.buildComparisonPanel({
      account: { portfolio_value: 97000 },
      scorecard: { starting_capital: 100000 },
    });
    expect(html).toContain('-3.00%');
    expect(html).toContain('class="negative"');
  });
});

describe('buildComparisonPanel — win-rate defensive fallback (pt.8 kickoff)', () => {
  it('closed_trades < 5 → "N=X, Need 5+ trades" (unreliable branch)', () => {
    const html = api.buildComparisonPanel({
      account: { portfolio_value: 103000 },
      scorecard: {
        starting_capital: 100000,
        closed_trades: 2, win_rate_pct: 0,
      },
    });
    expect(html).toContain('N=2');
    expect(html).toContain('Need 5+ trades');
    expect(html).not.toContain('target 50%+');
  });

  it('closed_trades = 0 (missing field) → "N=0, Need 5+"', () => {
    const html = api.buildComparisonPanel({
      account: { portfolio_value: 103000 },
      scorecard: { starting_capital: 100000 },
    });
    expect(html).toContain('N=0');
    expect(html).toContain('Need 5+');
  });

  it('closed_trades >= 5 + reliable true → win_rate_pct% (reliable branch)', () => {
    const html = api.buildComparisonPanel({
      account: { portfolio_value: 103000 },
      scorecard: {
        starting_capital: 100000,
        closed_trades: 10,
        win_rate_reliable: true,
        win_rate_pct: 65,
      },
    });
    expect(html).toContain('65%');
    expect(html).toContain('target 50%+');
    expect(html).not.toContain('N=');
  });

  it('closed_trades >= 5 but win_rate_reliable=false → still N= branch', () => {
    const html = api.buildComparisonPanel({
      account: { portfolio_value: 103000 },
      scorecard: {
        starting_capital: 100000,
        closed_trades: 10,
        win_rate_reliable: false,
        win_rate_pct: 0,
        win_rate_sample_size: 10,
      },
    });
    expect(html).toContain('N=10');
    expect(html).toContain('Need 5+');
  });

  it('win_rate_sample_size overrides closed_trades for display', () => {
    const html = api.buildComparisonPanel({
      account: { portfolio_value: 100000 },
      scorecard: {
        starting_capital: 100000,
        closed_trades: 3, win_rate_sample_size: 4,
      },
    });
    expect(html).toContain('N=4');
  });
});

describe('buildComparisonPanel — Live-mode card', () => {
  it('live_mode_active=false → NOT ACTIVE card with setup steps', () => {
    const html = api.buildComparisonPanel({
      account: { portfolio_value: 100000 },
      scorecard: { starting_capital: 100000, readiness_score: 40 },
      auto_deployer_config: { live_mode_active: false },
    });
    expect(html).toContain('NOT ACTIVE');
    expect(html).toContain('To activate live trading');
    expect(html).toContain('Hit 80/100');
    expect(html).toContain('alpaca.markets');
  });

  it('readiness < 80 → "Not ready yet" with deficit', () => {
    const html = api.buildComparisonPanel({
      account: { portfolio_value: 100000 },
      scorecard: { starting_capital: 100000, readiness_score: 40 },
    });
    expect(html).toContain('Not ready yet');
    expect(html).toContain('Need 40 more readiness points');
  });

  it('readiness >= 80 → "Ready for live trading!"', () => {
    const html = api.buildComparisonPanel({
      account: { portfolio_value: 100000 },
      scorecard: { starting_capital: 100000, readiness_score: 85 },
    });
    expect(html).toContain('Ready for live trading');
    expect(html).not.toContain('Not ready yet');
  });

  it('live_mode_active=true → ACTIVE live card + performance-gap note', () => {
    const html = api.buildComparisonPanel({
      account: { portfolio_value: 100000 },
      scorecard: { starting_capital: 100000 },
      auto_deployer_config: { live_mode_active: true },
    });
    expect(html).toContain('Live Trading');
    expect(html).toContain('ACTIVE');
    expect(html).toContain('comparison-delta');
    expect(html).toContain('Performance gap');
  });
});
