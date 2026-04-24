// Round-61 pt.8 — Vitest config for dashboard JS coverage.
//
// templates/dashboard.html ships ~7000 LOC of inline JS that pytest-cov
// can't see. This config wires up jsdom + V8 coverage so we can drive
// the dashboard's pure helpers and reducer-style functions in isolation.
//
// Test files live in tests/js/ to keep them visually separated from the
// 1484 Python tests in tests/. Coverage reports are written to
// coverage/js/ so they don't collide with pytest-cov's coverage/ dir.

import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/js/**/*.test.js'],
    // No global setup — each test file calls loadDashboardJs() itself
    // so failures are scoped to the file that triggers them.
    coverage: {
      provider: 'v8',
      reporter: ['text', 'text-summary'],
      reportsDirectory: 'coverage/js',
      // We instrument the extracted JS via the loader shim, not source
      // files directly — V8 coverage covers what gets executed in
      // jsdom, which IS the inline dashboard code via eval().
      include: [
        'tests/js/loadDashboardJs.js',
        'static/dashboard_render_core.js',
      ],
      // Round-61 pt.22: JS coverage ratchet infrastructure.
      //
      // Current measurement is 0% because the existing loader
      // (`tests/js/loadDashboardJs.js` and `loadRenderCore.js`) uses
      // `vm.runInThisContext()` which V8's coverage instrumentation
      // doesn't hook into. So the 392 tests exercise the code
      // correctly but V8 reports zero lines covered.
      //
      // Floor is set to 0 so CI passes now — the ratchet is in place,
      // ready for a follow-up PR to switch the loader to
      // `@vitest/coverage-istanbul` (which instruments code before
      // vm.runInThisContext runs) or convert the render-core module
      // to a proper ES import. Either change raises the measurable
      // coverage above 0; update the floor in the same PR to lock
      // in the gain.
      //
      // Never lower without a written PR justification — ratchet
      // only moves one direction (same rule as the Python
      // --cov-fail-under floor).
      thresholds: {
        lines: 0,
        functions: 0,
        branches: 0,
        statements: 0,
      },
    },
    // Reasonable timeout — jsdom + JS parse of 7k lines is ~200ms cold.
    testTimeout: 10000,
  },
});
