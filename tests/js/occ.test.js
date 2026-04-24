// Round-61 pt.8 — OCC option-symbol parser (_occParse).
//
// OCC format: ROOT(1-6) YY MM DD C|P STRIKE(8 digits, strike*1000)
// Example: HIMS260508P00027000 = HIMS, 2026-05-08, Put, $27.00 strike.
//
// The dashboard uses _occParse to render options as "HIMS put May 8, 2026
// $27" instead of the raw 17-character contract symbol nobody recognises.
// Round-58 + #110 + #112 + #114 all touch option labeling — pin the
// parser tightly.

import { describe, it, expect, beforeAll, vi } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('_occParse — valid OCC symbols', () => {
  it('HIMS 2026-05-08 P $27.00', () => {
    const out = api._occParse('HIMS260508P00027000');
    expect(out).toBeTruthy();
    expect(out.underlying).toBe('HIMS');
    expect(out.type).toBe('put');
    expect(out.strike).toBe(27.0);
    expect(out.expiryLabel).toBe('May 8, 2026');
  });

  it('AAPL 2025-12-19 C $250.00', () => {
    const out = api._occParse('AAPL251219C00250000');
    expect(out.underlying).toBe('AAPL');
    expect(out.type).toBe('call');
    expect(out.strike).toBe(250.0);
    expect(out.expiryLabel).toBe('Dec 19, 2025');
  });

  it('strike with cents (TQQQ $52.50)', () => {
    const out = api._occParse('TQQQ260117C00052500');
    expect(out.strike).toBe(52.5);
  });

  it('1-letter underlying (F = Ford)', () => {
    const out = api._occParse('F260116P00012000');
    expect(out.underlying).toBe('F');
    expect(out.strike).toBe(12.0);
  });

  it('6-letter underlying max length (TSLAQQ — synthetic test)', () => {
    const out = api._occParse('TSLAQQ260116P00012000');
    expect(out).toBeTruthy();
    expect(out.underlying).toBe('TSLAQQ');
  });
});

describe('_occParse — invalid input', () => {
  it('plain ticker returns null', () => {
    expect(api._occParse('AAPL')).toBeNull();
  });

  it('null / undefined / empty returns null', () => {
    expect(api._occParse(null)).toBeNull();
    expect(api._occParse(undefined)).toBeNull();
    expect(api._occParse('')).toBeNull();
  });

  it('lowercase rejected (OCC requires uppercase root)', () => {
    expect(api._occParse('aapl251219C00250000')).toBeNull();
  });

  it('non-CP rights letter rejected', () => {
    expect(api._occParse('AAPL251219X00250000')).toBeNull();
  });

  it('truncated symbol returns null', () => {
    expect(api._occParse('HIMS260508P000270')).toBeNull();
  });

  it('too-long underlying (7 chars) returns null', () => {
    expect(api._occParse('TOOLONG260508P00027000')).toBeNull();
  });
});

describe('_occParse — DTE calculation', () => {
  it('DTE clamped to 0 for past expirations', () => {
    // Force "now" to be after the expiration so DTE would naturally be negative
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2027, 0, 1)); // Jan 1, 2027
    try {
      const out = api._occParse('HIMS260508P00027000'); // expired May 8, 2026
      expect(out.dte).toBe(0); // never negative
    } finally {
      vi.useRealTimers();
    }
  });

  it('DTE positive for future expirations', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 4, 1)); // May 1, 2026
    try {
      const out = api._occParse('HIMS260508P00027000'); // May 8, 2026 → ~7 DTE
      expect(out.dte).toBeGreaterThan(5);
      expect(out.dte).toBeLessThan(10);
    } finally {
      vi.useRealTimers();
    }
  });
});
