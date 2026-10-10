"""Mirko's paper portfolio — SIMULATED, no real orders ever.

Three sleeves, each started with EUR 1,000:
  spot   — crypto spot: BTC/ETH/SOL weighted by the market read, up to 2 mid caps and
           1 low cap from Market Thoughts ideas. Rebalance every 4h, stops checked every 15 min.
  perps  — paper perpetuals from the majors' trade ideas: leverage <= 3x, every position has a
           stop and a target, 25% of sleeve equity as margin per trade, max 3 open.
  stocks — US penny stocks (< $5) screened from Yahoo Finance (liquidity + trend filters),
           decided once per US trading day, stops checked during market hours.

Prices: Binance (crypto), DexScreener (low caps), Yahoo chart API (stocks); EUR via frankfurter.app.
State: $DATA_DIR/paper_portfolio.json (Railway volume).
"""
from __future__ import annotations

import json, os, re, threading, time, datetime as dt
from pathlib import Path

import requests

UA = {"User-Agent": "Mozilla/5.0 (LedgerBot paper portfolio)"}
START_EUR = 1000.0
SPOT_FEE, PERP_FEE, STOCK_SLIP = 0.001, 0.0005, 0.002
TICK_S = 900
CRYPTO_EVERY_H = 4
MAX_LEV = 3.0


def _path() -> Path:
    return Path(os.environ.get("DATA_DIR", ".")) / "paper_portfolio.json"


def _get(url, params=None, timeout=15):
    r = requests.get(url, params=params, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _fmt(x: float) -> str:
    return f"{x:,.0f}" if x >= 1000 else f"{x:,.2f}" if x >= 1 else f"{x:.6g}"


def new_state() -> dict:
    t = time.time()
    return {"created": t, "currency": "EUR", "trades": [], "history": [],
            "sleeves": {k: {"cash": START_EUR, "positions": {}, "start": START_EUR, "last_decision": 0, "note": ""}
                        for k in ("spot", "perps", "stocks", "poly")}}


def load() -> dict:
    try:
        s = json.loads(_path().read_text())
    except Exception:
        return new_state()
    s["sleeves"].setdefault("poly", {"cash": START_EUR, "positions": {}, "start": START_EUR, "last_decision": 0, "note": "",
                                     "added": time.time()})   # Polymarket sleeve added later: own €1000 start
    return s


def save(s):
    s["trades"] = s["trades"][-400:]
    s["history"] = s["history"][-1500:]
    p = _path(); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(s, default=str)); tmp.replace(p)


# ---------------- prices ----------------
_fx = {"ts": 0, "v": 0.86}


def usd_eur() -> float:
    if time.time() - _fx["ts"] > 6 * 3600:
        try:
            _fx["v"] = float(_get("https://api.frankfurter.app/latest", {"from": "USD", "to": "EUR"})["rates"]["EUR"]); _fx["ts"] = time.time()
        except Exception:
            pass
    return _fx["v"]


def crypto_price(sym: str) -> float | None:
    import market_thoughts as mt
    try:
        return float(mt.spot("/api/v3/ticker/price", {"symbol": f"{sym}USDT"})["price"])
    except Exception:
        return None


def dex_price(chain: str, addr: str) -> float | None:
    try:
        ps = _get(f"https://api.dexscreener.com/tokens/v1/{chain}/{addr}")
        best = max(ps, key=lambda q: (q.get("liquidity") or {}).get("usd") or 0)
        return float(best.get("priceUsd") or 0) or None
    except Exception:
        return None


def stock_chart(sym: str, rng="6mo"):
    d = _get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}", {"range": rng, "interval": "1d"})["chart"]["result"][0]
    q = d["indicators"]["quote"][0]
    rows = [(t, c, v) for t, c, v in zip(d["timestamp"], q["close"], q["volume"]) if c is not None]
    return d["meta"], rows


def stock_price(sym: str) -> float | None:
    try:
        meta, _ = stock_chart(sym, "5d")
        return float(meta.get("regularMarketPrice"))
    except Exception:
        return None


GAMMA = "https://gamma-api.polymarket.com"
_POLY_KW = re.compile(r"bitcoin|btc|ethereum|eth\b|solana|crypto|fed|rate|inflation|cpi|recession|gdp|tariff|election|president|trump|china|ai\b|openai|nvidia|stock|s&p|nasdaq|oil|war|ceasefire|congress|senate|ecb|etf", re.I)


