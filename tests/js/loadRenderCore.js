// Round-61 pt.8 Option B — loader for the extracted dashboard_render_core.js.
//
// Unlike loadDashboardJs.js which reads templates/dashboard.html (~9700
// lines) and evaluates its giant inline <script> block, this loader
// targets ONLY the 300-line extracted module. Fast, no jsdom gymnastics
// beyond what vitest's environment already provides.
//
// The extracted file is an IIFE that attaches every function to `window`
// (and `globalThis`). Tests can then grab them by name off the returned
// api object (or off globalThis directly).
//
// Round-61 pt.33: instrument the source via istanbul-lib-instrument
// BEFORE vm.runInThisContext so vitest's istanbul coverage provider
// can see what gets executed. Without this step the IIFE runs
// uninstrumented and coverage reports 0% even though the 392 tests
// exercise every helper. Instrumented code writes to a per-file
// __coverage__ object that vitest's istanbul reporter collects when
// the test process exits. The instrumenter caches its output (per
// source string) so the cost is paid once per test file.

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import { createInstrumenter } from 'istanbul-lib-instrument';

const __dirname = dirname(fileURLToPath(import.meta.url));
const RENDER_CORE_PATH = join(__dirname, '..', '..', 'static', 'dashboard_render_core.js');

let _cachedSource = null;
let _cachedInstrumented = null;
const _instrumenter = createInstrumenter({
  esModules: false,
  produceSourceMap: false,
  // Leave the original code's `var`/IIFE shape alone — we only
  // want coverage probes injected, not transpilation.
  compact: false,
  // Round-61 pt.33: write probes to vitest's expected global
  // (`__VITEST_COVERAGE__`) so the istanbul reporter collects
  // them. The default `__coverage__` works for raw istanbul but
  // vitest's takeCoverage() reads from a different key.
  coverageVariable: '__VITEST_COVERAGE__',
});

export function readRenderCoreSource() {
  if (_cachedSource !== null) return _cachedSource;
  _cachedSource = readFileSync(RENDER_CORE_PATH, 'utf-8');
  return _cachedSource;
}

function _instrument(src) {
  if (_cachedInstrumented !== null) return _cachedInstrumented;
  _cachedInstrumented = _instrumenter.instrumentSync(src, RENDER_CORE_PATH);
  return _cachedInstrumented;
}

/** Run the extracted render-core module inside vitest's jsdom environment
 * and return a reference to the `DashboardRenderCore` namespace object
 * that the IIFE attaches to `window`. The source is instrumented so
 * coverage probes feed istanbul's reporter. */
export function loadRenderCore() {
  const src = _instrument(readRenderCoreSource());
  vm.runInThisContext(src, { filename: 'dashboard_render_core.js' });
  return globalThis.DashboardRenderCore;
}

export function _resetRenderCoreCache() {
  _cachedSource = null;
  _cachedInstrumented = null;
}
