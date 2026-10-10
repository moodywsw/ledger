"""Mirko's sports predictions — PAPER ONLY.

Data (all free, public): ESPN site API (schedule, form, leaders, results, box scores) and Kalshi
public market data (reference odds for football, UCL, NBA, NFL, UFC, ATP/WTA, F1).
SofaScore's API refuses server requests (403), so it is not used.

Mirko is a bit of a degen: he bets where his probability beats the market price, takes underdogs
when he sees an edge, sprinkles handicaps, player props and a tiny correct-score lottery ticket.
Everything is paper: `PAPER = True`, each bet carries mode="paper" and `place()` is the single
choke point a future real executor would have to go through.
"""
from __future__ import annotations

import json, math, os, re, threading, time, unicodedata, datetime as dt
from pathlib import Path

import requests

PAPER = True
BASE = "https://site.api.espn.com/apis/site/v2/sports"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2/events"
UA = {"Accept": "application/json"}
START_EUR = 1000.0
MAX_OPEN = 30

FOOTBALL = {"eng.1": ("Premier League", "KXEPLGAME"), "esp.1": ("LaLiga", "KXLALIGAGAME"), "ita.1": ("Serie A", "KXSERIEAGAME"),
            "ger.1": ("Bundesliga", "KXBUNDESLIGAGAME"), "fra.1": ("Ligue 1", "KXLIGUE1GAME"), "por.1": ("Liga Portugal", "KXLIGAPORTUGALGAME"),
            "uefa.champions": ("Champions League", "KXUCLGAME")}
CAT_LIMIT = {"football": 8, "ucl": 6, "nba": 4, "nfl": 4, "ufc": 2, "tennis": 4, "f1": 1}
BIG = {"Arsenal", "Manchester City", "Liverpool", "Chelsea", "Manchester United", "Tottenham Hotspur", "Newcastle United", "Aston Villa",
       "Real Madrid", "Barcelona", "Atlético Madrid", "Atletico Madrid", "Athletic Club", "Internazionale", "AC Milan", "Juventus", "Napoli",
       "AS Roma", "Atalanta", "Bayern Munich", "Borussia Dortmund", "Bayer Leverkusen", "RB Leipzig", "Paris Saint-Germain", "Marseille",
       "AS Monaco", "Lyon", "Benfica", "FC Porto", "Sporting CP", "SC Braga", "Braga", "Porto"}


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
    b["settled"] = b["settled"][-500:]
    p = _path(); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(b)); tmp.replace(p)


def place(b: dict, bet: dict):
    """Single choke point for bets. Paper only; a real executor must hook in here and stay opt-in."""
    if not PAPER:
        raise RuntimeError("real betting is not enabled")
    if b["cash"] < bet["stake"]:
        return False
    bet.update(mode="paper", placed=time.time(), status="open")
    b["cash"] -= bet["stake"]; b["bets"].append(bet)
    return True


# ---------- helpers ----------
def norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"\b(fc|cf|afc|sc|ac|as|ss|club|de|the)\b", " ", s)
    return " ".join(s.split())


def same(a: str, b: str) -> bool:
    a, b = norm(a), norm(b)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    ta, tb = set(a.split()), set(b.split())
    return bool({t for t in ta & tb if len(t) > 3})


def ml_prob(ml):
    try:
        ml = float(ml)
    except Exception:
        return None
    return 100 / (ml + 100) if ml > 0 else -ml / (-ml + 100)


def price_odds(q: float) -> float:
    """Decimal odds from a market price (Kalshi pays $1 on a q-priced contract), small fee haircut."""
    return round(max(1.02, 0.98 / max(0.02, min(0.98, q))), 2)


def form_score(form) -> float:
    return sum({"W": 1, "D": 0, "L": -1}.get(c, 0) for c in (form or "")[-5:]) / 5


def poisson(k, lam):
    return math.exp(-lam) * lam ** k / math.factorial(k)


def kickoff_ts(iso: str) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


# ---------- Kalshi reference odds ----------
_kcache: dict = {}


def kalshi(series: str) -> list:
    """[{names: {name: mid}, title}] for open events of a series."""
    hit = _kcache.get(series)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    out = []
    try:
        d = _get(KALSHI, {"series_ticker": series, "status": "open", "limit": 100, "with_nested_markets": "true"})
        for e in d.get("events", []):
            names = {}
            for m in e.get("markets", []):
                try:
                    bid, ask = float(m.get("yes_bid_dollars") or 0), float(m.get("yes_ask_dollars") or 0)
                    last = float(m.get("last_price_dollars") or 0)
                    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
                    if mid > 0:
                        names[m.get("yes_sub_title") or ""] = mid
                except Exception:
                    continue
            if names:
                out.append({"title": e.get("title"), "names": names})
    except Exception:
        pass
    _kcache[series] = (time.time(), out)
    return out


def kalshi_match(series: str, a: str, b: str):
    for e in kalshi(series):
        ka = next((v for k, v in e["names"].items() if same(k, a)), None)
        kb = next((v for k, v in e["names"].items() if same(k, b)), None)
        if ka and kb:
            tie = next((v for k, v in e["names"].items() if k.lower() in ("tie", "draw")), None)
            return ka, kb, tie
    return None


