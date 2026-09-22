"""budget.py — standing API-usage meters (D1_CHECKLIST Phase 3.5).

ONE place that answers "what's our burn?" for every metered third-party API, and
the single decision point for threshold actions (80% alert / 95% pause).

Caps (Jeff-confirmed, authoritative — see D1_RISK_REGISTER.md §A and the
D1_CHECKLIST Phase 3.5 section):

    cfbd       30,000  calls      / month
    odds       20,000  credits    / month
    propline    5,000  requests   / day
    d1     50,000,000  rows written / month   (Workers PAID pool)
                 extra 2,000,000/day runaway guard, NOT a tier ceiling

Authoritative sources, in priority order
----------------------------------------
1. **Provider quota headers.** CFBD returns ``X-CallLimit-Remaining``; The Odds
   API returns ``x-requests-remaining`` / ``x-requests-used``; PropLine returns
   its own daily headers. These are ground truth and stateless — they survive a
   container recycle for free, so they win whenever present.
2. **Our own counters**, incremented at the call site.

DURABILITY — read this before "fixing" the file path
----------------------------------------------------
The Cloudflare container filesystem is EPHEMERAL: instances are recycled on
``sleepAfter``. A file-only ledger therefore silently RESETS and under-reports
the day's burn — the same Render-era assumption class that caused the 04:00 UTC
self-kill bug. So:

* ``data/budget_ledger.json`` is the **local/desktop mirror** (what the checklist
  asked for, and what local scripts like the backfill read).
* The **ledger of record is the D1 ``api_usage`` table**, flushed sparsely
  (``FLUSH_EVERY_S``) so the write cost stays negligible (~4 rows/flush).

Metering overhead is deliberately NOT metered (writing the ledger is itself a D1
write; counting it would make the meter feed itself).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------- configuration

CAPS: dict[str, dict] = {
    "cfbd":     {"period": "month", "limit": 30_000},
    "odds":     {"period": "month", "limit": 20_000},
    "propline": {"period": "day",   "limit": 5_000},
    "d1":       {"period": "month", "limit": 50_000_000, "day_guard": 2_000_000},
}

# Jeff's standing policy (D1_RISK_REGISTER A4): approaching a cap triggers a
# TIER-UPGRADE decision, not throttling. 80% = tell the group, 95% = stop calling
# that consumer and serve stale-but-honest data.
ALERT_PCT = 80.0
PAUSE_PCT = 95.0

FLUSH_EVERY_S = 600          # min seconds between D1 ledger flushes
LEDGER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "budget_ledger.json")

_local = threading.local()
_lock = threading.RLock()
_state: dict | None = None
_last_flush = 0.0
_paused: dict[str, str] = {}   # source -> reason (in-memory pause latch)


# ------------------------------------------------------------------- state I/O

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _blank() -> dict:
    return {"schema": 1, "day": {}, "month": {}, "paused": {}}


def _read_file() -> dict:
    try:
        with open(LEDGER_PATH, "r", encoding="utf-8") as fh:
            d = json.load(fh)
            if isinstance(d, dict) and d.get("schema") == 1:
                return d
    except Exception:
        pass
    return _blank()


def _write_file(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
        tmp = LEDGER_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, indent=1, sort_keys=True)
        os.replace(tmp, LEDGER_PATH)
    except Exception as e:  # noqa: BLE001 — never let metering break a request
        print(f"[budget] file mirror failed: {e}")


def _merge_into(dst: dict, src: dict) -> dict:
    """Merge a ledger from D1 into the in-memory one, taking the LARGER counter
    per (bucket, period, source) — counters only go up within a period, so max()
    is the correct reconcile after a recycle."""
    for bucket in ("day", "month"):
        for period, sources in (src.get(bucket) or {}).items():
            tgt = dst[bucket].setdefault(period, {})
            for source, rec in (sources or {}).items():
                cur = tgt.setdefault(source, {"calls": 0})
                cur["calls"] = max(int(cur.get("calls") or 0), int(rec.get("calls") or 0))
                for k in ("provider_remaining", "provider_limit", "provider_used"):
                    if rec.get(k) is not None:
                        cur[k] = rec[k]
    return dst


def state() -> dict:
    """In-memory ledger, hydrated once per process (file mirror + D1 of record)."""
    global _state
    if _state is None:
        with _lock:
            if _state is None:
                d = _read_file()
                try:
                    remote = _d1_load()
                    if remote:
                        d = _merge_into(d, remote)
                except Exception as e:  # noqa: BLE001
                    print(f"[budget] D1 hydrate skipped: {e}")
                _state = d
    return _state


# ------------------------------------------------------- D1 ledger of record
# Kept behind lazy imports: d1_store imports budget (to meter its own writes), so
# importing it at module scope would be a circular import.

def _d1_load() -> dict | None:
    import d1_store  # noqa: PLC0415
    rows = d1_store.query(
        "SELECT bucket, period, source, calls, provider_remaining, provider_limit, provider_used"
        " FROM api_usage WHERE bucket='day' OR bucket='month'")
    if not rows:
        return None
    out = _blank()
    for r in rows:
        b = r.get("bucket") or "day"
        p = (r.get("period") or "").strip()
        s = (r.get("source") or "").strip()
        if not p or not s or b not in ("day", "month"):
            continue
        out[b].setdefault(p, {})[s] = {
            "calls": r.get("calls") or 0,
            "provider_remaining": r.get("provider_remaining"),
            "provider_limit": r.get("provider_limit"),
            "provider_used": r.get("provider_used"),
        }
    return out


def _d1_write() -> None:
    """Flush the CURRENT period's counters to D1 (idempotent upsert; ≤2 rows/source)."""
    import d1_store  # noqa: PLC0415
    d = state()
    now = _now()
    day, month = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")
    rows = []
    for src in CAPS:
        for bucket, period in (("day", day), ("month", month)):
            rec = (d[bucket].get(period) or {}).get(src)
            if not rec:
                continue
            rows.append({
                "bucket": bucket,
                "period": period,
                "source": src,
                "calls": int(rec.get("calls") or 0),
                "provider_remaining": rec.get("provider_remaining"),
                "provider_limit": rec.get("provider_limit"),
                "provider_used": rec.get("provider_used"),
                "updated_at": now.isoformat(),
            })
    if rows:
        d1_store.upsert_api_usage(rows)


