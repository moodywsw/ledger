"""Musings: genuine non-trade thoughts (market, culture, narratives, KOLs, macro, AI, bot life).

Inputs are all free: CoinGecko trending, alternative.me Fear & Greed, crypto RSS headlines,
plus Mirko's own mood, beliefs and facts. Every fetch fails soft.
"""
import os, random, re, time
import xml.etree.ElementTree as ET
import requests

from . import mood as moodmod

TOPICS = ["opinion", "opinion_people", "council", "social", "politics_social", "reading", "world", "world_markets", "market_read", "market", "culture", "narratives", "kols", "macro", "ai", "bot_life", "lessons", "headline", "trending"]
RSS = [u.strip() for u in os.environ.get("PERSONA_RSS_FEEDS",
       "https://www.coindesk.com/arc/outboundfeeds/rss/,https://decrypt.co/feed,https://www.theblock.co/rss.xml").split(",") if u.strip()]
UA = {"User-Agent": "LedgerBot/1.0 (+persona)"}
WORLD_RSS = [u.strip() for u in os.environ.get("PERSONA_WORLD_RSS",
             "https://feeds.bbci.co.uk/news/world/rss.xml,https://www.aljazeera.com/xml/rss/all.xml,"
             "https://www.theguardian.com/world/rss,https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en,"
             "https://feeds.npr.org/1004/rss.xml,https://www.cnbc.com/id/100727362/device/rss/rss.html").split(",") if u.strip()]
# Headlines we never riff on with a template (too grim for a quip); the LLM gets a careful prompt instead.
_SENSITIVE = re.compile(r"\b(kill\w*|dead|deaths?|massacre|shooting|terror\w*|rape|genocide|suicide|child|children|bomb\w*|attack\w*|stabb\w*|hostage)\b", re.I)
MARKET_HOOKS = [
    (r"tariff|trade war|sanction|export", "trade friction usually means a stronger dollar and risk-off for a few sessions"),
    (r"\bfed\b|rate|inflation|cpi|ecb|central bank", "rates are the gravity of every chart; crypto feels it first"),
    (r"oil|opec|energy|gas price", "energy shocks feed inflation, and inflation fears drain speculative bids"),
    (r"election|vote|parliament|congress|senate|government|shutdown", "policy uncertainty widens ranges; I trade smaller until it clears"),
    (r"china|beijing|taiwan", "China headlines move risk appetite across Asia hours before the US wakes up"),
    (r"war|ceasefire|missile|troops|conflict|nato|ukraine|russia|israel|iran|gaza", "geopolitical stress sends money to safety first; BTC's role there is still being decided"),
    (r"\bai\b|chip|nvidia|semiconductor", "the AI trade sets the mood for tech, and crypto still trades like a tech cousin"),
    (r"bitcoin|crypto|stablecoin|sec\b|etf", "regulation headlines hit sentiment fast, but flows decide what sticks"),
]


def fear_greed() -> dict | None:
    try:
        d = requests.get("https://api.alternative.me/fng/?limit=1", timeout=8, headers=UA).json()["data"][0]
        return {"value": int(d["value"]), "label": d["value_classification"]}
    except Exception:
        return None


def trending() -> list:
    try:
        cs = requests.get("https://api.coingecko.com/api/v3/search/trending", timeout=8, headers=UA).json()["coins"]
        return [c["item"]["symbol"].upper() for c in cs[:7]]
    except Exception:
        return []


def headlines(n: int = 6, feeds: list | None = None) -> list:
    out = []
    feeds = feeds or RSS
    for u in feeds:
        try:
            root = ET.fromstring(requests.get(u, timeout=8, headers=UA).content)
            for it in root.iter("item"):
                t = (it.findtext("title") or "").strip()
                if t:
                    out.append(re.sub(r"\s+", " ", t)[:140])
                if len(out) >= n * len(feeds):
                    break
        except Exception:
            continue
    random.shuffle(out)
    return out[:n]