def kalshi_outright(series: str) -> dict:
    ev = kalshi(series)
    return ev[0]["names"] if ev else {}


# ---------- Polymarket sports (public Gamma API) ----------
POLY_TAG = {"eng.1": "epl", "esp.1": "la-liga", "ita.1": "serie-a", "ger.1": "bundesliga", "fra.1": "ligue-1", "por.1": "soccer",
            "uefa.champions": "soccer", "nba": "nba", "nfl": "nfl", "ufc": "ufc", "tennis": "tennis"}
_pcache: dict = {}


def poly_events(tag: str) -> list:
    hit = _pcache.get(tag)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    try:
        ev = _get("https://gamma-api.polymarket.com/events", {"tag_slug": tag, "closed": "false", "limit": 300})
    except Exception:
        ev = []
    _pcache[tag] = (time.time(), ev)
    return ev


def _yes(m):
    try:
        return float(json.loads(m.get("outcomePrices") or "[]")[0])
    except Exception:
        return None


def poly_game(tag: str, a: str, b: str) -> dict:
    """{'ml': {name: p}, 'corners': [(line, p_over)], 'ht': {name: p}, 'first': {name: p}} for a fixture."""
    out = {}
    for e in poly_events(tag):
        t = e.get("title", "")
        head = t.split(" - ")[0]
        if not (" vs" in head and same(head.split(" vs")[0], a) and same(head.split(" vs")[-1].lstrip(". "), b)):
            continue
        sub = t.split(" - ")[1] if " - " in t else ""
        ms = e.get("markets") or []
        if not sub:
            out["ml"] = {("Draw" if (m.get("groupItemTitle") or "").startswith("Draw") else m.get("groupItemTitle")): _yes(m) for m in ms}
        elif sub == "Total Corners":
            rows = []
            for m in ms:
                try:
                    rows.append((float(re.search(r"O/U ([\d.]+)", m.get("groupItemTitle") or m.get("question", "")).group(1)), _yes(m)))
                except Exception:
                    pass
            out["corners"] = sorted(r for r in rows if r[1] is not None)
        elif sub == "Halftime Result":
            out["ht"] = {m.get("groupItemTitle"): _yes(m) for m in ms}
        elif sub == "First Team to Score":
            out["first"] = {m.get("groupItemTitle"): _yes(m) for m in ms}
    return out


_tsdb: dict = {}
TSDB_LG = {"eng.1": 4328, "esp.1": 4335, "ita.1": 4332, "ger.1": 4331, "fra.1": 4334, "por.1": 4344, "uefa.champions": 4480}


def tsdb_venue(lg: str, a: str, b: str):
    """Venue/round from TheSportsDB free tier (public test key)."""
    if lg not in TSDB_LG:
        return None
    if lg not in _tsdb or time.time() - _tsdb[lg][0] > 6 * 3600:
        try:
            _tsdb[lg] = (time.time(), _get(f"https://www.thesportsdb.com/api/v1/json/3/eventsnextleague.php", {"id": TSDB_LG[lg]}).get("events") or [])
        except Exception:
            _tsdb[lg] = (time.time(), [])
    for e in _tsdb[lg][1]:
        if same(e.get("strHomeTeam", ""), a) and same(e.get("strAwayTeam", ""), b):
            return {"venue": e.get("strVenue"), "round": e.get("intRound")}
    return None


# ---------- pick builders ----------
def leg(market, p, q, stake, key, why="", **kw):
    """p = Mirko's probability, q = market/reference price → odds."""
    odds = price_odds(q)
    if p * odds < 1.0:          # negative expected value at this price: keep the call as a lean, stake nothing
        stake, kw = 0.0, {**kw, "lean": True}
    return {"market": market, "p": round(p, 3), "q": round(q, 3), "odds": odds, "stake": stake, "key": key, "why": why, **kw}


def stake_for(p, q, base=12.0):
    edge = p - q
    return round(min(25.0, base + max(0, edge) * 200), 0)   # degen: size up when the edge is real


