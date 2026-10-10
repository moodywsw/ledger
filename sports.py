"""Mirko's sports predictions — PAPER ONLY. Free ESPN public JSON (scoreboard + summary).

Scope: only the big stuff — top-5 European leagues + Portugal (big clubs only), Champions League (all),
NBA regular season/playoffs (games with a top team), and UFC main/co-main events.
For each pick: main market (1X2 / moneyline / fight winner) + a few small markets (over/under, BTTS,
corners, cards, shots on target) with Mirko's probability, short reasoning and a paper stake.
Settles after the event from final scores + box-score stats; tracks hit rate and PnL.

`PAPER = True` and every bet carries {"mode": "paper"}. A real-money executor can later plug into
`place(bet)` — nothing real happens in this module.
"""
from __future__ import annotations

import json, os, threading, time, datetime as dt
from pathlib import Path

import requests

PAPER = True
BASE = "https://site.api.espn.com/apis/site/v2/sports"
UA = {"Accept": "application/json"}
START_EUR = 1000.0
MAIN_STAKE, SIDE_STAKE = 20.0, 5.0
SOCCER = {"uefa.champions": "Champions League", "eng.1": "Premier League", "esp.1": "LaLiga", "ita.1": "Serie A",
          "ger.1": "Bundesliga", "fra.1": "Ligue 1", "por.1": "Liga Portugal"}
BIG = {"Arsenal", "Manchester City", "Liverpool", "Chelsea", "Manchester United", "Tottenham Hotspur", "Newcastle United", "Aston Villa",
       "Real Madrid", "Barcelona", "Atlético Madrid", "Atletico Madrid", "Athletic Club", "Inter Milan", "Internazionale", "AC Milan", "Juventus", "Napoli",
       "AS Roma", "Atalanta", "Bayern Munich", "Borussia Dortmund", "Bayer Leverkusen", "RB Leipzig", "Paris Saint-Germain", "Marseille",
       "AS Monaco", "Lyon", "Benfica", "Porto", "FC Porto", "Sporting CP", "Braga", "SC Braga"}
NBA_TOP = {"Boston Celtics", "Oklahoma City Thunder", "Denver Nuggets", "New York Knicks", "Cleveland Cavaliers", "Los Angeles Lakers",
           "Golden State Warriors", "Minnesota Timberwolves", "Milwaukee Bucks", "Houston Rockets", "Dallas Mavericks", "Philadelphia 76ers"}


def _path() -> Path:
    return Path(os.environ.get("DATA_DIR", "data")) / "sports_book.json"


def _get(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=12)
    r.raise_for_status()
    return r.json()


def load() -> dict:
    try:
        return json.loads(_path().read_text())
    except Exception:
        return {"created": time.time(), "start": START_EUR, "cash": START_EUR, "bets": [], "settled": [], "mode": "paper"}


def save(b):
    b["settled"] = b["settled"][-400:]
    p = _path(); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(b)); tmp.replace(p)


def place(b: dict, bet: dict):
    """The single choke point for bets. Paper now; a real executor would hook in here (and must stay opt-in)."""
    if not PAPER:
        raise RuntimeError("real betting is not enabled")
    if b["cash"] < bet["stake"]:
        return
    bet.update(mode="paper", placed=time.time(), status="open")
    b["cash"] -= bet["stake"]; b["bets"].append(bet)


# ---------- probability helpers ----------
def ml_prob(ml) -> float | None:
    try:
        ml = float(ml)
    except Exception:
        return None
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def dec_odds(p: float, vig=0.05) -> float:
    return round(max(1.01, 1 / max(0.02, p * (1 + vig))), 2)


def rec_ppg(summary: str) -> float | None:
    try:
        w, d, l = [int(x) for x in summary.split("-")[:3]]
        n = w + d + l
        return (3 * w + d) / n if n else None
    except Exception:
        return None


def form_score(form: str | None) -> float:
    return sum({"W": 1, "D": 0, "L": -1}.get(c, 0) for c in (form or "")[-5:]) / 5


