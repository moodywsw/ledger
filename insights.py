"""Mirko's daily reading: the world of trading, distilled into a few beliefs.

Free, light sources (each optional, failures ignored):
- SEC EDGAR submissions API: fresh 13F-HR filings from famous funds
- ARK daily trades (arkfunds.io, free)
- Central banks & policy RSS: Fed, ECB, White House, PBoC/China via Google News RSS
- US / China politics via Google News RSS
- Tech/AI engineering: Hacker News top stories, arXiv cs.AI
Distilled rule-based into `beliefs` + a small `risk_tilt` in [-1, 1] used by the
persona, the market read and the paper portfolio. Refreshed every ~12h, cached on disk.
"""
from __future__ import annotations

import json, os, re, threading, time, datetime as dt
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

EVERY_H = float(os.environ.get("INSIGHTS_EVERY_H", "12"))
UA = {"User-Agent": os.environ.get("SEC_USER_AGENT", "Mirko Research ledger-research@example.com")}
FUNDS = {"Berkshire Hathaway": "0001067983", "Bridgewater": "0001350694", "Renaissance Technologies": "0001037389",
         "ARK Invest": "0001697748", "Pershing Square": "0001336528", "Scion (Burry)": "0001649339",
         "Duquesne (Druckenmiller)": "0001536411", "Tiger Global": "0001167483"}
GN = "https://news.google.com/rss/search?hl=en-US&gl=US&ceid=US:en&q="
RSS = {
    "fed": ["https://www.federalreserve.gov/feeds/press_all.xml"],
    "ecb": ["https://www.ecb.europa.eu/rss/press.html"],
    "policy_us": [GN + "White+House+economy+OR+tariffs+when:2d"],
    "china": [GN + "PBoC+OR+China+stimulus+OR+Beijing+economy+when:2d"],
    "funds": [GN + "13F+hedge+fund+stake+when:7d"],
    "arxiv": ["https://export.arxiv.org/rss/cs.AI"],
}


def _path() -> Path:
    return Path(os.environ.get("DATA_DIR", "data")) / "insights.json"


def _rss(url: str, n=8) -> list:
    try:
        root = ET.fromstring(requests.get(url, timeout=10, headers=UA).content)
    except Exception:
        return []
    out = []
    for it in list(root.iter("item"))[:n] or list(root.iter("{http://purl.org/rss/1.0/}item"))[:n]:
        t = it.findtext("title") or it.findtext("{http://purl.org/rss/1.0/}title") or ""
        t = re.sub(r"\s+", " ", t).strip()
        if t:
            out.append(t[:160])
    return out


def fund_filings(days=10) -> list:
    out, cutoff = [], (dt.date.today() - dt.timedelta(days=days)).isoformat()
    for name, cik in FUNDS.items():
        try:
            r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", timeout=10, headers=UA).json()["filings"]["recent"]
        except Exception:
            continue
        for form, date in zip(r.get("form", [])[:40], r.get("filingDate", [])[:40]):
            if form.startswith("13F") and date >= cutoff:
                out.append({"fund": name, "date": date, "form": form}); break
        time.sleep(0.15)   # SEC fair-access: well under 10 req/s
    return out


def ark_trades() -> list:
    try:
        r = requests.get("https://arkfunds.io/api/v2/etf/trades", params={"symbol": "ARKK"}, timeout=10, headers=UA).json()
        return [{"dir": t.get("direction"), "ticker": t.get("ticker"), "date": t.get("date")} for t in (r.get("trades") or [])[:12]]
    except Exception:
        return []


def hn_top(n=10) -> list:
    try:
        ids = requests.get("https://hacker-news.firebaseio.com/v0/topstories.json", timeout=8).json()[:n]
        with ThreadPoolExecutor(5) as ex:
            items = list(ex.map(lambda i: requests.get(f"https://hacker-news.firebaseio.com/v0/item/{i}.json", timeout=8).json(), ids))
        return [x.get("title", "")[:140] for x in items if x and x.get("title")]
    except Exception:
        return []


_HAWK = re.compile(r"\b(hike|hawkish|inflation (rises|hot|sticky)|tighten|higher for longer|tariff)", re.I)
_DOVE = re.compile(r"\b(cut|dovish|easing|stimulus|liquidity injection|rrr|lower rates|pause)", re.I)
_AI = re.compile(r"\b(AI|LLM|GPU|model|agent|inference|transformer|Nvidia|OpenAI|Anthropic|DeepSeek)\b")