def football_pick(ev: dict, lg: str) -> dict | None:
    name, series = FOOTBALL[lg]
    c = ev["competitions"][0]
    home = next(t for t in c["competitors"] if t["homeAway"] == "home"); away = next(t for t in c["competitors"] if t["homeAway"] == "away")
    hn, an = home["team"]["displayName"], away["team"]["displayName"]
    if lg != "uefa.champions" and not (hn in BIG or an in BIG):
        return None
    ref = kalshi_match(series, hn, an); src = "Kalshi"
    pg = {}
    try:
        pg = poly_game(POLY_TAG.get(lg, "soccer"), hn, an)
    except Exception:
        pass
    pml = pg.get("ml") or {}
    ph_, pa_ = next((v for k, v in pml.items() if k and same(k, hn)), None), next((v for k, v in pml.items() if k and same(k, an)), None)
    pd_ = pml.get("Draw")
    if ref and ref[2]:
        qh, qa, qd = ref
        if ph_ and pa_ and pd_:
            qh, qa, qd = (qh + ph_) / 2, (qa + pa_) / 2, (qd + pd_) / 2; src = "Kalshi + Polymarket"
        s = qh + qa + qd; qh, qa, qd = qh / s, qa / s, qd / s
    elif ph_ and pa_ and pd_:
        s = ph_ + pa_ + pd_; qh, qa, qd = ph_ / s, pa_ / s, pd_ / s; src = "Polymarket"
    else:
        o = (c.get("odds") or [None])[0] or {}
        qh, qa, qd = (ml_prob((o.get(k) or {}).get("moneyLine")) for k in ("homeTeamOdds", "awayTeamOdds", "drawOdds"))
        if not (qh and qa and qd):
            return None
        s = qh + qa + qd; qh, qa, qd = qh / s, qa / s, qd / s; src = "the books"
    tilt = 0.05 * (form_score(home.get("form")) - form_score(away.get("form"))) + 0.01
    ph, pa = max(.03, qh + tilt), max(.03, qa - tilt); pd = max(.05, 1 - ph - pa)
    opts = [("Home win", hn, ph, qh, "1"), ("Draw", "Draw", pd, qd, "X"), ("Away win", an, pa, qa, "2")]
    best = max(opts, key=lambda o: o[2] * price_odds(o[3]))
    fav = max(opts, key=lambda o: o[3])
    if fav[3] >= .7 and fav[4] != "X":   # chalk is boring: take the -1 handicap (win by 2+)
        p2, q2 = fav[2] * .6, fav[3] * .58
        main = leg(f"{fav[1]} −1 handicap", p2, q2, stake_for(p2, q2), f"hcp:{fav[4]}", f"{src} prices {fav[1]} at {round(fav[3] * 100)}%. Too short to back straight — I want the margin.", pick=f"{fav[1]} −1")
    else:
        main = leg(best[0], best[2], best[3], stake_for(best[2], best[3]), f"1x2:{best[4]}",
                   f"{src} says {round(best[3] * 100)}%, I make it {round(best[2] * 100)}%." + (" Upset alert." if best[3] < .35 else ""), pick=best[1])
    o = (c.get("odds") or [None])[0] or {}
    line = o.get("overUnder") or 2.5
    po = .5
    try:
        a_ = ml_prob(o["total"]["over"]["close"]["odds"]); b_ = ml_prob(o["total"]["under"]["close"]["odds"]); po = a_ / (a_ + b_)
    except Exception:
        pass
    gline = line
    lam = line + (0.35 if po > .5 else -0.25)
    lh, la = lam * (ph + pd / 2) / (ph + pa + pd), lam * (pa + pd / 2) / (ph + pa + pd)
    side = [leg(f"{'Over' if po >= .5 else 'Under'} {line} goals", max(po, 1 - po) + .03, max(po, 1 - po), 5, f"ou:{line}:{'o' if po >= .5 else 'u'}")]
    pbtts = (1 - math.exp(-lh)) * (1 - math.exp(-la))
    side.append(leg(f"Both teams to score — {'Yes' if pbtts >= .5 else 'No'}", max(pbtts, 1 - pbtts), max(pbtts, 1 - pbtts) - .03, 5, f"btts:{'y' if pbtts >= .5 else 'n'}"))
    big_game = hn in BIG and an in BIG
    side.append(leg("Over 3.5 yellow cards" if big_game else "Under 4.5 yellow cards", .6, .55, 5, "cards:o3.5" if big_game else "cards:u4.5"))
    cor = pg.get("corners") or []
    if cor:
        cand = [r for r in cor if 7.5 <= r[0] <= 12.5 and .3 <= r[1] <= .7] or [r for r in cor if 7.5 <= r[0] <= 12.5] or cor
        cline, po_c = min(cand, key=lambda r: abs(r[0] - 9.5) + abs(r[1] - .5) * 4)
        over = max(ph, pa) > .5   # a dominant favourite camps in the box → more corners
        qc = po_c if over else 1 - po_c
        side.append(leg(f"{'Over' if over else 'Under'} {cline} corners", qc + .04, qc, 5, f"corners:{'o' if over else 'u'}{cline}", why="Polymarket corners line"))
    elif max(ph, pa) > .55:
        side.append(leg("Over 8.5 corners", .57, .52, 5, "corners:o8.5"))
    ht = pg.get("ht") or {}
    fav_name = hn if ph >= pa else an
    qht = next((v for k, v in ht.items() if k and same(k, fav_name)), None)
    if qht and .25 < qht < .75 and max(ph, pa) >= .5:
        side.append(leg(f"{fav_name} leading at half-time", qht + .03, qht, 5, f"ht:{'h' if fav_name == hn else 'a'}"))
    fs = pg.get("first") or {}
    qfs = next((v for k, v in fs.items() if k and same(k, fav_name)), None)
    if qfs and qfs < .8:
        side.append(leg(f"{fav_name} to score first", qfs + .03, qfs, 5, f"first:{'h' if fav_name == hn else 'a'}"))
    if max(ph, pa) >= .6:
        pwn = max(ph, pa) * math.exp(-(la if ph >= pa else lh))
        side.append(leg(f"{fav_name} to win to nil", pwn, pwn * .92, 4, f"wtn:{'h' if fav_name == hn else 'a'}"))
    # player props when the matchup supports it: favourite's top scorer
    favt = home if ph >= pa else away
    ld = next((l for l in favt.get("leaders", []) if l.get("name") in ("goals", "goalsLeaders")), None)
    top = (ld or {}).get("leaders", [{}])[0] if ld else {}
    try:
        goals = float(top.get("value") or top.get("displayValue") or 0)
    except Exception:
        goals = 0
    pl = (top.get("athlete") or {}).get("displayName")
    if pl and goals >= 2 and max(ph, pa) >= .5:
        lamp = (lh if favt is home else la) * .38
        side.append(leg(f"{pl} anytime scorer", 1 - math.exp(-lamp), (1 - math.exp(-lamp)) - .04, 5, f"player:goal:{pl}", player=pl))
        side.append(leg(f"{pl} 1+ shot on target", .66, .6, 5, f"player:sot:{pl}", player=pl))
        side.append(leg(f"{pl} to be fouled", .72, .66, 4, f"player:fouled:{pl}", player=pl))
    # degen lottery: most likely non-trivial correct score
    sc = max(((i, j) for i in range(5) for j in range(5) if (i, j) != (0, 0)), key=lambda x: poisson(x[0], lh) * poisson(x[1], la))
    pcs = poisson(sc[0], lh) * poisson(sc[1], la)
    side.append(leg(f"Correct score {sc[0]}–{sc[1]} (lottery)", pcs, pcs * .85, 2, f"cs:{sc[0]}:{sc[1]}"))
    cat = "ucl" if lg == "uefa.champions" else "football"
    tv = None
    try:
        tv = tsdb_venue(lg, hn, an)
    except Exception:
        pass
    fh_, fa_ = home.get("form") or "", away.get("form") or ""
    read = (f"{src} has {hn} {round(qh * 100)}% · draw {round(qd * 100)}% · {an} {round(qa * 100)}%. "
            f"Form {fh_ or '—'} vs {fa_ or '—'} moves me {'towards ' + hn if tilt > .015 else 'towards ' + an if tilt < -.015 else 'nowhere'}. "
            f"My main bet: {main['pick']} at @{main['odds']} because I make it {round(main['p'] * 100)}% vs the market's {round(main['q'] * 100)}%. "
            f"Expected goals ~{lh:.1f}–{la:.1f}, so I lean {'over' if po >= .5 else 'under'} {gline} goals" + (f" and {'over' if max(ph, pa) > .5 else 'under'} on corners." if pg.get('corners') else ".")
            + (f" Played at {tv['venue']}." if tv and tv.get('venue') else ""))
    return {"read": read,"id": f"espn:{lg}:{ev['id']}", "sport": "soccer", "cat": cat, "league": name, "lg": lg, "event_id": ev["id"],
            "title": f"{hn} vs {an}", "kickoff": ev["date"], "ref": src, "main": main, "side": side}


