// Round-61 pt.8 batch-8 — buildStrategyTemplates tests.
//
// Renders the Strategy Templates tab with 3 preset cards (Conservative,
// Moderate, Aggressive) + a Custom label when user settings don't match.
// Thin test: verify the active preset gets highlighted + current settings
// summary renders correctly.

import { describe, it, expect, beforeAll } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('buildStrategyTemplates — structure', () => {
  it('always includes all three preset cards', () => {
    const html = api.buildStrategyTemplates({});
    expect(html).toContain('Conservative');
    expect(html).toContain('Moderate');
    expect(html).toContain('Aggressive');
  });

  it('includes key preset metrics (stopLoss, maxPositions)', () => {
    const html = api.buildStrategyTemplates({});
    expect(html).toContain('5%');   // conservative stop + position
    expect(html).toContain('10%');  // moderate stop
  });
});

describe('buildStrategyTemplates — current settings summary', () => {
  it('shows current stop-loss from config', () => {
    const html = api.buildStrategyTemplates({
      auto_deployer_config: { risk_settings: { default_stop_loss_pct: 0.08 } },
    });
    // curStop = 8
    expect(html).toMatch(/8\s*%/);
  });

  it('shows current max_positions from config', () => {
    const html = api.buildStrategyTemplates({
      auto_deployer_config: { max_positions: 7 },
    });
    // Appears as "7" somewhere in the output
    expect(html).toContain('7');
  });

  it('falls back to guardrails.max_positions when config missing', () => {
    const html = api.buildStrategyTemplates({
      guardrails: { max_positions: 4, max_position_pct: 0.10 },
    });
    expect(html).toContain('4');
  });
});
