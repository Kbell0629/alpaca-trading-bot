// Round-61 pt.8 Option B — direct tests against the extracted render-core
// module. This file loads static/dashboard_render_core.js in isolation
// (no templates/dashboard.html inline-script gymnastics) so coverage
// tooling sees it as its own unit.
//
// The existing tests in esc.test.js / format.test.js / occ.test.js /
// scheduler.test.js / freshnessChip.test.js / misc.test.js / etc. keep
// working — they go through loadDashboardJs which runs the inline <script>
// after this file's globals are already in place.

import { describe, it, expect, beforeAll, vi } from 'vitest';
import { loadRenderCore } from './loadRenderCore.js';

let core;
beforeAll(() => { core = loadRenderCore(); });

describe('loadRenderCore — module shape', () => {
  it('returns a DashboardRenderCore namespace with expected exports', () => {
    expect(core).toBeTruthy();
    for (const name of [
      'esc', 'jsStr',
      'fmtMoney', 'fmtPct', 'pnlClass', 'fmtUpdatedET', 'fmtAuditTime', 'fmtRelative',
      '_occParse',
      'SCHEDULED_TIME_MAP', 'parseSchedTs', 'latestForTask', 'fmtSchedLast',
      'getMarketRegime', 'heatmapColor', 'freshnessChip',
      'detectActivePreset',
      '_sanitizeReadmeHtml', '_README_ALLOWED_TAGS', '_README_ALLOWED_ATTRS',
    ]) {
      expect(core[name], `${name} missing from DashboardRenderCore`).toBeTruthy();
    }
  });

  it('attaches functions to globalThis for backward compat with inline callers', () => {
    // The inline <script> in dashboard.html calls esc/fmtMoney/etc. as
    // free globals. The IIFE must attach each export to window.
    expect(typeof globalThis.esc).toBe('function');
    expect(typeof globalThis.fmtMoney).toBe('function');
    expect(typeof globalThis._occParse).toBe('function');
    expect(globalThis.SCHEDULED_TIME_MAP).toEqual(core.SCHEDULED_TIME_MAP);
  });
});

describe('esc — XSS escape', () => {
  it('null / undefined → empty string', () => {
    expect(core.esc(null)).toBe('');
    expect(core.esc(undefined)).toBe('');
  });

  it('HTML special chars escaped', () => {
    expect(core.esc('<script>alert(1)</script>')).toBe(
      '&lt;script&gt;alert(1)&lt;/script&gt;');
    expect(core.esc('a & b')).toBe('a &amp; b');
    expect(core.esc('"q"')).toBe('&quot;q&quot;');
    expect(core.esc("'q'")).toBe('&#39;q&#39;');
    expect(core.esc('`q`')).toBe('&#96;q&#96;');
  });

  it('non-strings coerced via String()', () => {
    expect(core.esc(42)).toBe('42');
    expect(core.esc(true)).toBe('true');
  });
});

describe('jsStr', () => {
  it('null / undefined → empty', () => {
    expect(core.jsStr(null)).toBe('');
    expect(core.jsStr(undefined)).toBe('');
  });

  it('quotes and angle brackets escape to unicode', () => {
    expect(core.jsStr("a'b")).toBe('a\\u0027b');
    expect(core.jsStr('a"b')).toBe('a\\u0022b');
    expect(core.jsStr('a<b')).toBe('a\\u003cb');
    expect(core.jsStr('a>b')).toBe('a\\u003eb');
  });

  it('backslash + newlines + CR escape', () => {
    expect(core.jsStr('a\\b')).toBe('a\\\\b');
    expect(core.jsStr('a\nb')).toBe('a\\nb');
    expect(core.jsStr('a\rb')).toBe('a\\rb');
  });
});

