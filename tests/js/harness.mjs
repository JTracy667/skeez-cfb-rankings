// Minimal DOM + fetch harness for the page scripts. NO jsdom (QA ruling Q3): the page's own
// <script> block is executed in a node:vm context against a stub built from the real markup,
// so the tests exercise the shipped code, not a copy of it.
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';

const REPO = path.resolve(import.meta.dirname, '..', '..');

class ClassList {
  constructor(el) { this.el = el; }
  _set() { return new Set((this.el._class || '').split(/\s+/).filter(Boolean)); }
  _write(s) { this.el._class = [...s].join(' '); }
  add(c) { const s = this._set(); s.add(c); this._write(s); }
  remove(c) { const s = this._set(); s.delete(c); this._write(s); }
  contains(c) { return this._set().has(c); }
}

function makeElement(tag, attrs = {}) {
  const el = {
    tagName: tag.toUpperCase(),
    id: attrs.id || undefined,
    _class: attrs.class || '',
    dataset: {},
    style: {},
    innerHTML: '',
    textContent: '',
    children: [],
    _listeners: {},
    get classList() { return this._cl || (this._cl = new ClassList(this)); },
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    appendChild(child) { this.children.push(child); return child; },
    querySelectorAll() { return []; },
    querySelector() { return null; },
  };
  if (attrs.datatab) el.dataset.tab = attrs.datatab;
  // A real DOM stringifies .value (option/select/input), and the page relies on it
  // (`opts.includes(String(currentWeek))`). Faking that faithfully is the difference between
  // testing the page and testing the stub.
  let _html = '';
  Object.defineProperty(el, 'innerHTML', {
    get() { return _html; },
    set(v) { _html = v === undefined || v === null ? '' : String(v); el.children.length = 0; },
  });
  let _value = '';
  Object.defineProperty(el, 'value', {
    get() { return _value; },
    set(v) { _value = (v === undefined || v === null) ? '' : String(v); },
  });
  // AFTER the accessor exists -- assigning before it would be shadowed by the defineProperty
  // and every static option would read as value '' (which hid QA's §8 placeholder).
  if (attrs.value !== undefined) el.value = attrs.value;
  Object.defineProperty(el, 'className', {
    get() { return el._class; }, set(v) { el._class = v; },
  });
  return el;
}

// ── build the id/class registry from the real markup ────────────────────────────────────
export function buildDom(html) {
  const byId = new Map();
  const all = [];
  // Strip <script> bodies FIRST: the page's JS contains markup in template strings
  // ('<div class="proj-card">', the retry <button class="tab-btn">), which a tag scan
  // otherwise registers as real elements -- including a phantom .tab-btn.
  const markup = html.replace(/<script[\s\S]*?<\/script>/gi, '');
  const stack = [];
  const tagRe = /<(\/?)(\w+)([^>]*)>/g;
  let m;
  while ((m = tagRe.exec(markup)) !== null) {
    const closing = m[1] === '/';
    const tag = m[2];
    const attrs = m[3];
    if (/^(script|style)$/i.test(tag)) continue;
    const voidTag = /\/$/.test(m[0]) || /^(img|br|input|hr|meta|link)$/i.test(tag);
    if (closing) { stack.pop(); continue; }
    const id = (attrs.match(/\bid="([^"]*)"/) || [])[1];
    const cls = (attrs.match(/\bclass="([^"]*)"/) || [])[1] || '';
    const datatab = (attrs.match(/\bdata-tab="([^"]*)"/) || [])[1];
    const value = (attrs.match(/value="([^"]*)"/) || [])[1];
    const el = makeElement(tag, { id, class: cls, datatab, value });
    if (tag.toLowerCase() === 'select') {
      Object.defineProperty(el, 'options', { get: () => el.children });
    }
    // Nesting matters: the pages use delegated listeners on containers (#tabs), so an event
    // must bubble. Every element joins the stack, even unregistered ones, to keep it aligned.
    el._parent = stack[stack.length - 1] || null;
    if (el._parent) el._parent.children.push(el);
    if (id || cls) {
      all.push(el);
      if (id) byId.set(id, el);
    }
    if (!voidTag) stack.push(el);
  }

  const qsa = (sel) => all.filter(el => {
    const cls = (el._class || '').split(/\s+/).filter(Boolean);
    const parts = sel.split('.').filter(Boolean);
    return parts.every(p => cls.includes(p));
  });

  const doc = {
    _byId: byId,
    _listeners: {},
    getElementById: (id) => byId.get(id) || null,
    querySelectorAll: qsa,
    querySelector: (sel) => qsa(sel)[0] || null,
    createElement: (tag) => makeElement(tag),
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    visibilityState: 'visible',
    body: makeElement('body'),
  };
  return { doc, all, byId };
}

