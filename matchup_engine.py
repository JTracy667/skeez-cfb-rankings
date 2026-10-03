"""Pure CFB matchup engine -- shared by the site and the local card renderer.

Extracted verbatim from app.py so there is exactly ONE implementation. The site calls
this; scripts/matchup_report.py calls this with team records loaded straight from D1.

Everything here is PURE: give it team records and it returns the breakdown. No I/O, no
network, no caching -- which is what makes it safe to call from a local renderer that
must never touch an API.

The source of the records is the CALLER's problem, deliberately: the site passes the
served board, the card renderer passes D1 rows. Ranking pools are computed from whatever
list of teams it is handed.
"""

import re
import unicodedata

# Built once, then cached. Reset by tests.
_FBS_MATCH: tuple[set, set] | None = None


def _team_universe_from_d1() -> tuple[set, set] | None:
    """(FBS name variants, ALL known-team variants) straight from D1 `teams`.

    WHY THIS EXISTS: this module used to reach CFBD /teams (via cfbd_shared) to build the
    FBS name set -- a LIVE API CALL sitting inside the matchup path. D1 already carries
    every team with its classification, so the render path stays network-free. Returns
    None when D1 is unavailable so the caller can fall back.
    """
    try:
        import d1_store  # noqa: PLC0415 -- optional: tests run without D1
        rows = d1_store.query("SELECT name, abbr, classification FROM teams")
    except Exception:  # noqa: BLE001
        return None
    if not rows:
        return None
    fbs: set = set()
    known: set = set()
    for r in rows:
        variants: set = set()
        for nm in (r.get("name"), r.get("abbr")):
            variants |= _name_variants(nm)
        known |= variants
        if str(r.get("classification") or "").lower() == "fbs":
            fbs |= variants
    return (fbs, known)