describe('fmtMoney', () => {
  it('formats with comma + 2dp', () => {
    expect(core.fmtMoney(1234.5)).toBe('$1,234.50');
    expect(core.fmtMoney(0)).toBe('$0.00');
    expect(core.fmtMoney(-100)).toBe('$-100.00');
  });

  it('NaN/Infinity coerce to 0', () => {
    expect(core.fmtMoney(NaN)).toBe('$0.00');
    expect(core.fmtMoney(Infinity)).toBe('$0.00');
    expect(core.fmtMoney(null)).toBe('$0.00');
  });
});

describe('fmtPct', () => {
  it('positive gets +, negative keeps -', () => {
    expect(core.fmtPct(2.5)).toBe('+2.5%');
    expect(core.fmtPct(-1.5)).toBe('-1.5%');
    expect(core.fmtPct(0)).toBe('+0.0%');
  });

  it('NaN/garbage → +0.0%', () => {
    expect(core.fmtPct(NaN)).toBe('+0.0%');
    expect(core.fmtPct('garbage')).toBe('+0.0%');
  });
});

describe('pnlClass', () => {
  it('strict positive → positive', () => {
    expect(core.pnlClass(1)).toBe('positive');
  });
  it('strict negative → negative', () => {
    expect(core.pnlClass(-1)).toBe('negative');
  });
  it('zero and non-numeric → neutral', () => {
    expect(core.pnlClass(0)).toBe('neutral');
    expect(core.pnlClass(NaN)).toBe('neutral');
    expect(core.pnlClass('abc')).toBe('neutral');
  });
});

describe('fmtUpdatedET', () => {
  it('null / empty → N/A', () => {
    expect(core.fmtUpdatedET(null)).toBe('N/A');
    expect(core.fmtUpdatedET('')).toBe('N/A');
  });
  it('already-tagged ET string preserved', () => {
    expect(core.fmtUpdatedET('2026-04-24 14:35:00 ET')).toBe('14:35:00 ET');
  });
  it('ISO converted to ET', () => {
    const out = core.fmtUpdatedET('2026-04-24T16:00:00Z');
    expect(out).toMatch(/12:00:00\s+PM ET/);
  });
  it('invalid → N/A', () => {
    expect(core.fmtUpdatedET('garbage')).toBe('N/A');
  });
});

describe('fmtAuditTime', () => {
  it('null → em-dash', () => {
    expect(core.fmtAuditTime(null)).toBe('—');
  });
  it('invalid → passthrough', () => {
    expect(core.fmtAuditTime('not a date')).toBe('not a date');
  });
  it('valid ISO → ET-tagged localized string', () => {
    const out = core.fmtAuditTime('2026-04-24T20:00:00Z');
    expect(out).toContain('ET');
    expect(out).toContain('Apr');
  });
});

describe('fmtRelative', () => {
  const NOW = new Date('2026-04-24T20:00:00Z').getTime();

  it('null → never', () => {
    expect(core.fmtRelative(null)).toBe('never');
  });

  it('future → future', () => {
    vi.useFakeTimers(); vi.setSystemTime(NOW);
    try {
      expect(core.fmtRelative(new Date(NOW + 60000).toISOString())).toBe('future');
    } finally { vi.useRealTimers(); }
  });

  it('buckets s / m / h / d', () => {
    vi.useFakeTimers(); vi.setSystemTime(NOW);
    try {
      expect(core.fmtRelative(new Date(NOW - 30000).toISOString())).toBe('just now');
      expect(core.fmtRelative(new Date(NOW - 15 * 60000).toISOString())).toBe('15m ago');
      expect(core.fmtRelative(new Date(NOW - 2 * 3600000).toISOString())).toBe('2h ago');
      expect(core.fmtRelative(new Date(NOW - 3 * 86400000).toISOString())).toBe('3d ago');
    } finally { vi.useRealTimers(); }
  });

  it('unix-seconds numeric input', () => {
    vi.useFakeTimers(); vi.setSystemTime(NOW);
    try {
      expect(core.fmtRelative(Math.floor((NOW - 5 * 60000) / 1000))).toBe('5m ago');
    } finally { vi.useRealTimers(); }
  });

  it('garbage string passthrough', () => {
    expect(core.fmtRelative('garbage')).toBe('garbage');
  });
});