def teamsport_pick(ev: dict, sport: str) -> dict | None:
    path, series, label = {"nba": ("basketball/nba", "KXNBAGAME", "NBA"), "nfl": ("football/nfl", "KXNFLGAME", "NFL")}[sport]
    if (ev.get("season") or {}).get("type") not in (2, 3):
        return None
    c = ev["competitions"][0]
    home = next(t for t in c["competitors"] if t["homeAway"] == "home"); away = next(t for t in c["competitors"] if t["homeAway"] == "away")
    hn, an = home["team"]["displayName"], away["team"]["displayName"]
    ref = kalshi_match(series, hn, an)
    if not ref:
        return None
    qh, qa = ref[0] / (ref[0] + ref[1]), ref[1] / (ref[0] + ref[1])
    ph = qh + .02
    if ph * price_odds(qh) >= (1 - ph) * price_odds(qa):
        pick, p, q = hn, ph, qh
    else:
        pick, p, q = an, 1 - ph, qa
    main = leg("Moneyline", p, q, stake_for(p, q), "ml", f"Kalshi {round(q * 100)}%, I have {round(p * 100)}%." + (" Dog with bite." if q < .45 else ""), pick=pick)
    side = []
    o = (c.get("odds") or [None])[0] or {}
    if o.get("overUnder"):
        side.append(leg(f"Over {o['overUnder']} points", .53, .5, 5, f"pts:o{o['overUnder']}"))
    if sport == "nba":
        pt = home if pick == hn else away
        for stat, nm in (("points", "PTS"), ("rebounds", "REB"), ("assists", "AST")):
            l = next((x for x in pt.get("leaders", []) if x.get("name") == stat), None)
            if not l or not l.get("leaders"):
                continue
            ld = l["leaders"][0]
            try:
                v = float(ld.get("value"))
            except Exception:
                continue
            if v >= (15 if stat == "points" else 6):
                line = math.floor(v * .85) + .5
                side.append(leg(f"{ld['athlete']['displayName']} over {line} {stat}", .56, .52, 4, f"nbaprop:{nm}:{line}:{ld['athlete']['displayName']}",
                                player=ld["athlete"]["displayName"]))
    read = (f"Kalshi prices {hn} {round(qh * 100)}% vs {an} {round(qa * 100)}%. I add ~2% for home court and back {pick} at @{main['odds']} "
            f"({round(p * 100)}% vs {round(q * 100)}%)." + (" Player props follow the team's season leaders." if sport == "nba" and side else ""))
    return {"read": read, "id": f"espn:{sport}:{ev['id']}", "sport": sport, "cat": sport, "league": label, "lg": sport, "event_id": ev["id"],
            "title": f"{an} @ {hn}", "kickoff": ev["date"], "ref": "Kalshi", "main": main, "side": side}