def _norm_team_name(s: str | None) -> str:
    """Aggressive normalization for matching feed names against CFBD team names.

    The odds feed and CFBD spell the same school differently: the feed says "Ohio St.",
    "Pittsburgh Panthers", "San Jose State"; CFBD says "Ohio State", "Pittsburgh",
    "San Jos\u00e9 State". Folding accents, punctuation and a few noise words is what makes
    those line up. Do NOT loosen this into fuzzy matching -- a wrong match here would
    silently exclude a real FBS game from its own line data.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    parts = [p for p in s.split() if p and p not in ("university", "univ", "college", "the")]
    return "".join(parts)


def _name_variants(name: str | None) -> set[str]:
    """Normalized spellings a feed might plausibly use for one team name."""
    n = _norm_team_name(name)
    if not n:
        return set()
    out = {n}
    if "state" in n:                      # "Ohio State" -> "ohiost"
        out.add(n.replace("state", "st"))
    return {x for x in out if x}


def _fbs_match_set() -> tuple[set, set]:
    """(FBS name variants, ALL known-team name variants). Built once, then cached.

    FBS membership comes from CFBD's own classification field, so this is the same
    universe the site models -- no hand-kept team list to drift.
    """
    global _FBS_MATCH
    if _FBS_MATCH is None:
        # D1 FIRST: no API call in the matchup path.
        from_d1 = _team_universe_from_d1()
        if from_d1 and from_d1[0]:
            _FBS_MATCH = from_d1
            return _FBS_MATCH
        fbs: set = set()
        known: set = set()
        try:
            teams = list((cfbd_shared.teams_by_name() or {}).values())
            fbs_ids = {t.get("id") for t in teams
                       if str(t.get("classification", "")).lower() == "fbs"}
            for t in teams:
                names = [t.get("school")] + list(t.get("alternateNames") or [])
                vars_ = set()
                for nm in names:
                    vars_ |= _name_variants(nm)
                known |= vars_
                if t.get("id") in fbs_ids:
                    fbs |= vars_
            # Aliases resolve some feed spellings that `school`/alternateNames do not
            # ("Southeastern Louisiana" vs "SE Louisiana").
            try:
                for alias, tid in (cfbd_shared.team_aliases() or {}).items():
                    vars_ = _name_variants(alias)
                    known |= vars_
                    if tid in fbs_ids:
                        fbs |= vars_
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            print(f"[Odds] FBS name set unavailable ({e}) -- not filtering events")
        _FBS_MATCH = (fbs, known)
    return _FBS_MATCH


MATCHUP_SECTIONS = (
    ("epa", "EPA & EFFICIENCY"),
    ("trench", "TRENCH & HAVOC"),
    ("drives", "QUALITY DRIVES & FINISHING"),
    ("situational", "SITUATIONAL DOWNS"),
    ("field", "FIELD POSITION & SPECIAL TEAMS"),
)


MATCHUP_METRICS = (
    # EPA / PPA (from CFBD /ppa/teams). `def_epa_*` is EPA ALLOWED, so lower is
    # better -- confirmed against the payload (Ohio State -0.08, Georgia -0.14,
    # Washington -0.15 vs USC +0.19 for a defense ranked #119 by PTS/OPP).
    ("epa_play", "OFF EPA/PLAY", "epa", +1, 3),
    ("def_epa_play", "DEF EPA/PLAY ALWD", "epa", -1, 3),
    ("epa_pass", "OFF EPA/DROPBACK", "epa", +1, 3),
    ("def_epa_pass", "DEF EPA/DROPBACK ALWD", "epa", -1, 3),
    ("epa_rush", "OFF EPA/RUSH", "epa", +1, 3),
    ("def_epa_rush", "DEF EPA/RUSH ALWD", "epa", -1, 3),
    ("off_success_rate", "OFF SUCCESS RATE", "trench", +1, 4),
    ("off_explosiveness", "OFF EXPLOSIVENESS", "trench", +1, 3),
    ("off_line_yards", "OFF LINE YARDS", "trench", +1, 2),
    ("off_stuff_rate", "OFF STUFF RATE", "trench", -1, 3),
    ("off_rush_success", "OFF RUSH SUCCESS", "trench", +1, 4),
    ("off_pass_success", "OFF PASS SUCCESS", "trench", +1, 4),
    ("def_havoc_total", "DEF HAVOC RATE", "trench", +1, 4),
    ("def_havoc_front_seven", "DEF HAVOC FRONT-7", "trench", +1, 4),
    ("def_havoc_db", "DEF HAVOC DB", "trench", +1, 4),
    ("def_stuff_rate", "DEF STUFF RATE", "trench", +1, 3),
    ("def_line_yards", "DEF LINE YARDS ALWD", "trench", -1, 2),
    ("def_rush_success", "DEF RUSH SR ALWD", "trench", -1, 4),
    ("def_pass_success", "DEF PASS SR ALWD", "trench", -1, 4),
    ("off_eckel_rate", "ECKEL RATE", "drives", +1, 4),
    ("eckel_ratio", "ECKEL RATIO", "drives", +1, 4),
    ("off_ppo", "OFF PTS/OPP", "drives", +1, 2),
    ("def_ppo", "DEF PTS/OPP ALWD", "drives", -1, 2),
    ("pts_per_poss", "OFF PTS/DRIVE", "drives", +1, 2),
    ("def_pts_per_poss", "DEF PTS/DRIVE ALWD", "drives", -1, 2),
    ("off_standard_down_success", "OFF STD DOWN SR", "situational", +1, 4),
    ("off_passing_down_success", "OFF PASS DOWN SR", "situational", +1, 4),
    ("def_standard_down_success", "DEF STD DOWN SR ALWD", "situational", -1, 4),
    ("def_passing_down_success", "DEF PASS DOWN SR ALWD", "situational", -1, 4),
    ("net_field_pos", "NET FIELD POSITION", "field", +1, 2),
    ("sp_special_teams", "SP+ SPECIAL TEAMS", "field", +1, 1),
    ("fpi_eff_special_teams", "FPI SPECIAL TEAMS", "field", +1, 1),
    ("fpi_sor", "STRENGTH OF RECORD", "field", -1, 0),
    ("fpi_sos", "STRENGTH OF SCHEDULE", "field", -1, 0),
)


MATCHUP_EDGES = (
    ("EPA/PLAY vs EPA/PLAY ALWD", "epa_play", "def_epa_play", +1, 3),
    ("EPA/DROPBACK vs EPA/DROPBACK ALWD", "epa_pass", "def_epa_pass", +1, 3),
    ("EPA/RUSH vs EPA/RUSH ALWD", "epa_rush", "def_epa_rush", +1, 3),
    ("PASS OFFENSE vs PASS DEFENSE", "off_pass_success", "def_pass_success", +1, 4),
    ("RUSH OFFENSE vs RUSH DEFENSE", "off_rush_success", "def_rush_success", +1, 4),
    ("O-LINE YARDS vs D-LINE YARDS ALWD", "off_line_yards", "def_line_yards", +1, 2),
    # LIKE-FOR-LIKE ONLY. The old pairing here was off_explosiveness vs
    # def_havoc_total -- two different scales, so the "leader" was decided by which
    # metric's numbers happen to be bigger, not by either team (2026-10-02).
    ("EXPLOSIVENESS vs EXPLOSIVENESS ALWD", "off_explosiveness", "def_explosiveness", +1, 3),
    ("PTS/OPP vs PTS/OPP ALWD", "off_ppo", "def_ppo", +1, 2),
)


def resolve_team(query: str, teams: list):
    """Resolve a user-typed team to one analytics record.

    Returns (record, None) or (None, reason). Order: exact name, mascot, then a
    unique substring ('Oregon' matches 'Oregon'; 'Miami' is ambiguous and says so
    rather than silently picking one).
    """
    q = (query or "").strip().lower()
    if not q:
        return None, "no team given"
    for t in teams:
        if (t.get("name") or "").lower() == q:
            return t, None
    for t in teams:
        if (t.get("mascot") or "").lower() == q:
            return t, None
    hits = [t for t in teams if q in (t.get("name") or "").lower()]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, "ambiguous %r: %s" % (query, ", ".join(sorted(t["name"] for t in hits)[:8]))
    return None, "no FBS team matches %r" % query


def _matchup_rank_pool(teams: list) -> list:
    """FBS-only ranking pool.

    The analytics payload carries ~685 records (FBS + FCS), but the plan specifies
    national ranks across FBS (1-138). Ranking the whole payload produced nonsense
    like '#187 of 685' on a defensive rate, which reads as a real rank and is not.
    Membership comes from the site's canonical FBS matcher (CFBD's own
    classification field) so there is no second team list to drift.
    """
    try:
        fbs, _known = _fbs_match_set()
    except Exception:  # noqa: BLE001
        return teams
    pool = [t for t in teams if _name_variants(t.get("name")) & fbs]
    # Defensive: a matcher hiccup must not silently empty every rank badge.
    if len(pool) < 100:
        return teams
    return pool


def build_matchup_rankings(teams: list) -> dict:
    """National rank map per metric, best = 1, direction set by polarity.

    Ranks are computed over the FBS pool (see _matchup_rank_pool). Built once per
    request and shared by every row so a matchup costs one pass per metric rather
    than one pass per row.
    """
    pool = _matchup_rank_pool(teams)
    ctx = {}
    for key, _label, _section, polarity, _dec in MATCHUP_METRICS:
        vals = [(t.get("name"), float(v))
                for t in pool
                for v in (t.get(key),)
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        vals.sort(key=lambda nv: nv[1], reverse=(polarity > 0))
        ctx[key] = {"rank": {n: i + 1 for i, (n, _) in enumerate(vals)}, "n": len(vals)}
    return ctx


def _side_stat(rec: dict, key: str, ctx: dict, polarity: int) -> dict:
    v = rec.get(key)
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return {"value": None, "rank": None, "pct": None}
    r = (ctx.get(key) or {}).get("rank", {}).get(rec.get("name"))
    n = (ctx.get(key) or {}).get("n") or 0
    pct = round((1 - (r - 1) / n) * 100) if (r and n) else None
    return {"value": v, "rank": r, "pct": pct}


def _leader(home_val, away_val, polarity: int):
    """Which side holds the edge on a metric. Positive-adjusted away minus home."""
    if home_val is None or away_val is None:
        return None, None
    edge = round((away_val - home_val) * polarity, 4)
    return edge, ("away" if edge > 0 else ("home" if edge < 0 else "even"))


def matchup_breakdown(home_query: str, away_query: str, teams: list = None, ctx: dict = None) -> dict:
    """Full advanced-stat comparison for one game. Pure function -- no I/O."""
    teams = teams if teams is not None else _matchup_teams()
    if not teams:
        return {"error": "analytics data unavailable"}
    ctx = ctx if ctx is not None else build_matchup_rankings(teams)
    home, err_h = resolve_team(home_query, teams)
    away, err_a = resolve_team(away_query, teams)
    if err_h:
        return {"error": "home: %s" % err_h}
    if err_a:
        return {"error": "away: %s" % err_a}

    sections = []
    for sec_key, sec_label in MATCHUP_SECTIONS:
        rows = []
        for key, label, section, polarity, dec in MATCHUP_METRICS:
            if section != sec_key:
                continue
            h = _side_stat(home, key, ctx, polarity)
            a = _side_stat(away, key, ctx, polarity)
            edge, leader = _leader(h["value"], a["value"], polarity)
            rows.append({"key": key, "label": label, "polarity": polarity, "decimals": dec,
                         "home": h, "away": a, "edge": edge, "leader": leader})
        wins = {"home": sum(1 for r in rows if r["leader"] == "home"),
                "away": sum(1 for r in rows if r["leader"] == "away")}
        sections.append({"key": sec_key, "label": sec_label, "rows": rows, "wins": wins})

    # cross-side edges, both directions
    edges = []
    for label, off_key, def_key, polarity, dec in MATCHUP_EDGES:
        for off_rec, def_rec, direction in ((away, home, "away_o_vs_home_d"),
                                            (home, away, "home_o_vs_away_d")):
            o = _side_stat(off_rec, off_key, ctx, polarity)
            d = _side_stat(def_rec, def_key, ctx, polarity)
            if direction == "away_o_vs_home_d":
                h_side, a_side = d, o          # home's defense vs away's offense
            else:
                h_side, a_side = o, d          # home's offense vs away's defense
            edge, leader = _leader(h_side["value"], a_side["value"], polarity)
            edges.append({"direction": direction, "label": label, "polarity": polarity,
                          "decimals": dec, "offense_key": off_key, "defense_key": def_key,
                          "home": h_side, "away": a_side, "edge": edge, "leader": leader})

    return {
        "home": {"name": home.get("name"), "mascot": home.get("mascot"),
                 "conf": home.get("conf"), "composite": home.get("composite")},
        "away": {"name": away.get("name"), "mascot": away.get("mascot"),
                 "conf": away.get("conf"), "composite": away.get("composite")},
        "sections": sections,
        "edges": edges,
        "field_size": len(_matchup_rank_pool(teams)),
    }
