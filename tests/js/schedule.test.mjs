// Task 8 — Schedule startup request sequencing, tested against the REAL page markup.
// QA §8: schedule.html ships a static `<option value="1">Week 1</option>`, so a weeks-list
// FAILURE does not leave an empty dropdown. The harness builds the DOM from the file, which is
// what makes the failure case meaningful -- an empty-select stub passes for the wrong reason.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHarness } from './harness.mjs';

const WEEKS = { weeks: [5, 6, 7, 8] };
const sched = (week) => ({ status: 200, body: { week, season: 2026, matchups: [], note: '' } });

const mk = (over = {}, search = '') => createHarness({
  page: 'schedule.html',
  search,
  routes: { '/api/schedule/weeks': { status: 200, body: WEEKS }, ...over },
});

const scheduleCall = (h) => h.calls.find(c => c.url.includes('/api/schedule/fetch'));
const dropdown = (h) => [...h.el('weekSelect').options].map(o => o.value);

async function settle(h) { await h.start(); await h.flush(); await h.flush(); }

test('real markup: the week dropdown starts with a static Week 1 placeholder', async () => {
  const h = mk({ '/api/schedule/weeks': { status: 500, body: {} },
                 '/api/schedule/current-week': { status: 200, body: { week: 9 } },
                 '/api/schedule/fetch': sched(9) });
  assert.deepEqual(dropdown(h), ['1'],
    'the placeholder is exactly what makes an empty-looking dropdown ambiguous');
});

test('no forced week: current-week and weeks are issued concurrently', async () => {
  const h = mk({
    '/api/schedule/current-week': { defer: true },
    '/api/schedule/weeks': { defer: true },
    '/api/schedule/fetch': sched(7),
  });
  await h.start();

  // BOTH requests are in flight before either has resolved -> they overlapped.
  assert.deepEqual(h.pending().sort(), ['/api/schedule/current-week', '/api/schedule/weeks'].sort(),
    'both startup requests must be in flight together');

  h.resolve('/api/schedule/current-week', { status: 200, body: { week: 7 } });
  h.resolve('/api/schedule/weeks', { status: 200, body: WEEKS });
  await h.flush(); await h.flush();

  assert.deepEqual(dropdown(h), ['5', '6', '7', '8']);
  assert.equal(h.el('weekSelect').value, '7', 'the resolved current week is selected');
  assert.match(scheduleCall(h).url, /week=7$/, 'the selected week is the one fetched');
  assert.match(h.el('weekBadge').textContent, /Week 7 · 2026 Season/);
});

test('§8 weeks failure (HTTP 500) keeps the resolved week 9, not the placeholder 1', async () => {
  const h = mk({ '/api/schedule/weeks': { status: 500, body: {} },
                 '/api/schedule/current-week': { status: 200, body: { week: 9 } },
                 '/api/schedule/fetch': sched(9) });
  await settle(h);

  assert.deepEqual(dropdown(h), ['1'], 'the markup placeholder is still in the DOM');
  assert.equal(h.el('weekSelect').value, '9', 'the resolved week is selected, not the placeholder');
  assert.match(scheduleCall(h).url, /week=9$/, 'and week 9 is what gets fetched');
  assert.match(h.el('weekBadge').textContent, /Week 9 · 2026 Season/, 'the label agrees');
});

test('§8 weeks failure (fetch rejection) keeps the resolved week too', async () => {
  const h = mk({ '/api/schedule/weeks': { throw: 'network down' },
                 '/api/schedule/current-week': { status: 200, body: { week: 9 } },
                 '/api/schedule/fetch': sched(9) });
  await settle(h);
  assert.match(scheduleCall(h).url, /week=9$/);
});

test('§8 weeks failure with no resolved week uses week 1 deliberately, not by accident', async () => {
  const h = mk({ '/api/schedule/weeks': { status: 500, body: {} },
                 '/api/schedule/current-week': { status: 500, body: {} },
                 '/api/schedule/fetch': sched(1) });
  await settle(h);
  // Both lookups failed, so there is nothing to keep. Week 1 is used deliberately; the
  // dropdown is left showing the placeholder rather than a fabricated list.
  assert.deepEqual(dropdown(h), ['1']);
  assert.match(scheduleCall(h).url, /week=1$/);
});

test('current-week failure still renders a usable schedule from the loaded list', async () => {
  const h = mk({ '/api/schedule/current-week': { throw: 'network down' },
                 '/api/schedule/weeks': { status: 200, body: { weeks: [4, 5] } },
                 '/api/schedule/fetch': sched(4) });
  await settle(h);
  assert.deepEqual(dropdown(h), ['4', '5']);
  assert.match(scheduleCall(h).url, /week=4$/, 'first available option, unchanged semantics');
});

test('forced ?week=N never requests current-week and wins over the list', async () => {
  const h = mk({
    '/api/schedule/current-week': { status: 200, body: { week: 3 } },
    '/api/schedule/weeks': { status: 200, body: { weeks: [10, 11, 12] } },
    '/api/schedule/fetch': sched(12),
  }, '?week=12');
  await settle(h);
  assert.equal(h.count('/api/schedule/current-week'), 0, 'pointless with an override');
  assert.match(scheduleCall(h).url, /week=12$/);
});

test('a published list that OMITS the week keeps the first-available fallback', async () => {
  const h = mk({ '/api/schedule/current-week': { status: 200, body: { week: 9 } },
                 '/api/schedule/weeks': { status: 200, body: { weeks: [5, 6, 7] } },
                 '/api/schedule/fetch': sched(5) });
  await settle(h);
  assert.match(scheduleCall(h).url, /week=5$/, 'documented fallback, only when the list loaded');
});

test('a published list CONTAINING the week keeps it', async () => {
  const h = mk({ '/api/schedule/current-week': { status: 200, body: { week: 7 } },
                 '/api/schedule/weeks': { status: 200, body: { weeks: [6, 7, 8] } },
                 '/api/schedule/fetch': sched(7) });
  await settle(h);
  assert.equal(h.el('weekSelect').value, '7');
  assert.match(scheduleCall(h).url, /week=7$/);
});