// Tasks 1 + 7 — deferred projections, active-only rendering, debounced search. node --test, no jsdom.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHarness, fixture } from './harness.mjs';

const TEAMS = fixture(687);
const projPayload = (label = 'Top15') => ({ projections: [1, 2, 3].map(i => ({
  name: `Team 00${i}`, home_projection: { composite: 60 - i, projected_score: 30 - i, win_probability: 50 + i } })) });
const oddsPayload = (book = 'BetOnline') => ({ odds: [
  { home: 'Team 001', away: 'Team 002', spread: -3, total: 48, book_title: book }] });

const mk = (over = {}) => createHarness({
  page: 'analytics.html',
  routes: {
    '/api/analytics': { status: 200, body: { teams: TEAMS, season: 2026 } },
    '/api/projections': { status: 200, body: projPayload() },
    '/api/odds': { status: 200, body: oddsPayload() },
    ...over,
  },
});

const tabBtn = (h, name) => h.doc.querySelectorAll('.tab-btn').find(b => b.dataset.tab === name);

async function ready(h) { await h.start(); await h.flush(); await h.flush(); }

test('startup requests NO projections/odds and renders the primary content', async () => {
  const h = mk();
  await ready(h);

  assert.deepEqual(h.urls(), ['/api/analytics'], 'the startup trace contains one request only');
  assert.equal(h.el('content').style.display, 'block', 'primary content is revealed');
  assert.equal(h.rowsIn('spplus'), 687, 'the active section rendered the whole fixture');
  assert.equal(h.el('loading').style.display, 'none');
  assert.equal(h.html('projections'), '', 'the deferred pane is untouched, not even a spinner');
  assert.equal(h.count('/api/projections'), 0);
  assert.equal(h.count('/api/odds'), 0);
});

test('opening the tab loads BOTH feeds, after the content was already visible', async () => {
  const h = mk();
  await ready(h);
  h.click(tabBtn(h, 'projections'));
  await h.flush(); await h.flush();

  assert.equal(h.count('/api/projections'), 1);
  assert.equal(h.count('/api/odds'), 1);
  assert.match(h.html('projections'), /Multi-Factor Projections/, 'projections rendered');
  assert.match(h.html('projections'), /Live Betting Lines/, 'odds rendered');
  assert.match(h.html('projections'), /BetOnline/, 'the odds row is real');

  const rec = h.calls.find(c => c.url.includes('/api/projections'));
  assert.equal(rec.dom.contentVisible, true,
    'receipt: the projections request is issued AFTER the primary content is on screen');
  assert.equal(rec.dom.spplusRows, 687, 'and after the active section has rendered');
});

test('repeated activation issues no duplicate in-flight requests', async () => {
  const h = mk({ '/api/projections': { defer: true }, '/api/odds': { defer: true } });
  await ready(h);

  h.click(tabBtn(h, 'projections'));
  h.click(tabBtn(h, 'projections'));
  h.run('ensureProjections()');
  await h.flush();

  assert.equal(h.count('/api/projections'), 1, 'one in-flight request per feed, not three');
  assert.equal(h.count('/api/odds'), 1);

  h.resolve('/api/projections', { status: 200, body: projPayload() });
  h.resolve('/api/odds', { status: 200, body: oddsPayload() });
  await h.flush(); await h.flush();
  assert.match(h.html('projections'), /Multi-Factor Projections/, 'the deferred render landed');
});

test('reopening after the refresh interval refetches and updates both feeds', async () => {
  let odds = oddsPayload('BetOnline');
  const h = mk({ '/api/odds': () => ({ status: 200, body: odds }) });
  await ready(h);
  h.click(tabBtn(h, 'projections'));
  await h.flush(); await h.flush();
  assert.match(h.html('projections'), /BetOnline/);

  odds = oddsPayload('Circa');
  await h.advance(5 * 60 * 1000 + 1000);
  h.click(tabBtn(h, 'spplus'));            // tab hidden...
  h.click(tabBtn(h, 'projections'));       // ...then reopened, now stale
  await h.flush(); await h.flush();

  assert.equal(h.count('/api/odds'), 2, 'the interval made the data due again');
  assert.match(h.html('projections'), /Circa/, 'the reopened tab shows the fresh feed');
  assert.doesNotMatch(h.html('projections'), /BetOnline/, 'not the stale one');
});

test('one feed failing renders the other, with a retryable error (not "No data")', async () => {
  let projOk = false;
  const h = mk({ '/api/projections': () => (projOk
    ? { status: 200, body: projPayload() }
    : { status: 500, body: {} }) });
  await ready(h);
  h.click(tabBtn(h, 'projections'));
  await h.flush(); await h.flush();

  assert.match(h.html('projections'), /Live Betting Lines/, 'the healthy feed still renders');
  assert.match(h.html('projections'), /Projections unavailable/, 'the failed feed is visible');
  assert.match(h.html('projections'), /retryFeed\('projections'\)/, 'and carries a retry control');
  assert.doesNotMatch(h.html('projections'), /No projection or odds data available/,
    'a failure must not masquerade as "no data"');

  projOk = true;
  h.run("retryFeed('projections')");
  await h.flush(); await h.flush();
  assert.match(h.html('projections'), /Multi-Factor Projections/, 'retry recovers the feed');
  assert.doesNotMatch(h.html('projections'), /unavailable/, 'and clears the error');
});

