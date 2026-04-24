// Round-61 pt.8 batch-5 — openCancelOrderModal rendering tests.
//
// Pin: a Market order renders as "Market", a Limit order renders the price.
// Buy side explains "No money is spent"; sell side explains "You will keep
// holding your position". The Confirm button is wired to executeCancelOrder.

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
  api.openCancelOrderModal = globalThis.openCancelOrderModal;
});

function seed() {
  document.body.innerHTML = `
    <div id="toastContainer"></div>
    <div id="app"></div>
    <div id="logPanel"></div>
    <div id="cancelOrderModal" class="modal">
      <div id="cancelOrderSubtitle"></div>
      <div id="cancelOrderDetails"></div>
      <div id="cancelInfoContent"></div>
      <button id="cancelOrderConfirm">Cancel</button>
    </div>
  `;
}

describe('openCancelOrderModal — order details rendering', () => {
  beforeEach(seed);

  it('market buy shows "Market" in the price column', () => {
    api.openCancelOrderModal('abc', 'AAPL', 'buy', 'market', 10, 'Market');
    const details = document.getElementById('cancelOrderDetails').textContent;
    expect(details).toContain('BUY 10 shares of AAPL');
    expect(details).toContain('market @ Market');
  });

  it('limit sell shows the price via fmtMoney', () => {
    api.openCancelOrderModal('id2', 'AAPL', 'sell', 'limit', 5, 150.25);
    const details = document.getElementById('cancelOrderDetails').textContent;
    expect(details).toContain('SELL 5 shares of AAPL');
    expect(details).toContain('limit @ $150.25');
  });

  it('singular share shows "share" not "shares"', () => {
    api.openCancelOrderModal('id3', 'AAPL', 'buy', 'market', 1, 'Market');
    const details = document.getElementById('cancelOrderDetails').textContent;
    // qty === 1 → singular
    expect(details).toContain('1 share of AAPL');
    expect(details).not.toContain('1 shares');
  });
});

describe('openCancelOrderModal — buy vs sell explanation', () => {
  beforeEach(seed);

  it('buy side explains no money will be spent', () => {
    api.openCancelOrderModal('id', 'AAPL', 'buy', 'limit', 10, 100);
    const info = document.getElementById('cancelInfoContent').innerHTML;
    expect(info).toContain('No money is spent');
    expect(info).toContain('will be cancelled');
    expect(info).toContain('will <strong>NOT</strong> buy');
  });

  it('sell side explains the position will be kept', () => {
    api.openCancelOrderModal('id', 'AAPL', 'sell', 'limit', 10, 100);
    const info = document.getElementById('cancelInfoContent').innerHTML;
    expect(info).toContain('will <strong>NOT</strong> be sold');
    expect(info).toContain('keep holding');
  });
});

describe('openCancelOrderModal — Confirm button wiring', () => {
  beforeEach(seed);

  it('onclick handler is assigned', () => {
    api.openCancelOrderModal('id', 'AAPL', 'buy', 'market', 1, 'Market');
    const btn = document.getElementById('cancelOrderConfirm');
    expect(typeof btn.onclick).toBe('function');
  });
});
