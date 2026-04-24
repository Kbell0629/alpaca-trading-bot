// Round-61 pt.8 batch-9 — section-visibility helpers.
//
// `getHiddenSections` / `setHiddenSections` / `toggleSection` power the
// Show / Hide Sections menu. Round-trip state through localStorage —
// corrupted JSON in localStorage must degrade gracefully.

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
  api.getHiddenSections = globalThis.getHiddenSections;
  api.setHiddenSections = globalThis.setHiddenSections;
  api.toggleSection = globalThis.toggleSection;
});

beforeEach(() => {
  localStorage.clear();
});

describe('getHiddenSections', () => {
  it('empty storage → empty array', () => {
    expect(api.getHiddenSections()).toEqual([]);
  });

  it('valid JSON array round-trips', () => {
    localStorage.setItem('hiddenSections', JSON.stringify(['a', 'b']));
    expect(api.getHiddenSections()).toEqual(['a', 'b']);
  });

  it('corrupted JSON → empty array (graceful)', () => {
    localStorage.setItem('hiddenSections', '{not valid');
    expect(api.getHiddenSections()).toEqual([]);
  });
});

describe('setHiddenSections', () => {
  it('writes array as JSON', () => {
    api.setHiddenSections(['x', 'y']);
    expect(localStorage.getItem('hiddenSections')).toBe('["x","y"]');
  });

  it('null/undefined treated as empty array', () => {
    api.setHiddenSections(null);
    expect(localStorage.getItem('hiddenSections')).toBe('[]');
    api.setHiddenSections(undefined);
    expect(localStorage.getItem('hiddenSections')).toBe('[]');
  });
});

describe('toggleSection', () => {
  it('adds id when not hidden', () => {
    api.toggleSection('positions');
    expect(api.getHiddenSections()).toContain('positions');
  });

  it('removes id when already hidden', () => {
    api.setHiddenSections(['positions']);
    api.toggleSection('positions');
    expect(api.getHiddenSections()).not.toContain('positions');
  });

  it('multiple toggles round-trip the state', () => {
    api.toggleSection('a');
    api.toggleSection('b');
    expect(api.getHiddenSections().sort()).toEqual(['a', 'b']);
    api.toggleSection('a');
    expect(api.getHiddenSections()).toEqual(['b']);
  });
});
