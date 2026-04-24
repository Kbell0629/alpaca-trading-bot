// Round-61 pt.8 batch-5 — fmtSchedLast tests.
//
// Scheduler-aware variant of fmtRelative. Treats bare YYYY-MM-DD entries
// as that task's scheduled fire time (auto_deployer → 9:45 AM local, etc).
// Round-11 fix: a daily task that ran today rendered as "1d ago" under
// the old UTC-midnight interpretation.

import { describe, it, expect, beforeAll, vi } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
  api.fmtSchedLast = globalThis.fmtSchedLast;
});

describe('fmtSchedLast — nulls / unparseable', () => {
  it('null / undefined → "never"', () => {
    expect(api.fmtSchedLast(null)).toBe('never');
    expect(api.fmtSchedLast(undefined)).toBe('never');
  });

  it('unparseable string returns String(v)', () => {
    expect(api.fmtSchedLast('garbage')).toBe('garbage');
  });
});

describe('fmtSchedLast — time buckets', () => {
  const FIXED_NOW = new Date(2026, 3, 24, 16, 0, 0).getTime(); // local 4pm

  it('< 1 min → "just now"', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      // Timestamp 30s ago (unix-seconds)
      expect(api.fmtSchedLast(Math.floor((FIXED_NOW - 30_000) / 1000)))
        .toBe('just now');
    } finally {
      vi.useRealTimers();
    }
  });

  it('"scheduled" for negative diff (future)', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      expect(api.fmtSchedLast(Math.floor((FIXED_NOW + 60_000) / 1000)))
        .toBe('scheduled');
    } finally {
      vi.useRealTimers();
    }
  });

  it('1-59 min → "Xm ago"', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      expect(api.fmtSchedLast(Math.floor((FIXED_NOW - 15 * 60_000) / 1000)))
        .toBe('15m ago');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('fmtSchedLast — scheduler-aware date handling', () => {
  // Round-11 fix: bare YYYY-MM-DD entries use task's fire time, not
  // UTC midnight.
  it('same-day daily_close (16:05) ran earlier today → "Xh ago" not "1d ago"', () => {
    // Set now to 5pm local on 2026-04-24. daily_close fires at 16:05.
    const now = new Date(2026, 3, 24, 17, 0, 0).getTime();
    vi.useFakeTimers();
    vi.setSystemTime(now);
    try {
      const out = api.fmtSchedLast('2026-04-24', 'daily_close');
      // 17:00 - 16:05 = 55 minutes
      expect(out).toBe('55m ago');
    } finally {
      vi.useRealTimers();
    }
  });

  it('yesterday\'s daily_close (>24h ago) → "yesterday" (calendar-day math)', () => {
    // daily_close fires 16:05. From 2026-04-23's 16:05 to 2026-04-24 17:00
    // is 24h55m, so hrs=24 triggers the calendar-day branch.
    const now = new Date(2026, 3, 24, 17, 0, 0).getTime();
    vi.useFakeTimers();
    vi.setSystemTime(now);
    try {
      const out = api.fmtSchedLast('2026-04-23', 'daily_close');
      expect(out).toBe('yesterday');
    } finally {
      vi.useRealTimers();
    }
  });

  it('3 calendar days ago → "3d ago"', () => {
    const now = new Date(2026, 3, 24, 10, 0, 0).getTime();
    vi.useFakeTimers();
    vi.setSystemTime(now);
    try {
      const out = api.fmtSchedLast('2026-04-21', 'daily_close');
      expect(out).toBe('3d ago');
    } finally {
      vi.useRealTimers();
    }
  });
});
