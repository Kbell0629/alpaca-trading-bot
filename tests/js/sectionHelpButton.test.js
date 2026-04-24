// Round-61 pt.8 batch-9 — sectionHelpButton tests.
//
// Renders the ℹ button that sits beside every section header. Accepts an
// arbitrary `sectionId` string — must be escaped on both the aria-label
// AND the inline-onclick call so attacker-controlled IDs can't break out.

import { describe, it, expect, beforeAll } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('sectionHelpButton', () => {
  it('renders a button element with the help class', () => {
    const html = api.sectionHelpButton('positions');
    expect(html).toContain('<button');
    expect(html).toContain('class="section-help-btn"');
  });

  it('aria-label includes the section id', () => {
    const html = api.sectionHelpButton('positions');
    expect(html).toContain('aria-label="Help for positions"');
  });

  it('onclick invokes openSectionGuide with the id', () => {
    const html = api.sectionHelpButton('readiness');
    expect(html).toContain("openSectionGuide('readiness')");
  });

  it('HTML-escapes the section id (XSS guard)', () => {
    const html = api.sectionHelpButton('<img src=x>');
    expect(html).not.toContain('<img src=x');
    expect(html).toContain('&lt;img');
  });

  it('single-quote in section id is JS-escaped (attribute breakout guard)', () => {
    const html = api.sectionHelpButton("x'y");
    // Either the esc() HTML-escape or a JS escape — must not have a raw '
    // that could close the onclick attribute early.
    const unsafe = html.indexOf("openSectionGuide('x'y')");
    expect(unsafe).toBe(-1);
  });
});
