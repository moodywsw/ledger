"""
trade_cards.py — compact Discord embeds for trade entries and exits.

Pure functions (no network, no state), unit-tested in
tests/test_trade_cards.py. ledger_bot.speak() posts the returned dict as
{"embeds": [card]}. Kept deliberately short:

  ENTRY  🟢        blue   🪙 token + ticker, 📋 CA + links, 💵 entry price/MC, 💰 size, 🧠 one-line thesis
  EXIT   ✅ / 🔴   green / red by PnL   🪙 token, 📋 CA + links, 📈/📉 PnL (% · SOL · USD), 💵 entry → exit MC
  TRIM   ✅ / 🔴   same as EXIT, for a partial take-profit / partial stop
  THESIS 🧠        violet  Mirko's own call: 🪙 token, 📋 CA + links, ≤3 reasons, ❌ invalidation, 🎯 conviction

Scale-ins (top-ups, dip buys) are intentionally NOT posted.
"""
from __future__ import annotations

COLOR_ENTRY = 0x60A5FA   # blue
COLOR_WIN = 0x34D399     # green
COLOR_LOSS = 0xF87171    # red

THESIS_MAX = 140


def fmt_money(usd: float | None, signed: bool = False) -> str:
    if usd is None:
        return "—"
    sign = ("+" if usd >= 0 else "-") if signed else ("-" if usd < 0 else "")
    v = abs(usd)
    if v >= 1_000_000:
        return f"{sign}${v / 1_000_000:.2f}M"
    if v >= 10_000:
        return f"{sign}${v / 1_000:.1f}K"
    if v >= 100:
        return f"{sign}${v:,.0f}"
    return f"{sign}${v:,.2f}"


def fmt_price(usd: float | None) -> str:
    if not usd:
        return "—"
    if usd >= 1:
        return f"${usd:,.4f}"
    return f"${usd:.4g}" if usd >= 1e-4 else f"${usd:.3e}"


def _mc(usd: float | None) -> str:
    return f"{fmt_money(usd)} MC" if usd else "— MC"


def fmt_sol(sol: float | None, signed: bool = False) -> str:
    if sol is None:
        return "—"
    return f"{sol:+.3f} SOL" if signed else f"{sol:.3f} SOL"


def fmt_pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:+.1f}%"


def token_links(mint: str) -> str:
    return " · ".join([
        f"[DexScreener](https://dexscreener.com/solana/{mint})",
        f"[Pump.fun](https://pump.fun/coin/{mint})",
        f"[Solscan](https://solscan.io/token/{mint})",
    ])


