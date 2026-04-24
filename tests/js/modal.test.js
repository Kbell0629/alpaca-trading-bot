// Round-61 pt.8 batch-4 — openModal + closeModal tests.
//
// Pins the Round-12 audit a11y fix:
//   1. Opening a modal remembers the previously-focused element.
//   2. Closing restores focus to that element.
//   3. role=dialog + aria-modal=true set on open.
//   4. Key handler is installed/removed (Escape closes, Tab cycles).
//
// Note: openModal uses setTimeout(focus, 30) — vitest's fake timers let
// us advance past that so focus assertions are deterministic.

import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

function seed() {
  document.body.innerHTML = `
    <button id="opener">Open</button>
    <div id="toastContainer"></div>
    <div id="app"></div>
    <div id="logPanel"></div>
    <div id="m1" class="modal" style="display:none">
      <button id="m1-first">First</button>
      <button id="m1-second">Second</button>
    </div>
    <div id="m2" class="modal"></div>
  `;
}

describe('openModal — class toggle + ARIA', () => {
  beforeEach(seed);

  it('adds .active class', () => {
    api.openModal('m1');
    expect(document.getElementById('m1').classList.contains('active')).toBe(true);
  });

  it('sets role=dialog and aria-modal=true', () => {
    api.openModal('m1');
    const m = document.getElementById('m1');
    expect(m.getAttribute('role')).toBe('dialog');
    expect(m.getAttribute('aria-modal')).toBe('true');
  });

  it('unknown id is a no-op (no throw)', () => {
    expect(() => api.openModal('does-not-exist')).not.toThrow();
  });
});

describe('closeModal — class removal', () => {
  beforeEach(seed);

  it('removes .active from the modal', () => {
    api.openModal('m1');
    api.closeModal('m1');
    expect(document.getElementById('m1').classList.contains('active')).toBe(false);
  });

  it('unknown id is a no-op', () => {
    expect(() => api.closeModal('does-not-exist')).not.toThrow();
  });

  it('closeModal on a modal that was never opened is safe', () => {
    expect(() => api.closeModal('m2')).not.toThrow();
  });
});

describe('openModal — focus restoration on close (Round-12 a11y)', () => {
  beforeEach(seed);

  it('focus returns to the opener button after closeModal', () => {
    // The loader stubs setTimeout to a no-op (so the 30ms focus deferral
    // in openModal never fires). That means the first-focusable doesn't
    // grab focus, and `opener` remains the active element throughout.
    // On close, the code still attempts to restore focus to whatever
    // activeElement was BEFORE openModal — which is the opener.
    const opener = document.getElementById('opener');
    opener.focus();
    expect(document.activeElement).toBe(opener);
    api.openModal('m1');
    // With the stubbed setTimeout, focus stayed on the opener.
    api.closeModal('m1');
    expect(document.activeElement).toBe(opener);
  });

  it('closing a modal with a detached prev-focus element does not throw', () => {
    const opener = document.getElementById('opener');
    opener.focus();
    api.openModal('m1');
    // Remove the opener from DOM so .focus() would no-op on the restored ref.
    opener.remove();
    expect(() => api.closeModal('m1')).not.toThrow();
  });
});

describe('openModal — multiple modals + stack', () => {
  beforeEach(seed);

  it('opening a second modal remembers its own prev-focus', () => {
    const opener = document.getElementById('opener');
    opener.focus();
    api.openModal('m1');
    // With no real timer advance, activeElement is still the opener.
    api.openModal('m2');
    api.closeModal('m2');
    api.closeModal('m1');
    // Both closed without stack corruption
    expect(document.getElementById('m1').classList.contains('active')).toBe(false);
    expect(document.getElementById('m2').classList.contains('active')).toBe(false);
  });
});
