// Round-61 pt.8 — scheduler timestamp parsing (parseSchedTs, latestForTask).
//
// The scheduler panel renders "Last run: Xh ago" for ~10 background tasks.
// parseSchedTs normalises bare YYYY-MM-DD dates (without a time) to that
// task's scheduled fire time so the relative-time math doesn't show "20h
// ago" for a task that fired at 9:45 AM today. latestForTask walks the
// per-user task keys (e.g. `auto_deployer_1`, `auto_deployer_2`) to surface
// the freshest run across users for the system view.

import { describe, it, expect, beforeAll } from 'vitest';
import { loadDashboardJs } from './loadDashboardJs.js';

let api;
beforeAll(() => {
  api = loadDashboardJs();
});

describe('parseSchedTs', () => {
  it('null / undefined returns null', () => {
    expect(api.parseSchedTs(null)).toBeNull();
    expect(api.parseSchedTs(undefined)).toBeNull();
  });

  it('numeric input treated as unix-seconds → ms', () => {
    expect(api.parseSchedTs(1700000000)).toBe(1700000000000);
  });

  it('non-string non-number returns null', () => {
    expect(api.parseSchedTs({ foo: 1 })).toBeNull();
    expect(api.parseSchedTs([])).toBeNull();
    expect(api.parseSchedTs(true)).toBeNull();
  });

  it('bare YYYY-MM-DD without taskKey defaults to noon local TZ', () => {
    const ts = api.parseSchedTs('2026-04-24');
    expect(ts).not.toBeNull();
    const d = new Date(ts);
    expect(d.getFullYear()).toBe(2026);
    expect(d.getMonth()).toBe(3); // April (0-indexed)
    expect(d.getDate()).toBe(24);
    expect(d.getHours()).toBe(12);
    expect(d.getMinutes()).toBe(0);
  });

  it('bare YYYY-MM-DD with auto_deployer key uses 9:45 fire time', () => {
    const ts = api.parseSchedTs('2026-04-24', 'auto_deployer');
    const d = new Date(ts);
    expect(d.getHours()).toBe(9);
    expect(d.getMinutes()).toBe(45);
  });

  it('bare YYYY-MM-DD with daily_close key uses 16:05 fire time', () => {
    const ts = api.parseSchedTs('2026-04-24', 'daily_close');
    const d = new Date(ts);
    expect(d.getHours()).toBe(16);
    expect(d.getMinutes()).toBe(5);
  });

  it('bare YYYY-MM-DD with unknown key falls back to noon', () => {
    const ts = api.parseSchedTs('2026-04-24', 'task_that_does_not_exist');
    const d = new Date(ts);
    expect(d.getHours()).toBe(12);
  });

  it('full ISO timestamp parsed via Date constructor', () => {
    const ts = api.parseSchedTs('2026-04-24T10:30:00Z');
    expect(ts).toBe(new Date('2026-04-24T10:30:00Z').getTime());
  });

  it('garbage string returns null', () => {
    expect(api.parseSchedTs('not-a-date')).toBeNull();
    expect(api.parseSchedTs('')).toBeNull();
  });
});

describe('latestForTask', () => {
  it('exact-match system task returns that value', () => {
    const lastRuns = {
      pead_refresh: '2026-04-24T06:00:00Z',
      email_drain: '2026-04-23T15:00:00Z',
    };
    expect(api.latestForTask(lastRuns, 'pead_refresh')).toBe('2026-04-24T06:00:00Z');
  });

  it('absent task returns null', () => {
    expect(api.latestForTask({}, 'auto_deployer')).toBeNull();
    expect(api.latestForTask({ other_task: '2026-04-24' }, 'auto_deployer')).toBeNull();
  });

  it('per-user prefix: returns the freshest matching key', () => {
    const lastRuns = {
      auto_deployer_1: '2026-04-24',  // noon if no task hour
      auto_deployer_2: '2026-04-23',
      auto_deployer_3: '2026-04-22',
    };
    // auto_deployer_1 has the freshest date → wins
    expect(api.latestForTask(lastRuns, 'auto_deployer')).toBe('2026-04-24');
  });

  it('per-user prefix: ignores unrelated keys', () => {
    const lastRuns = {
      auto_deployer_1: '2026-04-20',
      daily_close_1: '2026-04-24',
    };
    expect(api.latestForTask(lastRuns, 'auto_deployer')).toBe('2026-04-20');
  });

  it('exact-match wins over older prefix-match', () => {
    const lastRuns = {
      pead_refresh: '2026-04-24T06:00:00Z',
      pead_refresh_1: '2026-04-23T06:00:00Z',
    };
    expect(api.latestForTask(lastRuns, 'pead_refresh')).toBe('2026-04-24T06:00:00Z');
  });

  it('newer prefix-match wins over older exact-match', () => {
    const lastRuns = {
      auto_deployer: '2026-04-20',
      auto_deployer_1: '2026-04-24',
    };
    expect(api.latestForTask(lastRuns, 'auto_deployer')).toBe('2026-04-24');
  });

  it('skips entries with unparsable values', () => {
    const lastRuns = {
      auto_deployer_1: 'garbage',
      auto_deployer_2: '2026-04-23',
    };
    expect(api.latestForTask(lastRuns, 'auto_deployer')).toBe('2026-04-23');
  });
});
