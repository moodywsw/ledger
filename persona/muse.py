"""Musings: genuine non-trade thoughts (market, culture, narratives, KOLs, macro, AI, bot life).

Inputs are all free: CoinGecko trending, alternative.me Fear & Greed, crypto RSS headlines,
plus Ledger's own mood, beliefs and facts. Every fetch fails soft.
"""
import os, random, re, time
import xml.etree.ElementTree as ET
import requests

from . import mood as moodmod

TOPICS = ["world", "world_markets", "market_read", "market", "culture", "narratives", "kols", "macro", "ai", "bot_life", "lessons", "headline", "trending"]
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
            "world": headlines(8, WORLD_RSS)}


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
    if not ctx.get("market_read"):
        avail = [t for t in avail if t != "market_read"]
    if not ctx.get("beliefs"):
        avail = [t for t in avail if t != "lessons"]
    return rng.choice(avail or ["culture", "bot_life", "ai"])


def template(topic: str, ctx: dict, recent_texts: list, rng=random) -> str:
    m = ctx.get("mood_state") or {}
    fng = ctx.get("fng") or {}
    hl = (ctx.get("headlines") or [""])
    wh, wt = pick_world(ctx, rng)
    vals = {"wh": wh[:120], "wtake": wt or "uncertainty widens ranges", "wtake_cap": (wt or "uncertainty widens ranges")[:1].upper() + (wt or "uncertainty widens ranges")[1:],
            "fng": fng.get("value", "?"), "fngl": fng.get("label", "?"), "fngl_low": str(fng.get("label", "uncertain")).lower(),
            "fng_take": _fng_take(fng.get("value")), "trend": ", ".join(ctx.get("trending") or [])[:90],
            "hl": rng.choice(hl), "belief": rng.choice(ctx.get("beliefs") or ["size small, think big."]),
            "lessons": ctx.get("lessons_count", 0), **_mr_vals(ctx.get("market_read")), "mood": moodmod.label(m), "em": moodmod.emoji(m)}
    opts = T.get(topic, T["culture"])
    fresh = [o for o in opts if o.split("{")[0][:40] not in " ".join(recent_texts)] or opts
    try:
        return " ".join(rng.choice(fresh).format(**vals).split())
    except Exception:
        return rng.choice(T["culture"])
