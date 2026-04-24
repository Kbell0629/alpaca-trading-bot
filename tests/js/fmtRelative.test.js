// Round-61 pt.8 batch-3 — fmtRelative tests.
//
// Used for non-scheduler relative times (activity log timestamps, etc).
// The scheduler-aware variant (fmtSchedLast / parseSchedTs) is tested
// in scheduler.test.js.

import { describe, it, expect, beforeAll, vi } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('fmtRelative — null / invalid input', () => {
  it('null / undefined / empty → "never"', () => {
    expect(api.fmtRelative(null)).toBe('never');
    expect(api.fmtRelative(undefined)).toBe('never');
    expect(api.fmtRelative('')).toBe('never');
    expect(api.fmtRelative(0)).toBe('never');  // 0 is falsy too
  });

  it('non-numeric non-YYYY-MM-DD string returns the raw input', () => {
    expect(api.fmtRelative('garbage')).toBe('garbage');
    expect(api.fmtRelative('abc')).toBe('abc');
  });
});

describe('fmtRelative — time buckets', () => {
  const FIXED_NOW = new Date('2026-04-24T20:00:00Z').getTime();

  it('< 1 min → "just now"', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      expect(api.fmtRelative(new Date(FIXED_NOW - 30_000).toISOString())).toBe('just now');
      expect(api.fmtRelative(new Date(FIXED_NOW - 59_000).toISOString())).toBe('just now');
    } finally {
      vi.useRealTimers();
    }
  });

  it('1-59 min → "Xm ago"', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      expect(api.fmtRelative(new Date(FIXED_NOW - 60_000).toISOString())).toBe('1m ago');
      expect(api.fmtRelative(new Date(FIXED_NOW - 30 * 60_000).toISOString())).toBe('30m ago');
      expect(api.fmtRelative(new Date(FIXED_NOW - 59 * 60_000).toISOString())).toBe('59m ago');
    } finally {
      vi.useRealTimers();
    }
  });

  it('1-23 h → "Xh ago"', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      expect(api.fmtRelative(new Date(FIXED_NOW - 3600_000).toISOString())).toBe('1h ago');
      expect(api.fmtRelative(new Date(FIXED_NOW - 5 * 3600_000).toISOString())).toBe('5h ago');
      expect(api.fmtRelative(new Date(FIXED_NOW - 23 * 3600_000).toISOString())).toBe('23h ago');
    } finally {
      vi.useRealTimers();
    }
  });

  it('>= 24 h → "Xd ago"', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      expect(api.fmtRelative(new Date(FIXED_NOW - 86400_000).toISOString())).toBe('1d ago');
      expect(api.fmtRelative(new Date(FIXED_NOW - 7 * 86400_000).toISOString())).toBe('7d ago');
    } finally {
      vi.useRealTimers();
    }
  });

  it('future timestamp → "future"', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      expect(api.fmtRelative(new Date(FIXED_NOW + 60_000).toISOString())).toBe('future');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('fmtRelative — numeric input (unix seconds)', () => {
  const FIXED_NOW = new Date('2026-04-24T20:00:00Z').getTime();

  it('treats numeric input as unix-seconds (ms *1000)', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      // 10 minutes ago in unix-seconds
      const tenMinAgo = Math.floor((FIXED_NOW - 10 * 60_000) / 1000);
      expect(api.fmtRelative(tenMinAgo)).toBe('10m ago');
    } finally {
      vi.useRealTimers();
    }
  });
});
