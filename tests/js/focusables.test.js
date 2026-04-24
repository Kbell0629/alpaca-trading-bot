// Round-61 pt.8 batch-2 — _focusablesIn tests.
//
// Powers keyboard-accessibility on every modal: openModal() uses this to
// pick the first focusable element on open, and the modal key handler
// uses it to implement Tab-wraparound (last-to-first and first-to-last).
// Regression here = keyboard users get trapped or lose focus.

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('_focusablesIn — basic inclusion', () => {
  let root;
  beforeEach(() => {
    root = document.createElement('div');
    document.body.appendChild(root);
  });

  it('includes enabled buttons', () => {
    root.innerHTML = '<button>A</button><button>B</button>';
    const found = api._focusablesIn(root);
    expect(found.length).toBe(2);
  });

  it('includes anchors with href', () => {
    root.innerHTML = '<a href="#x">link</a>';
    const found = api._focusablesIn(root);
    expect(found.length).toBe(1);
  });

  it('includes enabled inputs (not hidden, not type=hidden)', () => {
    root.innerHTML = '<input type="text"><input type="hidden"><input type="checkbox">';
    const found = api._focusablesIn(root);
    // text + checkbox, not the hidden
    expect(found.length).toBe(2);
  });

  it('includes tabindex >= 0 elements', () => {
    root.innerHTML = '<div tabindex="0">focusable</div><div tabindex="-1">not</div>';
    const found = api._focusablesIn(root);
    expect(found.length).toBe(1);
    expect(found[0].tabIndex).toBe(0);
  });
});

describe('_focusablesIn — exclusions', () => {
  let root;
  beforeEach(() => {
    root = document.createElement('div');
    document.body.appendChild(root);
  });

  it('disabled button NOT included', () => {
    root.innerHTML = '<button disabled>A</button><button>B</button>';
    const found = api._focusablesIn(root);
    expect(found.length).toBe(1);
    expect(found[0].textContent).toBe('B');
  });

  it('disabled input NOT included', () => {
    root.innerHTML = '<input type="text" disabled><input type="text">';
    const found = api._focusablesIn(root);
    expect(found.length).toBe(1);
  });

  it('anchor without href NOT included', () => {
    root.innerHTML = '<a>no href</a><a href="#x">with href</a>';
    const found = api._focusablesIn(root);
    expect(found.length).toBe(1);
  });

  it('tabindex=-1 NOT included (explicitly unfocusable)', () => {
    root.innerHTML = '<div tabindex="-1">no</div>';
    const found = api._focusablesIn(root);
    expect(found.length).toBe(0);
  });
});

describe('_focusablesIn — preserves DOM order', () => {
  it('returns elements in document order (important for Tab cycling)', () => {
    const root = document.createElement('div');
    root.innerHTML = `
      <button>First</button>
      <input type="text">
      <a href="#x">Link</a>
      <button>Last</button>
    `;
    document.body.appendChild(root);
    const found = api._focusablesIn(root);
    expect(found.length).toBe(4);
    expect(found[0].textContent).toBe('First');
    expect(found[found.length - 1].textContent).toBe('Last');
  });
});

describe('_focusablesIn — nested elements', () => {
  it('finds focusables inside nested containers', () => {
    const root = document.createElement('div');
    root.innerHTML = `
      <div class="outer">
        <div class="inner">
          <button>deep</button>
        </div>
      </div>
    `;
    document.body.appendChild(root);
    const found = api._focusablesIn(root);
    expect(found.length).toBe(1);
    expect(found[0].textContent).toBe('deep');
  });
});