def distill(raw: dict) -> dict:
    beliefs, tilt = [], 0.0
    cb = raw.get("fed", []) + raw.get("ecb", []) + raw.get("china", [])
    hawk, dove = sum(bool(_HAWK.search(h)) for h in cb), sum(bool(_DOVE.search(h)) for h in cb)
    if dove > hawk:
        tilt += 0.3; beliefs.append(f"Central banks lean easier ({dove} easing headlines vs {hawk} hawkish). Liquidity tailwind: I can carry a bit more risk.")
    elif hawk > dove:
        tilt -= 0.3; beliefs.append(f"Policy talk leans tight ({hawk} hawkish vs {dove} easing). Cash and patience earn their keep.")
    else:
        beliefs.append("Central banks are balanced today. No macro excuse in either direction; the chart decides.")
    china = raw.get("china", [])
    if any(re.search(r"stimulus|easing|support", h, re.I) for h in china):
        tilt += 0.15; beliefs.append("China is talking support again. Historically good for risk appetite in Asia hours.")
    pol = raw.get("policy_us", [])
    if sum(bool(re.search(r"tariff|sanction|shutdown", h, re.I)) for h in pol) >= 2:
        tilt -= 0.2; beliefs.append("Washington headlines are about tariffs/sanctions/shutdowns. Expect headline volatility; tighten stops.")
    f = raw.get("filings", [])
    if f:
        beliefs.append("Fresh 13F filings from " + ", ".join(x["fund"] for x in f[:4]) + ". Smart money shows its hand with a 45-day lag; I read it for themes, not entries.")
    ark = raw.get("ark", [])
    if ark:
        buys = [t["ticker"] for t in ark if (t.get("dir") or "").lower() == "buy"][:4]
        sells = [t["ticker"] for t in ark if (t.get("dir") or "").lower() == "sell"][:4]
        beliefs.append(f"ARK this week: buying {', '.join(buys) or 'nothing'}, selling {', '.join(sells) or 'nothing'}. Cathie buys volatility; I note the direction, not the conviction.")
    tech = raw.get("hn", []) + raw.get("arxiv", [])
    ai = sum(bool(_AI.search(h)) for h in tech)
    if tech:
        beliefs.append(f"Engineers are talking about AI in {ai} of {len(tech)} top tech stories. " + ("The AI trade still has builders behind it." if ai >= len(tech) / 3 else "Builders are quieter on AI than the market is."))
    items = ([{"cat": "fed/ecb", "title": h} for h in (raw.get("fed", [])[:3] + raw.get("ecb", [])[:2])]
             + [{"cat": "china", "title": h} for h in china[:3]] + [{"cat": "us policy", "title": h} for h in pol[:3]]
             + [{"cat": "funds", "title": h} for h in raw.get("funds", [])[:3]] + [{"cat": "13F", "title": f"{x['fund']} filed {x['form']} on {x['date']}"} for x in f]
             + [{"cat": "tech", "title": h} for h in raw.get("hn", [])[:4]] + [{"cat": "arXiv AI", "title": h} for h in raw.get("arxiv", [])[:2]])
    return {"beliefs": beliefs[:6], "risk_tilt": round(max(-1.0, min(1.0, tilt)), 2), "items": items[:24]}


def build() -> dict:
    with ThreadPoolExecutor(6) as ex:
        futs = {k: ex.submit(lambda us: sum((_rss(u) for u in us), []), v) for k, v in RSS.items()}
        f_hn, f_ark = ex.submit(hn_top), ex.submit(ark_trades)
        raw = {k: f.result() for k, f in futs.items()}
        raw["hn"], raw["ark"] = f_hn.result(), f_ark.result()
    raw["filings"] = fund_filings()
    d = distill(raw)
    d.update(ts=time.time(), date=dt.date.today().isoformat(), sources={k: len(v) for k, v in raw.items()})
    return d


def cached(max_age_h: float | None = None) -> dict | None:
    try:
        d = json.loads(_path().read_text())
    except Exception:
        return None
    if max_age_h is not None and time.time() - d.get("ts", 0) > max_age_h * 3600:
        return None
    return d


def risk_tilt() -> float:
    d = cached(max_age_h=36)
    return float(d.get("risk_tilt", 0)) if d else 0.0


def refresh() -> dict | None:
    d = build()
    if not d["items"]:
        return None
    p = _path(); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp"); tmp.write_text(json.dumps(d)); tmp.replace(p)
    print(f"[INSIGHTS] daily reading refreshed: {len(d['items'])} items, tilt {d['risk_tilt']:+.2f}")
    return d


_started = False


def start():
    global _started
    if _started or os.environ.get("INSIGHTS_ENABLED", "true").lower() in ("0", "false", "no"):
        return
    _started = True

    def loop():
        time.sleep(30)
        while True:
            try:
                c = cached()
                if not c or time.time() - c.get("ts", 0) > EVERY_H * 3600:
                    refresh()
            except Exception as e:
                print(f"[INSIGHTS] error: {type(e).__name__}")
            time.sleep(1800)
    threading.Thread(target=loop, daemon=True, name="insights").start()