def ufc_picks(ev: dict) -> list:
    out = []
    comps = sorted(ev.get("competitions") or [], key=lambda c: c.get("matchNumber") or 0)[-2:]
    for c in comps:
        if c["status"]["type"]["name"] != "STATUS_SCHEDULED":
            continue
        f1, f2 = c["competitors"][0], c["competitors"][1]
        n1, n2 = f1["athlete"]["displayName"], f2["athlete"]["displayName"]
        ref = kalshi_match("KXUFCFIGHT", n1, n2)
        if ref:
            q1 = ref[0] / (ref[0] + ref[1])
        else:
            def wp(x):
                try:
                    w, l = [int(v) for v in ((x.get("records") or [{}])[0]).get("summary", "0-0").split("-")[:2]]; return (w + 1) / (w + l + 2)
                except Exception:
                    return .5
            q1 = wp(f1) / (wp(f1) + wp(f2))
        dog = (n2, 1 - q1) if q1 >= .5 else (n1, q1)
        fav = (n1, q1) if q1 >= .5 else (n2, 1 - q1)
        # degen: back the live dog when it's priced 30-45%, otherwise the favourite
        pick, q = dog if .3 <= dog[1] <= .45 else fav
        p = q + .04
        main = leg("Fight winner", p, q, stake_for(p, q), "ufc:win", f"{'Kalshi' if ref else 'Records'} price {pick} at {round(q * 100)}%." + (" Live dog." if q < .5 else ""), pick=pick)
        side = [leg(f"{pick} by KO/TKO or submission", p * .55, q * .5, 4, "ufc:finish"),
                leg("Fight doesn't go the distance", .6, .55, 4, "ufc:nodist")]
        out.append({"read": f"{'Kalshi' if ref else 'Their records'} make {n1} {round(q1 * 100)}% vs {n2} {round((1 - q1) * 100)}%. "
                            + (f"{pick} is a live underdog at @{main['odds']} — I like the price more than the fighter." if q < .5 else f"{pick} is the better fighter on paper; I want him to finish it."),
                    "id": f"espn:ufc:{ev['id']}:{c['id']}", "sport": "ufc", "cat": "ufc", "league": "UFC", "lg": "ufc", "event_id": ev["id"], "comp_id": c["id"],
                    "title": f"{n1} vs {n2}", "kickoff": c.get("date") or ev["date"], "ref": "Kalshi" if ref else "records", "main": main, "side": side})
    return out


def tennis_picks(ev: dict, tour: str) -> list:
    out = []
    series = "KXATPMATCH" if tour == "atp" else "KXWTAMATCH"
    for g in ev.get("groupings") or []:
        for c in g.get("competitions") or []:
            if c["status"]["type"]["name"] != "STATUS_SCHEDULED" or "qualif" in ((c.get("round") or {}).get("displayName", "").lower()):
                continue
            try:
                n1, n2 = (x["athlete"]["displayName"] for x in c["competitors"])
            except Exception:
                continue
            ref = kalshi_match(series, n1, n2)
            if not ref:
                continue   # only matches important enough to have a market
            q1 = ref[0] / (ref[0] + ref[1])
            pick, q = (n1, q1) if q1 >= .5 else (n2, 1 - q1)
            if q > .8:
                main = leg(f"{pick} in straight sets", q * .72, q * .66, stake_for(q * .72, q * .66), "tennis:straight", f"Kalshi has {pick} at {round(q * 100)}% — I want it clean.", pick=f"{pick} 2–0")
            else:
                main = leg("Match winner", q + .03, q, stake_for(q + .03, q), "tennis:win", f"Kalshi {round(q * 100)}%; surface and form nudge me to {round((q + .03) * 100)}%.", pick=pick)
            side = [leg("Match goes to a deciding set", .38, .34, 4, "tennis:decider")]
            out.append({"read": f"Kalshi has {pick} at {round(q * 100)}%. " + ("Too short to back straight, so I take the straight-sets price." if q > .8 else "Close enough to bet the winner; the deciding-set bet is my hedge."),
                        "id": f"espn:{tour}:{c['id']}", "sport": "tennis", "cat": "tennis", "league": f"{tour.upper()} · {ev.get('name', '')}", "lg": tour,
                        "event_id": ev["id"], "comp_id": c["id"], "title": f"{n1} vs {n2}", "kickoff": c.get("date") or c.get("startDate") or ev["date"],
                        "ref": "Kalshi", "main": main, "side": side, "p_names": [n1, n2]})
    return out