describe('_occParse', () => {
  it('valid symbol decoded', () => {
    const out = core._occParse('HIMS260508P00027000');
    expect(out.underlying).toBe('HIMS');
    expect(out.type).toBe('put');
    expect(out.strike).toBe(27);
    expect(out.expiryLabel).toBe('May 8, 2026');
  });

  it('invalid → null', () => {
    expect(core._occParse(null)).toBeNull();
    expect(core._occParse('AAPL')).toBeNull();
    expect(core._occParse('aapl251219C00250000')).toBeNull(); // lowercase
    expect(core._occParse('AAPL251219X00250000')).toBeNull(); // bad right letter
  });

  it('DTE clamped at 0 for expired options', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2027, 0, 1));
    try {
      expect(core._occParse('HIMS260508P00027000').dte).toBe(0);
    } finally { vi.useRealTimers(); }
  });
});

describe('parseSchedTs + latestForTask', () => {
  it('parseSchedTs — bare YYYY-MM-DD with known task → fire-time local', () => {
    const ts = core.parseSchedTs('2026-04-24', 'daily_close');
    const d = new Date(ts);
    expect(d.getHours()).toBe(16);
    expect(d.getMinutes()).toBe(5);
  });

  it('parseSchedTs — numeric unix-seconds → ms', () => {
    expect(core.parseSchedTs(1700000000)).toBe(1700000000000);
  });

  it('latestForTask — prefix-match selects newest', () => {
    const last = {
      auto_deployer_1: '2026-04-24',
      auto_deployer_2: '2026-04-23',
    };
    expect(core.latestForTask(last, 'auto_deployer')).toBe('2026-04-24');
  });

  it('latestForTask — unrelated keys ignored', () => {
    expect(core.latestForTask({ other: '2026-04-24' }, 'auto_deployer')).toBeNull();
  });
});

describe('fmtSchedLast', () => {
  it('null → never', () => {
    expect(core.fmtSchedLast(null)).toBe('never');
  });

  it('future → scheduled', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 3, 24, 10, 0, 0));
    try {
      expect(core.fmtSchedLast(Math.floor((Date.now() + 60000) / 1000))).toBe('scheduled');
    } finally { vi.useRealTimers(); }
  });

  it('yesterday calendar-day rollover', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 3, 24, 17, 0, 0));
    try {
      expect(core.fmtSchedLast('2026-04-23', 'daily_close')).toBe('yesterday');
    } finally { vi.useRealTimers(); }
  });
});

describe('getMarketRegime', () => {
  it('explicit regime wins', () => {
    expect(core.getMarketRegime({ market_regime: 'bear' })).toBe('bear');
  });
  it('no picks → neutral', () => {
    expect(core.getMarketRegime({})).toBe('neutral');
  });
  it('bull/bear from pick averages', () => {
    const bull = { picks: Array(10).fill({ daily_change: 3 }) };
    const bear = { picks: Array(10).fill({ daily_change: -3 }) };
    expect(core.getMarketRegime(bull)).toBe('bull');
    expect(core.getMarketRegime(bear)).toBe('bear');
  });
});

describe('heatmapColor', () => {
  it('null → empty', () => {
    expect(core.heatmapColor(null)).toBe('empty');
  });
  it('tiers', () => {
    expect(core.heatmapColor(-5)).toBe('loss-big');
    expect(core.heatmapColor(-1)).toBe('loss');
    expect(core.heatmapColor(-0.2)).toBe('loss-small');
    expect(core.heatmapColor(0)).toBe('flat');
    expect(core.heatmapColor(0.2)).toBe('win-small');
    expect(core.heatmapColor(1)).toBe('win');
    expect(core.heatmapColor(5)).toBe('win-big');
  });
});

