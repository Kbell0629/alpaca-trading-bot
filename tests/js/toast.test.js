// Round-61 pt.8 batch-3 — toast + toastFromApiError tests.
//
// Every network error surfaces as a toast. XSS regression here = attacker-
// controlled JSON error field lands as HTML. We escape via `esc(msg)` and
// include the correlation ID so support can trace back to server logs.

import { describe, it, expect, beforeAll, beforeEach } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

/** The loader scaffolds a #toastContainer div. Clear it between tests so
 * toast assertions are independent. */
function clearToasts() {
  const c = document.getElementById('toastContainer');
  if (c) c.innerHTML = '';
}

describe('toast — basic render', () => {
  beforeEach(clearToasts);

  it('appends a toast with the info class by default', () => {
    api.toast('hello');
    const c = document.getElementById('toastContainer');
    const t = c.querySelector('.toast');
    expect(t).toBeTruthy();
    expect(t.classList.contains('info')).toBe(true);
  });

  it('sets class based on the type argument', () => {
    api.toast('ok', 'success');
    api.toast('bad', 'error');
    api.toast('heads up', 'warning');
    const toasts = document.querySelectorAll('.toast');
    expect(toasts[0].classList.contains('success')).toBe(true);
    expect(toasts[1].classList.contains('error')).toBe(true);
    expect(toasts[2].classList.contains('warning')).toBe(true);
  });

  it('message text is visible', () => {
    api.toast('Order filled');
    const c = document.getElementById('toastContainer');
    expect(c.textContent).toContain('Order filled');
  });
});

describe('toast — XSS escape', () => {
  beforeEach(clearToasts);

  it('dangerous HTML in the message is escaped', () => {
    api.toast('<img src=x onerror="alert(1)">');
    const c = document.getElementById('toastContainer');
    // The raw tag must NOT be in the DOM as an element
    expect(c.querySelector('img')).toBeNull();
    // But the escaped text must appear
    expect(c.textContent).toContain('<img');
  });

  it('correlationId is HTML-escaped too', () => {
    api.toast('Oops', 'error', '<script>x</script>');
    const c = document.getElementById('toastContainer');
    // No injected <script> tag
    expect(c.querySelector('script')).toBeNull();
    // Ref text shows up (escaped)
    expect(c.textContent).toContain('ref:');
    expect(c.textContent).toContain('<script>x</script>');
  });
});

describe('toast — correlation ID display', () => {
  beforeEach(clearToasts);

  it('omits the ref suffix when correlationId is empty', () => {
    api.toast('Done', 'success');
    const c = document.getElementById('toastContainer');
    expect(c.textContent).not.toContain('ref:');
  });

  it('renders the ref suffix when correlationId is non-empty', () => {
    api.toast('Request failed', 'error', 'abc-123');
    const c = document.getElementById('toastContainer');
    expect(c.textContent).toContain('ref: abc-123');
  });
});

describe('toast — retry callback', () => {
  beforeEach(clearToasts);

  it('Retry button renders when a retryFn is provided', () => {
    api.toast('Network down', 'error', '', () => {});
    const c = document.getElementById('toastContainer');
    const btn = c.querySelector('button');
    expect(btn).toBeTruthy();
    expect(btn.textContent).toBe('Retry');
  });

  it('no Retry button when retryFn is null/undefined', () => {
    api.toast('Minor hiccup', 'warning');
    const c = document.getElementById('toastContainer');
    expect(c.querySelector('button')).toBeNull();
  });

  it('clicking Retry invokes the callback and removes the toast', () => {
    let called = 0;
    api.toast('Fetch failed', 'error', '', () => { called += 1; });
    const btn = document.querySelector('#toastContainer button');
    btn.click();
    expect(called).toBe(1);
    // After the click the toast should be gone
    expect(document.querySelector('#toastContainer .toast')).toBeNull();
  });

  it('retryFn that throws does not crash the dashboard', () => {
    api.toast('blow', 'error', '', () => { throw new Error('boom'); });
    const btn = document.querySelector('#toastContainer button');
    expect(() => btn.click()).not.toThrow();
  });
});

describe('toastFromApiError', () => {
  beforeEach(clearToasts);

  it('null data → fallback message shown as error', () => {
    api.toastFromApiError(null, 'Fallback text');
    const c = document.getElementById('toastContainer');
    const t = c.querySelector('.toast');
    expect(t.classList.contains('error')).toBe(true);
    expect(c.textContent).toContain('Fallback text');
  });

  it('data with error field uses that message', () => {
    api.toastFromApiError({ error: 'Too many requests' }, 'fallback');
    const c = document.getElementById('toastContainer');
    expect(c.textContent).toContain('Too many requests');
    expect(c.textContent).not.toContain('fallback');
  });

  it('correlation_id from response surfaces in the toast', () => {
    api.toastFromApiError(
      { error: 'Upstream timeout', correlation_id: 'trace-42' },
      'fallback');
    const c = document.getElementById('toastContainer');
    expect(c.textContent).toContain('Upstream timeout');
    expect(c.textContent).toContain('ref: trace-42');
  });

  it('data without error field falls back', () => {
    api.toastFromApiError({ status: 500 }, 'Generic failure');
    const c = document.getElementById('toastContainer');
    expect(c.textContent).toContain('Generic failure');
  });

  it('no fallback + no error → default "Request failed"', () => {
    api.toastFromApiError(null);
    const c = document.getElementById('toastContainer');
    expect(c.textContent).toContain('Request failed');
  });
});
