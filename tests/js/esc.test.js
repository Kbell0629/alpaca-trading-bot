// Round-61 pt.8 — XSS escape helpers (esc, jsStr).
//
// These are the dashboard's first line of defense against script injection
// when API data lands in innerHTML or quoted attribute strings. A regression
// here is a security regression — pin every branch.

import { describe, it, expect, beforeAll } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('esc', () => {
  it('null and undefined return empty string', () => {
    expect(api.esc(null)).toBe('');
    expect(api.esc(undefined)).toBe('');
  });

  it('plain ASCII unchanged', () => {
    expect(api.esc('hello world')).toBe('hello world');
    expect(api.esc('AAPL')).toBe('AAPL');
  });

  it('ampersand escaped first to avoid double-encoding', () => {
    expect(api.esc('A & B')).toBe('A &amp; B');
  });

  it('angle brackets escaped to prevent tag injection', () => {
    expect(api.esc('<script>alert(1)</script>')).toBe(
      '&lt;script&gt;alert(1)&lt;/script&gt;'
    );
  });

  it('double quotes escaped for attribute-context safety', () => {
    expect(api.esc('a"b')).toBe('a&quot;b');
  });

  it('single quotes escaped for attribute-context safety', () => {
    expect(api.esc("a'b")).toBe('a&#39;b');
  });

  it('backticks escaped (template-literal safety)', () => {
    expect(api.esc('a`b')).toBe('a&#96;b');
  });

  it('numeric input coerces to string', () => {
    expect(api.esc(42)).toBe('42');
  });

  it('object input coerces via String()', () => {
    expect(api.esc({})).toBe('[object Object]');
  });

  it('combined dangerous payload escapes every character', () => {
    const payload = `<img src=x onerror="alert('xss')">`;
    const out = api.esc(payload);
    expect(out).not.toContain('<');
    expect(out).not.toContain('"');
    expect(out).not.toContain("'");
    expect(out).toContain('&lt;');
    expect(out).toContain('&quot;');
    expect(out).toContain('&#39;');
  });
});

describe('jsStr', () => {
  it('null and undefined return empty string', () => {
    expect(api.jsStr(null)).toBe('');
    expect(api.jsStr(undefined)).toBe('');
  });

  it('plain ASCII unchanged', () => {
    expect(api.jsStr('hello')).toBe('hello');
  });

  it('backslash doubled', () => {
    expect(api.jsStr('a\\b')).toBe('a\\\\b');
  });

  it("single quote → unicode escape", () => {
    expect(api.jsStr("a'b")).toBe('a\\u0027b');
  });

  it('double quote → unicode escape', () => {
    expect(api.jsStr('a"b')).toBe('a\\u0022b');
  });

  it('less-than → unicode escape (prevents </script> breakout)', () => {
    expect(api.jsStr('a<b')).toBe('a\\u003cb');
  });

  it('numeric input coerces to string', () => {
    expect(api.jsStr(42)).toBe('42');
  });

  it('combined dangerous payload escapes every character', () => {
    const payload = `';alert('xss')//`;
    const out = api.jsStr(payload);
    // After escaping, the payload should NOT contain a literal single quote
    expect(out).not.toContain("'");
    // It SHOULD contain the unicode-escaped form
    expect(out).toContain('\\u0027');
  });
});