# ---------- soccer ----------
def soccer_pick(ev: dict, lg: str) -> dict | None:
    c = ev["competitions"][0]
    home = next(t for t in c["competitors"] if t["homeAway"] == "home"); away = next(t for t in c["competitors"] if t["homeAway"] == "away")
    hn, an = home["team"]["displayName"], away["team"]["displayName"]
    if lg != "uefa.champions" and not (hn in BIG or an in BIG):
        return None
    o = (c.get("odds") or [None])[0] or {}
    ph, pa, pd = ml_prob((o.get("homeTeamOdds") or {}).get("moneyLine")), ml_prob((o.get("awayTeamOdds") or {}).get("moneyLine")), ml_prob((o.get("drawOdds") or {}).get("moneyLine"))
    hr = rec_ppg(((home.get("records") or [{}])[0]).get("summary", "")); ar = rec_ppg(((away.get("records") or [{}])[0]).get("summary", ""))
    fh, fa = form_score(home.get("form")), form_score(away.get("form"))
    if ph and pa and pd:
        s = ph + pa + pd; ph, pa, pd = ph / s, pa / s, pd / s; src = "market"
    else:
        gap = ((hr or 1.4) - (ar or 1.4)) / 3 + 0.08   # home edge
        ph = max(0.12, min(0.8, 0.42 + gap)); pa = max(0.1, min(0.75, 0.30 - gap)); pd = max(0.15, 1 - ph - pa); src = "form"
    tilt = 0.04 * (fh - fa)           # Mirko's own read: recent form nudges the market a little
    ph, pa = max(0.03, ph + tilt), max(0.03, pa - tilt)
    s = ph + pa + pd; ph, pa, pd = ph / s, pa / s, pd / s
    opts = [("Home win", hn, ph), ("Draw", "Draw", pd), ("Away win", an, pa)]
    main = max(opts, key=lambda x: x[2])
    if main[2] < 0.45:   # no strong favourite: double chance on the better side
        dc = ("Home or draw", f"{hn} or draw", ph + pd) if ph >= pa else ("Away or draw", f"{an} or draw", pa + pd)
        main = dc
    ou = o.get("overUnder") or 2.5
    try:
        po = ml_prob(((o.get("total") or {}).get("over") or {}).get("close", {}).get("odds")) or 0.5
        pu = ml_prob(((o.get("total") or {}).get("under") or {}).get("close", {}).get("odds")) or 0.5
        po = po / (po + pu)
    except Exception:
        po = 0.5
    big_game = hn in BIG and an in BIG
    fav = hn if ph >= pa else an; fav_p = max(ph, pa)
    side = [
        {"market": f"{'Over' if po >= .5 else 'Under'} {ou} goals", "key": f"ou:{ou}:{'o' if po >= .5 else 'u'}", "p": round(max(po, 1 - po), 3),
         "why": "the goals line from the book, nudged by both teams' form"},
        {"market": "Both teams to score — " + ("Yes" if po >= .5 and min(ph, pa) > .2 else "No"), "key": "btts:" + ("y" if po >= .5 and min(ph, pa) > .2 else "n"),
         "p": round(0.55 if po >= .5 else 0.56, 3), "why": "open game expected" if po >= .5 else "one side should keep it tight"},
        {"market": "Over 3.5 yellow cards" if big_game else "Under 4.5 yellow cards", "key": "cards:o3.5" if big_game else "cards:u4.5",
         "p": 0.58 if big_game else 0.6, "why": "big-club clash: tackles fly and referees book early" if big_game else "no rivalry heat, discipline should hold"},
        {"market": "Over 8.5 corners", "key": "corners:o8.5", "p": 0.55 if fav_p > .55 else 0.52, "why": f"{fav} should camp in the final third" if fav_p > .55 else "two teams that attack wide"},
    ]
    if fav_p >= .6:
        side.append({"market": f"{fav} over 4.5 shots on target", "key": f"sot:{'h' if fav == hn else 'a'}:o4.5", "p": 0.55, "why": "heavy favourite, volume of chances"})
    if big_game:
        side.append({"market": "Special: a manager/coach to be booked", "key": "special:coach_card", "p": 0.18,
                     "why": "touchline temperature in big games; long shot, tiny stake", "unsettleable": True})
    return {"id": f"espn:{lg}:{ev['id']}", "sport": "soccer", "league": SOCCER[lg], "lg": lg, "event_id": ev["id"],
            "title": f"{hn} vs {an}", "kickoff": ev["date"], "main": {"market": main[0], "pick": main[1], "p": round(main[2], 3), "odds": dec_odds(main[2]),
            "why": f"{'book' if src == 'market' else 'records'} + form ({home.get('form') or '—'} vs {away.get('form') or '—'})"}, "side": side}


