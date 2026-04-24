// Round-61 pt.8 batch-5 — sell-fraction modal math.
//
// openSellFractionModal is used by Sell 25% / Sell 50% (and open for future
// Sell 75%) UX. Pins the share-count math (floor + min-1 guard) and the
// pro-rata P&L math on the sold fraction.

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
  api.openSellFractionModal = globalThis.openSellFractionModal;
  api.openSellHalfModal = globalThis.openSellHalfModal;
});

function seed() {
  document.body.innerHTML = `
    <div id="toastContainer"></div>
    <div id="app"></div>
    <div id="logPanel"></div>
    <div id="sellHalfModal" class="modal">
      <div id="sellHalfTitle"></div>
      <div id="sellHalfSubtitle"></div>
      <div id="sellHalfDetails"></div>
      <div id="sellHalfInfoContent"></div>
      <button id="sellHalfConfirm">Confirm</button>
    </div>
  `;
}

describe('openSellFractionModal — share-count math', () => {
  beforeEach(seed);

  it('50% of 20 shares → sell 10, keep 10', () => {
    api.openSellFractionModal('AAPL', 20, 100, 110, 200, 10, 0.5);
    const title = document.getElementById('sellHalfTitle').textContent;
    const details = document.getElementById('sellHalfDetails').textContent;
    expect(title).toBe('Sell 50% of AAPL');
    expect(details).toContain('10 of 20 shares');
    expect(details).toContain('Keeping');
    expect(details).toContain('10 shares');
  });

  it('25% of 19 shares → sell 4 (floored), keep 15', () => {
    api.openSellFractionModal('AAPL', 19, 100, 110, 200, 10, 0.25);
    const details = document.getElementById('sellHalfDetails').textContent;
    // 19 * 0.25 = 4.75 → floor 4
    expect(details).toContain('4 of 19 shares');
  });

  it('tiny fraction rounds UP to the 1-share minimum', () => {
    // 3 shares × 0.1 = 0.3 → Math.floor → 0 → clamped to 1
    api.openSellFractionModal('X', 3, 10, 12, 6, 20, 0.1);
    const details = document.getElementById('sellHalfDetails').textContent;
    expect(details).toContain('1 of 3 shares');
  });
});

describe('openSellFractionModal — pro-rata P&L math', () => {
  beforeEach(seed);

  it('P&L scales with fraction (10 of 20 shares → half the P&L)', () => {
    // Total P&L = $200 on 20 shares; selling 10 → $100 realized
    api.openSellFractionModal('AAPL', 20, 100, 110, 200, 10, 0.5);
    const info = document.getElementById('sellHalfInfoContent').innerHTML;
    expect(info).toContain('$100.00');
    expect(info).toContain('+10.0%');
  });

  it('negative P&L carries sign + red colour', () => {
    api.openSellFractionModal('BUST', 10, 100, 80, -200, -20, 0.5);
    const info = document.getElementById('sellHalfInfoContent').innerHTML;
    expect(info).toContain('-20.0%');
    expect(info).toContain('var(--red)');
    // -200 P&L × 5/10 = -100
    expect(info).toContain('$-100.00');
  });
});

describe('openSellHalfModal → openSellFractionModal(0.5)', () => {
  beforeEach(seed);

  it('half-specific wrapper delegates to 50% fraction path', () => {
    api.openSellHalfModal('AAPL', 20, 100, 110, 200, 10);
    const title = document.getElementById('sellHalfTitle').textContent;
    expect(title).toBe('Sell 50% of AAPL');
  });
});

describe('openSellFractionModal — Confirm button wiring', () => {
  beforeEach(seed);

  it('onclick handler assigned', () => {
    api.openSellFractionModal('X', 10, 100, 110, 100, 10, 0.5);
    const btn = document.getElementById('sellHalfConfirm');
    expect(typeof btn.onclick).toBe('function');
  });
});