describe('freshnessChip', () => {
  const NOW = new Date('2026-04-24T20:00:00Z').getTime();

  it('null → empty string (no chip)', () => {
    expect(core.freshnessChip(null)).toBe('');
  });
  it('emits data-label attribute when label is provided', () => {
    vi.useFakeTimers(); vi.setSystemTime(NOW);
    try {
      const out = core.freshnessChip(new Date(NOW - 10000).toISOString(), 'Factor Health');
      expect(out).toContain('data-label="Factor Health"');
      expect(out).toContain('Factor Health 10s ago');
    } finally { vi.useRealTimers(); }
  });
  it('stale + very-stale tiers', () => {
    vi.useFakeTimers(); vi.setSystemTime(NOW);
    try {
      const stale = core.freshnessChip(new Date(NOW - 3 * 60000).toISOString());
      expect(stale).toContain('class="data-freshness stale"');
      const veryStale = core.freshnessChip(new Date(NOW - 10 * 60000).toISOString());
      expect(veryStale).toContain('class="data-freshness very-stale"');
    } finally { vi.useRealTimers(); }
  });
});

describe('detectActivePreset', () => {
  const mk = (stop, maxPos, perStock) => ({
    guardrails: { max_positions: maxPos, max_position_pct: perStock },
    auto_deployer_config: {
      max_positions: maxPos,
      risk_settings: { default_stop_loss_pct: stop },
    },
  });
  it('conservative', () => {
    expect(core.detectActivePreset(mk(0.05, 3, 0.05))).toBe('conservative');
  });
  it('moderate (10% stop, 5 pos, 10% per stock)', () => {
    expect(core.detectActivePreset(mk(0.10, 5, 0.10))).toBe('moderate');
  });
  it('moderate (auto-migrated 7%)', () => {
    expect(core.detectActivePreset(mk(0.10, 5, 0.07))).toBe('moderate');
  });
  it('aggressive', () => {
    expect(core.detectActivePreset(mk(0.05, 10, 0.20))).toBe('aggressive');
  });
  it('custom fallback', () => {
    expect(core.detectActivePreset(mk(0.08, 4, 0.10))).toBe('custom');
  });
  it('empty → moderate defaults', () => {
    expect(core.detectActivePreset({})).toBe('moderate');
  });
});

describe('_sanitizeReadmeHtml', () => {
  it('preserves allowed structural tags', () => {
    const out = core._sanitizeReadmeHtml('<h1>A</h1><p><strong>B</strong></p>');
    expect(out).toContain('<h1>A</h1>');
    expect(out).toContain('<strong>B</strong>');
  });
  it('strips <script>, <iframe>, <style>', () => {
    expect(core._sanitizeReadmeHtml('<script>x</script>')).not.toContain('<script');
    expect(core._sanitizeReadmeHtml('<iframe></iframe>')).not.toContain('<iframe');
    expect(core._sanitizeReadmeHtml('<style></style>')).not.toContain('<style');
  });
  it('strips onclick/onerror', () => {
    expect(core._sanitizeReadmeHtml('<a onclick="x" href="#">a</a>')).not.toContain('onclick');
    expect(core._sanitizeReadmeHtml('<img onerror="x" src="y">')).not.toContain('onerror');
  });
  it('defangs javascript:/data:/vbscript: URIs', () => {
    expect(core._sanitizeReadmeHtml('<a href="javascript:x">a</a>')).not.toContain('javascript:');
    expect(core._sanitizeReadmeHtml('<a href="DATA:text/html,x">a</a>')).not.toMatch(/data:/i);
    expect(core._sanitizeReadmeHtml('<a href="vbscript:x">a</a>')).not.toContain('vbscript:');
  });
  it('preserves id + class globally', () => {
    const out = core._sanitizeReadmeHtml('<p class="c" id="i">x</p>');
    expect(out).toContain('class="c"');
    expect(out).toContain('id="i"');
  });
});