def _one_line(text: str | None, limit: int = THESIS_MAX) -> str | None:
    if not text:
        return None
    t = " ".join(str(text).split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


import re as _re

_PLAIN = [
    (_re.compile(r"(\d+) tracked wallets? bought", _re.I), lambda m: "smart wallets I follow are buying" if m.group(1) != "1" else "a smart wallet I follow just bought"),
    (_re.compile(r"trending on fomo", _re.I), lambda m: "it's trending on Fomo"),
    (_re.compile(r"\+\d+% 1h, not vertical", _re.I), lambda m: "it's climbing steadily without going vertical"),
    (_re.compile(r"buyers lead", _re.I), lambda m: "buyers clearly outnumber sellers"),
    (_re.compile(r"fresh pump\.fun", _re.I), lambda m: "a fresh pump.fun launch that passed my safety checks"),
    (_re.compile(r"accumulat", _re.I), lambda m: "quiet accumulation before the move"),
    (_re.compile(r"graduat", _re.I), lambda m: "it graduated and is holding its level"),
    (_re.compile(r"copy|mirror|smart", _re.I), lambda m: "smart money is rotating in"),
]


def plain_thesis(text_or_list, limit: int = 170) -> str | None:
    """Turn raw signal fragments ('SOL +0.3% 24h (chop)', 'buyers lead 21/9 txns 1h') into 1-2 plain English lines."""
    if not text_or_list:
        return None
    parts = text_or_list if isinstance(text_or_list, (list, tuple)) else _re.split(r"[;•\n]|, (?=[a-z+$\d])", str(text_or_list))
    out = []
    for p in parts:
        p = str(p).strip(" .-")
        if not p or _re.match(r"^(SOL|BTC|ETH) [+-]?\d", p):     # market-wide noise, not a thesis
            continue
        hit = next((f(m) for rx, f in _PLAIN for m in [rx.search(p)] if m), None)
        if hit:
            if hit not in out: out.append(hit)
        else:
            q = _re.sub(r"\([^)]*\)|\b\d[\d.,/%x$kKmM+-]*\b|\b(txns?|1h|6h|24h|liq|top10|dev|mc|vol)\b", "", p)
            q = " ".join(q.split()).strip(" ,:;-")
            if len(q.split()) >= 3 and q.lower() not in (o.lower() for o in out):
                out.append(q)
    if not out:
        return "Momentum and smart-money flow lined up; sized for the risk."
    txt = out[0][0].upper() + out[0][1:]
    if len(out) > 1:
        txt += ", and " + " and ".join(out[1:3]) if len(out) == 2 else ", " + ", ".join(out[1:-1][:1]) + " and " + out[-1]
    return _one_line(txt.rstrip(".") + ".", limit)


def conviction_bar(score10: int | float | None) -> str | None:
    if score10 is None:
        return None
    n = max(1, min(10, round(float(score10))))
    emo = "🟢" if n >= 8 else "🟡" if n >= 6 else "🟠" if n >= 4 else "🔴"
    return f"{emo} `{'▰' * n}{'▱' * (10 - n)}` **{n}/10**"


def conviction10(v) -> int | None:
    """Accepts 1-10 numbers or 'low'/'medium'/'high'."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return max(1, min(10, round(v)))
    return {"low": 4, "medium": 6, "high": 8, "very high": 9}.get(str(v).lower())


def _token_label(symbol: str | None, name: str | None) -> str:
    sym = (symbol or "?").lstrip("$")
    return f"{name} (${sym})" if name and name.upper() != sym.upper() else f"${sym}"


def _desc(mint: str, thesis: str | None = None) -> str:
    lines = [f"```\n{mint}\n```", token_links(mint)]
    if thesis:
        lines += ["", f"💡 {thesis}"]
    return "\n".join(lines)


def _finish(card: dict, logo_url: str | None) -> dict:
    if logo_url and str(logo_url).startswith("https://"):
        card["thumbnail"] = {"url": logo_url}
    card["footer"] = {"text": "Mirko · on-chain"}
    card["timestamp"] = _now_iso()
    return card


COLOR_BUY = 0x3B82F6     # blue
COLOR_STOP = 0xEF4444    # red
COLOR_WATCH = 0xA78BFA   # violet


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def entry_card(*, mint: str, symbol: str, name: str | None = None, price_usd: float | None = None,
               mcap_usd: float | None = None, size_sol: float | None = None, size_usd: float | None = None,
               thesis=None, conviction=None, logo_url: str | None = None, **_unused) -> dict:
    """🟢 BUY: logo, CA block + links, Entry MC · Size · Conviction, 1-2 line plain thesis."""
    size = f"**{fmt_money(size_usd)}**" if size_usd is not None else (f"**{fmt_sol(size_sol)}**" if size_sol is not None else "—")
    fields = [{"name": "📊 Entry MC", "value": f"**{fmt_money(mcap_usd)}**" if mcap_usd else "—", "inline": True},
              {"name": "💰 Size", "value": size, "inline": True}]
    cb = conviction_bar(conviction10(conviction))
    if cb:
        fields.append({"name": "🎯 Conviction", "value": cb, "inline": True})
    return _finish({"title": f"🟢 BUY · {_token_label(symbol, name)}"[:256], "url": f"https://dexscreener.com/solana/{mint}",
                    "description": _desc(mint, plain_thesis(thesis)), "color": COLOR_BUY, "fields": fields}, logo_url)


def exit_card(*, mint: str, symbol: str, name: str | None = None, partial_fraction: float | None = None,
              entry_mcap_usd: float | None = None, exit_mcap_usd: float | None = None,
              pnl_sol: float | None = None, pnl_usd: float | None = None, pnl_pct: float | None = None,
              received_usd: float | None = None, reason: str | None = None, remaining_fraction: float | None = None,
              logo_url: str | None = None, thesis=None, **_unused) -> dict:
    """CLOSE · PROFIT / CLOSE · LOSS / TAKE PROFIT x% — MC entry→exit, PnL $ and % with colour emoji."""
    ref = pnl_usd if pnl_usd is not None else (pnl_sol if pnl_sol is not None else (pnl_pct or 0))
    win = ref >= 0
    is_trim = partial_fraction is not None and 0 < partial_fraction < 0.999
    stop = bool(reason) and any(k in str(reason).lower() for k in ("stop", "rug", "trailing")) and not win
    if is_trim:
        word = f"🟢 TAKE PROFIT {partial_fraction:.0%}" if win else f"🔴 CUT {partial_fraction:.0%}"
    elif stop:
        word = "🔴 CLOSE · STOP LOSS"
    else:
        word = "🟢 CLOSE · PROFIT" if win else "🔴 CLOSE · LOSS"
    dot = "🟢" if win else "🔴"
    pnl_main = fmt_money(pnl_usd, signed=True) if pnl_usd is not None else fmt_sol(pnl_sol, signed=True) if pnl_sol is not None else "—"
    fields = [
        {"name": "📊 MC", "value": f"{fmt_money(entry_mcap_usd) if entry_mcap_usd else '—'} → **{fmt_money(exit_mcap_usd) if exit_mcap_usd else '—'}**", "inline": True},
        {"name": "💵 PnL", "value": f"{dot} **{pnl_main}** ({fmt_pct(pnl_pct)})", "inline": True},
    ]
    rem = remaining_fraction if remaining_fraction is not None else ((1 - partial_fraction) if is_trim else 0.0)
    fields.append({"name": "🎒 Position", "value": f"{rem:.0%} still riding" if rem > 0.001 else "fully closed", "inline": True})
    return _finish({"title": f"{word} · {_token_label(symbol, name)}"[:256], "url": f"https://dexscreener.com/solana/{mint}",
                    "description": _desc(mint, plain_thesis(thesis) if thesis else None),
                    "color": COLOR_WIN if win else (COLOR_STOP if stop else COLOR_LOSS), "fields": fields}, logo_url)


def thesis_card(*, mint: str, symbol: str, name: str | None = None, why: list | None = None,
                mcap_usd: float | None = None, conviction=None, logo_url: str | None = None, **_unused) -> dict:
    """👀 WATCH: Mirko's own call — logo, CA block + links, plain thesis, MC + conviction bar."""
    fields = [{"name": "📊 MC", "value": f"**{fmt_money(mcap_usd)}**" if mcap_usd else "—", "inline": True}]
    cb = conviction_bar(conviction10(conviction))
    if cb:
        fields.append({"name": "🎯 Conviction", "value": cb, "inline": True})
    return _finish({"title": f"👀 WATCH · {_token_label(symbol, name)}"[:256], "url": f"https://dexscreener.com/solana/{mint}",
                    "description": _desc(mint, plain_thesis(why)), "color": COLOR_WATCH, "fields": fields}, logo_url)


def card_to_text(card: dict) -> str:
    """Plain-text rendering (console log, journal, and the DISCORD_TRADE_FORMAT=text fallback)."""
    lines = [f"**{card['title']}**", card.get("description", "")]
    lines.append(" · ".join(f"{f['name']} {f['value']}" for f in card.get("fields", [])))
    return "\n".join(x for x in lines if x)
