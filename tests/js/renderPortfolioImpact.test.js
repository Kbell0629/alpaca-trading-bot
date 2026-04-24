// Round-61 pt.8 batch-7 — renderPortfolioImpact early-exit paths.
//
// The full function reads module-local `dashboardData` which can't be
// injected from outside the vm context (it's a `let` in dashboard.html's
// script scope). We CAN exercise the early-exit paths that bail out
// before reading dashboardData's inner state.

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

function seedTarget() {
  document.body.innerHTML = `
    <div id="toastContainer"></div>
    <div id="app"></div>
    <div id="logPanel"></div>
    <div id="deployPortfolioImpact">pre-existing content</div>
  `;
}

describe('renderPortfolioImpact — early exits', () => {
  beforeEach(seedTarget);

  it('no #deployPortfolioImpact element → silent no-op', () => {
    document.body.innerHTML = '<div id="toastContainer"></div>';
    expect(() => api.renderPortfolioImpact('AAPL', 'breakout', 10, 150, 1500, 150))
      .not.toThrow();
  });

  it('null dashboardData → target cleared', () => {
    // dashboardData defaults to null in the script. That means any call
    // triggers the null-guard branch and empties target.innerHTML.
    api.renderPortfolioImpact('AAPL', 'breakout', 10, 150, 1500, 150);
    const el = document.getElementById('deployPortfolioImpact');
    expect(el.innerHTML).toBe('');
  });
});
