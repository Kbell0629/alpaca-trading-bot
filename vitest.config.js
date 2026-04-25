// Round-61 pt.8 — Vitest config for dashboard JS coverage.
// Round-61 pt.33 — switched provider from V8 to istanbul so the
// extracted dashboard_render_core.js (loaded via vm.runInThisContext)
// is actually instrumented. V8's coverage hooks bypass code that
// goes through `vm.runInThisContext`, so pt.22's V8-based config
// reported 0% even though the 392 tests exercise the helpers
// thoroughly. Istanbul instruments at parse time so it sees
// everything jsdom evaluates.
//
// templates/dashboard.html ships ~7000 LOC of inline JS that pytest-cov
// can't see. This config wires up jsdom + istanbul so we can drive the
// dashboard's pure helpers and reducer-style functions in isolation.
//
// Test files live in tests/js/ to keep them visually separated from the
// 1700+ Python tests in tests/. Coverage reports are written to
// coverage/js/ so they don't collide with pytest-cov's coverage/ dir.

import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'jsdom',
    include: ['tests/js/**/*.test.js'],
    // No global setup — each test file calls loadDashboardJs() itself
    // so failures are scoped to the file that triggers them.
    coverage: {
      provider: 'istanbul',
      reporter: ['text', 'text-summary', 'json-summary'],
      reportsDirectory: 'coverage/js',
      // Round-61 pt.33: include the extracted render-core module.
      // The inline dashboard.html JS is exercised via loadDashboardJs.js
      // but the 9700-line template itself isn't a pure module so we
      // don't add it to the include list (would inflate the denominator
      // with HTML/CSS noise). The render-core extraction (pt.8 Option B)
      // is the long-term home for testable helpers; coverage on that
      // file climbs as more helpers are extracted.
      include: [
        'static/dashboard_render_core.js',
      ],
      exclude: [
        'tests/**',
        'node_modules/**',
        'coverage/**',
      ],
      // Round-61 pt.33: ratchet floor. Locally measured istanbul
      // coverage on the extracted render-core (after pt.33's loader
      // fix to write probes to __VITEST_COVERAGE__):
      //   Statements   : 93.12% (379/407)
      //   Branches     : 86.85% (403/464)
      //   Functions    : 100%   (42/42)
      //   Lines        : 96.09% (320/333)
      // Floor set ~3-5% below measured to leave headroom for
      // environment drift across Node versions / dep upgrades.
      // NEVER lower without a written PR justification — same
      // rule as the Python --cov-fail-under floor. Bump it in any
      // PR that meaningfully expands the render-core module + tests.
      thresholds: {
        lines: 92,
        functions: 95,
        branches: 80,
        statements: 90,
      },
    },
    // Reasonable timeout — jsdom + JS parse of 7k lines is ~200ms cold.
    testTimeout: 10000,
  },
});
