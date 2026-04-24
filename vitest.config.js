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
      include: ['tests/js/loadDashboardJs.js'],
    },
    // Reasonable timeout — jsdom + JS parse of 7k lines is ~200ms cold.
    testTimeout: 10000,
  },
});
