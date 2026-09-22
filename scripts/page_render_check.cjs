#!/usr/bin/env node
/**
 * page_render_check.cjs — prove the public pages actually RENDER, not just 200.
 *
 * A 200 means the server answered; it does NOT mean the page works. A page can
 * ship, return 200, and throw in its own inline JS (often masked by a try/catch
 * as "server unreachable"), leaving the table empty forever.
 *
 * This harness runs each page's real inline script in a Node VM against the LIVE
 * API, lets the async fetches settle, records every innerHTML write, and counts
 * rendered <tr> / card nodes.
 *
 * Usage: node scripts/page_render_check.cjs [baseUrl]
 */
'use strict';
const vm = require('vm');

const BASE = process.argv[2] || 'https://skeezcfb-rankings.com';
const PAGES = ['/', '/analytics', '/schedule', '/win-totals'];
const SETTLE_MS = Number(process.env.SETTLE_MS || 6000);

const allWrites = []; // { key, html }
const asyncErrors = []; // rejections from the page's own promise chains
process.on('unhandledRejection', (e) => {
  asyncErrors.push(`${(e && e.name) || 'Error'}: ${(e && e.message) || String(e)}`);
});

function makeEl(key, tag) {
  const rec = { key, tag: (tag || 'div').toUpperCase(), writes: [], text: '' };
  const el = {
    tagName: rec.tag,
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    children: [],
    _rec: rec,
  };
  return new Proxy(el, {
    get(t, k) {
      if (k === 'innerHTML') return rec.writes.length ? rec.writes[rec.writes.length - 1] : '';
      if (k === 'textContent') return rec.text;
      if (k === 'outerHTML') return rec.writes.length ? rec.writes[rec.writes.length - 1] : '';
      if (k in t) return t[k];
      if (typeof k === 'symbol') return undefined;
      // Collection/primitive-shaped members must not degrade into a function,
      // or page code that iterates them ([...sel.options]) throws.
      if (k === 'options' || k === 'children' || k === 'files' || k === 'selectedOptions'
          || k === 'childNodes' || k === 'rows' || k === 'cells') return [];
      if (k === 'value' || k === 'id' || k === 'className' || k === 'name') return '';
      if (k === 'checked' || k === 'disabled' || k === 'selected' || k === 'hidden') return false;
      if (k === 'length') return 0;
      if (k === 'selectedIndex') return -1;
      return (...args) => (args[0] && typeof args[0] === 'object' ? args[0] : undefined);
    },
    set(t, k, v) {
      if (k === 'innerHTML' || k === 'outerHTML') {
        const s = String(v == null ? '' : v);
        rec.writes.push(s);
        allWrites.push({ key, html: s });
        return true;
      }
      if (k === 'textContent') { rec.text = String(v == null ? '' : v); return true; }
      t[k] = v;
      return true;
    },
  });
}

function buildSandbox(base) {
  const cache = new Map();
  const keyed = (key, tag) => {
    if (!cache.has(key)) cache.set(key, makeEl(key, tag));
    return cache.get(key);
  };
  const document = {
    getElementById: (id) => (id ? keyed('#' + id, 'div') : null),
    querySelector: (s) => keyed('q:' + s, 'div'),
    querySelectorAll: () => [],
    getElementsByClassName: () => [],
    getElementsByTagName: () => [],
    createElement: (t) => makeEl('created:' + t, t),
    createDocumentFragment: () => makeEl('fragment', 'fragment'),
    createTextNode: (t) => keyed('text:' + String(t).slice(0, 20), 'text'),
    addEventListener() {},
    body: keyed('__body', 'body'),
    head: keyed('__head', 'head'),
    documentElement: keyed('__html', 'html'),
    title: '', readyState: 'complete', cookie: '', hidden: false,
  };
  const errors = [];
  const guard = (name) => (fn, ...a) => {
    if (typeof fn !== 'function') return 0;
    try { return fn(...a); } catch (e) { errors.push(`${name}: ${e.name}: ${e.message}`); return 0; }
  };
  const sandbox = {
    console: { log() {}, warn() {}, error() {}, info() {}, debug() {}, trace() {} },
    document,
    navigator: { userAgent: 'render-check', language: 'en-US', onLine: true },
    location: { href: base + '/', origin: base, pathname: '/', search: '', hash: '' },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {}, clear() {} },
    sessionStorage: { getItem: () => null, setItem() {}, removeItem() {}, clear() {} },
    fetch: (url, opts) => {
      const u = /^https?:/i.test(String(url)) ? String(url) : base + (String(url).startsWith('/') ? url : '/' + url);
      return fetch(u, opts);
    },
    setTimeout: (fn, ms) => setTimeout(guard('setTimeout')(fn), Math.min(Number(ms) || 0, 2000)),
    clearTimeout: (t) => clearTimeout(t),
    setInterval: () => 0,
    clearInterval() {},
    requestAnimationFrame: (fn) => setTimeout(guard('rAF')(fn), 0),
    cancelAnimationFrame() {},
    AbortController: globalThis.AbortController,
    Promise, JSON, Math, Date, Number, String, Object, Array, Boolean, RegExp, Error,
    TypeError, Map, Set, Symbol, Proxy, Reflect, isNaN, isFinite, parseInt, parseFloat,
    encodeURIComponent, decodeURIComponent, Intl, URL, URLSearchParams,
    TextEncoder, TextDecoder, structuredClone, queueMicrotask,
  };
  sandbox.window = sandbox;
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  return { sandbox, errors };
}