def _poly_prices(m: dict) -> tuple[float, float] | None:
    try:
        o = [x.lower() for x in json.loads(m.get("outcomes") or "[]")]; pr = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        return pr[o.index("yes")], pr[o.index("no")]
    except Exception:
        return None


def poly_market(mid: str) -> dict | None:
    try:
        return _get(f"{GAMMA}/markets/{mid}")
    except Exception:
        return None


def poly_price(pos: dict) -> float | None:
    m = poly_market(pos["market_id"])
    if not m:
        return None
    yn = _poly_prices(m)
    if not yn:
        return None
    if m.get("closed"):   # resolved: winning side pays 1
        win = "YES" if yn[0] > yn[1] else "NO"
        pos["resolved"] = win
        return 1.0 if pos["side"] == win else 0.0
    return yn[0] if pos["side"] == "YES" else yn[1]


def poly_view(m: dict, yes: float) -> tuple[str, float, str] | None:
    """Mirko's prediction. Simple, honest edges: fade long shots (favourite-longshot bias), back strong
    favourites near expiry, and tilt crypto/macro questions with his own market read + daily reading."""
    q = m.get("question") or ""
    try:
        import market_thoughts as mt, insights
        mr = mt.cached(max_age_h=12) or {}; tilt = insights.risk_tilt()
    except Exception:
        mr, tilt = {}, 0.0
    reg = (mr.get("regime") or {}).get("key", "chop")
    p = yes
    why = []
    if yes < 0.22:
        p = yes * 0.7; why.append("long shots are usually overpriced on prediction markets")
    elif yes > 0.78:
        p = yes + (1 - yes) * 0.3; why.append("strong favourite; the crowd tends to underprice near-certainties")
    if re.search(r"bitcoin|btc|crypto|eth|solana", q, re.I) and re.search(r"above|reach|hit|higher", q, re.I):
        adj = {"risk_on": 0.06, "risk_off": -0.06}.get(reg, 0) + 0.03 * tilt
        if adj:
            p += adj; why.append(f"my market read is {(mr.get('regime') or {}).get('label', 'mixed').lower()}")
    p = max(0.01, min(0.99, p))
    edge_yes, edge_no = p - yes, (1 - p) - (1 - yes)
    if max(edge_yes, edge_no) < 0.03:
        return None
    side = "YES" if edge_yes > edge_no else "NO"
    return side, p, "; ".join(why) or "the price disagrees with my read"


