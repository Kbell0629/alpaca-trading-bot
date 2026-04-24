// Round-61 pt.8 batch-8 — buildShortStrategyCard tests.
//
// Renders the "6. Short Selling" card. Shows ACTIVE / STANDBY / TURNED OFF
// badge, current short count, SPY 20d momentum, top candidate. Gated by
// bear-market + SPY_momentum_20d < -3% AND user-enabled.

import { describe, it, expect, beforeAll } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('buildShortStrategyCard — status badge branches', () => {
  it('user disabled → TURNED OFF badge', () => {
    const html = api.buildShortStrategyCard({
      auto_deployer_config: { short_selling: { enabled: false } },
    });
    expect(html).toContain('TURNED OFF');
    expect(html).toContain('badge-inactive');
  });

  it('bear market + SPY < -3% + user enabled → ACTIVE (BEAR MKT)', () => {
    const html = api.buildShortStrategyCard({
      market_regime: 'bear',
      spy_momentum_20d: -5.5,
      auto_deployer_config: { short_selling: { enabled: true } },
    });
    expect(html).toContain('ACTIVE (BEAR MKT)');
    expect(html).toContain('badge-active');
  });

  it('neutral market → STANDBY badge', () => {
    const html = api.buildShortStrategyCard({
      market_regime: 'neutral',
      spy_momentum_20d: 1.2,
      auto_deployer_config: {},
    });
    expect(html).toContain('STANDBY');
    expect(html).toContain('badge-pending');
  });

  it('bear market but SPY only -1% → STANDBY (not unlocked yet)', () => {
    const html = api.buildShortStrategyCard({
      market_regime: 'bear',
      spy_momentum_20d: -1.0,
      auto_deployer_config: {},
    });
    expect(html).toContain('STANDBY');
    expect(html).not.toContain('ACTIVE (BEAR MKT)');
  });
});

describe('buildShortStrategyCard — conditions message', () => {
  it('active explains bear market detected', () => {
    const html = api.buildShortStrategyCard({
      market_regime: 'bear',
      spy_momentum_20d: -5,
      auto_deployer_config: { short_selling: { enabled: true } },
    });
    expect(html).toContain('Bear market detected');
  });

  it('disabled shows "turned OFF" message', () => {
    const html = api.buildShortStrategyCard({
      auto_deployer_config: { short_selling: { enabled: false } },
    });
    expect(html).toContain('Short selling is turned OFF');
  });

  it('standby shows SPY 20d value for context', () => {
    const html = api.buildShortStrategyCard({
      market_regime: 'neutral',
      spy_momentum_20d: 0.5,
      auto_deployer_config: {},
    });
    expect(html).toContain('SPY 20d: +0.5%');
    expect(html).toContain('Waiting for bear market');
  });
});

describe('buildShortStrategyCard — position count', () => {
  it('counts qty < 0 positions', () => {
    const html = api.buildShortStrategyCard({
      positions: [
        { symbol: 'A', qty: 10 },    // long
        { symbol: 'B', qty: -5 },    // short
        { symbol: 'C', qty: -2 },    // short
      ],
      auto_deployer_config: { short_selling: { max_short_positions: 3 } },
    });
    expect(html).toContain('2/3');
  });

  it('defaults maxShorts to 1 when not configured', () => {
    const html = api.buildShortStrategyCard({
      positions: [{ symbol: 'X', qty: -1 }],
      auto_deployer_config: {},
    });
    expect(html).toContain('1/1');
  });
});

describe('buildShortStrategyCard — top candidate', () => {
  it('renders top short candidate when present', () => {
    const html = api.buildShortStrategyCard({
      auto_deployer_config: {},
      short_candidates: [{ symbol: 'TSLA', short_score: 18 }],
    });
    expect(html).toContain('TSLA');
    expect(html).toContain('(score 18)');
  });

  it('shows "None scoring ≥15" when no candidates', () => {
    const html = api.buildShortStrategyCard({
      auto_deployer_config: {},
    });
    expect(html).toContain('None scoring');
  });

  it('XSS in candidate symbol is escaped', () => {
    const html = api.buildShortStrategyCard({
      auto_deployer_config: {},
      short_candidates: [{ symbol: '<script>x</script>', short_score: 20 }],
    });
    expect(html).not.toContain('<script>x</script>');
    expect(html).toContain('&lt;script&gt;');
  });
});

describe('buildShortStrategyCard — toggle button', () => {
  it('enabled user sees "Turn Off" button', () => {
    const html = api.buildShortStrategyCard({
      auto_deployer_config: { short_selling: { enabled: true } },
    });
    expect(html).toContain('Turn Off');
    expect(html).toContain('toggleShortSelling(false)');
  });

  it('disabled user sees "Turn On" button', () => {
    const html = api.buildShortStrategyCard({
      auto_deployer_config: { short_selling: { enabled: false } },
    });
    expect(html).toContain('Turn On');
    expect(html).toContain('toggleShortSelling(true)');
  });
});