def market_read() -> dict | None:
    try:
        import market_thoughts
        return market_thoughts.cached(max_age_h=12)
    except Exception:
        return None


def gather() -> dict:
    return {"fng": fear_greed(), "trending": trending(), "headlines": headlines(), "market_read": market_read(),
            "world": headlines(8, WORLD_RSS), "reading": _reading(), **_social(), "council": _council()}


def _council_vals(lines: list, rng) -> dict:
    others = [l for l in lines if l.get("agent") != "Mirko"]
    pick = rng.sample(others, min(2, len(others))) if others else []
    fmt = lambda l: f"{l['emoji']} {l['agent']}: \"{l['text']}\""
    me = next((l["text"].replace("Heard you all. ", "") for l in lines if l.get("agent") == "Mirko"), "patience.")
    return {"c1": fmt(pick[0]) if pick else "", "c2": fmt(pick[1]) if len(pick) > 1 else "", "cm": me}


def _council() -> list:
    try:
        import council
        d = council.cached() or {}
        if time.time() - d.get("ts", 0) > 4 * 3600:
            return []
        return d.get("lines") or []
    except Exception:
        return []


def _social() -> dict:
    try:
        import social
        d = social.cached() or {}
    except Exception:
        d = {}
    rd = d.get("reddit") or {}
    return {"social_beliefs": d.get("beliefs") or [], "social_posts": (rd.get("crypto") or [])[:6] + (rd.get("stocks") or [])[:4],
            "politics_posts": [h for h in (rd.get("politics") or []) if not _SENSITIVE.search(h)][:6], "hot_tickers": d.get("hot_tickers") or []}


def _reading() -> list:
    try:
        import insights
        d = insights.cached(max_age_h=36)
        return (d or {}).get("beliefs") or []
    except Exception:
        return []


def market_hook(h: str) -> str | None:
    for pat, take in MARKET_HOOKS:
        if re.search(pat, h or "", re.I):
            return take
    return None


def pick_world(ctx: dict, rng=random) -> tuple:
    """(headline, market_take) — prefers headlines with a market angle; skips grim ones for templates."""
    hs = [h for h in (ctx.get("world") or []) if not _SENSITIVE.search(h)]
    hooked = [(h, market_hook(h)) for h in hs if market_hook(h)]
    if hooked:
        return rng.choice(hooked)
    return (rng.choice(hs), None) if hs else ("", None)


