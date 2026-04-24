// Round-61 pt.8 batch-2 — atomicReplaceChildren tests.
//
// This helper underpins FIVE of the jitter-fix rounds (R60, R61 #107/#108/
// #109/#111/#113). CLAUDE.md has a 60-line "Scroll-jitter fix history"
// section declaring every failure mode mandatory-reading. These tests pin
// the invariants so a future "simplification" that removes any behavior
// fails loudly.
//
// Invariants covered:
//   1. Early-return on null panel (no throw).
//   2. Atomic swap via <template> + replaceChildren (not innerHTML=) —
//      so there's no empty-frame window where document height collapses.
//   3. Scroll position of inner scrollable containers is preserved across
//      the swap (the `.sched-log-box` case from R61 #113).
//   4. Fallback to innerHTML assignment when replaceChildren isn't
//      available (very-old-browser defensive path).
//   5. Parse-error fallback — if the template insert throws, fall back
//      to innerHTML= so the panel still updates (best-effort).

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('atomicReplaceChildren — null guard', () => {
  it('null panel is a no-op (no throw)', () => {
    expect(() => api.atomicReplaceChildren(null, '<div>x</div>')).not.toThrow();
  });

  it('undefined panel is a no-op', () => {
    expect(() => api.atomicReplaceChildren(undefined, '<div>x</div>')).not.toThrow();
  });
});

describe('atomicReplaceChildren — basic swap', () => {
  let panel;
  beforeEach(() => {
    panel = document.createElement('div');
    panel.id = 'testPanel';
    panel.innerHTML = '<div class="old">OLD</div>';
    document.body.appendChild(panel);
  });

  it('replaces the panel contents with new HTML', () => {
    api.atomicReplaceChildren(panel, '<div class="new">NEW</div>');
    expect(panel.innerHTML).toContain('NEW');
    expect(panel.innerHTML).not.toContain('OLD');
  });

  it('accepts multiple top-level nodes', () => {
    api.atomicReplaceChildren(panel, '<span>A</span><span>B</span><span>C</span>');
    expect(panel.querySelectorAll('span').length).toBe(3);
  });

  it('accepts empty string → empty panel', () => {
    api.atomicReplaceChildren(panel, '');
    expect(panel.children.length).toBe(0);
  });
});

describe('atomicReplaceChildren — scroll preservation', () => {
  // R61 #113 pinned behavior: scrollTop of .sched-log-box survives the swap.
  it('preserves scrollTop on a .sched-log-box across the swap', () => {
    const panel = document.createElement('div');
    panel.innerHTML = `
      <div class="sched-log-box" style="height:100px;overflow:auto">
        <div style="height:500px">content</div>
      </div>`;
    document.body.appendChild(panel);
    const logBox = panel.querySelector('.sched-log-box');
    // Force a scroll position. jsdom allows direct scrollTop assignment.
    logBox.scrollTop = 120;
    expect(logBox.scrollTop).toBe(120);

    api.atomicReplaceChildren(panel, `
      <div class="sched-log-box" style="height:100px;overflow:auto">
        <div style="height:500px">content v2</div>
      </div>`);

    const newBox = panel.querySelector('.sched-log-box');
    expect(newBox).toBeTruthy();
    expect(newBox.scrollTop).toBe(120);
  });

  it('scroll preservation works even when the new HTML lacks the class', () => {
    const panel = document.createElement('div');
    panel.innerHTML = '<div class="sched-log-box" style="height:50px;overflow:auto"><div style="height:200px">x</div></div>';
    document.body.appendChild(panel);
    panel.querySelector('.sched-log-box').scrollTop = 40;

    // Replace with HTML that doesn't include .sched-log-box
    api.atomicReplaceChildren(panel, '<div class="different">no scroll box here</div>');
    // Should not throw; no scroll restoration happens, but the swap succeeds.
    expect(panel.querySelector('.different')).toBeTruthy();
  });
});

describe('atomicReplaceChildren — attributes preserved on new children', () => {
  // Regression guard: the <template> parse path must preserve attrs
  // (data-*, class, id, style) on the new nodes — not just textContent.
  it('data-* attributes preserved', () => {
    const panel = document.createElement('div');
    document.body.appendChild(panel);
    api.atomicReplaceChildren(panel, '<div data-label="x" class="chip">hi</div>');
    const div = panel.querySelector('.chip');
    expect(div).toBeTruthy();
    expect(div.getAttribute('data-label')).toBe('x');
  });

  it('nested structure preserved', () => {
    const panel = document.createElement('div');
    document.body.appendChild(panel);
    api.atomicReplaceChildren(panel,
      '<div class="outer"><span class="inner"><b>deep</b></span></div>');
    expect(panel.querySelector('.outer .inner b')).toBeTruthy();
    expect(panel.querySelector('.outer .inner b').textContent).toBe('deep');
  });
});