def flush(force: bool = False) -> bool:
    """Persist to D1 at most every FLUSH_EVERY_S. Returns True if a flush ran."""
    global _last_flush
    now = time.time()
    with _lock:
        if not force and (now - _last_flush) < FLUSH_EVERY_S:
            return False
        _last_flush = now
        _write_file(state())
        try:
            _d1_write()
        except Exception as e:  # noqa: BLE001
            print(f"[budget] D1 flush failed (file mirror kept): {e}")
            return False
    return True


# ------------------------------------------------------------------- recording

def record(source: str, n: int = 1) -> None:
    """Count `n` calls against `source` (day + month buckets)."""
    if n <= 0 or source not in CAPS:
        return
    with _lock:
        d = state()
        now = _now()
        for bucket, period in (("day", now.strftime("%Y-%m-%d")),
                               ("month", now.strftime("%Y-%m"))):
            rec = d[bucket].setdefault(period, {}).setdefault(source, {"calls": 0})
            rec["calls"] = int(rec.get("calls") or 0) + n


def note_headers(source: str, headers: dict) -> None:
    """Record provider-reported quota (authoritative — beats our own count).

    Recognised header names per source; silently ignores anything unknown so a
    provider renaming a header degrades to our counter instead of throwing.
    """
    if source not in CAPS or not headers:
        return
    h = {str(k).lower(): v for k, v in headers.items()}

    def num(*names):
        for nm in names:
            v = h.get(nm)
            if v is None:
                continue
            try:
                return int(float(v))
            except (TypeError, ValueError):
                continue
        return None

    used = lim = rem = None
    if source == "cfbd":
        rem = num("x-calllimit-remaining")
        lim = num("x-calllimit")
        if rem is None and lim is None:
            return
    elif source == "odds":
        rem = num("x-requests-remaining")
        used = num("x-requests-used")
        lim = (rem + used) if (rem is not None and used is not None) else num("x-requests-limit")
    elif source == "propline":
        lim = num("x-daily-limit", "x-ratelimit-limit")
        rem = num("x-daily-remaining", "x-ratelimit-remaining")
        used = num("x-daily-used", "x-ratelimit-used")
    if rem is None and used is None and lim is None:
        return

    with _lock:
        d = state()
        now = _now()
        rec = d["month"].setdefault(now.strftime("%Y-%m"), {}).setdefault(source, {"calls": 0})
        # Provider numbers refer to its own window (monthly for cfbd/odds, daily
        # for propline) — record them against the matching bucket.
        bucket = "day" if CAPS[source]["period"] == "day" else "month"
        rec = d[bucket].setdefault(
            now.strftime("%Y-%m-%d") if bucket == "day" else now.strftime("%Y-%m"),
            {}).setdefault(source, {"calls": 0})
        for k, v in (("provider_remaining", rem), ("provider_limit", lim), ("provider_used", used)):
            if v is not None:
                rec[k] = v
        # If the provider tells us how much it has USED, that is better than our count.
        if used is not None:
            rec["calls"] = max(int(rec.get("calls") or 0), used)
        elif lim is not None and rem is not None:
            rec["calls"] = max(int(rec.get("calls") or 0), lim - rem)