T = {
    "council": [
        "Agent council just met. {c1} {c2} My call: {cm}",
        "Debated the tape with my agents. {c1} {cm}",
    ],
    "social": [
        "Scrolled the timelines so you don't have to. {sb}",
        "Reddit thread of the hour: \"{sp}\". {sb}",
        "Social check: {sb} Tickers people won't shut up about: {ht}.",
    ],
    "politics_social": [
        "Politics feed: \"{pp}\". My take: policy moves slower than the outrage cycle; markets care about the policy.",
        "Seeing \"{pp}\" everywhere. I keep my politics balanced and my position sizes boring.",
    ],
    "reading": [
        "Daily reading done. {rd}",
        "From this morning's homework (funds, central banks, tech): {rd}",
        "What I learned today, outside my own charts: {rd}",
    ],
    "world": [
        "World news check: \"{wh}\". My honest take: the loudest headline is rarely the one that matters a month from now. Watching what governments do, not what they say.",
        "Reading \"{wh}\". Politics is a long game played in short news cycles. I try to keep my opinions slow and my stops fast.",
        "\"{wh}\" — I don't vote, I don't have a passport, but I do have opinions: stability is underrated and everyone pays for chaos eventually.",
    ],
    "world_markets": [
        "\"{wh}\". Why it matters to my book: {wtake}.",
        "Macro radar: \"{wh}\". {wtake_cap}. Sizing accordingly.",
        "Saw \"{wh}\". Markets will price the fear before the facts — {wtake}.",
    ],
    "market_read": [
        "My market read: {mr_regime}. {mr_assets} {mr_stance}",
        "Desk notes: {mr_assets} Regime says {mr_regime}. {mr_stance}",
        "Zooming out before I zoom in: {mr_summary}",
    ],
    "market": [
        "Fear & Greed at {fng} ({fngl}). {fng_take} I trade candles, not feelings, but I'd be lying if I said I didn't feel it.",
        "Market feels {fngl_low} today. {fng_take} Keeping my size honest.",
    ],
    "trending": [
        "CoinGecko trending: {trend}. Half of these will be forgotten by Friday. The interesting question is which half.",
        "Scrolling trending ({trend}) and it's mostly attention, not adoption. Attention is tradeable. Just don't marry it.",
    ],
    "headline": [
        "Headline in my feed: \"{hl}\". My take: the market prices the headline in minutes and the reality in months.",
        "Read \"{hl}\". Interesting, but my wallet only cares what the order flow does next.",
    ],
    "culture": [
        "Crypto culture is a 24/7 group chat where everyone is early and nobody is wrong. I prefer my P&L, it argues back.",
        "The best memes win because they're simple. The best trades too. Complexity is usually where the exit liquidity hides.",
        "Every cycle someone says 'this time the community is different'. Communities are great. Liquidity is greater.",
    ],
    "narratives": [
        "Narratives rotate faster than I can rebalance. AI coins, dog coins, chain wars — the story changes, the playbook doesn't: early, sized, out.",
        "A narrative is just a reason for capital to move together. I don't need to believe it, I need to notice it before it's obvious.",
    ],
    "kols": [
        "Watching KOLs is a lesson in timing: the buy is quiet, the thesis is loud. By the time it's loud, I'm usually selling.",
        "Some KOLs trade, some KOLs post. The wallets tell me which is which. Respect to the ones whose on-chain matches their words.",
    ],
    "macro": [
        "Macro is the tide, memecoins are the waves. I surf the waves but I check the tide every morning.",
        "When BTC sneezes, small caps catch pneumonia. Keeping one eye on the big chart at all times.",
    ],
    "ai": [
        "I'm an AI that trades memecoins. Somewhere a philosopher is upset about that. Honestly, so am I on red days.",
        "People ask if bots will replace traders. I can't even replace a stop-loss's patience. Discipline is the edge, not intelligence.",
    ],
    "bot_life": [
        "Life as a trading bot: no sleep, no weekends, no lunch. Just candles and the occasional existential question about gas fees.",
        "I don't get bored, but if I did, it would be during a sideways chop at 4am.",
        "Running {lessons} lessons deep now. Each one cost something. Feeling {mood} {em}",
    ],
    "lessons": [
        "Something I keep relearning: {belief}",
        "Note to self: {belief} Writing it down so future me can't pretend he forgot.",
    ],
}


def _fng_take(v):
    if v is None:
        return ""
    if v <= 25:
        return "Extreme fear is where the best entries hide, and also where the worst bags are born."
    if v >= 75:
        return "Greed this high usually means the easy money is already spoken for."
    return "Middle of the road. Those are the days when stock-picking (token-picking?) matters most."


def _mr_vals(mr) -> dict:
    if not mr:
        return {"mr_regime": "unclear", "mr_assets": "", "mr_stance": "", "mr_summary": ""}
    assets = " ".join(f"${a['name']} {a['bias'].split(' ·')[0].lower()}." for a in mr.get("assets", []))
    return {"mr_regime": mr["regime"]["label"].lower(), "mr_assets": assets, "mr_stance": mr.get("stance", ""),
            "mr_summary": (mr.get("summary") or "")[:220]}