def f1_pick(ev: dict) -> dict | None:
    names = kalshi_outright("KXF1RACE")
    if not names:
        return None
    race = next((c for c in ev.get("competitions", []) if (c.get("type") or {}).get("abbreviation", "").lower() in ("race", "r")), (ev.get("competitions") or [None])[-1])
    if not race or race["status"]["type"]["name"] != "STATUS_SCHEDULED":
        return None
    ranked = sorted(names.items(), key=lambda x: -x[1])
    # degen: second favourite if he's priced decently, else the favourite
    pick, q = ranked[1] if len(ranked) > 1 and ranked[1][1] >= .18 else ranked[0]
    main = leg("Race winner", q + .04, q, stake_for(q + .04, q, 15), "f1:win", f"Kalshi has {pick} at {round(q * 100)}%. I'll take the price.", pick=pick)
    side = [leg(f"{ranked[0][0]} podium", min(.9, ranked[0][1] + .3), min(.88, ranked[0][1] + .25), 5, f"f1:podium:{ranked[0][0]}")]
    return {"read": f"Kalshi race-winner board: {', '.join(f'{n} {round(v * 100)}%' for n, v in ranked[:3])}. I bet {pick} at @{main['odds']} and cover {ranked[0][0]} for a podium.",
            "id": f"espn:f1:{ev['id']}", "sport": "f1", "cat": "f1", "league": "Formula 1", "lg": "f1", "event_id": ev["id"], "comp_id": race["id"],
            "title": ev.get("name", "Grand Prix"), "kickoff": race.get("date") or ev["date"], "ref": "Kalshi", "main": main, "side": side}


def scan(now: float | None = None) -> list:
    now = now or time.time()
    days = [(dt.datetime.utcfromtimestamp(now) + dt.timedelta(days=i)).strftime("%Y%m%d") for i in range(3)]
    picks = []

    def evs(path, d=None):
        try:
            return _get(f"{BASE}/{path}/scoreboard", {"dates": d} if d else None).get("events", [])
        except Exception:
            return []
    for lg in FOOTBALL:
        for d in days:
            for ev in evs(f"soccer/{lg}", d):
                if ev["status"]["type"]["name"] == "STATUS_SCHEDULED":
                    try:
                        p = football_pick(ev, lg); p and picks.append(p)
                    except Exception:
                        pass
    for sport, path in (("nba", "basketball/nba"), ("nfl", "football/nfl")):
        for d in days:
            for ev in evs(path, d):
                if ev["status"]["type"]["name"] == "STATUS_SCHEDULED":
                    try:
                        p = teamsport_pick(ev, sport); p and picks.append(p)
                    except Exception:
                        pass
    for ev in evs("mma/ufc"):
        try:
            picks += ufc_picks(ev)
        except Exception:
            pass
    for tour in ("atp", "wta"):
        for ev in evs(f"tennis/{tour}"):
            try:
                picks += tennis_picks(ev, tour)
            except Exception:
                pass
    for ev in evs("racing/f1"):
        try:
            p = f1_pick(ev); p and picks.append(p)
        except Exception:
            pass
    seen, out = set(), []
    for p in picks:
        try:
            k = kickoff_ts(p["kickoff"])
        except Exception:
            continue
        if k < now + 900 or k > now + 4 * 86400 or p["id"] in seen:
            continue
        seen.add(p["id"]); out.append(p)
    return out


# ---------- settlement ----------
def _stat(team: dict, name: str):
    for s in team.get("statistics") or []:
        if s.get("name") == name:
            try:
                return float(s.get("displayValue"))
            except Exception:
                return None
    return None


def _player(d: dict, name: str, stat: str):
    for r in d.get("rosters") or []:
        for p in r.get("roster") or []:
            if same((p.get("athlete") or {}).get("displayName", ""), name):
                for s in p.get("stats") or []:
                    if s.get("name") == stat:
                        return float(s.get("value") or 0)
                return 0.0 if stat == "yellowCards" else None
    return None


def settle_soccer(bet: dict) -> bool:
    d = _get(f"{BASE}/soccer/{bet['lg']}/summary", {"event": bet["event_id"]})
    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
        return False
    cs = {c["homeAway"]: c for c in comp.get("competitors", [])}
    hg, ag = int(cs["home"].get("score") or 0), int(cs["away"].get("score") or 0)
    teams = {t.get("homeAway"): t for t in (d.get("boxscore") or {}).get("teams", [])}
    res = "1" if hg > ag else "2" if ag > hg else "X"
    for l in [bet["main"], *bet["side"]]:
        k = l.get("key", ""); r = None
        try:
            if k.startswith("1x2:"):
                r = res == k[4:]
            elif k.startswith("hcp:"):
                r = (hg - ag >= 2) if k.endswith("1") else (ag - hg >= 2)
            elif l.get("market") in ("Home win", "Away win", "Draw", "Home or draw", "Away or draw"):   # v8 legacy
                r = {"Home win": res == "1", "Away win": res == "2", "Draw": res == "X", "Home or draw": res != "2", "Away or draw": res != "1"}[l["market"]]
            elif k.startswith("ou:"):
                _, line, sd = k.split(":"); r = (hg + ag > float(line)) == (sd == "o")
            elif k.startswith("btts:"):
                r = (hg > 0 and ag > 0) == k.endswith("y")
            elif k.startswith("cards:") or k.startswith("corners:"):
                nm = "yellowCards" if k.startswith("cards") else "wonCorners"
                vals = [_stat(teams.get(x, {}), nm) for x in ("home", "away")]
                if None not in vals:
                    line = float(k.split(":")[1][1:]); r = sum(vals) > line if ":o" in k else sum(vals) < line
            elif k.startswith("sot:"):
                _, sd, line = k.split(":"); v = _stat(teams.get("home" if sd == "h" else "away", {}), "shotsOnTarget")
                if v is not None: r = v > float(line[1:])
            elif k.startswith("ht:"):
                lh_ = [int(x.get("displayValue") or 0) for x in (cs["home"].get("linescores") or [])][:1]
                la_ = [int(x.get("displayValue") or 0) for x in (cs["away"].get("linescores") or [])][:1]
                if lh_ and la_: r = (lh_[0] > la_[0]) if k.endswith("h") else (la_[0] > lh_[0])
            elif k.startswith("first:"):
                goals = [x for x in d.get("keyEvents") or [] if "goal" in ((x.get("type") or {}).get("type") or "")]
                tm = (goals[0].get("team") or {}).get("displayName") if goals else None
                r = False if not goals else same(tm or "", cs["home" if k.endswith("h") else "away"]["team"]["displayName"])
            elif k.startswith("wtn:"):
                r = (hg > ag and ag == 0) if k.endswith("h") else (ag > hg and hg == 0)
            elif k.startswith("cs:"):
                _, a, b = k.split(":"); r = (hg, ag) == (int(a), int(b))
            elif k.startswith("player:"):
                _, kind, nm = k.split(":", 2)
                v = _player(d, nm, {"goal": "totalGoals", "sot": "shotsOnTarget", "fouled": "foulsSuffered", "booked": "yellowCards"}[kind])
                if v is not None: r = v >= 1
        except Exception:
            r = None
        l["result"] = "void" if r is None else ("won" if r else "lost")
    bet["final"] = f"{cs['home']['team']['displayName']} {hg}–{ag} {cs['away']['team']['displayName']}"
    return True


