// Round-61 pt.8 batch-4 — openClosePositionModal wheel math + equity path.
//
// High-value target: this modal shows the user what will happen when they
// click Confirm on a close-position action. Getting the math wrong leads
// the user to think they're selling a covered call (capped-upside, safe)
// when they're actually selling a naked call (unlimited loss) — or vice
// versa. Round-27 introduced the wheel-aware breakdown; we pin its math.
//
// Tests verify the DOM side-effects of calling the function (it renders
// into #closeModalTitle, #closeModalSubtitle, #closeModalDetails,
// #closeInfoContent). We don't intercept executeClosePosition — the
// Confirm button is assigned a handler but we never click it.

import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  // Expose openClosePositionModal before first load since it isn't on
  // the default api list. We attach directly via the vm context.
  api = loadDashboardJs();
  // openClosePositionModal isn't in the loader's default exposed list.
  // Grab it from globalThis where vm.runInThisContext left it.
  api.openClosePositionModal = globalThis.openClosePositionModal;
});

/** Scaffold the #closeModal modal + the helper nodes the function writes
 * into. Also ensure openModal() has something to toggle. */
function seedModalDom() {
  document.body.innerHTML = `
    <div id="toastContainer"></div>
    <div id="app"></div>
    <div id="logPanel"></div>
    <div id="closeModal" class="modal" role="dialog">
      <h2 id="closeModalTitle"></h2>
      <div id="closeModalSubtitle"></div>
      <div id="closeModalDetails"></div>
      <div id="closeInfoContent"></div>
      <button id="closeModalConfirm">Confirm</button>
    </div>
  `;
}

describe('openClosePositionModal — short put (wheel sold put)', () => {
  beforeEach(seedModalDom);

  it('HIMS 2026-05-08 $27 short put — title + action', () => {
    // Premium $2.05 per share (mark $2.05 × 100 = $205 collected)
    // Currently worth $1.62 — P&L = 205 - 162 = +$43 gain
    // Use fake timer so DTE is stable
    vi.useFakeTimers();
    vi.setSystemTime(new Date(2026, 4, 1)); // May 1, 2026
    try {
      api.openClosePositionModal(
        'HIMS260508P00027000', -1, 2.05, 1.62, 0, 0);
    } finally {
      vi.useRealTimers();
    }
    const title = document.getElementById('closeModalTitle').textContent;
    const subtitle = document.getElementById('closeModalSubtitle').textContent;
    expect(title).toBe('BUY-TO-CLOSE HIMS260508P00027000');
    expect(subtitle).toContain('Short 1 put');
    expect(subtitle).toContain('HIMS');
    expect(subtitle).toContain('$27.00');
    expect(subtitle).toContain('May 8, 2026');
  });

  it('short put shows premium collected, cost to close, breakeven', () => {
    api.openClosePositionModal('HIMS260508P00027000', -1, 2.05, 1.62, 0, 0);
    const details = document.getElementById('closeModalDetails').textContent;
    // Premium collected = 2.05 × 100 = $205.00
    expect(details).toContain('$205.00');
    // Cost to close = 1.62 × 100 = $162.00
    expect(details).toContain('$162.00');
    // Breakeven = strike - premium = 27 - 2.05 = 24.95
    expect(details).toContain('$24.95');
    // Max profit = premium = $205.00 (already asserted above)
    // Max loss = strike×100 - premium = 2700 - 205 = $2,495.00
    expect(details).toContain('$2,495.00');
  });

  it('short put realised P&L is positive when mark drops below entry', () => {
    api.openClosePositionModal('HIMS260508P00027000', -1, 2.05, 1.62, 0, 0);
    const info = document.getElementById('closeInfoContent').innerHTML;
    // premium 205 - 162 = +43
    expect(info).toContain('$43.00');
    expect(info).toContain('gain');
  });

  it('short put assignment note references strike + expiry + share count', () => {
    api.openClosePositionModal('HIMS260508P00027000', -1, 2.05, 1.62, 0, 0);
    const info = document.getElementById('closeInfoContent').innerHTML;
    // If HIMS closes BELOW $27.00 on May 8, 2026, you're assigned 100 shares
    expect(info).toContain('below');
    expect(info).toContain('$27.00');
    expect(info).toContain('100 shares');
    // Cash outlay = 27 × 100 = $2,700.00
    expect(info).toContain('$2,700.00');
  });
});

