// Round-61 pt.8 batch-7 — buildTodaysClosesPanel tests.
//
// Round-34 panel. Shows every trade that closed today with per-trade P&L
// + net total. Regression here hides user-visible trade results.

import { describe, it, expect, beforeAll } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('buildTodaysClosesPanel — empty state', () => {
  it('no todays_closes → returns empty string (panel hidden)', () => {
    expect(api.buildTodaysClosesPanel({})).toBe('');
    expect(api.buildTodaysClosesPanel({ todays_closes: [] })).toBe('');
    expect(api.buildTodaysClosesPanel({ todays_closes: null })).toBe('');
  });
});

describe('buildTodaysClosesPanel — basic rendering', () => {
  const data = {
    todays_closes: [
      {
        symbol: 'AAPL', strategy: 'breakout', exit_reason: 'trailing_stop',
        exit_timestamp: '2026-04-24T14:30:00-04:00', exit_price: 185.50,
        pnl: 350.0, pnl_pct: 3.5,
      },
      {
        symbol: 'NVDA', strategy: 'mean_reversion', exit_reason: 'profit_target',
        exit_timestamp: '2026-04-24T15:45:00-04:00', exit_price: 905.25,
        pnl: -120.50, pnl_pct: -1.2,
      },
    ],
  };

  it('renders one data <tr> per close', () => {
    const html = api.buildTodaysClosesPanel(data);
    const dataRows = html.match(/<tr>/g) || [];
    // Header <tr> has a style attr so matches /<tr /. Data rows are bare <tr>.
    expect(dataRows.length).toBe(2);
  });

  it('positive P&L renders in green', () => {
    const html = api.buildTodaysClosesPanel(data);
    expect(html).toContain('var(--green)');
    expect(html).toContain('$350.00');
    expect(html).toContain('+3.50%');
  });

  it('negative P&L renders in red', () => {
    const html = api.buildTodaysClosesPanel(data);
    expect(html).toContain('var(--red)');
    expect(html).toContain('$-120.50');
    expect(html).toContain('-1.20%');
  });

  it('net P&L total = sum of closes', () => {
    const html = api.buildTodaysClosesPanel(data);
    // 350 + (-120.50) = $229.50
    expect(html).toContain('$229.50');
  });
});

describe('buildTodaysClosesPanel — orphan marker', () => {
  it('orphan_close=true flags the row with [orphan] marker', () => {
    const html = api.buildTodaysClosesPanel({
      todays_closes: [{
        symbol: 'SOXL', strategy: 'trailing_stop', exit_reason: 'stop',
        exit_timestamp: '2026-04-24T10:00:00-04:00', exit_price: 110.0,
        pnl: 0, pnl_pct: 0, orphan_close: true,
      }],
    });
    expect(html).toContain('[orphan]');
    expect(html).toContain('Synthetic entry');
  });

  it('orphan_close=false does not render marker', () => {
    const html = api.buildTodaysClosesPanel({
      todays_closes: [{
        symbol: 'AAPL', strategy: 'breakout', exit_reason: 'ts',
        exit_timestamp: '2026-04-24T10:00:00-04:00', exit_price: 150,
        pnl: 100, pnl_pct: 1,
      }],
    });
    expect(html).not.toContain('[orphan]');
  });
});

describe('buildTodaysClosesPanel — missing fields', () => {
  it('missing pnl renders em-dash, not "$NaN"', () => {
    const html = api.buildTodaysClosesPanel({
      todays_closes: [{
        symbol: 'X', strategy: '?', exit_reason: '?',
        exit_timestamp: '2026-04-24T10:00:00-04:00',
      }],
    });
    expect(html).not.toContain('$NaN');
    expect(html).toContain('—');
  });

  it('missing exit_price renders em-dash', () => {
    const html = api.buildTodaysClosesPanel({
      todays_closes: [{
        symbol: 'X', strategy: '?', exit_reason: '?', pnl: 0, pnl_pct: 0,
      }],
    });
    // Exit column should be em-dash when exit_price is missing
    expect(html).toContain('—');
  });

  it('XSS in symbol / strategy / reason is escaped', () => {
    const html = api.buildTodaysClosesPanel({
      todays_closes: [{
        symbol: '<img src=x onerror=alert(1)>',
        strategy: '<script>x</script>',
        exit_reason: 'reason<"',
        exit_timestamp: '2026-04-24T10:00:00-04:00',
        exit_price: 100, pnl: 50, pnl_pct: 1,
      }],
    });
    expect(html).not.toContain('<img src=x');
    expect(html).not.toContain('<script>x</script>');
    expect(html).toContain('&lt;img');
    expect(html).toContain('&lt;script&gt;');
  });
});
