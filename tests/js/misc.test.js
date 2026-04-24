// Round-61 pt.8 batch-6 (cont) — assorted pure helpers.
//
// Covers small-surface utilities that fit the test harness cleanly:
//   * heatmapColor — heat tier for daily P&L squares
//   * fmtAuditTime — admin-log ISO → "Apr 24, 11:30:00 PM ET"
//   * ariaSort — screener column sort-aria accessor
//   * getMarketRegime — derives "bull"/"bear"/"neutral" from pick averages

import { describe, it, expect, beforeAll, vi } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('heatmapColor', () => {
  it('null / undefined → "empty"', () => {
    expect(api.heatmapColor(null)).toBe('empty');
    expect(api.heatmapColor(undefined)).toBe('empty');
  });

  it('steep loss (< -2%) → loss-big', () => {
    expect(api.heatmapColor(-5)).toBe('loss-big');
    expect(api.heatmapColor(-2.01)).toBe('loss-big');
  });

  it('moderate loss (-2 to -0.5) → loss', () => {
    expect(api.heatmapColor(-1)).toBe('loss');
    expect(api.heatmapColor(-0.6)).toBe('loss');
  });

  it('small loss (-0.5 to -0.05) → loss-small', () => {
    expect(api.heatmapColor(-0.1)).toBe('loss-small');
  });

  it('flat (-0.05 to +0.05) → flat', () => {
    expect(api.heatmapColor(0)).toBe('flat');
    expect(api.heatmapColor(0.02)).toBe('flat');
    expect(api.heatmapColor(-0.03)).toBe('flat');
  });

  it('small win (0.05 to 0.5) → win-small', () => {
    expect(api.heatmapColor(0.1)).toBe('win-small');
  });

  it('moderate win (0.5 to 2) → win', () => {
    expect(api.heatmapColor(1)).toBe('win');
  });

  it('big win (>= 2) → win-big', () => {
    expect(api.heatmapColor(3)).toBe('win-big');
    expect(api.heatmapColor(20)).toBe('win-big');
  });
});

describe('fmtAuditTime', () => {
  it('empty / null / undefined → em-dash', () => {
    expect(api.fmtAuditTime(null)).toBe('—');
    expect(api.fmtAuditTime('')).toBe('—');
    expect(api.fmtAuditTime(undefined)).toBe('—');
  });

  it('unparseable input returned as-is', () => {
    expect(api.fmtAuditTime('not a date')).toBe('not a date');
  });

  it('valid ISO renders with ET suffix', () => {
    const out = api.fmtAuditTime('2026-04-24T20:00:00Z');
    expect(out).toContain('ET');
    expect(out).toContain('Apr');
  });
});

describe('ariaSort', () => {
  it("returns 'none' when column is not the active sort target", () => {
    // Without any screener sort happening, ariaSort returns 'none'.
    expect(api.ariaSort('some_random_col')).toBe('none');
  });
});

describe('getMarketRegime', () => {
  it('returns the explicit regime if data provides one', () => {
    expect(api.getMarketRegime({ market_regime: 'bear' })).toBe('bear');
  });

  it('no picks → neutral', () => {
    expect(api.getMarketRegime({ picks: [] })).toBe('neutral');
    expect(api.getMarketRegime({})).toBe('neutral');
  });

  it('average > +1% → bull', () => {
    const picks = Array.from({ length: 20 }, (_, i) => ({ daily_change: 2.5 }));
    expect(api.getMarketRegime({ picks })).toBe('bull');
  });

  it('average < -1% → bear', () => {
    const picks = Array.from({ length: 20 }, (_, i) => ({ daily_change: -2 }));
    expect(api.getMarketRegime({ picks })).toBe('bear');
  });

  it('average within [-1, +1] → neutral', () => {
    const picks = Array.from({ length: 20 }, (_, i) => ({ daily_change: 0.3 }));
    expect(api.getMarketRegime({ picks })).toBe('neutral');
  });
});