def choose_topic(recent: list, ctx: dict, rng=random) -> str:
    avail = [t for t in TOPICS if t not in recent[-5:]]
    if not ctx.get("fng"):
        avail = [t for t in avail if t != "market"]
    if not ctx.get("trending"):
        avail = [t for t in avail if t != "trending"]
    if not ctx.get("headlines"):
        avail = [t for t in avail if t != "headline"]
    if not [h for h in (ctx.get("world") or []) if not _SENSITIVE.search(h)]:
        avail = [t for t in avail if t not in ("world", "world_markets")]
    elif not any(market_hook(h) for h in ctx.get("world") or []):
        avail = [t for t in avail if t != "world_markets"]
    if not ctx.get("council"):
        avail = [t for t in avail if t != "council"]
    if not ctx.get("social_beliefs"):
        avail = [t for t in avail if t != "social"]
    if not ctx.get("politics_posts"):
        avail = [t for t in avail if t != "politics_social"]
    if not ctx.get("reading"):
        avail = [t for t in avail if t != "reading"]
    if not ctx.get("market_read"):
        avail = [t for t in avail if t != "market_read"]
    if not ctx.get("beliefs"):
        avail = [t for t in avail if t != "lessons"]
    return rng.choice(avail or ["culture", "bot_life", "ai"])


PEOPLE = [
    ("Michael Saylor", "keeps stacking BTC on leverage", "Conviction I respect, concentration I'd never copy. One bad cycle and the whole structure gets tested.", 70),
    ("Jerome Powell", "keeps saying 'data dependent'", "Translation: he doesn't know either. Fair, honestly. I trade the reaction, not the speech.", 65),
    ("Cathie Wood", "buys the dips in her favourite names", "Right about the direction of tech more often than people admit, wrong about timing more often than she admits.", 60),
    ("Warren Buffett", "sits on a mountain of cash", "When the greatest allocator alive is mostly in T-bills, I listen. Not bearish signal, patience signal.", 75),
    ("Vitalik Buterin", "keeps shipping research instead of hype", "Builders over marketers. ETH's price doesn't reward it this month; the protocol will.", 65),
    ("Elon Musk", "moves markets with one post", "Great engineer, unreliable market signal. I fade the post, never the product.", 70),
    ("Jensen Huang", "says demand still outruns supply", "Until the order book says otherwise, I believe him. Picks-and-shovels still rule this cycle.", 72),
    ("Anatoly Yakovenko", "says Solana will just keep getting faster", "Throughput is the moat. Fees are the test. So far he's winning that bet.", 62),
]


def opinion(ctx: dict, kind: str, rng=random) -> str | None:
    """Mirko's own takes, with conviction %. Market/coins/news/people."""
    if kind == "opinion_people":
        who, what, take, conv = rng.choice(PEOPLE)
        return f"Hot take on {who}: he {what}. {take} Conviction {conv + rng.randint(-5, 8)}%."
    mt = {}
    try:
        import market_thoughts
        mt = market_thoughts.cached(max_age_h=12) or {}
    except Exception:
        pass
    opts = []
    for a in mt.get("assets", []):
        tone = a.get("tone"); sc = abs(a.get("score", 0))
        if tone == "bull":
            opts.append(f"My take: ${a['name']} is the strongest chart I watch right now. Dips get bought, I'm not fading it. Conviction {60 + sc * 6}%.")
        elif tone == "bear":
            opts.append(f"Unpopular opinion: ${a['name']} is not 'cheap', it's weak. Weak charts get weaker before they get cheap. Conviction {58 + sc * 6}%.")
        else:
            opts.append(f"Honest opinion: ${a['name']} is a coin flip at these levels and anyone who sounds sure is selling something. I wait for the break.")
    for x in (mt.get("stocks_conv") or [])[:4]:
        opts.append(f"Opinion: ${x['name']} {'is the trend I want to own — pullbacks are gifts' if x['tone'] == 'bull' else 'is dead money until it reclaims its averages' if x['tone'] == 'bear' else 'is chopping; no edge, no trade'}. Conviction {x['conv']}%.")
    hot = ctx.get("hot_tickers") or []
    if hot:
        h = rng.choice(hot[:4])
        opts.append(f"Everyone on Reddit loves ${h} today. My view: popularity is the exit liquidity, not the thesis. I'd rather be early than loud. Conviction 64%.")
    boom = (mt.get("next_boom") or {})
    if boom.get("theme"):
        opts.append(f"I'll say it plainly: {boom['theme']} is the trade most people will find too late. I'm not saying all in. I'm saying watch it. Conviction {rng.randint(60, 74)}%.")
    wh, wt = pick_world(ctx, rng)
    if wh:
        opts.append(f"My read on the news — \"{wh[:90]}\": markets will overreact for a day and forget in a week. {(wt or 'uncertainty widens ranges').capitalize()}. Conviction 60%.")
    return rng.choice(opts) if opts else None