async function run(html, base) {
  allWrites.length = 0;
  asyncErrors.length = 0;
  const { sandbox, errors } = buildSandbox(base);
  const ctx = vm.createContext(sandbox);
  const scripts = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)].map((m) => m[1]);
  scripts.forEach((code, i) => {
    if (!code.trim()) return;
    try {
      vm.runInContext(code, ctx, { timeout: 20000, filename: `block ${i + 1} (${code.length}b)` });
    } catch (e) {
      errors.push(`block ${i + 1} (${code.length}b): ${e.name}: ${e.message}`);
    }
  });
  // let the page's fetch(...).then(render) chain actually run
  const deadline = Date.now() + SETTLE_MS;
  let seen = -1;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 250));
    if (allWrites.length && allWrites.length === seen) break;
    seen = allWrites.length;
  }
  return { scripts: scripts.length, errors: errors.concat(asyncErrors), writes: allWrites.slice() };
}

const countTr = (s) => (s.match(/<tr[\s>]/gi) || []).length;
const countCards = (s) => (s.match(/class="[^"]*\b(card|game-card|schedule-card|matchup)\b/gi) || []).length;

(async () => {
  let failures = 0;
  for (const p of PAGES) {
    const url = BASE + p;
    let res, html;
    try {
      res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } });
      html = await res.text();
    } catch (e) {
      console.log(`FAIL ${p.padEnd(12)} FETCH ERROR ${e.message}`);
      failures++;
      continue;
    }
    const r = await run(html, BASE);

    // Cloudflare injects its own script into every response; its failure inside
    // this sandbox is not the site's code.
    const siteErrors = r.errors.filter((e) => !/b\.createElement is not a function/.test(e));
    const injected = r.errors.filter((e) => /b\.createElement is not a function/.test(e));

    let best = { key: '(none)', rows: 0, cards: 0, bytes: 0 };
    for (const w of r.writes) {
      const rows = countTr(w.html);
      const cards = countCards(w.html);
      if (rows + cards > best.rows + best.cards) best = { key: w.key, rows, cards, bytes: w.html.length };
    }
    const rendered = best.rows + best.cards;
    const ok = res.status === 200 && rendered > 0 && siteErrors.length === 0;
    if (!ok) failures++;
    console.log(
      `${ok ? 'OK  ' : 'FAIL'} ${p.padEnd(12)} http=${res.status} scripts=${r.scripts} ` +
      `writes=${r.writes.length} best=${best.key} rows=${best.rows} cards=${best.cards} (${best.bytes}b)`,
    );
    for (const e of siteErrors.slice(0, 3)) console.log(`       SITE-ERROR: ${e.slice(0, 160)}`);
    if (injected.length) console.log(`       (ignored: Cloudflare-injected block, ${injected.length})`);
  }
  console.log(failures === 0 ? 'RENDER CHECK PASS' : `RENDER CHECK FAIL (${failures} page(s))`);
  process.exit(failures === 0 ? 0 : 1);
})();