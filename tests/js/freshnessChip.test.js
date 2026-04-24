// Round-61 pt.8 batch-2 — freshnessChip tests.
//
// Renders a "last updated Xs ago" pill on every panel that auto-refreshes.
// Pinned by the post-60 architectural invariant in CLAUDE.md:
//   "freshnessChip(updatedAt, label) MUST emit data-label='...' when label
//    is non-empty so the in-place patch can regenerate the chip."
//
// Additional invariants exercised here: age-tier classes (fresh / stale /
// very-stale), relative-time rollover thresholds (s/m/h), XSS escape on
// the label + title, empty-return on null/invalid input.

import { describe, it, expect, beforeAll, vi } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

/** Small helper: parse the returned HTML string into a DOM element so
 * tests can inspect attributes / classes without string-matching. */
function parse(html) {
  const wrapper = document.createElement('div');
  wrapper.innerHTML = html;
  return wrapper.firstElementChild;
}

describe('freshnessChip — null / invalid input', () => {
  it('null updatedAt returns empty string (no chip)', () => {
    expect(api.freshnessChip(null)).toBe('');
    expect(api.freshnessChip(undefined)).toBe('');
    expect(api.freshnessChip('')).toBe('');
  });

  it('unparseable string returns empty (no chip, no throw)', () => {
    expect(api.freshnessChip('not a date', 'Label')).toBe('');
  });
});

describe('freshnessChip — age tiers', () => {
  // The helper compares against Date.now(); use vitest fake timers so
  // the age is deterministic regardless of when the test runs.
  const FIXED_NOW = new Date('2026-04-24T20:00:00Z').getTime();

  it('< 60s → "Ns ago" with no staleness class', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      const out = api.freshnessChip(new Date(FIXED_NOW - 5000).toISOString());
      const el = parse(out);
      expect(el.textContent).toContain('5s ago');
      expect(el.classList.contains('stale')).toBe(false);
      expect(el.classList.contains('very-stale')).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('1-59 min → "Nm ago" (no staleness at ≤2min, stale at >2min)', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      const fresh = parse(api.freshnessChip(new Date(FIXED_NOW - 90 * 1000).toISOString()));
      expect(fresh.textContent).toContain('1m ago');
      expect(fresh.classList.contains('stale')).toBe(false);

      const stale = parse(api.freshnessChip(new Date(FIXED_NOW - 3 * 60 * 1000).toISOString()));
      expect(stale.textContent).toContain('3m ago');
      expect(stale.classList.contains('stale')).toBe(true);
      expect(stale.classList.contains('very-stale')).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('> 5 min → very-stale class', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      const el = parse(api.freshnessChip(new Date(FIXED_NOW - 10 * 60 * 1000).toISOString()));
      expect(el.textContent).toContain('10m ago');
      expect(el.classList.contains('very-stale')).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('≥ 1h → "Nh ago"', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      const el = parse(api.freshnessChip(new Date(FIXED_NOW - 2 * 3600 * 1000).toISOString()));
      expect(el.textContent).toContain('2h ago');
      expect(el.classList.contains('very-stale')).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('negative age (future timestamp) clamps to 0s', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      const el = parse(api.freshnessChip(new Date(FIXED_NOW + 10000).toISOString()));
      expect(el.textContent).toContain('0s ago');
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('freshnessChip — post-60 data-label invariant', () => {
  // CLAUDE.md: "freshnessChip MUST emit data-label='...' when label is
  // non-empty so the in-place patch can regenerate the chip." The
  // in-place refresh path matches on [data-label="X"] to replace just
  // the chip without rebuilding #app.
  const FIXED_NOW = new Date('2026-04-24T20:00:00Z').getTime();

  it('label argument emitted as data-label attribute', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      const out = api.freshnessChip(
        new Date(FIXED_NOW - 10000).toISOString(),
        'Factor Health');
      const el = parse(out);
      expect(el.getAttribute('data-label')).toBe('Factor Health');
      // Also appears as prefix in the visible text
      expect(el.textContent).toContain('Factor Health');
    } finally {
      vi.useRealTimers();
    }
  });

  it('no label → no data-label attribute (don\'t emit empty ones)', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      const out = api.freshnessChip(new Date(FIXED_NOW - 10000).toISOString());
      const el = parse(out);
      expect(el.hasAttribute('data-label')).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('freshnessChip — XSS safety', () => {
  const FIXED_NOW = new Date('2026-04-24T20:00:00Z').getTime();

  it('label is HTML-escaped in the output', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      const out = api.freshnessChip(
        new Date(FIXED_NOW - 1000).toISOString(),
        '<script>alert(1)</script>');
      // Raw string inspection — inner HTML must be escaped
      expect(out).not.toContain('<script>alert(1)</script>');
      expect(out).toContain('&lt;script&gt;');
    } finally {
      vi.useRealTimers();
    }
  });

  it('title attribute contains the updatedAt string', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      const ts = new Date(FIXED_NOW - 1000).toISOString();
      const out = api.freshnessChip(ts, 'Panel');
      const el = parse(out);
      // The title attr exposes the raw timestamp for user hover-inspection
      expect(el.getAttribute('title')).toContain(ts);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe('freshnessChip — numeric input (unix seconds)', () => {
  const FIXED_NOW = new Date('2026-04-24T20:00:00Z').getTime();

  it('accepts unix-seconds number and treats as ms*1000', () => {
    vi.useFakeTimers();
    vi.setSystemTime(FIXED_NOW);
    try {
      const thirtySecondsAgo = Math.floor((FIXED_NOW - 30_000) / 1000);
      const out = api.freshnessChip(thirtySecondsAgo);
      const el = parse(out);
      expect(el.textContent).toContain('30s ago');
    } finally {
      vi.useRealTimers();
    }
  });
});