test('search re-renders only the visible section, debounced (687-team fixture)', async () => {
  const h = mk();
  await ready(h);

  // instrument the section renderers: which ones run, how often
  h.run(`window.__counts = {};
    for (const k of Object.keys(SECTION_RENDERERS)) {
      const f = SECTION_RENDERERS[k];
      SECTION_RENDERERS[k] = (l) => { window.__counts[k] = (window.__counts[k] || 0) + 1; return f(l); };
    }
    'ok'`);
  const before = h.run('JSON.parse(JSON.stringify(window.__counts))');
  assert.deepEqual(Object.keys(before), [], 'no section re-rendered by the instrumentation itself');

  // after the initial load only the ACTIVE section had been built
  const renderCountAfterLoad = h.run('window.__counts.spplus || 0');

  h.el('search').value = 'Team 01';
  h.fire(h.el('search'), 'input');
  await h.flush();
  assert.equal(h.rowsIn('spplus'), 687, 'the debounce window has not elapsed: no render yet');

  await h.advance(200);
  await h.flush();

  const counts = h.run('JSON.parse(JSON.stringify(window.__counts))');
  assert.equal(counts.spplus, renderCountAfterLoad + 1, 'the visible section re-rendered once');
  assert.deepEqual(Object.keys(counts).filter(k => k !== 'spplus'), [],
    'no hidden section was rebuilt by a keystroke');
  assert.equal(h.rowsIn('spplus'), 10, 'and it shows the filtered rows (Team 010-019)');
  assert.match(h.html('summary'), /Total Teams/, 'summary stays accurate');

  // a second keystroke inside the window collapses into one render
  h.el('search').value = 'Team 02';
  h.fire(h.el('search'), 'input');
  await h.advance(50);
  h.el('search').value = 'Team 03';
  h.fire(h.el('search'), 'input');
  await h.advance(200);
  await h.flush();
  const after = h.run('window.__counts.spplus');
  assert.equal(after, counts.spplus + 1, 'two keystrokes in the window produced ONE render');
  assert.equal(h.rowsIn('spplus'), 10, 'and the latest query won');
});

test('switching tabs renders that section with the current filter, never blank', async () => {
  const h = mk();
  await ready(h);
  h.el('search').value = 'Team 01';
  h.fire(h.el('search'), 'input');
  await h.advance(200);
  await h.flush();
  assert.equal(h.rowsIn('spplus'), 10);

  h.click(tabBtn(h, 'composite'));
  await h.flush();
  assert.ok(h.rowsIn('composite') > 0, 'the newly active section is not blank');
  assert.equal(h.rowsIn('composite'), 10, 'and it respects the live filter');

  h.el('search').value = '';
  h.fire(h.el('search'), 'input');
  await h.advance(200);
  await h.flush();
  assert.equal(h.rowsIn('composite'), 687, 'clearing the search restores the full table');
});

// ── §7 (QA remediation): the timer/visibility refresh must not call a removed function ──
test('the 5-minute refresh fires with no page error and does not wake a hidden pane', async () => {
  const h = mk();
  await ready(h);
  assert.equal(h.count('/api/analytics'), 1);

  await h.advance(5 * 60 * 1000 + 1000);   // the interval fires for real here
  await h.flush();

  assert.deepEqual(h.pageErrors, [],
    'the timer refresh threw -- reloadAnalytics() must call a function that exists');
  assert.equal(h.count('/api/analytics'), 2, 'the analytics data is re-read');
  assert.equal(h.count('/api/projections'), 0, 'a hidden Projections pane must not fetch');
  assert.equal(h.count('/api/odds'), 0);
});

test('visibilitychange refresh works, and only the active tab refreshes its feeds', async () => {
  const h = mk();
  await ready(h);

  h.setVisibility('visible');
  h.fireDocument('visibilitychange');
  await h.flush(); await h.flush();
  assert.deepEqual(h.pageErrors, [], 'visibilitychange threw');
  assert.equal(h.count('/api/analytics'), 2);
  assert.equal(h.count('/api/projections'), 0, 'still hidden: no feed request');

  // now make the Projections tab active and let its data go stale
  h.click(tabBtn(h, 'projections'));
  await h.flush(); await h.flush();
  const odds1 = h.count('/api/odds');
  const proj1 = h.count('/api/projections');

  await h.advance(5 * 60 * 1000 + 1000);
  await h.flush(); await h.flush();
  assert.deepEqual(h.pageErrors, []);
  assert.equal(h.count('/api/odds'), odds1 + 1, 'the active tab refreshes its feeds');
  assert.equal(h.count('/api/projections'), proj1 + 1);

  // and going back to a metric tab stops the feed refreshes again
  h.click(tabBtn(h, 'spplus'));
  await h.flush();
  const odds2 = h.count('/api/odds');
  await h.advance(5 * 60 * 1000 + 1000);
  await h.flush();
  assert.equal(h.count('/api/odds'), odds2, 'no feed traffic while another tab is active');
  assert.deepEqual(h.pageErrors, []);
});

test('the source badge reports real state instead of a stale odds count', async () => {
  const h = mk({ '/api/odds': { status: 500, body: {} } });
  await ready(h);
  assert.match(h.el('sourceBadge').textContent, /687 teams/, 'analytics count is reported');

  h.click(tabBtn(h, 'projections'));
  await h.flush(); await h.flush();
  const badge = h.el('sourceBadge').textContent;
  assert.match(badge, /odds unavailable/, `a failed feed must be visible in the badge: ${badge}`);
  assert.doesNotMatch(badge, /0 odds/, 'the badge must not claim an odds count it did not get');
});
