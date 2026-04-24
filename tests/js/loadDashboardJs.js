// Round-61 pt.8 — JS loader shim for templates/dashboard.html.
//
// The dashboard ships ~7000 LOC of inline JS that pytest-cov can't see.
// This loader pulls the big inline <script> block out of the HTML and
// runs it inside jsdom, exposing every top-level function on a
// returned `api` object so test files can call them directly.
//
// Why we extract from the HTML instead of importing a .js module:
//   * The dashboard has no build step; the JS lives inline because
//     the deploy is "stdlib only, no bundler". Pulling the source
//     into a .js sibling would be a refactor risk for runtime; this
//     shim lets us keep the JS where it is and still test it.
//   * `node --check` already extracts the same way (see CLAUDE.md
//     pickup checklist) — we're reusing the proven approach.
//
// Limitations:
//   * Functions/consts that close over module-level state (window,
//     document, fetch) need stubs — each test file installs the ones
//     it cares about before calling loadDashboardJs().
//   * Async-startup paths (window.addEventListener('load', ...)) fire
//     immediately on jsdom but are no-ops without a backing fetch
//     mock; tests that exercise them must install fetch first.

import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const DASHBOARD_PATH = join(__dirname, '..', '..', 'templates', 'dashboard.html');

let _cachedSource = null;

/** Pull the LARGE inline <script> block (the one starting with `function esc`)
 * from templates/dashboard.html. Cached so repeated calls don't reparse the
 * 9.6k-line HTML file. Skips the small service-worker registration script
 * at the top of the file. */
export function readDashboardJs() {
  if (_cachedSource !== null) return _cachedSource;
  const html = readFileSync(DASHBOARD_PATH, 'utf-8');
  // Match every <script> ... </script> block that has no `src=` attribute.
  // The two big inline scripts are the SW registration (small) + the dashboard
  // (big). We keep both so any helper that the SW-registration script defines
  // is also available — there isn't one today, but it's cheap insurance.
  const blocks = [];
  const re = /<script>([\s\S]*?)<\/script>/g;
  let m;
  while ((m = re.exec(html)) !== null) {
    blocks.push(m[1]);
  }
  if (blocks.length === 0) {
    throw new Error('loadDashboardJs: no inline <script> blocks found in ' + DASHBOARD_PATH);
  }
  _cachedSource = blocks.join('\n;\n');
  return _cachedSource;
}

/** Execute the dashboard JS inside the current jsdom context (vitest's
 * environment: 'jsdom' provides window/document already). Returns an object
 * mapping known top-level names to their function references so tests can
 * call them as `api.esc(...)`.
 *
 * Optional `stubs` parameter overlays values onto the global object BEFORE
 * the script runs — use it to install fetch mocks, fixed timers, etc.
 *
 * The list of exported names is explicit (not auto-discovered) so adding a
 * new helper to dashboard.html requires opting into tests for it. That
 * discourages ratchet-by-accident.
 */
export function loadDashboardJs(stubs = {}) {
  const src = readDashboardJs();
  // Copy stubs onto window so the script picks them up via global lookup.
  for (const [k, v] of Object.entries(stubs)) {
    // eslint-disable-next-line no-undef
    globalThis[k] = v;
    // eslint-disable-next-line no-undef
    window[k] = v;
  }
  // Some dashboard code references `window.location.href.includes(...)`.
  // jsdom provides a default location of about:blank; if a test needs a
  // specific URL it should install a stub via the harness's separate
  // `setLocation()` helper (not yet built — add when first test needs it).

  // Install no-op stubs for the cdn-loaded libs the dashboard expects.
  // jsdom doesn't load <script src="...">, so Chart and marked are absent.
  if (typeof globalThis.Chart === 'undefined') {
    globalThis.Chart = class { constructor() {} update() {} destroy() {} };
  }
  if (typeof globalThis.marked === 'undefined') {
    globalThis.marked = { parse: (s) => s };
  }
  // Stop the in-script `setInterval`s + `requestAnimationFrame`s from
  // actually scheduling. Tests can re-enable per-call by injecting
  // their own stubs after load.
  if (typeof globalThis.setInterval === 'function') {
    const origSetInterval = globalThis.setInterval;
    globalThis.setInterval = (fn, ms) => 0;
    // Stash so a future test can restore if needed
    globalThis.__origSetInterval = origSetInterval;
  }
  if (typeof globalThis.requestAnimationFrame === 'undefined') {
    globalThis.requestAnimationFrame = () => 0;
  }
  // fetch — jsdom doesn't ship one. Default to "always-200 with empty data"
  // so the dashboard's auto-init `refreshData` doesn't reject and pollute
  // the test output with unhandled-rejection noise. Tests that need a
  // specific response shape should install their own fetch via stubs.
  if (typeof globalThis.fetch === 'undefined') {
    globalThis.fetch = () => Promise.resolve({
      ok: true,
      status: 200,
      headers: new Map(),
      json: () => Promise.resolve({}),
      text: () => Promise.resolve(''),
    });
  }
  // Scaffold the DOM nodes that the dashboard's helpers expect to find.
  // Without these, toast/log/scroll helpers throw on first call.
  const ensureNode = (id, tag = 'div') => {
    if (!document.getElementById(id)) {
      const el = document.createElement(tag);
      el.id = id;
      document.body.appendChild(el);
    }
  };
  ensureNode('toastContainer');
  ensureNode('app');
  ensureNode('logPanel');

  // Run the dashboard JS as a top-level script so its `function` declarations
  // attach to the global object (jsdom's window). An IIFE wrapper would make
  // them local to the wrapper and unreachable from tests. We use
  // vm.runInThisContext rather than `eval(src)` so syntax errors get a
  // useful filename in the stack trace.
  vm.runInThisContext(src, { filename: 'dashboard.inline.js' });

  // Build an api object exposing the functions tests typically poke.
  // Add to this list when you add a test for a new function — it's
  // intentionally explicit (see module docstring above).
  const api = {};
  const exposed = [
    // XSS / encoding helpers
    'esc', 'jsStr',
    // Formatting helpers
    'fmtMoney', 'fmtPct', 'pnlClass', 'fmtUpdatedET',
    // OCC option-symbol parser
    '_occParse',
    // Scheduler timestamp helpers
    'parseSchedTs', 'latestForTask',
    // Atomic DOM swap helper (anti-jitter — see CLAUDE.md)
    'atomicReplaceChildren',
    // Toast / log helpers
    'toast', 'addLog', 'renderLog',
    // Modal helpers
    'openModal', 'closeModal',
    // Scroll helpers
    'scrollToTop', 'scrollToSection',
  ];
  for (const name of exposed) {
    if (typeof globalThis[name] === 'function') {
      api[name] = globalThis[name];
    }
  }
  return api;
}

/** Reset the cached source so a test that mutates the file (rare) can re-read.
 * Also useful in `afterEach` if your test pollutes globalThis. */
export function _resetLoaderCache() {
  _cachedSource = null;
}
