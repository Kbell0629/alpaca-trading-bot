// Round-61 pt.8 batch-7 — README HTML sanitizer.
//
// _sanitizeReadmeHtml runs on admin-supplied README markdown (rendered by
// marked) before it's inserted into #readmeContent. Critical XSS barrier.
// Allowlist: structural tags + anchors/images; strips everything else.
// Also defangs javascript:/data:/vbscript: URIs.

import { describe, it, expect, beforeAll } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('_sanitizeReadmeHtml — allowed tags kept', () => {
  it('preserves headings, paragraphs, bold/italic', () => {
    const out = api._sanitizeReadmeHtml('<h1>Title</h1><p>Hello <strong>world</strong> and <em>emphasis</em></p>');
    expect(out).toContain('<h1>Title</h1>');
    expect(out).toContain('<strong>world</strong>');
    expect(out).toContain('<em>emphasis</em>');
  });

  it('preserves lists + code + blockquote', () => {
    const out = api._sanitizeReadmeHtml('<ul><li>a</li><li>b</li></ul><pre><code>const x = 1</code></pre><blockquote>quote</blockquote>');
    expect(out).toContain('<ul>');
    expect(out).toContain('<li>a</li>');
    expect(out).toContain('<code>');
    expect(out).toContain('<blockquote>');
  });

  it('preserves tables', () => {
    const out = api._sanitizeReadmeHtml('<table><thead><tr><th>H</th></tr></thead><tbody><tr><td>D</td></tr></tbody></table>');
    expect(out).toContain('<table>');
    expect(out).toContain('<th>H</th>');
    expect(out).toContain('<td>D</td>');
  });
});

describe('_sanitizeReadmeHtml — disallowed tags unwrapped', () => {
  it('<script> tag stripped', () => {
    const out = api._sanitizeReadmeHtml('<p>safe</p><script>alert(1)</script>');
    expect(out).not.toContain('<script');
    expect(out).not.toContain('</script>');
  });

  it('<iframe> stripped', () => {
    const out = api._sanitizeReadmeHtml('<p>before</p><iframe src="evil.html"></iframe><p>after</p>');
    expect(out).not.toContain('<iframe');
  });

  it('<style> stripped', () => {
    const out = api._sanitizeReadmeHtml('<style>body{display:none}</style><p>visible</p>');
    expect(out).not.toContain('<style');
    expect(out).toContain('<p>visible</p>');
  });

  it('<svg onload=...> stripped (not on allowlist — potential XSS vector)', () => {
    const out = api._sanitizeReadmeHtml('<svg onload="alert(1)"><text>hi</text></svg>');
    expect(out).not.toContain('<svg');
    expect(out).not.toContain('onload');
  });
});

describe('_sanitizeReadmeHtml — attribute stripping', () => {
  it('onclick attribute stripped from allowed tag', () => {
    const out = api._sanitizeReadmeHtml('<a href="#x" onclick="alert(1)">link</a>');
    expect(out).toContain('href="#x"');
    expect(out).not.toContain('onclick');
  });

  it('onerror attribute stripped from <img>', () => {
    const out = api._sanitizeReadmeHtml('<img src="ok.png" onerror="alert(1)" alt="x">');
    expect(out).toContain('src="ok.png"');
    expect(out).toContain('alt="x"');
    expect(out).not.toContain('onerror');
  });

  it('class and id on any tag preserved (global allow)', () => {
    const out = api._sanitizeReadmeHtml('<p class="special" id="first" data-evil="x">text</p>');
    expect(out).toContain('class="special"');
    expect(out).toContain('id="first"');
    expect(out).not.toContain('data-evil');
  });
});

describe('_sanitizeReadmeHtml — URI defanging', () => {
  it('javascript: href removed', () => {
    const out = api._sanitizeReadmeHtml('<a href="javascript:alert(1)">bad</a>');
    expect(out).not.toContain('javascript:');
  });

  it('data: href removed', () => {
    const out = api._sanitizeReadmeHtml('<a href="data:text/html,x">bad</a>');
    expect(out).not.toContain('data:');
  });

  it('vbscript: href removed', () => {
    const out = api._sanitizeReadmeHtml('<a href="vbscript:MsgBox(1)">bad</a>');
    expect(out).not.toContain('vbscript:');
  });

  it('https:// href preserved', () => {
    const out = api._sanitizeReadmeHtml('<a href="https://example.com">ok</a>');
    expect(out).toContain('href="https://example.com"');
  });

  it('case-insensitive defang (JavaScript:, DATA:)', () => {
    const out = api._sanitizeReadmeHtml('<a href="  JavaScript:alert(1)">x</a>');
    expect(out).not.toMatch(/javascript:/i);
  });

  it('data: src on <img> also stripped', () => {
    const out = api._sanitizeReadmeHtml('<img src="data:image/png;base64,x" alt="x">');
    expect(out).not.toContain('data:');
  });
});
