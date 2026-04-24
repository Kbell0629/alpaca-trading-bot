// Round-61 pt.8 batch-6 — openKillSwitchModal tests.
//
// The kill-switch modal is the user's emergency stop. Pt.8 scope: verify the
// modal opens cleanly when triggered. The async execute path is tested on
// the backend via http_harness.

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
  api.openKillSwitchModal = globalThis.openKillSwitchModal;
});

function seed() {
  document.body.innerHTML = `
    <div id="toastContainer"></div>
    <div id="app"></div>
    <div id="logPanel"></div>
    <div id="killSwitchModal" class="modal"></div>
    <div id="killSwitchResultsModal" class="modal">
      <div id="killSwitchResults"></div>
    </div>
  `;
}

describe('openKillSwitchModal', () => {
  beforeEach(seed);

  it('opens the kill-switch confirmation modal', () => {
    api.openKillSwitchModal();
    expect(document.getElementById('killSwitchModal').classList.contains('active')).toBe(true);
  });

  it('is idempotent (second call stays open)', () => {
    api.openKillSwitchModal();
    api.openKillSwitchModal();
    expect(document.getElementById('killSwitchModal').classList.contains('active')).toBe(true);
  });
});