def nba_pick(ev: dict) -> dict | None:
    if (ev.get("season") or {}).get("type") not in (2, 3):
        return None   # regular season / playoffs only
    c = ev["competitions"][0]
    home = next(t for t in c["competitors"] if t["homeAway"] == "home"); away = next(t for t in c["competitors"] if t["homeAway"] == "away")
    hn, an = home["team"]["displayName"], away["team"]["displayName"]
    if not (hn in NBA_TOP or an in NBA_TOP):
        return None
    o = (c.get("odds") or [None])[0] or {}
    ph, pa = ml_prob((o.get("homeTeamOdds") or {}).get("moneyLine")), ml_prob((o.get("awayTeamOdds") or {}).get("moneyLine"))
    if ph and pa:
        ph = ph / (ph + pa)
    else:
        def wp(t):
            try:
                w, l = [int(x) for x in ((t.get("records") or [{}])[0]).get("summary", "0-0").split("-")[:2]]; return (w + 1) / (w + l + 2)
            except Exception:
                return .5
        a, b = wp(home), wp(away); ph = max(.1, min(.9, a / (a + b) + .03))
    pick = (hn, ph) if ph >= .5 else (an, 1 - ph)
    tot = o.get("overUnder")
    side = [{"market": f"Over {tot} points", "key": f"pts:o{tot}", "p": 0.52, "why": "pace of two good offences"}] if tot else []
    return {"id": f"espn:nba:{ev['id']}", "sport": "nba", "league": "NBA", "lg": "nba", "event_id": ev["id"], "title": f"{an} @ {hn}",
            "kickoff": ev["date"], "main": {"market": "Moneyline", "pick": pick[0], "p": round(pick[1], 3), "odds": dec_odds(pick[1]), "why": "book/record edge + home court"},
            "side": side}


def ufc_picks(ev: dict) -> list:
    out = []
    comps = sorted(ev.get("competitions") or [], key=lambda c: c.get("matchNumber") or 0)[-2:]   # main + co-main
    for c in comps:
        if c["status"]["type"]["name"] != "STATUS_SCHEDULED":
            continue
        fs = c["competitors"]
        def rec(x):
            try:
                w, l = [int(v) for v in ((x.get("records") or [{}])[0]).get("summary", "0-0").split("-")[:2]]; return w, l
            except Exception:
                return 0, 0
        (w1, l1), (w2, l2) = rec(fs[0]), rec(fs[1])
        s1, s2 = (w1 + 1) / (w1 + l1 + 2), (w2 + 1) / (w2 + l2 + 2)
        p1 = max(.25, min(.8, s1 / (s1 + s2)))
        n1, n2 = fs[0]["athlete"]["displayName"], fs[1]["athlete"]["displayName"]
        pick = (n1, p1, fs[0]["id"]) if p1 >= .5 else (n2, 1 - p1, fs[1]["id"])
        out.append({"id": f"espn:ufc:{ev['id']}:{c['id']}", "sport": "ufc", "league": "UFC", "lg": "ufc", "event_id": ev["id"], "comp_id": c["id"],
                    "title": f"{n1} vs {n2}", "kickoff": c.get("date") or ev["date"], "card": ev.get("name"),
                    "main": {"market": "Fight winner", "pick": pick[0], "athlete_id": pick[2], "p": round(pick[1], 3), "odds": dec_odds(pick[1]),
                             "why": f"records {w1}-{l1} vs {w2}-{l2}; experience edge"}, "side": [{"market": "Fight goes the distance — No", "key": "ufc:finish", "p": 0.55,
                                                                                                  "why": "main-event finishers", "unsettleable": True}]})
    return out


