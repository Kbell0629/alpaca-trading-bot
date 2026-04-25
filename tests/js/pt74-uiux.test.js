// Round-61 pt.74 — UI/UX "Pro" polish helpers.
//
// Vitest coverage for the new pt74RenderSkeleton, pt74FormatFreshness,
// pt74RenderFreshnessChip, pt74RenderRiskBadge, pt74WirePasswordToggles
// helpers + toggleFocusMode (body class + localStorage).

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  // Provide a benign showToast stub so toggleFocusMode doesn't crash.
  globalThis.showToast = () => {};
  api = loadDashboardJs();
});

describe('pt74RenderSkeleton', () => {
  it('renders rows-only by default', () => {
    const html = api.pt74RenderSkeleton();
    expect(html).toContain('pt74-skeleton-line');
    expect(html).not.toContain('pt74-skeleton-grid');
  });

  it('renders cards when cards > 0', () => {
    const html = api.pt74RenderSkeleton({cards: 4, rows: 0});
    expect(html).toContain('pt74-skeleton-grid');
    // 4 cards × 3 lines + grid overhead
    expect((html.match(/pt74-skeleton-card/g) || []).length).toBe(4);
  });

  it('renders both rows and cards', () => {
    const html = api.pt74RenderSkeleton({cards: 2, rows: 3});
    expect(html).toContain('pt74-skeleton-grid');
    expect(html).toContain('pt74-skeleton-line');
  });

  it('handles missing opts gracefully', () => {
    const html = api.pt74RenderSkeleton(undefined);
    expect(typeof html).toBe('string');
    expect(html.length).toBeGreaterThan(0);
  });
});

describe('pt74FormatFreshness', () => {
  it('returns "fetch failed" on error state', () => {
    const f = api.pt74FormatFreshness(Date.now(), true);
    expect(f.state).toBe('error');
    expect(f.text).toContain('fetch failed');
  });

  it('returns "never updated" with no timestamp', () => {
    const f = api.pt74FormatFreshness(null, false);
    expect(f.state).toBe('stale');
    expect(f.text).toContain('never updated');
  });

  it('returns "just now" within 60s', () => {
    const f = api.pt74FormatFreshness(Date.now() - 5_000, false);
    expect(f.state).toBe('ok');
    expect(f.text.toLowerCase()).toContain('just now');
  });

  it('returns seconds-ago between 60-300s', () => {
    const f = api.pt74FormatFreshness(Date.now() - 120_000, false);
    expect(f.state).toBe('ok');
    expect(f.text).toContain('s ago');
  });

  it('returns minutes-ago between 5min-1hr', () => {
    const f = api.pt74FormatFreshness(Date.now() - 600_000, false);
    expect(f.state).toBe('stale');
    expect(f.text).toContain('m ago');
  });

  it('returns hours-ago beyond 1hr', () => {
    const f = api.pt74FormatFreshness(Date.now() - 7200_000, false);
    expect(f.state).toBe('stale');
    expect(f.text).toContain('h ago');
  });
});

describe('pt74RenderFreshnessChip', () => {
  it('renders with the dot + chip class', () => {
    const html = api.pt74RenderFreshnessChip(Date.now(), false);
    expect(html).toContain('pt74-fresh-chip');
    expect(html).toContain('class="dot"');
  });

  it('error state adds the error class', () => {
    const html = api.pt74RenderFreshnessChip(null, true);
    expect(html).toContain('error');
  });

  it('stale state adds the stale class', () => {
    const html = api.pt74RenderFreshnessChip(Date.now() - 600_000, false);
    expect(html).toContain('stale');
  });
});

describe('pt74RenderRiskBadge', () => {
  it('paper by default', () => {
    const html = api.pt74RenderRiskBadge();
    expect(html).toContain('PAPER');
    expect(html).not.toContain('class="pt74-risk-badge live"');
  });

  it('live mode adds the live class', () => {
    const html = api.pt74RenderRiskBadge({live: true});
    expect(html).toContain('LIVE');
    expect(html).toContain('pt74-risk-badge live');
  });

  it('detail string is appended', () => {
    const html = api.pt74RenderRiskBadge({live: false, detail: 'max -$300/day'});
    expect(html).toContain('max -$300/day');
  });

  it('custom label respected', () => {
    const html = api.pt74RenderRiskBadge({label: 'CASH'});
    expect(html).toContain('CASH');
  });
});

describe('toggleFocusMode', () => {
  beforeEach(() => {
    document.body.className = '';
    try { localStorage.removeItem('pt74_focusMode'); } catch (_) {}
  });

  it('toggles body class and persists', () => {
    api.toggleFocusMode();
    expect(document.body.classList.contains('focus-mode')).toBe(true);
    expect(localStorage.getItem('pt74_focusMode')).toBe('1');
  });

  it('toggles back off', () => {
    api.toggleFocusMode();
    api.toggleFocusMode();
    expect(document.body.classList.contains('focus-mode')).toBe(false);
    expect(localStorage.getItem('pt74_focusMode')).toBe('0');
  });

  it('updates aria-pressed on the pill', () => {
    document.body.innerHTML = '<button id="focusModePill" aria-pressed="false"></button>';
    api.toggleFocusMode();
    const pill = document.getElementById('focusModePill');
    expect(pill.getAttribute('aria-pressed')).toBe('true');
    api.toggleFocusMode();
    expect(pill.getAttribute('aria-pressed')).toBe('false');
  });
});

describe('pt74WirePasswordToggles', () => {
  it('flips an input from password to text on click', () => {
    document.body.innerHTML =
      '<div class="pt74-pw-wrapper">' +
        '<input id="pw" type="password" value="secret">' +
        '<button class="pt74-pw-toggle" type="button">Show</button>' +
      '</div>';
    api.pt74WirePasswordToggles();
    const btn = document.querySelector('.pt74-pw-toggle');
    btn.click();
    expect(document.getElementById('pw').type).toBe('text');
    expect(btn.textContent).toBe('Hide');
    btn.click();
    expect(document.getElementById('pw').type).toBe('password');
    expect(btn.textContent).toBe('Show');
  });

  it('idempotent — wiring twice does not double-fire', () => {
    document.body.innerHTML =
      '<div class="pt74-pw-wrapper">' +
        '<input id="pw" type="password">' +
        '<button class="pt74-pw-toggle" type="button">Show</button>' +
      '</div>';
    api.pt74WirePasswordToggles();
    api.pt74WirePasswordToggles();
    document.querySelector('.pt74-pw-toggle').click();
    // After ONE click, the type should be 'text'. If wiring duplicated,
    // the click would fire two handlers and the type would flip twice
    // back to 'password'.
    expect(document.getElementById('pw').type).toBe('text');
  });

  it('handles missing wrapper gracefully', () => {
    document.body.innerHTML = '<button class="pt74-pw-toggle" type="button">Show</button>';
    expect(() => api.pt74WirePasswordToggles()).not.toThrow();
  });
});