def decide_poly(s, now):
    sl = s["sleeves"]["poly"]
    for k in list(sl["positions"]):   # settle resolved markets
        p = sl["positions"][k]
        px = poly_price(p)
        if px is None:
            continue
        p["last_px"] = px
        if p.get("resolved"):
            val = p["qty"] * px; sl["cash"] += val; sl["positions"].pop(k)
            _trade(s, "poly", "settled", p["sym"], px, val, f"Resolved {p['resolved']}: I said {p['side']}.", pnl_pct=round((px / p["entry"] - 1) * 100, 2))
    if len(sl["positions"]) < 4:
        try:
            ms = _get(f"{GAMMA}/markets", {"active": "true", "closed": "false", "order": "volume24hr", "ascending": "false", "limit": 500})
        except Exception:
            ms = []
        cands = []
        for m in ms:
            yn = _poly_prices(m)
            try:
                end = dt.datetime.fromisoformat((m.get("endDate") or "").replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if not yn or not (2 * 86400 <= end - now <= 120 * 86400) or not (0.04 <= yn[0] <= 0.96) or (m.get("liquidityNum") or 0) < 50_000:
                continue
            if str(m["id"]) in sl["positions"]:
                continue
            v = poly_view(m, yn[0])
            if v:
                cands.append((bool(_POLY_KW.search(m.get("question") or "")), m.get("volume24hr") or 0, m, yn, v))
        cands.sort(key=lambda c: (-c[0], -c[1]))
        held = {p.get("event") for p in sl["positions"].values()}
        picks = []
        for c in cands:
            ev = str(((c[2].get("events") or [{}])[0]).get("id") or c[2]["id"])
            if ev in held:
                continue   # one bet per event (no doubling the same view via complementary markets)
            held.add(ev); c[2]["_ev"] = ev; picks.append(c)
        for rel, _, m, yn, (side, p, why) in picks[:4 - len(sl["positions"])]:
            px = yn[0] if side == "YES" else yn[1]
            stake = min(sl["cash"], 100.0)
            if stake < 10:
                break
            mid = str(m["id"])
            sl["positions"][mid] = {"sym": (m.get("question") or "")[:90], "kind": "poly", "side": side, "market_id": mid, "slug": m.get("slug"),
                                    "qty": stake / px, "entry": px, "last_px": px, "opened": now, "mirko_p": round(p if side == "YES" else 1 - p, 3),
                                    "end": m.get("endDate"), "event": m.get("_ev"),
                                    "why": f"I say {side} at {p if side == 'YES' else 1 - p:.0%} vs market {px:.0%}: {why}."}
            sl["cash"] -= stake
            _trade(s, "poly", f"bet {side}", (m.get("question") or "")[:60], px, stake, sl["positions"][mid]["why"])
    sl["note"] = "Up to 4 live Polymarket questions, €100 paper stake each, only when my probability differs from the market by 3+ points. Settles at resolution."
    sl["last_decision"] = now


def price_usd(pos: dict) -> float | None:
    k = pos.get("kind")
    if k == "poly":
        return poly_price(pos)
    if k == "dex":
        return dex_price(pos["chain"], pos["address"])
    if k == "stock":
        return stock_price(pos["sym"])
    return crypto_price(pos["sym"])


def us_market_open(now: float | None = None) -> bool:
    from zoneinfo import ZoneInfo
    t = dt.datetime.fromtimestamp(now or time.time(), ZoneInfo("America/New_York"))
    return t.weekday() < 5 and (dt.time(9, 30) <= t.time() <= dt.time(16, 0))


# ---------------- accounting ----------------
def pos_value(p: dict, px: float | None, fx: float) -> float:
    px = px or p.get("last_px") or p["entry"]
    if p.get("side") in ("Long", "Short"):          # perp: margin + pnl
        d = 1 if p["side"] == "Long" else -1
        return max(0.0, p["margin"] + p["margin"] * p["lev"] * d * (px / p["entry"] - 1))
    if p.get("kind") == "poly":
        return p["qty"] * px          # stake is in EUR already; price is the outcome share price
    return p["qty"] * px * fx


def _trade(s, sleeve, action, sym, px_usd, eur, why, **extra):
    s["trades"].append({"ts": time.time(), "sleeve": sleeve, "action": action, "sym": sym, "px_usd": px_usd,
                        "eur": round(eur, 2), "why": why[:220], **extra})


def buy_spot(s, sleeve, sym, eur, px_usd, fx, why, kind="cex", stop_pct=0.12, **meta):
    sl = s["sleeves"][sleeve]
    eur = min(eur, sl["cash"])
    if eur < 10 or not px_usd:
        return
    fee = SPOT_FEE if kind != "stock" else STOCK_SLIP
    qty = eur * (1 - fee) / (px_usd * fx)
    p = sl["positions"].get(sym)
    if p:
        p["entry"] = (p["entry"] * p["qty"] + px_usd * qty) / (p["qty"] + qty); p["qty"] += qty
    else:
        sl["positions"][sym] = {"sym": sym, "kind": kind, "qty": qty, "entry": px_usd, "opened": time.time(), "why": why,
                                "stop": px_usd * (1 - stop_pct), "peak": px_usd, "last_px": px_usd, **meta}
    sl["cash"] -= eur
    _trade(s, sleeve, "buy", sym, px_usd, eur, why)


def sell_spot(s, sleeve, sym, frac, px_usd, fx, why):
    sl = s["sleeves"][sleeve]; p = sl["positions"].get(sym)
    if not p or not px_usd:
        return
    fee = SPOT_FEE if p["kind"] != "stock" else STOCK_SLIP
    q = p["qty"] * min(1.0, frac)
    eur = q * px_usd * fx * (1 - fee)
    pnl = (px_usd / p["entry"] - 1) * 100
    sl["cash"] += eur; p["qty"] -= q
    if p["qty"] <= 1e-12 or frac >= 0.999:
        sl["positions"].pop(sym)
    _trade(s, sleeve, "sell", sym, px_usd, eur, why, pnl_pct=round(pnl, 2))


# ---------------- decisions ----------------
def spot_targets(mr: dict) -> tuple[dict, str]:
    """Target weights from the market read. Cash is a position too."""
    key = (mr.get("regime") or {}).get("key", "chop")
    risk_budget = {"risk_on": 0.85, "chop": 0.6, "risk_off": 0.35}.get(key, 0.5)
    try:
        import insights
        tilt = insights.risk_tilt()
    except Exception:
        tilt = 0.0
    risk_budget = max(0.2, min(0.9, risk_budget * (1 + 0.15 * tilt)))   # daily reading nudges risk, never dominates
    w, notes = {}, []
    tone_w = {"bull": 1.0, "neutral": 0.5, "bear": 0.0}
    core = {a["name"]: tone_w[a["tone"]] * {"BTC": 1.3, "ETH": 1.0, "SOL": 1.0}[a["name"]] for a in mr.get("assets", [])}
    tot = sum(core.values())
    core_budget = risk_budget * 0.75
    for k, v in core.items():
        if tot and v:
            w[k] = {"w": core_budget * v / tot, "kind": "cex", "book": "core", "stop_pct": 0.25,
                    "why": f"Core (long-term): {k} is {next(a['bias'] for a in mr['assets'] if a['name'] == k).lower()}. Sized by the read, wide stop, I don't shake out on noise."}
    for i in (mr.get("mid_caps") or [])[:2]:
        w[i["name"]] = {"w": risk_budget * 0.10, "kind": "cex", "book": "swing", "why": f"Swing: {i['why']}. Half off at +20%, rest trails.", "stop": i["stop"]}
    lows = [l for l in (mr.get("low_caps") or []) if not l.get("spec")] or []
    if lows and key != "risk_off":
        l = lows[0]
        w[l["name"]] = {"w": 0.05, "kind": "dex", "book": "snipe", "why": f"Snipe, low cap ({l['chain']}): {l['why']}. Sized for zero.",
                        "chain": l["chain"], "address": l["address"], "stop": l["stop"]}
    notes.append(f"Regime {mr.get('regime', {}).get('label', '?')}: {risk_budget:.0%} risk budget, rest in cash"
                 + (f" (daily reading tilt {tilt:+.2f})." if tilt else "."))
    return w, " ".join(notes)


def decide_spot(s, mr, fx, now):
    sl = s["sleeves"]["spot"]
    targets, note = spot_targets(mr)
    prices = {sym: price_usd({"kind": t["kind"], "sym": sym, "chain": t.get("chain"), "address": t.get("address")}) for sym, t in targets.items()}
    for sym, p in sl["positions"].items():
        if sym not in prices:
            prices[sym] = price_usd(p)
    eq = sl["cash"] + sum(pos_value(p, prices.get(k), fx) for k, p in sl["positions"].items())
    for sym in list(sl["positions"]):
        if sym not in targets:
            sell_spot(s, "spot", sym, 1.0, prices.get(sym), fx, "Dropped from the plan: the read no longer supports it.")
    for sym, t in targets.items():
        px = prices.get(sym)
        if not px:
            continue
        cur = pos_value(sl["positions"][sym], px, fx) if sym in sl["positions"] else 0.0
        diff = t["w"] * eq - cur
        band = 0.08 if t.get("book") == "core" else 0.03   # core is patient: only rebalance on big drift
        if diff < -band * eq:
            sell_spot(s, "spot", sym, -diff / cur, px, fx, "Trim back to target weight.")
        elif diff > band * eq:
            stop_pct = t["stop_pct"] if t.get("stop_pct") else max(0.08, min(0.30, 1 - t["stop"] / px)) if t.get("stop") and t["stop"] < px else (0.10 if t["kind"] == "cex" and sym in ("BTC", "ETH", "SOL") else 0.15)
            buy_spot(s, "spot", sym, diff, px, fx, t["why"], kind=t["kind"], stop_pct=stop_pct, book=t.get("book", "swing"),
                     **({"chain": t["chain"], "address": t["address"]} if t["kind"] == "dex" else {}))
        if sym in sl["positions"]:
            sl["positions"][sym]["why"] = t["why"]
    sl["note"] = note + " Core majors are long-term holds; mid caps are swings with scale-outs; one low-cap snipe at most."
    sl["last_decision"] = now


def breakout_snipes(mr: dict) -> list:
    """Momentum snipes: a major closing through its nearest resistance with a bullish read, or losing support with a bearish one."""
    out = []
    for a in mr.get("assets", []):
        px, res, sup = a.get("price"), sorted(a.get("resistance") or []), sorted(a.get("support") or [])
        if not px:
            continue
        below = [r for r in res if r < px]; above = [x for x in sup if x > px]
        if a.get("tone") == "bull" and below and px <= below[-1] * 1.02:
            lvl = below[-1]; stop = lvl * 0.985
            out.append({"name": a["name"], "side": "Long", "entry_lo": lvl, "entry_hi": px * 1.002, "stop": stop, "t1": px + 2 * (px - stop), "rr": 2.0,
                        "why": f"breakout snipe: reclaimed {_fmt(lvl)}, tight stop just under it"})
        if a.get("tone") == "bear" and above and px >= above[0] * 0.98:
            lvl = above[0]; stop = lvl * 1.015
            out.append({"name": a["name"], "side": "Short", "entry_lo": px * 0.998, "entry_hi": lvl, "stop": stop, "t1": px - 2 * (stop - px), "rr": 2.0,
                        "why": f"breakdown snipe: lost {_fmt(lvl)}, tight stop just above it"})
    return out


def decide_perps(s, mr, fx, now):
    sl = s["sleeves"]["perps"]
    key = (mr.get("regime") or {}).get("key", "chop")
    eq = sl["cash"] + sum(pos_value(p, crypto_price(p["sym"]), fx) for p in sl["positions"].values())
    for i in breakout_snipes(mr) + list(mr.get("trade_ideas", [])):
        sym = i["name"]
        if sym in sl["positions"] or len(sl["positions"]) >= 3:
            continue
        px = crypto_price(sym)
        if not px:
            continue
        lo, hi = min(i["entry_lo"], i["entry_hi"]), max(i["entry_lo"], i["entry_hi"])
        if not (lo * 0.995 <= px <= hi * 1.005):
            continue  # only fill inside the idea's entry zone, like a resting limit order
        aligned = (i["side"] == "Long" and key == "risk_on") or (i["side"] == "Short" and key == "risk_off")
        lev = MAX_LEV if (aligned and (i.get("rr") or 0) >= 2.5) else 2.0
        stop_dist = abs(px / i["stop"] - 1)
        if stop_dist * lev >= 0.6:   # keep the stop well inside liquidation
            lev = max(1.0, round(0.5 / stop_dist, 1))
        margin = min(sl["cash"], eq * 0.25)
        if margin < 20:
            continue
        sl["cash"] -= margin
        fee = margin * lev * PERP_FEE
        sl["positions"][sym] = {"sym": sym, "kind": "perp", "side": i["side"], "lev": lev, "margin": margin - fee, "entry": px,
                                "stop": i["stop"], "target": i["t1"], "opened": now, "last_px": px, "risk": abs(px - i["stop"]), "scaled": False,
                                "why": f"{i['side']} {sym} {lev:g}x: {i['why']}. Stop {_fmt(i['stop'])}, target {_fmt(i['t1'])} (R:R {i.get('rr')})."}
        _trade(s, "perps", f"open {i['side'].lower()}", sym, px, margin, sl["positions"][sym]["why"], lev=lev)
    sl["note"] = (f"{len(sl['positions'])} open · snipes on breakouts/breakdowns plus level-based ideas, max {MAX_LEV:g}x. "
                  "Tight stops, half off at 1R then the stop goes to breakeven, rest rides to target.")
    sl["last_decision"] = now


def screen_stocks(n=4) -> list:
    syms = {}
    for scr in ("most_actives", "small_cap_gainers", "day_gainers", "aggressive_small_caps", "undervalued_growth_stocks"):
        try:
            qs = _get("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved", {"scrIds": scr, "count": 100})["finance"]["result"][0]["quotes"]
        except Exception:
            continue
        for q in qs:
            px, av, mc = q.get("regularMarketPrice"), q.get("averageDailyVolume3Month") or 0, q.get("marketCap") or 0
            if q.get("quoteType") != "EQUITY" or q.get("exchange") not in ("NMS", "NYQ", "NCM", "NGM", "ASE", "PCX"):
                continue
            if px and 0.5 <= px <= 5 and av >= 2_000_000 and mc >= 50e6 and (q.get("regularMarketChangePercent") or 0) < 25:
                syms[q["symbol"]] = q
    out = []
    for sym, q in list(syms.items())[:40]:
        try:
            _, rows = stock_chart(sym)
        except Exception:
            continue
        cl = [r[1] for r in rows]
        if len(cl) < 60:
            continue
        s20, s50 = sum(cl[-20:]) / 20, sum(cl[-50:]) / 50
        px = cl[-1]
        dv = sum(r[1] * (r[2] or 0) for r in rows[-20:]) / 20
        if not (px > s20 > s50) or px > s20 * 1.25 or dv < 3e6:
            continue
        mom = (px / cl[-21] - 1) * 100
        out.append({"sym": sym, "name": q.get("shortName") or sym, "px": px, "score": mom - (px / s20 - 1) * 100,
                    "why": f"{q.get('shortName') or sym}: ${px:.2f}, above a rising 20D/50D, {mom:+.0f}% 1M, ${dv / 1e6:.0f}M/day traded"})
    return sorted(out, key=lambda x: -x["score"])[:n]


def catalyst_snipes(n=2) -> list:
    """Volume-spike movers: up 5-20% today on 3x+ normal volume, still under $5 and liquid."""
    out = []
    for scr in ("day_gainers", "small_cap_gainers", "most_actives"):
        try:
            qs = _get("https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved", {"scrIds": scr, "count": 100})["finance"]["result"][0]["quotes"]
        except Exception:
            continue
        for q in qs:
            px, av, v, ch = q.get("regularMarketPrice"), q.get("averageDailyVolume3Month") or 0, q.get("regularMarketVolume") or 0, q.get("regularMarketChangePercent") or 0
            if q.get("quoteType") != "EQUITY" or q.get("exchange") not in ("NMS", "NYQ", "NCM", "NGM", "ASE", "PCX"):
                continue
            if px and 0.5 <= px <= 5 and av >= 1_000_000 and v >= 3 * av and 5 <= ch <= 20 and (q.get("marketCap") or 0) >= 50e6:
                out.append({"sym": q["symbol"], "px": px, "score": v / av,
                            "why": f"Catalyst snipe: {q.get('shortName') or q['symbol']} +{ch:.0f}% on {v / av:.1f}x normal volume. Tight 8% stop, half off at +15%."})
    seen, res = set(), []
    for o in sorted(out, key=lambda x: -x["score"]):
        if o["sym"] not in seen: seen.add(o["sym"]); res.append(o)
    return res[:n]


def decide_stocks(s, fx, now, snipes_only=False):
    sl = s["sleeves"]["stocks"]
    eq0 = sl["cash"] + sum(pos_value(p, None, fx) for p in sl["positions"].values())
    nsn = sum(1 for p in sl["positions"].values() if p.get("book") == "catalyst")
    for pk in catalyst_snipes():
        if pk["sym"] in sl["positions"] or nsn >= 2:
            continue
        buy_spot(s, "stocks", pk["sym"], min(sl["cash"], eq0 * 0.10), pk["px"], fx, pk["why"], kind="stock", stop_pct=0.08, book="catalyst"); nsn += 1
    sl["last_snipe"] = now
    if snipes_only:
        return
    picks = screen_stocks()
    keep = {p["sym"] for p in picks}
    for sym in list(sl["positions"]):
        p = sl["positions"][sym]
        if sym not in keep and p.get("book") != "catalyst":
            px = stock_price(sym)
            if px and px < p.get("entry", px) * 1.0 and now - p["opened"] > 3 * 86400:
                sell_spot(s, "stocks", sym, 1.0, px, fx, "Trend faded and it fell off the screen.")
    eq = sl["cash"] + sum(pos_value(p, stock_price(k), fx) for k, p in sl["positions"].items())
    for pk in picks:
        if pk["sym"] in sl["positions"] or sum(1 for p in sl["positions"].values() if p.get("book") != "catalyst") >= 3:
            continue
        buy_spot(s, "stocks", pk["sym"], min(sl["cash"], eq * 0.18), pk["px"], fx, "Patient hold: " + pk["why"], kind="stock", stop_pct=0.15, book="hold")
    sl["note"] = ("Two books: up to 3 patient holds (liquid sub-$5 names in a clean uptrend, ~18% each, 15% stops) and up to 2 catalyst snipes "
                  "(volume spikes, 10% each, 8% stops, half off at +15%). Lots of cash on purpose: I wait for my pitch.")
    sl["last_decision"] = now


def check_stops(s, fx, now):
    for name, sl in s["sleeves"].items():
        for sym in list(sl["positions"]):
            p = sl["positions"][sym]
            if p["kind"] == "stock" and not us_market_open(now):
                continue
            if p["kind"] == "poly":
                continue   # settled in decide_poly
            px = price_usd(p) if p["kind"] != "perp" else crypto_price(sym)
            if not px:
                continue
            p["last_px"] = px
            if p["kind"] == "perp":
                d = 1 if p["side"] == "Long" else -1
                ret = d * (px / p["entry"] - 1)
                hit_stop = (px <= p["stop"]) if d > 0 else (px >= p["stop"])
                hit_tp = (px >= p["target"]) if d > 0 else (px <= p["target"])
                one_r = p.get("risk") and ((px - p["entry"]) * d >= p["risk"])
                if one_r and not p.get("scaled") and not hit_tp:
                    half = pos_value(p, px, fx) * 0.5 * (1 - PERP_FEE * p["lev"])
                    sl["cash"] += half; p["margin"] *= 0.5; p["scaled"] = True; p["stop"] = p["entry"]
                    _trade(s, name, "scale out", sym, px, half, "1R reached: took half, stop to breakeven.", pnl_pct=round(ret * p["lev"] * 100, 2), lev=p["lev"])
                    continue
                if ret * p["lev"] <= -0.9 or hit_stop or hit_tp:
                    val = pos_value(p, px, fx) * (1 - PERP_FEE * p["lev"])
                    sl["cash"] += val; sl["positions"].pop(sym)
                    why = "Liquidation guard" if ret * p["lev"] <= -0.9 else ("Target hit" if hit_tp else "Stop hit")
                    _trade(s, name, f"close {p['side'].lower()}", sym, px, val, f"{why}.", pnl_pct=round(ret * p["lev"] * 100, 2), lev=p["lev"])
                continue
            p["peak"] = max(p.get("peak", px), px)
            tp = {"snipe": 0.30, "swing": 0.20, "catalyst": 0.15}.get(p.get("book"))
            if tp and not p.get("scaled") and px >= p["entry"] * (1 + tp):
                p["scaled"] = True; p["stop"] = max(p["stop"], p["entry"])
                sell_spot(s, name, sym, 0.5, px, fx, f"+{tp:.0%}: took half, the rest rides with a breakeven stop.")
                continue
            if p["peak"] >= p["entry"] * 1.25:             # trail once +25%
                p["stop"] = max(p["stop"], p["peak"] * 0.85)
            if px <= p["stop"]:
                sell_spot(s, name, sym, 1.0, px, fx, "Stop hit." if px < p["entry"] else "Trailing stop locked the gain.")


def snapshot(s, fx) -> dict:
    out, tot = {}, 0.0
    for name, sl in s["sleeves"].items():
        v = sl["cash"] + sum(pos_value(p, None, fx) for p in sl["positions"].values())
        out[name] = round(v, 2); tot += v
    out["total"] = round(tot, 2)
    return out


def tick(now: float | None = None, force=False):
    import market_thoughts as mt
    now = now or time.time()
    s = load(); fx = usd_eur()
    mr = mt.cached(max_age_h=8)
    try:
        check_stops(s, fx, now)
        if mr and (force or now - s["sleeves"]["spot"]["last_decision"] >= CRYPTO_EVERY_H * 3600):
            decide_spot(s, mr, fx, now)
        if mr and (force or now - s["sleeves"]["perps"]["last_decision"] >= CRYPTO_EVERY_H * 3600):
            decide_perps(s, mr, fx, now)
        if force or now - s["sleeves"]["poly"]["last_decision"] >= 86400:
            decide_poly(s, now)
        last = dt.datetime.fromtimestamp(s["sleeves"]["stocks"]["last_decision"], dt.timezone.utc).date()
        if (force or us_market_open(now)) and (force or last != dt.datetime.fromtimestamp(now, dt.timezone.utc).date()):
            decide_stocks(s, fx, now)
        elif us_market_open(now) and now - s["sleeves"]["stocks"].get("last_snipe", 0) >= 3600:
            decide_stocks(s, fx, now, snipes_only=True)   # catalyst scan every hour while the market is open
    except Exception as e:
        print(f"[PORTFOLIO] tick error: {type(e).__name__}: {str(e)[:120]}")
    snap = snapshot(s, fx)
    s["history"].append({"ts": now, **snap})
    s["fx"] = fx
    save(s)
    return s


def commentary(s: dict, include_poly: bool = False) -> dict:
    """Mirko's own words on why he holds what he holds."""
    sp, pe, st = (s["sleeves"][k] for k in ("spot", "perps", "stocks"))
    intro = ("Sniper with a long memory: I hold a patient core and hunt a few sharp entries around it. "
             "Spot: BTC/ETH/SOL are long-term holds sized by my market read with wide stops, plus swing trades in mid caps that are being accumulated "
             "and at most one low-cap snipe sized for zero. Winners pay me early (half off), the rest trails. "
             "Perps: breakout and breakdown snipes plus level-based ideas, max 3x, tight stops, half off at 1R and the stop goes to breakeven. "
             "Penny stocks: catalyst snipes on volume spikes with 8% stops, and a few patient trend holds. "
             "Discipline first: cash is a position, I never chase a candle I didn't see build, and every trade has an exit before it has an entry. "
             "My daily reading (fund filings, central banks, policy, tech) tilts how much risk I take.")
    ks = [k for k in s["sleeves"] if include_poly or k != "poly"]
    return {"intro": intro, "sleeves": {k: s["sleeves"][k].get("note", "") for k in ks},
            "positions": [{"sleeve": k, "sym": p["sym"] if p.get("kind") == "poly" else sym, "why": p.get("why", "")} for k in ks for sym, p in s["sleeves"][k]["positions"].items()]}


def public_view(include_poly: bool = False) -> dict:
    s = load(); fx = s.get("fx") or 0.86
    sleeves = {}
    for name, sl in s["sleeves"].items():
        if name == "poly" and not include_poly:
            continue   # Polymarket lives on the Predictions tab
        pos = []
        for sym, p in sl["positions"].items():
            v = pos_value(p, None, fx)
            cost = p["margin"] if p["kind"] == "perp" else p["qty"] * p["entry"] * (1 if p["kind"] == "poly" else fx)
            pos.append({"sym": sym, "kind": p["kind"], "side": p.get("side", "Spot"), "lev": p.get("lev"), "entry_usd": p["entry"],
                        "last_usd": p.get("last_px"), "value_eur": round(v, 2), "pnl_pct": round((v / cost - 1) * 100, 2) if cost else None,
                        "stop_usd": p.get("stop"), "target_usd": p.get("target"), "opened": p.get("opened"), "why": p.get("why", ""),
                        "chain": p.get("chain"), "book": p.get("book"), "mirko_p": p.get("mirko_p"), "title": p["sym"] if p["kind"] == "poly" else None, "slug": p.get("slug"), "end": p.get("end")})
        val = sl["cash"] + sum(x["value_eur"] for x in pos)
        sleeves[name] = {"value_eur": round(val, 2), "cash_eur": round(sl["cash"], 2), "pnl_pct": round((val / sl["start"] - 1) * 100, 2),
                         "positions": sorted(pos, key=lambda x: -x["value_eur"]), "last_decision": sl["last_decision"], "note": sl.get("note", "")}
    tot = sum(v["value_eur"] for v in sleeves.values())
    start_tot = sum(s["sleeves"][k]["start"] for k in sleeves)
    hist = s["history"][-500:]
    return {"simulated": True, "currency": "EUR", "created": s["created"], "total_eur": round(tot, 2),
            "pnl_pct": round((tot / start_tot - 1) * 100, 2), "start_eur": start_tot, "sleeves": sleeves,
            "history": [{"t": h["ts"], "v": round(sum(h.get(k, 0) for k in sleeves), 2)} for h in hist[:: max(1, len(hist) // 200)]],
            "trades": [t for t in reversed(s["trades"][-80:]) if include_poly or t.get("sleeve") != "poly"][:60], "commentary": commentary(s, include_poly)}


_live = {"ts": 0.0, "data": None}
_live_lock = threading.Lock()


def live_prices(max_age=20.0) -> dict:
    """Light refresh for the site: current price + PnL per open position, cached ~20s so visitors can't hammer upstream APIs."""
    with _live_lock:
        if _live["data"] and time.time() - _live["ts"] < max_age:
            return _live["data"]
        s = load(); fx = s.get("fx") or 0.86
        from concurrent.futures import ThreadPoolExecutor
        items = [(n, sym, p) for n, sl in s["sleeves"].items() for sym, p in sl["positions"].items()]
        def px_of(it):
            _, sym, p = it
            try:
                return crypto_price(sym) if p["kind"] == "perp" else price_usd(p)
            except Exception:
                return None
        with ThreadPoolExecutor(6) as ex:
            pxs = list(ex.map(px_of, items))
        out = {}
        for (n, sym, p), px in zip(items, pxs):
            px = px or p.get("last_px")
            v = pos_value(p, px, fx); cost = p["margin"] if p["kind"] == "perp" else p["qty"] * p["entry"] * (1 if p["kind"] == "poly" else fx)
            out.setdefault(n, {})[sym] = {"last_usd": px, "value_eur": round(v, 2), "pnl_pct": round((v / cost - 1) * 100, 2) if cost else None}
        _live.update(ts=time.time(), data={"ts": time.time(), "positions": out})
        return _live["data"]


_started = False


def start():
    global _started
    if _started or os.environ.get("PAPER_PORTFOLIO_ENABLED", "true").lower() in ("0", "false", "no"):
        return
    _started = True

    def loop():
        time.sleep(120)
        while True:
            try:
                tick()
            except Exception as e:
                print(f"[PORTFOLIO] loop error: {type(e).__name__}")
            time.sleep(TICK_S)
    threading.Thread(target=loop, daemon=True, name="paper-portfolio").start()
    print("[PORTFOLIO] paper portfolio on (simulated): crypto every 4h, stocks daily in US hours, stops every 15 min")
