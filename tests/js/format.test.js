// Round-61 pt.8 — Money / pct / pnl-class formatting helpers.
//
// These format every dollar amount + percentage on the dashboard. A
// regression here is visible to every user every refresh.

import { describe, it, expect, beforeAll } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('fmtMoney', () => {
  it('formats positive number with two decimals + comma grouping', () => {
    expect(api.fmtMoney(1234.5)).toBe('$1,234.50');
    expect(api.fmtMoney(1000000)).toBe('$1,000,000.00');
  });

  it('zero renders as $0.00', () => {
    expect(api.fmtMoney(0)).toBe('$0.00');
  });

  it('negative number keeps the minus sign before the dollar', () => {
    // Note: locale-string formatting puts the minus before $: `-$X.XX`
    expect(api.fmtMoney(-50)).toBe('$-50.00');
  });

  it('null / undefined / NaN coerced to $0.00 (no $NaN bug)', () => {
    expect(api.fmtMoney(null)).toBe('$0.00');
    expect(api.fmtMoney(undefined)).toBe('$0.00');
    expect(api.fmtMoney(NaN)).toBe('$0.00');
    expect(api.fmtMoney('abc')).toBe('$0.00');
  });

  it('Infinity coerced to $0.00 (isFinite guard)', () => {
    expect(api.fmtMoney(Infinity)).toBe('$0.00');
    expect(api.fmtMoney(-Infinity)).toBe('$0.00');
  });

  it('string-numeric coerces correctly', () => {
    expect(api.fmtMoney('100.5')).toBe('$100.50');
  });

  it('rounds to 2 decimals (cuts off third)', () => {
    // toLocaleString with maximumFractionDigits:2 → banker-ish rounding
    expect(api.fmtMoney(1.234)).toBe('$1.23');
    expect(api.fmtMoney(1.236)).toBe('$1.24');
  });
});

describe('fmtPct', () => {
  it('positive number gets explicit + prefix', () => {
    expect(api.fmtPct(2.5)).toBe('+2.5%');
    expect(api.fmtPct(0)).toBe('+0.0%');
  });

  it('negative number keeps the minus, no + prefix', () => {
    expect(api.fmtPct(-1.5)).toBe('-1.5%');
  });

  it('one decimal place always rendered', () => {
    expect(api.fmtPct(5)).toBe('+5.0%');
    expect(api.fmtPct(5.49)).toBe('+5.5%');
  });

  it('null / undefined / NaN coerced to +0.0%', () => {
    expect(api.fmtPct(null)).toBe('+0.0%');
    expect(api.fmtPct(undefined)).toBe('+0.0%');
    expect(api.fmtPct(NaN)).toBe('+0.0%');
    expect(api.fmtPct('garbage')).toBe('+0.0%');
  });

  it('Infinity coerced to +0.0%', () => {
    expect(api.fmtPct(Infinity)).toBe('+0.0%');
    expect(api.fmtPct(-Infinity)).toBe('+0.0%');
  });
});

describe('pnlClass', () => {
  it('strictly positive → positive', () => {
    expect(api.pnlClass(1)).toBe('positive');
    expect(api.pnlClass(0.01)).toBe('positive');
    expect(api.pnlClass(1000)).toBe('positive');
  });

  it('strictly negative → negative', () => {
    expect(api.pnlClass(-1)).toBe('negative');
    expect(api.pnlClass(-0.01)).toBe('negative');
  });

  it('exactly zero → neutral (NOT positive — break-even is not a win)', () => {
    expect(api.pnlClass(0)).toBe('neutral');
    expect(api.pnlClass('0')).toBe('neutral');
    expect(api.pnlClass('0.00')).toBe('neutral');
  });

  it('non-numeric / NaN → neutral', () => {
    expect(api.pnlClass(NaN)).toBe('neutral');
    expect(api.pnlClass('abc')).toBe('neutral');
    expect(api.pnlClass(null)).toBe('neutral');
    expect(api.pnlClass(undefined)).toBe('neutral');
  });

  it('string-numeric correctly classified', () => {
    expect(api.pnlClass('5')).toBe('positive');
    expect(api.pnlClass('-5')).toBe('negative');
  });
});

describe('fmtUpdatedET', () => {
  it('null / empty returns N/A', () => {
    expect(api.fmtUpdatedET(null)).toBe('N/A');
    expect(api.fmtUpdatedET(undefined)).toBe('N/A');
    expect(api.fmtUpdatedET('')).toBe('N/A');
  });

  it('server-tagged ET string short-circuits the time extraction', () => {
    expect(api.fmtUpdatedET('2026-04-24 14:35:00 ET')).toBe('14:35:00 ET');
    // 12-hour with AM/PM
    expect(api.fmtUpdatedET('2026-04-24 2:35:00 PM ET')).toBe('2:35:00 PM ET');
  });

  it('legacy UTC string is parsed and re-rendered as ET', () => {
    // 2026-04-24T16:00:00Z is exactly noon ET (DST → UTC-4)
    const out = api.fmtUpdatedET('2026-04-24 16:00:00 UTC');
    expect(out).toMatch(/12:00:00\s+PM ET/);
  });

  it('garbage input returns N/A (no exception leaks)', () => {
    expect(api.fmtUpdatedET('not-a-date')).toBe('N/A');
  });
});