describe('openClosePositionModal — short call (covered call)', () => {
  beforeEach(seedModalDom);

  it('SOXL short call — breakeven = strike + premium', () => {
    // Sold a covered call: premium $2.00, strike $115
    api.openClosePositionModal('SOXL260515C00115000', -1, 2.00, 1.50, 0, 0);
    const details = document.getElementById('closeModalDetails').textContent;
    // Breakeven = 115 + 2 = 117
    expect(details).toContain('$117.00');
    // Premium collected = 2 × 100 = $200
    expect(details).toContain('$200.00');
  });

  it('short call max loss labelled "Unlimited (naked call)"', () => {
    api.openClosePositionModal('SOXL260515C00115000', -1, 2.00, 1.50, 0, 0);
    const details = document.getElementById('closeModalDetails').innerHTML;
    expect(details).toContain('Unlimited (naked call)');
  });

  it('short call assignment note uses "above" for the strike trigger', () => {
    api.openClosePositionModal('SOXL260515C00115000', -1, 2.00, 1.50, 0, 0);
    const info = document.getElementById('closeInfoContent').innerHTML;
    expect(info).toContain('above');
    expect(info).toContain('called away');
  });
});

describe('openClosePositionModal — long option', () => {
  beforeEach(seedModalDom);

  it('long call — max loss = premium paid; max profit = null (—)', () => {
    // Bought call: premium paid $3, strike $100
    api.openClosePositionModal('AAPL260620C00100000', 1, 3.00, 3.50, 0, 0);
    const title = document.getElementById('closeModalTitle').textContent;
    expect(title).toBe('SELL-TO-CLOSE AAPL260620C00100000');
    const details = document.getElementById('closeModalDetails').textContent;
    // Breakeven = strike + premium = 100 + 3 = 103
    expect(details).toContain('$103.00');
    // Max loss = premium paid = $300
    expect(details).toContain('$300.00');
  });

  it('long put — breakeven = strike - premium', () => {
    api.openClosePositionModal('AAPL260620P00100000', 1, 3.00, 3.50, 0, 0);
    const details = document.getElementById('closeModalDetails').textContent;
    // Breakeven = 100 - 3 = 97
    expect(details).toContain('$97.00');
  });
});

describe('openClosePositionModal — equity path', () => {
  beforeEach(seedModalDom);

  it('equity position uses shares × price breakdown, no wheel math', () => {
    // Long 18 shares AAPL bought at $150, current $160
    api.openClosePositionModal('AAPL', 18, 150, 160, 180, 6.67);
    const title = document.getElementById('closeModalTitle').textContent;
    expect(title).toBe('Close AAPL Position');
    const details = document.getElementById('closeModalDetails').textContent;
    // Shares
    expect(details).toContain('18 shares of AAPL');
    // Cost basis = 150 × 18 = $2,700
    expect(details).toContain('$2,700.00');
    // Current value = 160 × 18 = $2,880
    expect(details).toContain('$2,880.00');
  });

  it('equity P&L uses fmtPct for the percentage', () => {
    api.openClosePositionModal('AAPL', 18, 150, 160, 180, 6.67);
    const info = document.getElementById('closeInfoContent').innerHTML;
    // fmtPct(6.67) → "+6.7%"
    expect(info).toContain('+6.7%');
    expect(info).toContain('$180.00');
    expect(info).toContain('gain');
  });

  it('equity negative P&L renders in red with "loss" label', () => {
    api.openClosePositionModal('BUST', 10, 100, 80, -200, -20);
    const info = document.getElementById('closeInfoContent').innerHTML;
    expect(info).toContain('var(--red)');
    expect(info).toContain('loss');
    expect(info).toContain('-20.0%');
  });
});

describe('openClosePositionModal — confirm button handler', () => {
  beforeEach(seedModalDom);

  it('sets onclick on #closeModalConfirm', () => {
    api.openClosePositionModal('AAPL', 10, 150, 160, 100, 5);
    const btn = document.getElementById('closeModalConfirm');
    expect(typeof btn.onclick).toBe('function');
  });

  it('options path also wires the confirm button', () => {
    api.openClosePositionModal('HIMS260508P00027000', -1, 2.05, 1.62, 0, 0);
    const btn = document.getElementById('closeModalConfirm');
    expect(typeof btn.onclick).toBe('function');
  });
});

describe('openClosePositionModal — multiple contracts multiplier', () => {
  beforeEach(seedModalDom);

  it('-2 contracts doubles the premium collected + max loss', () => {
    // 2 short puts at $2.05 premium each, strike $27
    api.openClosePositionModal('HIMS260508P00027000', -2, 2.05, 1.62, 0, 0);
    const details = document.getElementById('closeModalDetails').textContent;
    // Premium = 2.05 × 100 × 2 = $410
    expect(details).toContain('$410.00');
    // Max loss = (27 × 100 × 2) - 410 = 5400 - 410 = $4,990
    expect(details).toContain('$4,990.00');
    // Subtitle "2 put" (plural)
    const subtitle = document.getElementById('closeModalSubtitle').textContent;
    expect(subtitle).toContain('Short 2 put');
  });
});