// ── harness ─────────────────────────────────────────────────────────────────────────────
export function createHarness({ page, search = '', routes = {}, live = false }) {
  const html = fs.readFileSync(path.join(REPO, page), 'utf8');
  const script = html.match(/<script>([\s\S]*)<\/script>/)[1];
  const { doc, byId } = buildDom(html);

  const calls = [];
  const deferred = new Map();
  const timers = new Map();
  let timerSeq = 0;
  let fakeNow = 1_700_000_000_000;
  const intervals = [];
  const pageErrors = [];

  // Count BODY rows only: /<tr>/ also matches the <thead> row.
  const rowsIn = (id) => {
    const el = byId.get(id);
    return el ? (el.innerHTML.match(/<tr><td/g) || []).length : 0;
  };

  // live=true drives the page against the REAL API (relative /api/... URLs resolved
  // against the site) -- the 'execute it, don't just parse it' rule.
  const noteCall = (u, opts) => {
    const rec = { url: String(u), method: (opts && opts.method) || 'GET', t: fakeNow,
      dom: { contentVisible: byId.get('content') ? byId.get('content').style.display === 'block' : null,
             spplusRows: rowsIn('spplus'),
             projections: byId.get('projections') ? byId.get('projections').innerHTML.slice(0, 60) : null } };
    calls.push(rec);
    return rec;
  };
  const realFetch = live
    ? (u, o) => { noteCall(u, o); return globalThis.fetch(new URL(u, 'https://skeezcfb-rankings.com').href, o); }
    : null;

  const fetchStub = (url, opts = {}) => {
    const u = String(url);
    const rec = {
      url: u, method: (opts && opts.method) || 'GET', t: fakeNow,
      dom: {
        contentVisible: byId.get('content') ? byId.get('content').style.display === 'block' : null,
        activeSectionRows: rowsIn(byId.get('search') ? 'active' : 'nope'),
        spplusRows: rowsIn('spplus'),
        projections: byId.get('projections') ? byId.get('projections').innerHTML.slice(0, 60) : null,
      },
    };
    calls.push(rec);
    const key = Object.keys(routes).find(k => u.includes(k));
    const spec = typeof routes[key] === 'function' ? routes[key]() : (routes[key] || { status: 404, body: {} });
    if (spec.defer) {
      return new Promise((resolve, reject) => { deferred.set(u, { resolve, reject, rec }); });
    }
    if (spec.throw) { rec.failed = spec.throw; return Promise.reject(new Error(spec.throw)); }
    return Promise.resolve({
      ok: spec.status === 200,
      status: spec.status || 200,
      json: async () => spec.body || {},
    });
  };

  const sandbox = {
    document: doc,
    location: { search, href: `http://local/${page}${search}` },
    URLSearchParams,
    fetch: live ? realFetch : fetchStub,
    console: live
      ? { log: (...a) => console.log('[page]', ...a),
          warn: (...a) => console.warn('[page]', ...a),
          error: (...a) => { pageErrors.push(a.map(String).join(' ')); console.error('[page]', ...a); } }
      : { log: () => {}, warn: () => {}, error: () => {} },
    setTimeout: live ? setTimeout : (fn, ms = 0) => { const id = ++timerSeq; timers.set(id, { fn, at: fakeNow + ms }); return id; },
    clearTimeout: live ? clearTimeout : (id) => { timers.delete(id); },
    setInterval: live ? setInterval
      : (fn, ms) => { const id = { fn, ms, nextAt: fakeNow + ms }; intervals.push(id); return id; },
    clearInterval: live ? clearInterval : () => {},
  };
  sandbox.window = sandbox;
  if (!live) {
    const realDate = Date;
    sandbox.Date = class extends realDate {
      constructor(...a) { super(...(a.length ? a : [fakeNow])); }
      static now() { return fakeNow; }
    };
  }

  const ctx = vm.createContext(sandbox);
  const run = (expr) => vm.runInContext(expr, ctx);
  const flush = async () => {
    await new Promise(r => setImmediate(r));
    await new Promise(r => setImmediate(r));
  };

  return {
    run, calls, byId, doc,
    el: (id) => byId.get(id),
    html: (id) => (byId.get(id) || {}).innerHTML || '',
    rowsIn,
    urls: () => calls.map(c => c.url),
    count: (substr) => calls.filter(c => c.url.includes(substr)).length,
    resolve(urlSubstr, spec) {
      for (const [u, d] of deferred) {
        if (u.includes(urlSubstr)) {
          deferred.delete(u);
          if (spec.throw) { d.reject(new Error(spec.throw)); return; }
          d.resolve({ ok: (spec.status || 200) === 200, status: spec.status || 200,
                      json: async () => spec.body || {} });
          return;
        }
      }
      throw new Error(`no deferred fetch matching ${urlSubstr}; pending: ${[...deferred.keys()]}`);
    },
    pending: () => [...deferred.keys()],
    async start() { vm.runInContext(script, ctx); await flush(); },
    fire(el, type, extra = {}) {
      // Bubbling: the pages delegate (one listener on #tabs for 15 buttons).
      let stopped = false;
      const ev = { target: el, preventDefault() {}, stopPropagation() { stopped = true; }, ...extra };
      for (let node = el; node && !stopped; node = node._parent) {
        (node._listeners[type] || []).forEach(fn => fn(ev));
      }
    },
    click(el) { this.fire(el, 'click'); },
    // Move the clock AND run whatever it makes due -- a debounce that never fires is a
    // test that never tests the debounce.
    async advance(ms) {
      if (live) { await new Promise(r => setTimeout(r, ms)); await flush(); return; }
      fakeNow += ms;
      for (let pass = 0; pass < 10; pass++) {
        const due = [...timers].filter(([, t]) => t.at <= fakeNow);
        const dueIntervals = intervals.filter(iv => iv.nextAt <= fakeNow);
        if (!due.length && !dueIntervals.length) break;
        due.forEach(([id]) => timers.delete(id));
        dueIntervals.forEach(iv => { iv.nextAt += iv.ms; });
        const fire = (fn) => {
          try {
            const r = fn();
            if (r && typeof r.catch === 'function') {
              r.catch(e => pageErrors.push(e && e.message ? e.message : String(e)));
            }
          } catch (e) { pageErrors.push(e && e.message ? e.message : String(e)); }
        };
        due.forEach(([, t]) => fire(t.fn));
        dueIntervals.forEach(iv => fire(iv.fn));
        await flush();
      }
      await flush();
    },
    flush,
    pageErrors,
    setVisibility(state) { doc.visibilityState = state; },
    fireDocument(type) {
      const ev = { target: doc, preventDefault() {}, stopPropagation() {} };
      try {
        (doc._listeners[type] || []).forEach(fn => {
          const r = fn(ev);
          if (r && typeof r.catch === 'function') {
            r.catch(e => pageErrors.push(e && e.message ? e.message : String(e)));
          }
        });
      } catch (e) { pageErrors.push(e && e.message ? e.message : String(e)); }
    },
    timersDue: () => [...timers.values()].filter(t => t.at <= fakeNow),
  };
}

// 687-team fixture (the live payload size) built from the real field names the page reads.
export function fixture(n = 687) {
  return Array.from({ length: n }, (_, i) => ({
    name: `Team ${String(i).padStart(3, '0')}`,
    team_id: 1000 + i,
    sp_plus: 30 - i * 0.03, fpi: 25 - i * 0.02, srs: 12 - i * 0.02, elo: 2000 - i,
    sp_rank: i + 1, fpi_rank: i + 1, recruiting_rank: i + 1,
    composite: 60 - i * 0.05, logo_url: '',
  }));
}
