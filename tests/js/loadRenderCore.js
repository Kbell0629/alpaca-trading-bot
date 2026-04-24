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

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const __dirname = dirname(fileURLToPath(import.meta.url));
const RENDER_CORE_PATH = join(__dirname, '..', '..', 'static', 'dashboard_render_core.js');

let _cachedSource = null;

export function readRenderCoreSource() {
  if (_cachedSource !== null) return _cachedSource;
  _cachedSource = readFileSync(RENDER_CORE_PATH, 'utf-8');
  return _cachedSource;
}

/** Run the extracted render-core module inside vitest's jsdom environment
 * and return a reference to the `DashboardRenderCore` namespace object
 * that the IIFE attaches to `window`. */
export function loadRenderCore() {
  const src = readRenderCoreSource();
  vm.runInThisContext(src, { filename: 'dashboard_render_core.js' });
  return globalThis.DashboardRenderCore;
}

export function _resetRenderCoreCache() {
  _cachedSource = null;
}
