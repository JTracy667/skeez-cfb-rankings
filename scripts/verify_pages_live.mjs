// Execute the shipped page JS against the LIVE API and count real rendered rows.
// Parsing is not evidence (a ReferenceError mid-render still "parses"); row counts are.
//   node scripts/verify_pages_live.mjs
import { createHarness } from '../tests/js/harness.mjs';

const allHarnesses = [];

const fails = [];
const check = (label, ok, detail) => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${label}  ${detail}`);
  if (!ok) fails.push(label);
};

// ── Analytics: primary render, then the deferred Projections & Odds tab ──────────────────
{
  const h = createHarness({ page: 'analytics.html', live: true });
  allHarnesses.push(['analytics', h]);
  const t0 = Date.now();
  await h.start();
  for (let i = 0; i < 40 && h.rowsIn('spplus') === 0; i++) await h.advance(250);
  const rows = h.rowsIn('spplus');
  check('analytics: SP+ table renders', rows > 600, `${rows} rows in ${Date.now() - t0}ms`);
  check('analytics: startup asked for NO projections', h.count('/api/projections') === 0,
    `projections calls: ${h.count('/api/projections')}`);
  check('analytics: no page error text', !/Failed to load data/.test(h.html('loading')),
    `loading pane: ${h.html('loading').slice(0, 40) || '(hidden)'}`);

  const btn = h.doc.querySelectorAll('.tab-btn').find(b => b.dataset.tab === 'projections');
  h.click(btn);
  for (let i = 0; i < 60 && !/Betting Lines|Projections/.test(h.html('projections')); i++) {
    await h.advance(250);
  }
  const pane = h.html('projections');
  check('analytics: projections pane populated on activation',
    /Multi-Factor Projections/.test(pane), `${h.count('/api/projections')} projections call(s)`);
  check('analytics: odds table populated on activation', /odds-table/.test(pane),
    `${h.count('/api/odds')} odds call(s)`);
  check('analytics: no error state', !/unavailable|No projection or odds/.test(pane),
    `pane ${pane.length} bytes`);
}

// ── Schedule: week resolution + real matchup rows ───────────────────────────────────────
{
  const h = createHarness({ page: 'schedule.html', live: true });
  allHarnesses.push(['schedule', h]);
  await h.start();
  // matchups render as .matchup-card divs, not <tr> rows -- count the real marker.
  const cards = () => (h.html('matchupsContainer').match(/class="matchup-card"/g) || []).length;
  for (let i = 0; i < 60 && cards() === 0; i++) await h.advance(250);
  const opts = [...h.el('weekSelect').options].map(o => o.value);
  check('schedule: week dropdown populated', opts.length > 0, `${opts.length} weeks: ${opts.slice(0, 4)}...`);
  check('schedule: selected week is published', opts.includes(h.el('weekSelect').value),
    `selected ${h.el('weekSelect').value}`);
  check('schedule: matchup cards render', cards() > 0, `${cards()} cards`);
  const badge = h.el('weekBadge').textContent;
  check('schedule: week badge is populated', /Week . d+ . . d{4} Season . . d+ Games/.test(badge) || /Week/.test(badge),
    badge || '(empty)');
  check('schedule: no current-week call when none needed or exactly one when needed',
    h.count('/api/schedule/current-week') <= 1, `${h.count('/api/schedule/current-week')} call(s)`);

  // The stale-data notice must MATCH the API's staleness flag. An outage now serves the last good
  // slate instead of an empty page, so this indicator is the only thing separating "old data"
  // from "silently wrong data": it has to be wired, and it must not cry wolf when data is fresh.
  const _base = (typeof process !== 'undefined' && process.env && process.env.CFB_BASE_URL)
    || 'https://skeezcfb-rankings.com';
  let _apiStale = null;
  try {
    _apiStale = !!(await (await fetch(`${_base}/api/schedule/weeks`)).json()).stale;
  } catch (e) { _apiStale = null; }
  const _notice = h.el('dataNotice');
  const _shown = !!_notice && _notice.style.display !== 'none'
                 && (_notice.textContent || '').length > 0;
  check('schedule: stale-data notice matches the API staleness flag',
    _apiStale !== null && _shown === _apiStale,
    `api stale=${_apiStale}, notice shown=${_shown}`
      + (_shown ? `: ${(_notice.textContent || '').slice(0, 70)}` : ''));
}

// A page JS error that never reaches the network -- the ReferenceError class that a syntax
// check misses -- is a failure even when the markup looks plausible.
for (const [page, h] of allHarnesses) {
  const jsErrors = h.pageErrors.filter(e => !/Failed to load|Projections/.test(e));
  check(`${page}: no page JS exceptions`, jsErrors.length === 0, jsErrors.slice(0, 2).join(' | ') || 'clean');
}

console.log(fails.length ? `\n${fails.length} FAILED: ${fails.join('; ')}` : '\nALL LIVE PAGE CHECKS PASSED');
process.exit(fails.length ? 1 : 0);