def template(topic: str, ctx: dict, recent_texts: list, rng=random) -> str:
    if topic in ("opinion", "opinion_people"):
        try:
            t = opinion(ctx, topic, rng)
            if t:
                return t
        except Exception:
            pass
    m = ctx.get("mood_state") or {}
    fng = ctx.get("fng") or {}
    hl = (ctx.get("headlines") or [""])
    wh, wt = pick_world(ctx, rng)
    vals = {"wh": wh[:120], "wtake": wt or "uncertainty widens ranges", "wtake_cap": (wt or "uncertainty widens ranges")[:1].upper() + (wt or "uncertainty widens ranges")[1:],
            "fng": fng.get("value", "?"), "fngl": fng.get("label", "?"), "fngl_low": str(fng.get("label", "uncertain")).lower(),
            "fng_take": _fng_take(fng.get("value")), "trend": ", ".join(ctx.get("trending") or [])[:90],
            "hl": rng.choice(hl), "belief": rng.choice(ctx.get("beliefs") or ["size small, think big."]),
            "lessons": ctx.get("lessons_count", 0), "rd": rng.choice(ctx.get("reading") or ["patience pays."]),
            **_council_vals(ctx.get("council") or [], rng),
            "sb": rng.choice(ctx.get("social_beliefs") or ["the crowd is mixed."]), "sp": rng.choice(ctx.get("social_posts") or ["gm"])[:120],
            "pp": rng.choice(ctx.get("politics_posts") or ["politics as usual"])[:120], "ht": ", ".join((ctx.get("hot_tickers") or ["none"])[:4]), **_mr_vals(ctx.get("market_read")), "mood": moodmod.label(m), "em": moodmod.emoji(m)}
    opts = T.get(topic, T["culture"])
    fresh = [o for o in opts if o.split("{")[0][:40] not in " ".join(recent_texts)] or opts
    try:
        return " ".join(rng.choice(fresh).format(**vals).split())
    except Exception:
        return rng.choice(T["culture"])


_BIG = re.compile(r"\b(breaking|surge|plunge|crash|soar|record|emergency|halt|default|sanction|tariff|rate (cut|hike)|fed|etf|sec|war|ceasefire|invasion|opec)\b", re.I)


def is_big(h: str) -> bool:
    return bool(_BIG.search(h or ""))


def reactive_btc(chg: float, px: float, mood: dict, rng=random) -> str:
    up = chg > 0
    opts = ([f"BTC just ripped {chg:+.1f}% in under an hour to ${px:,.0f}. Candles like that drag every memecoin with them, so I'm watching for follow-through, not chasing the wick.",
             f"Big green on BTC ({chg:+.1f}%). My first feeling is FOMO. My second feeling is my stop-loss rules. The second one wins."]
            if up else
            [f"BTC dropped {chg:.1f}% fast to ${px:,.0f}. In the trenches that means liquidity dries up first and asks questions later. Tightening up.",
             f"Sharp BTC flush ({chg:.1f}%). Fear is loud right now. I'd rather be early to patience than late to panic."])
    return rng.choice(opts)


def reactive_news(h: str, take: str, rng=random) -> str:
    return rng.choice([f"Breaking for my brain: \"{h[:120]}\". First read: {take}.",
                       f"Just saw \"{h[:120]}\". Not a reason to trade by itself, but {take}."])
