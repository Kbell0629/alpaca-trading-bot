// Round-61 pt.8 batch-3 — scrollToSection tests.
//
// Powers the nav-bar tab clicks. Two invariants:
//   * Active-tab class is set on the clicked tab after scrollToSection runs
//     (so renderDashboard's next refresh doesn't reset the highlight to
//     Overview — Round-53 fix).
//   * window._activeNavSection is persisted so renderDashboard can restore
//     the highlight on refresh.

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

/** Seed a fake nav-bar + target section in the DOM so scrollToSection can
 * find its anchors. */
function seedDom() {
  document.body.innerHTML = `
    <div class="header-v2" style="height:60px">Header</div>
    <div class="nav-tabs">
      <div class="nav-tab" onclick="scrollToSection('section-overview')">Overview</div>
      <div class="nav-tab" onclick="scrollToSection('section-positions')">Positions</div>
      <div class="nav-tab" onclick="scrollToSection('section-picks')">Picks</div>
    </div>
    <div id="section-overview">overview content</div>
    <div id="section-positions">positions content</div>
    <div id="section-picks">picks content</div>
    <div id="toastContainer"></div>
    <div id="app"></div>
  `;
  // jsdom doesn't implement window.scrollTo — stub it so scrollToSection
  // doesn't throw when it calls window.scrollTo(...).
  window.scrollTo = () => {};
}

describe('scrollToSection — active-tab highlight (Round-53)', () => {
  beforeEach(seedDom);

  it('sets `.active` on the tab whose onclick targets the id', () => {
    api.scrollToSection('section-positions');
    const tabs = document.querySelectorAll('.nav-tab');
    expect(tabs[0].classList.contains('active')).toBe(false);   // Overview
    expect(tabs[1].classList.contains('active')).toBe(true);    // Positions
    expect(tabs[2].classList.contains('active')).toBe(false);   // Picks
  });

  it('clears active class from previously-active tabs before setting new', () => {
    // First click — activate Overview
    api.scrollToSection('section-overview');
    expect(document.querySelectorAll('.nav-tab')[0].classList.contains('active')).toBe(true);
    // Second click — activate Picks. Overview should lose active.
    api.scrollToSection('section-picks');
    const tabs = document.querySelectorAll('.nav-tab');
    expect(tabs[0].classList.contains('active')).toBe(false);
    expect(tabs[2].classList.contains('active')).toBe(true);
  });
});

describe('scrollToSection — _activeNavSection persistence', () => {
  beforeEach(seedDom);

  it('stores the target id on window so renderDashboard can restore it', () => {
    api.scrollToSection('section-picks');
    expect(window._activeNavSection).toBe('section-picks');
  });

  it('overwrites previous _activeNavSection on subsequent calls', () => {
    api.scrollToSection('section-overview');
    expect(window._activeNavSection).toBe('section-overview');
    api.scrollToSection('section-positions');
    expect(window._activeNavSection).toBe('section-positions');
  });
});

describe('scrollToSection — no-op on missing target', () => {
  beforeEach(seedDom);

  it('unknown section id is a no-op (no throw)', () => {
    expect(() => api.scrollToSection('section-does-not-exist')).not.toThrow();
    // Prior state not touched
    const tabs = document.querySelectorAll('.nav-tab');
    // (No tab should have been made active — we started clean)
    expect([...tabs].some(t => t.classList.contains('active'))).toBe(false);
  });
});