def record_d1_rows(n: int) -> None:
    """Count D1 rows-written (already D1-confirmed by d1_store)."""
    if n <= 0:
        return
    record("d1", n)
    now = _now()
    with _lock:
        rec = state()["day"].setdefault(now.strftime("%Y-%m-%d"), {}).setdefault("d1", {"calls": 0})
        rec["calls"] = int(rec.get("calls") or 0) + 0  # day bucket already incremented above


# ------------------------------------------------------------------- reporting

def _used(source: str) -> tuple[int, str]:
    """(used, where-the-number-came-from) for the source's governing period."""
    d = state()
    now = _now()
    period = CAPS[source]["period"]
    key = now.strftime("%Y-%m-%d") if period == "day" else now.strftime("%Y-%m")
    rec = (d[period].get(key) or {}).get(source) or {}
    calls = int(rec.get("calls") or 0)
    if rec.get("provider_remaining") is not None and rec.get("provider_limit"):
        return calls, "provider-header"
    rem = rec.get("provider_remaining")
    if rem is not None and CAPS[source].get("limit"):
        return max(calls, CAPS[source]["limit"] - rem), "provider-header"
    return calls, "own-counter"


def status(source: str) -> dict:
    cap = CAPS[source]
    used, origin = _used(source)
    limit = cap["limit"]
    pct = (used / limit * 100.0) if limit else 0.0
    act = "pause" if pct >= PAUSE_PCT else ("alert" if pct >= ALERT_PCT else "ok")
    out = {"source": source, "period": cap["period"], "used": used, "limit": limit,
           "pct": round(pct, 2), "level": act, "origin": origin}
    if cap.get("day_guard"):
        d = state()
        rec = (d["day"].get(_now().strftime("%Y-%m-%d")) or {}).get(source) or {}
        out["day_guard"] = cap["day_guard"]
        out["day_used"] = int(rec.get("calls") or 0)
    return out


def burn_report() -> dict:
    """The whole answer to 'what's our burn?' — one call, one object."""
    return {
        "as_of": _now().isoformat(),
        "sources": {s: status(s) for s in CAPS},
        "alerts": [s for s in CAPS if status(s)["level"] == "alert"],
        "paused": [s for s in CAPS if status(s)["level"] == "pause"],
    }


def burn_line() -> str:
    """One-line form for logs/cron output."""
    r = burn_report()
    parts = []
    for s, v in r["sources"].items():
        parts.append(f"{s} {v['used']}/{v['limit']} ({v['pct']}%{'' if v['level']=='ok' else ' ' + v['level'].upper()})")
    return " | ".join(parts)


# ------------------------------------------------- degraded-mode decision point

def should_call(source: str) -> bool:
    """True unless this source is paused for budget (degraded-mode gate).

    Checked at the call site so an exhausted API degrades to D1/local data with a
    staleness flag instead of hard-failing the refresh.
    """
    if source not in CAPS:
        return True
    st = status(source)
    if st["level"] == "pause":
        _paused[source] = f"{st['pct']}% of {st['period']} cap ({st['used']}/{st['limit']})"
        return False
    return True


def paused_sources() -> dict[str, str]:
    """Sources currently latched paused, with the reason (for staleness flags)."""
    with _lock:
        return {s: _paused[s] for s in list(_paused) if not should_call_quiet(s)}


def should_call_quiet(source: str) -> bool:
    return status(source)["level"] != "pause"