def scan(now: float | None = None) -> list:
    now = now or time.time()
    picks = []
    days = [(dt.datetime.utcfromtimestamp(now) + dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(3)]
    for lg in SOCCER:
        for d in days:
            try:
                for ev in _get(f"{BASE}/soccer/{lg}/scoreboard", {"dates": d}).get("events", []):
                    if ev["status"]["type"]["name"] == "STATUS_SCHEDULED":
                        p = soccer_pick(ev, lg)
                        if p: picks.append(p)
            except Exception:
                continue
    for d in days:
        try:
            for ev in _get(f"{BASE}/basketball/nba/scoreboard", {"dates": d}).get("events", []):
                if ev["status"]["type"]["name"] == "STATUS_SCHEDULED":
                    p = nba_pick(ev)
                    if p: picks.append(p)
        except Exception:
            pass
    try:
        for ev in _get(f"{BASE}/mma/ufc/scoreboard").get("events", []):
            picks += ufc_picks(ev)
    except Exception:
        pass
    seen, out = set(), []
    for p in picks:
        try:
            if dt.datetime.fromisoformat(p["kickoff"].replace("Z", "+00:00")).timestamp() < now + 900:
                continue   # never bet into a match that is about to start
        except Exception:
            continue
        if p["id"] not in seen:
            seen.add(p["id"]); out.append(p)
    return out


# ---------- settlement ----------
def _stat(team: dict, name: str) -> float | None:
    for s in team.get("statistics") or []:
        if s.get("name") == name:
            try:
                return float(s.get("displayValue"))
            except Exception:
                return None
    return None


def settle_soccer(bet: dict) -> bool:
    d = _get(f"{BASE}/soccer/{bet['lg']}/summary", {"event": bet["event_id"]})
    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
        return False
    cs = {c["homeAway"]: c for c in comp.get("competitors", [])}
    hg, ag = int(cs["home"].get("score") or 0), int(cs["away"].get("score") or 0)
    teams = {t.get("homeAway"): t for t in (d.get("boxscore") or {}).get("teams", [])}
    hn, an = cs["home"]["team"]["displayName"], cs["away"]["team"]["displayName"]
    res = "Home win" if hg > ag else "Away win" if ag > hg else "Draw"
    m = bet["main"]
    won = {"Home win": res == "Home win", "Away win": res == "Away win", "Draw": res == "Draw",
           "Home or draw": res != "Away win", "Away or draw": res != "Home win"}[m["market"]]
    m["result"] = "won" if won else "lost"
    tot = hg + ag
    for s in bet["side"]:
        k = s["key"]; r = None
        if k.startswith("ou:"):
            _, line, sd = k.split(":"); r = (tot > float(line)) == (sd == "o")
        elif k.startswith("btts:"):
            r = (hg > 0 and ag > 0) == k.endswith("y")
        elif k.startswith("cards:") or k.startswith("corners:"):
            nm = "yellowCards" if k.startswith("cards") else "wonCorners"
            vals = [_stat(teams.get(x, {}), nm) for x in ("home", "away")]
            if None not in vals:
                v = sum(vals); line = float(k.split(":")[1][1:]); r = v > line if ":o" in k else v < line
        elif k.startswith("sot:"):
            _, sd, line = k.split(":"); v = _stat(teams.get("home" if sd == "h" else "away", {}), "shotsOnTarget")
            if v is not None: r = v > float(line[1:])
        s["result"] = "void" if r is None else ("won" if r else "lost")
    bet["final"] = f"{hn} {hg}–{ag} {an}"
    return True


def settle_generic(bet: dict) -> bool:
    sport = "basketball/nba" if bet["sport"] == "nba" else "mma/ufc"
    d = _get(f"{BASE}/{sport}/summary", {"event": bet["event_id"]})
    comps = (d.get("header") or {}).get("competitions") or []
    comp = next((c for c in comps if str(c.get("id")) == str(bet.get("comp_id", c.get("id")))), comps[0] if comps else {})
    if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
        return False
    win = next((c for c in comp.get("competitors", []) if c.get("winner")), None)
    nm = ((win or {}).get("team") or (win or {}).get("athlete") or {}).get("displayName")
    bet["main"]["result"] = "won" if nm == bet["main"]["pick"] else ("void" if not nm else "lost")
    for s in bet["side"]:
        s["result"] = "void"
        if s["key"].startswith("pts:"):
            try:
                tot = sum(int(c.get("score") or 0) for c in comp.get("competitors", [])); s["result"] = "won" if tot > float(s["key"][5:]) else "lost"
            except Exception:
                pass
    bet["final"] = " vs ".join(f"{((c.get('team') or c.get('athlete') or {}).get('displayName'))} {c.get('score', '')}".strip() for c in comp.get("competitors", []))
    return True


def _payout(leg: dict, stake: float) -> float:
    return stake * leg.get("odds", dec_odds(leg["p"])) if leg.get("result") == "won" else (stake if leg.get("result") == "void" else 0.0)


def tick(now: float | None = None, force=False):
    now = now or time.time()
    b = load()
    for bet in list(b["bets"]):
        try:
            if now < dt.datetime.fromisoformat(bet["kickoff"].replace("Z", "+00:00")).timestamp() + 2 * 3600:
                continue
            done = settle_soccer(bet) if bet["sport"] == "soccer" else settle_generic(bet)
        except Exception:
            continue
        if not done:
            continue
        ret = _payout(bet["main"], bet["main"]["stake"]) + sum(_payout(s, s["stake"]) for s in bet["side"])
        bet.update(status="settled", returned=round(ret, 2), pnl=round(ret - bet["stake"], 2), settled_at=now)
        b["cash"] += ret; b["bets"].remove(bet); b["settled"].append(bet)
    if force or now - b.get("last_scan", 0) > 6 * 3600:
        have = {x["id"] for x in b["bets"]} | {x["id"] for x in b["settled"]}
        new = [p for p in scan(now) if p["id"] not in have]
        new.sort(key=lambda p: (p["sport"] != "soccer" or p["lg"] != "uefa.champions", -p["main"]["p"]))
        for p in new[: max(0, 10 - len(b["bets"]))]:
            p["main"]["stake"] = MAIN_STAKE
            for s in p["side"]:
                s["odds"] = dec_odds(s["p"]); s["stake"] = 1.0 if s.get("unsettleable") else SIDE_STAKE
            p["side"] = [s for s in p["side"] if not s.get("unsettleable")] + [s for s in p["side"] if s.get("unsettleable")][:1]
            p["stake"] = round(p["main"]["stake"] + sum(s["stake"] for s in p["side"]), 2)
            place(b, p)
        b["last_scan"] = now
    save(b)
    return b


def public_view() -> dict:
    b = load()
    st = b["settled"]
    legs = [l for x in st for l in [x["main"], *x["side"]] if l.get("result") in ("won", "lost")]
    hit = sum(1 for l in legs if l["result"] == "won") / len(legs) if legs else None
    mains = [x["main"] for x in st if x["main"].get("result") in ("won", "lost")]
    open_val = sum(x["stake"] for x in b["bets"])
    return {"mode": "paper", "start": b["start"], "cash": round(b["cash"], 2), "open_stake": round(open_val, 2),
            "value": round(b["cash"] + open_val, 2), "pnl": round(sum(x.get("pnl", 0) for x in st), 2),
            "hit_rate": round(hit, 3) if hit is not None else None, "main_hit_rate": round(sum(m["result"] == "won" for m in mains) / len(mains), 3) if mains else None,
            "n_settled": len(st), "open": sorted(b["bets"], key=lambda x: x["kickoff"]), "settled": list(reversed(st[-30:]))}


_started = False


def start():
    global _started
    if _started or os.environ.get("SPORTS_ENABLED", "true").lower() in ("0", "false", "no"):
        return
    _started = True

    def loop():
        time.sleep(60)
        while True:
            try:
                tick()
            except Exception as e:
                print(f"[SPORTS] error: {type(e).__name__}")
            time.sleep(1800)
    threading.Thread(target=loop, daemon=True, name="sports").start()
