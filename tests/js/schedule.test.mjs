// Task 8 — Schedule startup request sequencing. node --test, no jsdom.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHarness } from './harness.mjs';

const WEEKS = { weeks: [5, 6, 7, 8] };
const SCHED = { status: 200, body: { week: 7, season: 2026, matchups: [], note: '' } };

const scheduleCall = (h) => h.calls.find(c => c.url.includes('/api/schedule/fetch'));

test('no forced week: current-week and weeks are issued concurrently', async () => {
  const h = createHarness({
    page: 'schedule.html',
    routes: {
      '/api/schedule/current-week': { defer: true },
      '/api/schedule/weeks': { defer: true },
      '/api/schedule/fetch': SCHED,
    },
  });
  await h.start();

  // BOTH requests are in flight before either has resolved -> they overlapped.
  assert.deepEqual(h.pending().sort(), [
    '/api/schedule/current-week',
    '/api/schedule/weeks',
  ].sort(), 'both startup requests must be in flight together');

  h.resolve('/api/schedule/current-week', { status: 200, body: { week: 7 } });
  h.resolve('/api/schedule/weeks', { status: 200, body: WEEKS });
  await h.flush();
  await h.flush();

  assert.deepEqual([...h.el('weekSelect').options].map(o => o.value), ['5', '6', '7', '8']);
  assert.equal(h.el('weekSelect').value, '7', 'the resolved current week is selected');
  assert.match(scheduleCall(h).url, /week=7$/, 'the selected week is the one fetched');
});

test('current-week failure still renders a usable schedule', async () => {
  const h = createHarness({
    page: 'schedule.html',
    routes: {
      '/api/schedule/current-week': { throw: 'network down' },
      '/api/schedule/weeks': { status: 200, body: { weeks: [4, 5] } },
      '/api/schedule/fetch': { status: 200, body: { week: 4, season: 2026, matchups: [] } },
    },
  });
  await h.start();
  await h.flush();
  await h.flush();

  assert.deepEqual([...h.el('weekSelect').options].map(o => o.value), ['4', '5']);
  // default currentWeek (1) is not published -> first available option, unchanged semantics
  assert.match(scheduleCall(h).url, /week=4$/, 'the page must not load empty');
});

test('weeks-list failure keeps the resolved week instead of defaulting to 1', async () => {
  const h = createHarness({
    page: 'schedule.html',
    routes: {
      '/api/schedule/current-week': { status: 200, body: { week: 9 } },
      '/api/schedule/weeks': { status: 500, body: {} },
      '/api/schedule/fetch': { status: 200, body: { week: 9, season: 2026, matchups: [] } },
    },
  });
  await h.start();
  await h.flush();
  await h.flush();

  assert.equal(h.el('weekSelect').options.length, 0, 'no weeks were published to the dropdown');
  assert.match(scheduleCall(h).url, /week=9$/,
    'an empty dropdown must NOT silently fall back to week 1');
});

test('forced ?week=N never requests current-week', async () => {
  const h = createHarness({
    page: 'schedule.html',
    search: '?week=12',
    routes: {
      '/api/schedule/current-week': { status: 200, body: { week: 3 } },
      '/api/schedule/weeks': { status: 200, body: { weeks: [10, 11, 12] } },
      '/api/schedule/fetch': { status: 200, body: { week: 12, season: 2026, matchups: [] } },
    },
  });
  await h.start();
  await h.flush();
  await h.flush();

  assert.equal(h.count('/api/schedule/current-week'), 0,
    'a forced week makes the current-week lookup pointless');
  assert.match(scheduleCall(h).url, /week=12$/, 'the forced week wins');
});

test('unavailable requested week falls back to the first available option', async () => {
  const h = createHarness({
    page: 'schedule.html',
    routes: {
      '/api/schedule/current-week': { status: 200, body: { week: 9 } },
      '/api/schedule/weeks': { status: 200, body: { weeks: [5, 6, 7] } },
      '/api/schedule/fetch': { status: 200, body: { week: 5, season: 2026, matchups: [] } },
    },
  });
  await h.start();
  await h.flush();
  await h.flush();

  assert.match(scheduleCall(h).url, /week=5$/,
    'bye/unpublished week falls back to sel.options[0], as before');
});
