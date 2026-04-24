// Round-61 pt.8 batch-10 — direct tests for the panel-render helpers
// extracted into dashboard_render_core.js. These run in isolation via
// tests/js/loadRenderCore.js — no jsdom + inline <script> gymnastics.

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { loadRenderCore } from './loadRenderCore.js';

let core;
beforeAll(() => { core = loadRenderCore(); });

// ---------------------------------------------------------------------------
// Section-visibility helpers (localStorage-backed)
// ---------------------------------------------------------------------------
describe('getHiddenSections / setHiddenSections / toggleSectionId', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it('defaults to empty array when localStorage is empty', () => {
    expect(core.getHiddenSections()).toEqual([]);
  });

  it('returns the array written by setHiddenSections', () => {
    core.setHiddenSections(['section-heatmap', 'section-tax-report']);
    expect(core.getHiddenSections()).toEqual(['section-heatmap', 'section-tax-report']);
  });

  it('returns empty array when localStorage has corrupt JSON', () => {
    localStorage.setItem('hiddenSections', '{not json');
    expect(core.getHiddenSections()).toEqual([]);
  });

  it('setHiddenSections(null) stores []', () => {
    core.setHiddenSections(null);
    expect(core.getHiddenSections()).toEqual([]);
  });

  it('toggleSectionId adds missing section', () => {
    core.toggleSectionId('section-heatmap');
    expect(core.getHiddenSections()).toEqual(['section-heatmap']);
  });

  it('toggleSectionId removes existing section', () => {
    core.setHiddenSections(['section-heatmap', 'section-tax-report']);
    core.toggleSectionId('section-heatmap');
    expect(core.getHiddenSections()).toEqual(['section-tax-report']);
  });

  it('toggleSectionId returns the new array', () => {
    const out = core.toggleSectionId('x');
    expect(out).toEqual(['x']);
    const out2 = core.toggleSectionId('x');
    expect(out2).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// sectionHelpButton
// ---------------------------------------------------------------------------
describe('sectionHelpButton', () => {
  it('renders a button with the section-id in aria-label and onclick', () => {
    const html = core.sectionHelpButton('heatmap');
    expect(html).toContain('aria-label="Help for heatmap"');
    expect(html).toContain("onclick=\"openSectionGuide('heatmap')\"");
    expect(html).toContain('&#9432;'); // circled-i icon
  });

  it('escapes dangerous characters in section-id', () => {
    const html = core.sectionHelpButton('<img src=x onerror=alert(1)>');
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });
});

// ---------------------------------------------------------------------------
// buildGuardrailMeters
// ---------------------------------------------------------------------------
describe('buildGuardrailMeters', () => {
  it('green state when well under both limits', () => {
    const html = core.buildGuardrailMeters(-0.5, 100000, 100000, {
      daily_loss_limit_pct: 0.03,
      max_drawdown_pct: 0.10,
      peak_portfolio_value: 100000,
    });
    expect(html).toContain('meter-fill green');
    expect(html).not.toContain('guardrail-warning');
  });

  it('yellow warning at >50% of daily loss used', () => {
    const html = core.buildGuardrailMeters(-1.8, 98200, 100000, {
      daily_loss_limit_pct: 0.03,
    });
    expect(html).toContain('meter-fill yellow');
    expect(html).toContain('Over 50% of daily loss limit used');
  });

  it('red warning at >80% of daily loss used', () => {
    const html = core.buildGuardrailMeters(-2.7, 97300, 100000, {
      daily_loss_limit_pct: 0.03,
    });
    expect(html).toContain('meter-fill red');
    expect(html).toContain('Approaching daily loss limit!');
  });

  it('drawdown meter computes current drawdown from peak', () => {
    const html = core.buildGuardrailMeters(0, 90000, 100000, {
      peak_portfolio_value: 100000,
      max_drawdown_pct: 0.10,
    });
    // Current DD = (100000 - 90000) / 100000 = 10% vs 10% limit → 100%
    expect(html).toContain('Approaching max drawdown limit!');
  });

  it('falls back to window.guardrailsData when no 4th arg provided', () => {
    globalThis.guardrailsData = {
      daily_loss_limit_pct: 0.02,
      max_drawdown_pct: 0.05,
    };
    try {
      const html = core.buildGuardrailMeters(-0.5, 100000, 100000);
      expect(html).toContain('2% limit');
      expect(html).toContain('5% limit');
    } finally {
      delete globalThis.guardrailsData;
    }
  });

  it('handles zero daily-loss limit without divide-by-zero', () => {
    const html = core.buildGuardrailMeters(-2, 98000, 100000, {
      daily_loss_limit_pct: 0,
    });
    expect(html).toContain('meter-fill green');
  });
});

// ---------------------------------------------------------------------------
// buildTodaysClosesPanel
// ---------------------------------------------------------------------------
describe('buildTodaysClosesPanel', () => {
  it('returns empty string when no closes', () => {
    expect(core.buildTodaysClosesPanel({})).toBe('');
    expect(core.buildTodaysClosesPanel({ todays_closes: [] })).toBe('');
    expect(core.buildTodaysClosesPanel({ todays_closes: null })).toBe('');
  });

  it('renders one row per close', () => {
    const html = core.buildTodaysClosesPanel({
      todays_closes: [
        { symbol: 'AAPL', strategy: 'breakout', exit_reason: 'trailing_stop',
          exit_price: 182.34, pnl: 145.20, pnl_pct: 4.2, exit_timestamp: '2026-04-24T14:32:00Z' },
        { symbol: 'SOXL', strategy: 'mean_reversion', exit_reason: 'profit_target',
          exit_price: 18.40, pnl: -22.50, pnl_pct: -1.8, exit_timestamp: '2026-04-24T15:10:00Z' },
      ],
    });
    expect(html).toContain('<strong>AAPL</strong>');
    expect(html).toContain('<strong>SOXL</strong>');
    expect(html).toContain('$182.34');
    expect(html).toContain('+4.20%');
    expect(html).toContain('-1.80%');
  });

  it('color-codes positive P&L green, negative red', () => {
    const html = core.buildTodaysClosesPanel({
      todays_closes: [
        { symbol: 'WIN', pnl: 100, pnl_pct: 2 },
        { symbol: 'LOSS', pnl: -50, pnl_pct: -1 },
      ],
    });
    expect(html).toContain('var(--green)');
    expect(html).toContain('var(--red)');
  });

  it('tags orphan closes with a warning badge', () => {
    const html = core.buildTodaysClosesPanel({
      todays_closes: [
        { symbol: 'ORPH', pnl: 50, orphan_close: true },
      ],
    });
    expect(html).toContain('[orphan]');
    expect(html).toContain('var(--orange)');
  });

  it('shows em-dash when pnl is missing', () => {
    const html = core.buildTodaysClosesPanel({
      todays_closes: [{ symbol: 'UNK', exit_timestamp: '2026-04-24T12:00:00Z' }],
    });
    // Row with —
    expect(html).toMatch(/—/);
  });

  it('escapes hostile strings in symbol / strategy / reason', () => {
    const html = core.buildTodaysClosesPanel({
      todays_closes: [
        { symbol: '<img src=x>', strategy: '<script>x</script>', exit_reason: '"evil"' },
      ],
    });
    expect(html).not.toContain('<img src=x>');
    expect(html).not.toContain('<script>x</script>');
    expect(html).toContain('&lt;img');
    expect(html).toContain('&quot;evil&quot;');
  });

  it('totals net P&L across all closes', () => {
    const html = core.buildTodaysClosesPanel({
      todays_closes: [
        { symbol: 'A', pnl: 100 },
        { symbol: 'B', pnl: -30 },
        { symbol: 'C', pnl: 50 },
      ],
    });
    expect(html).toContain('$120.00');
  });
});

// ---------------------------------------------------------------------------
// buildShortStrategyCard
// ---------------------------------------------------------------------------
describe('buildShortStrategyCard', () => {
  it('ACTIVE badge when bear regime + SPY < -3% + user enabled', () => {
    const html = core.buildShortStrategyCard({
      market_regime: 'bear',
      spy_momentum_20d: -4.5,
      auto_deployer_config: { short_selling: { enabled: true } },
    });
    expect(html).toContain('ACTIVE (BEAR MKT)');
    expect(html).toContain('Bear market detected');
  });

  it('STANDBY when conditions not met but user enabled', () => {
    const html = core.buildShortStrategyCard({
      market_regime: 'bull',
      spy_momentum_20d: 2,
      auto_deployer_config: { short_selling: { enabled: true } },
    });
    expect(html).toContain('STANDBY');
    expect(html).toContain('Waiting for bear market');
  });

  it('TURNED OFF when user disabled short selling', () => {
    const html = core.buildShortStrategyCard({
      market_regime: 'bear',
      spy_momentum_20d: -5,
      auto_deployer_config: { short_selling: { enabled: false } },
    });
    expect(html).toContain('TURNED OFF');
    expect(html).toContain('turned OFF');
    expect(html).toContain('Turn On');
  });

  it('counts active short positions', () => {
    const html = core.buildShortStrategyCard({
      positions: [
        { qty: 100 }, { qty: -50 }, { qty: -10 }, { qty: 0 }, { qty: 30 },
      ],
    });
    expect(html).toContain('2/1');
  });

  it('shows top short candidate when available', () => {
    const html = core.buildShortStrategyCard({
      short_candidates: [{ symbol: 'BEAR', short_score: 22 }],
    });
    expect(html).toContain('BEAR (score 22)');
  });

  it('shows "None scoring >=15" when no candidates', () => {
    const html = core.buildShortStrategyCard({ short_candidates: [] });
    expect(html).toContain('None scoring &ge;15');
  });

  it('reads fallback market_regime from economic_calendar', () => {
    const html = core.buildShortStrategyCard({
      economic_calendar: { market_regime: 'BEAR' },
      spy_momentum_20d: -5,
    });
    expect(html).toContain('ACTIVE (BEAR MKT)');
  });
});

// ---------------------------------------------------------------------------
// buildComparisonPanel
// ---------------------------------------------------------------------------
describe('buildComparisonPanel', () => {
  it('paper-vs-live layout when live inactive', () => {
    const html = core.buildComparisonPanel({
      account: { portfolio_value: 105000 },
      scorecard: { starting_capital: 100000, readiness_score: 40 },
    });
    expect(html).toContain('Paper Trading');
    expect(html).toContain('Live Trading');
    expect(html).toContain('NOT ACTIVE');
    expect(html).toContain('+5.00%');
    expect(html).toContain('40/100');
  });

  it('shows "Need 5+ trades" when closed_trades < 5 (post-61 pt.8 defensive)', () => {
    const html = core.buildComparisonPanel({
      account: { portfolio_value: 103200 },
      scorecard: { starting_capital: 100000, closed_trades: 2 },
    });
    expect(html).toContain('N=2');
    expect(html).toContain('Need 5+ trades');
    expect(html).not.toMatch(/target 50%\+/);
  });

  it('shows "target 50%+" only when closed_trades >= 5 AND win_rate_reliable != false', () => {
    const html = core.buildComparisonPanel({
      account: { portfolio_value: 103200 },
      scorecard: {
        starting_capital: 100000,
        closed_trades: 10,
        win_rate_pct: 60,
        win_rate_reliable: true,
      },
    });
    expect(html).toContain('60%');
    expect(html).toContain('target 50%+');
    expect(html).not.toContain('Need 5+ trades');
  });

  it('even with closed_trades >= 5, respects explicit win_rate_reliable=false', () => {
    const html = core.buildComparisonPanel({
      account: { portfolio_value: 103200 },
      scorecard: {
        starting_capital: 100000,
        closed_trades: 10,
        win_rate_reliable: false,
        win_rate_sample_size: 10,
      },
    });
    expect(html).toContain('N=10');
    expect(html).toContain('Need 5+ trades');
  });

  it('ready badge when readiness_score >= 80', () => {
    const html = core.buildComparisonPanel({
      account: { portfolio_value: 110000 },
      scorecard: { starting_capital: 100000, readiness_score: 85 },
    });
    expect(html).toContain('Ready for live trading');
    // Ready-path message ("Ready...Readiness score: 85/100"). Not-ready
    // message "Need N more readiness points" must be absent.
    expect(html).not.toContain('more readiness points');
  });

  it('not-ready badge when readiness_score < 80', () => {
    const html = core.buildComparisonPanel({
      account: { portfolio_value: 102000 },
      scorecard: { starting_capital: 100000, readiness_score: 40 },
    });
    expect(html).toContain('Not ready yet');
    expect(html).toContain('Need 40 more readiness points');
  });

  it('live-active branch shows real-money card', () => {
    const html = core.buildComparisonPanel({
      account: { portfolio_value: 100000 },
      scorecard: { starting_capital: 100000 },
      auto_deployer_config: { live_mode_active: true },
    });
    expect(html).toContain('Real money — actual trades');
    expect(html).toContain('Performance gap');
  });
});

// ---------------------------------------------------------------------------
// buildStrategyTemplates
// ---------------------------------------------------------------------------
describe('buildStrategyTemplates', () => {
  it('renders all three preset cards', () => {
    const html = core.buildStrategyTemplates({});
    expect(html).toContain('Conservative');
    expect(html).toContain('Moderate');
    expect(html).toContain('Aggressive');
  });

  it('marks the detected preset as ACTIVE', () => {
    const html = core.buildStrategyTemplates({
      auto_deployer_config: { max_positions: 5, risk_settings: { default_stop_loss_pct: 0.10 } },
      guardrails: { max_positions: 5, max_position_pct: 0.10 },
    });
    // moderate preset should be active
    const activeMatches = html.match(/preset-card-v2 active/g) || [];
    expect(activeMatches.length).toBe(1);
    expect(html).toContain('MODERATE');
  });

  it('custom label when no preset matches', () => {
    const html = core.buildStrategyTemplates({
      auto_deployer_config: { max_positions: 7, risk_settings: { default_stop_loss_pct: 0.08 } },
    });
    expect(html).toContain('CUSTOM');
  });

  it('Apply buttons wired to applyPreset(key)', () => {
    const html = core.buildStrategyTemplates({});
    expect(html).toContain("applyPreset('conservative')");
    expect(html).toContain("applyPreset('moderate')");
    expect(html).toContain("applyPreset('aggressive')");
  });
});

// ---------------------------------------------------------------------------
// buildNextActionsPanel
// ---------------------------------------------------------------------------
describe('buildNextActionsPanel', () => {
  it('ON badges when autoDeployerEnabled=true + killSwitchActive=false', () => {
    const html = core.buildNextActionsPanel(
      { open_orders: [], positions: [] },
      { autoDeployerEnabled: true, killSwitchActive: false, guardrails: {} },
    );
    expect(html).toContain('badge-on">ON');
    expect(html).not.toContain('Kill Switch: ACTIVE');
  });

  it('OFF badges when killSwitchActive=true', () => {
    const html = core.buildNextActionsPanel(
      { open_orders: [], positions: [] },
      { autoDeployerEnabled: true, killSwitchActive: true, guardrails: {} },
    );
    expect(html).toContain('badge-off">OFF');
    expect(html).toContain('Kill Switch: ACTIVE');
  });

  it('pending orders list when open_orders present', () => {
    const html = core.buildNextActionsPanel(
      {
        open_orders: [{ symbol: 'AAPL', side: 'buy', qty: 10, type: 'limit', limit_price: '150' }],
        positions: [],
      },
      { autoDeployerEnabled: true, killSwitchActive: false },
    );
    expect(html).toContain('Pending Actions');
    expect(html).toContain('AAPL limit buy (10 shares) @ $150');
  });

  it('current positions list when positions present but no orders', () => {
    const html = core.buildNextActionsPanel(
      { open_orders: [], positions: [{ symbol: 'SOXL', qty: 20 }] },
      { autoDeployerEnabled: true, killSwitchActive: false },
    );
    expect(html).toContain('Current Status');
    expect(html).toContain('SOXL: holding 20 shares');
  });

  it('"No pending orders" when both arrays empty', () => {
    const html = core.buildNextActionsPanel(
      { open_orders: [], positions: [] },
      { autoDeployerEnabled: true, killSwitchActive: false },
    );
    expect(html).toContain('No pending orders or positions');
  });

  it('renders guardrail pills with supplied guardrails', () => {
    const html = core.buildNextActionsPanel(
      { open_orders: [], positions: [] },
      {
        guardrails: {
          daily_loss_limit_pct: 0.05,
          max_drawdown_pct: 0.15,
          max_positions: 8,
          max_position_pct: 0.12,
        },
      },
    );
    expect(html).toContain('Daily loss limit: 5%');
    expect(html).toContain('Max drawdown: 15%');
    expect(html).toContain('Max positions: 8');
    expect(html).toContain('Max per stock: 12%');
  });

  it('falls back to window globals when opts not provided', () => {
    globalThis.autoDeployerEnabled = false;
    globalThis.killSwitchActive = true;
    globalThis.guardrailsData = { daily_loss_limit_pct: 0.02 };
    try {
      const html = core.buildNextActionsPanel({ open_orders: [], positions: [] });
      expect(html).toContain('Kill Switch: ACTIVE');
      expect(html).toContain('Daily loss limit: 2%');
    } finally {
      delete globalThis.autoDeployerEnabled;
      delete globalThis.killSwitchActive;
      delete globalThis.guardrailsData;
    }
  });
});
