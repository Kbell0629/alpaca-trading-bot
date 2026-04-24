// Round-61 pt.8 batch-7 — detectActivePreset tests.
//
// Inspects the user's guardrails + auto_deployer_config and returns one of
// 'conservative' / 'moderate' / 'aggressive' / 'custom'. Powers the
// Strategy Templates tab — regression here shows the wrong preset as
// active to the user.

import { describe, it, expect, beforeAll } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

function build({ stopPct = 0.10, maxPos = 5, maxPerStock = 0.10 } = {}) {
  return {
    guardrails: { max_positions: maxPos, max_position_pct: maxPerStock },
    auto_deployer_config: {
      max_positions: maxPos,
      risk_settings: { default_stop_loss_pct: stopPct },
    },
  };
}

describe('detectActivePreset — conservative', () => {
  it('5% stop + 3 positions + 5% per stock → conservative', () => {
    expect(api.detectActivePreset(build({
      stopPct: 0.05, maxPos: 3, maxPerStock: 0.05,
    }))).toBe('conservative');
  });
});

describe('detectActivePreset — moderate', () => {
  it('10% stop + 5 positions + 10% per stock → moderate', () => {
    expect(api.detectActivePreset(build({
      stopPct: 0.10, maxPos: 5, maxPerStock: 0.10,
    }))).toBe('moderate');
  });

  it('Round-20 migration: 10% stop + 5 positions + 7% per stock → moderate', () => {
    // User who auto-migrated from 10% to 7% per-stock should still show
    // Moderate, not Custom.
    expect(api.detectActivePreset(build({
      stopPct: 0.10, maxPos: 5, maxPerStock: 0.07,
    }))).toBe('moderate');
  });
});

describe('detectActivePreset — aggressive', () => {
  it('5% stop + 8+ positions + 15%+ per stock → aggressive', () => {
    expect(api.detectActivePreset(build({
      stopPct: 0.05, maxPos: 8, maxPerStock: 0.15,
    }))).toBe('aggressive');
  });

  it('5% stop + 10 positions + 20% per stock → aggressive', () => {
    expect(api.detectActivePreset(build({
      stopPct: 0.05, maxPos: 10, maxPerStock: 0.20,
    }))).toBe('aggressive');
  });
});

describe('detectActivePreset — custom fallback', () => {
  it('nothing matches → custom', () => {
    expect(api.detectActivePreset(build({
      stopPct: 0.08, maxPos: 4, maxPerStock: 0.10,
    }))).toBe('custom');
  });

  it('empty object → falls through to default moderate-ish values but not a match', () => {
    // Defaults: stopPct=0.10, maxPos=5, maxPerStock=0.10 → matches moderate
    expect(api.detectActivePreset({})).toBe('moderate');
  });

  it('near-miss (conservative stop but aggressive position count) → custom', () => {
    expect(api.detectActivePreset(build({
      stopPct: 0.05, maxPos: 5, maxPerStock: 0.05,
    }))).toBe('custom');
  });
});