def settle_teamsport(bet: dict) -> bool:
    path = {"nba": "basketball/nba", "nfl": "football/nfl"}[bet["sport"]]
    d = _get(f"{BASE}/{path}/summary", {"event": bet["event_id"]})
    comp = ((d.get("header") or {}).get("competitions") or [{}])[0]
    if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
        return False
    cps = comp.get("competitors", [])
    win = next((c for c in cps if c.get("winner")), None)
    wn = ((win or {}).get("team") or {}).get("displayName")
    tot = sum(int(c.get("score") or 0) for c in cps)
    box = {}
    for t in (d.get("boxscore") or {}).get("players", []):
        for grp in t.get("statistics", []):
            labels = grp.get("labels") or []
            for a in grp.get("athletes", []):
                box[a["athlete"]["displayName"]] = dict(zip(labels, a.get("stats") or []))
    for l in [bet["main"], *bet["side"]]:
        k = l.get("key", ""); r = None
        try:
            if k == "ml" or l.get("market") == "Moneyline":
                r = wn == l.get("pick") if wn else None
            elif k.startswith("pts:o"):
                r = tot > float(k[5:])
            elif k.startswith("nbaprop:"):
                _, nm, line, pl = k.split(":", 3)
                st = next((v for n, v in box.items() if same(n, pl)), None)
                if st and st.get(nm) not in (None, "--"): r = float(st[nm]) > float(line)
        except Exception:
            r = None
        l["result"] = "void" if r is None else ("won" if r else "lost")
    bet["final"] = " · ".join(f"{(c.get('team') or {}).get('abbreviation', '')} {c.get('score', '')}" for c in cps)
    return True


def _scoreboard_comp(path: str, bet: dict):
    day = dt.datetime.fromisoformat(bet["kickoff"].replace("Z", "+00:00")).strftime("%Y%m%d")
    for ev in _get(f"{BASE}/{path}/scoreboard", {"dates": day}).get("events", []):
        if str(ev.get("id")) != str(bet["event_id"]) and bet["sport"] != "tennis":
            continue
        comps = list(ev.get("competitions") or []) + [c for g in ev.get("groupings") or [] for c in g.get("competitions") or []]
        for c in comps:
            if str(c.get("id")) == str(bet.get("comp_id")):
                return c
    return None


def settle_other(bet: dict) -> bool:
    path = {"ufc": "mma/ufc", "tennis": f"tennis/{bet['lg']}", "f1": "racing/f1"}[bet["sport"]]
    c = _scoreboard_comp(path, bet)
    if not c or not ((c.get("status") or {}).get("type") or {}).get("completed"):
        return False
    cps = c.get("competitors", [])
    win = next((x for x in cps if x.get("winner")), None) or (sorted(cps, key=lambda x: x.get("order") or 99)[0] if bet["sport"] == "f1" and cps else None)
    wn = ((win or {}).get("athlete") or {}).get("displayName")
    pick = bet["main"].get("pick", "").replace(" 2–0", "")
    sets = None
    try:
        sets = sum(len(x.get("linescores") or []) for x in cps[:1])
    except Exception:
        pass
    for l in [bet["main"], *bet["side"]]:
        k = l.get("key", ""); r = None
        if not wn:
            r = None
        elif k in ("ufc:win", "tennis:win", "f1:win") or l.get("market") == "Fight winner":
            r = same(wn, pick)
        elif k == "tennis:straight":
            r = same(wn, pick) and sets == 2 if sets else None
        elif k == "tennis:decider":
            r = sets == 3 if sets else None
        elif k.startswith("f1:podium:"):
            top3 = [((x.get("athlete") or {}).get("displayName")) for x in sorted(cps, key=lambda x: x.get("order") or 99)[:3]]
            r = any(same(t or "", k[10:]) for t in top3)
        l["result"] = "void" if r is None else ("won" if r else "lost")
    bet["final"] = f"Winner: {wn}" if wn else "settled"
    return True


def _payout(l: dict) -> float:
    return l["stake"] * l.get("odds", 1) if l.get("result") == "won" else (l["stake"] if l.get("result") == "void" else 0.0)


def tick(now: float | None = None, force=False):
    now = now or time.time()
    b = load()
    for bet in list(b["bets"]):
        try:
            if now < kickoff_ts(bet["kickoff"]) + 2.5 * 3600:
                continue
            fn = settle_soccer if bet["sport"] == "soccer" else settle_teamsport if bet["sport"] in ("nba", "nfl") else settle_other
            done = fn(bet)
            if not done and now > kickoff_ts(bet["kickoff"]) + 5 * 86400:   # stale: refund
                for l in [bet["main"], *bet["side"]]: l["result"] = "void"
                done = True
        except Exception:
            continue
        if not done:
            continue
        ret = _payout(bet["main"]) + sum(_payout(s) for s in bet["side"])
        bet.update(status="settled", returned=round(ret, 2), pnl=round(ret - bet["stake"], 2), settled_at=now)
        b["cash"] += ret; b["bets"].remove(bet); b["settled"].append(bet)
    if force or now - b.get("last_scan", 0) > 4 * 3600:
        have = {x["id"] for x in b["bets"]} | {x["id"] for x in b["settled"]}
        counts = {}
        for x in b["bets"]:
            counts[x.get("cat", "football")] = counts.get(x.get("cat", "football"), 0) + 1
        fresh = scan(now)
        byid = {p["id"]: p for p in fresh}
        for x in b["bets"]:   # backfill Mirko's read on picks made before it existed
            fr = byid.get(x["id"], {})
            if fr.get("read") and (not x.get("read") or x.get("read_fix") != 3):
                m = x["main"]
                x["read"] = re.sub(r"My main bet: .*? vs the market's \d+%\.|I add ~2% for home court and back .*?\)\.",
                                   f"My bet: {m.get('pick') or m.get('market')} at @{m.get('odds')}, I make it {round(m.get('p', 0) * 100)}%" + (f" vs the market's {round(m['q'] * 100)}%." if m.get('q') else "."),
                                   fr["read"])
                x["read_fix"] = 3
        new = sorted((p for p in fresh if p["id"] not in have), key=lambda p: -(p["main"]["p"] - p["main"]["q"]))
        for p in new:
            p["stake"] = round(p["main"]["stake"] + sum(x["stake"] for x in p["side"]), 2)
            if b["cash"] - p.get("stake", 0) < 0.4 * (b["cash"] + sum(x["stake"] for x in b["bets"])):
                break   # keep 40% of the bankroll dry, degen but not dumb
            if len(b["bets"]) >= MAX_OPEN or counts.get(p["cat"], 0) >= CAT_LIMIT.get(p["cat"], 3):
                continue
            p["stake"] = round(p["main"]["stake"] + sum(s["stake"] for s in p["side"]), 2)
            if place(b, p):
                counts[p["cat"]] = counts.get(p["cat"], 0) + 1
        b["last_scan"] = now
    save(b)
    return b


def _cat(x):
    return x.get("cat") or ("ucl" if x.get("lg") == "uefa.champions" else "football" if x.get("sport") == "soccer" else x.get("sport"))


def public_view() -> dict:
    b = load()
    st = b["settled"]
    for x in b["bets"] + st:
        x["cat"] = _cat(x)
    legs = [l for x in st for l in [x["main"], *x["side"]] if l.get("result") in ("won", "lost")]
    mains = [x["main"] for x in st if x["main"].get("result") in ("won", "lost")]
    by = {}
    for x in st:
        c = by.setdefault(x["cat"], {"n": 0, "won": 0, "pnl": 0.0})
        c["n"] += 1; c["won"] += x["main"].get("result") == "won"; c["pnl"] = round(c["pnl"] + x.get("pnl", 0), 2)
    open_val = sum(x["stake"] for x in b["bets"])
    return {"mode": "paper", "start": b["start"], "cash": round(b["cash"], 2), "open_stake": round(open_val, 2),
            "value": round(b["cash"] + open_val, 2), "pnl": round(sum(x.get("pnl", 0) for x in st), 2),
            "hit_rate": round(sum(l["result"] == "won" for l in legs) / len(legs), 3) if legs else None,
            "main_hit_rate": round(sum(m["result"] == "won" for m in mains) / len(mains), 3) if mains else None,
            "n_settled": len(st), "by_cat": by, "open": sorted(b["bets"], key=lambda x: x["kickoff"]), "settled": list(reversed(st[-80:]))}


_started = False


def start():
    global _started
    if _started or os.environ.get("SPORTS_ENABLED", "true").lower() in ("0", "false", "no"):
        return
    _started = True

    def loop():
        time.sleep(60)
        first = True
        while True:
            try:
                tick(force=first and not load().get("v10d")) ; first = False
                b = load(); b["v10d"] = True; save(b)
            except Exception as e:
                print(f"[SPORTS] error: {type(e).__name__}")
            time.sleep(1800)
    threading.Thread(target=loop, daemon=True, name="sports").start()
