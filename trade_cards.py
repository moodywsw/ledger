"""
trade_cards.py — compact Discord embeds for trade entries and exits.

Pure functions (no network, no state), unit-tested in
tests/test_trade_cards.py. ledger_bot.speak() posts the returned dict as
{"embeds": [card]}. Kept deliberately short:

  ENTRY  🟢        blue   🪙 token + ticker, 📋 CA + links, 💵 entry price/MC, 💰 size, 🧠 one-line thesis
  EXIT   ✅ / 🔴   green / red by PnL   🪙 token, 📋 CA + links, 📈/📉 PnL (% · SOL · USD), 💵 entry → exit MC
  TRIM   ✅ / 🔴   same as EXIT, for a partial take-profit / partial stop
  THESIS 🧠        violet  Ledger's own call: 🪙 token, 📋 CA + links, ≤3 reasons, ❌ invalidation, 🎯 conviction

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


def _token_label(symbol: str | None, name: str | None) -> str:
    # No "$" prefix on purpose: "$TICKER" triggers other bots in the server.
    sym = symbol or "?"
    return f"{name} ({sym})" if name and name.upper() != sym.upper() else sym


def _header(mint: str) -> list:
    return [f"📋 `{mint}`", token_links(mint)]


def entry_card(*, mint: str, symbol: str, name: str | None = None, price_usd: float | None = None,
               mcap_usd: float | None = None, size_sol: float | None = None, size_usd: float | None = None,
               thesis: str | None = None, **_unused) -> dict:
    size = " · ".join(x for x in (fmt_sol(size_sol) if size_sol is not None else None,
                                  fmt_money(size_usd) if size_usd is not None else None) if x) or "—"
    fields = [
        {"name": "💵 Entry", "value": f"{fmt_price(price_usd)} · {_mc(mcap_usd)}", "inline": True},
        {"name": "💰 Size", "value": size, "inline": True},
    ]
    desc = _header(mint)
    t = _one_line(thesis)
    if t:
        desc.append(f"🧠 {t}")
    return {
        "title": f"🟢 ENTRY · 🪙 {_token_label(symbol, name)}"[:256],
        "description": "\n".join(desc),
        "color": COLOR_ENTRY,
        "fields": fields,
    }


def exit_card(*, mint: str, symbol: str, name: str | None = None, partial_fraction: float | None = None,
              entry_mcap_usd: float | None = None, exit_mcap_usd: float | None = None,
              pnl_sol: float | None = None, pnl_usd: float | None = None, pnl_pct: float | None = None,
              **_unused) -> dict:
    """partial_fraction=None/1.0 → EXIT; 0<f<1 → TRIM of that fraction."""
    ref = pnl_usd if pnl_usd is not None else (pnl_sol if pnl_sol is not None else (pnl_pct or 0))
    win = ref >= 0
    is_trim = partial_fraction is not None and 0 < partial_fraction < 0.999
    word = f"TRIM {partial_fraction:.0%}" if is_trim else "EXIT"
    pnl = " · ".join([f"**{fmt_pct(pnl_pct)}**"]
                     + ([fmt_sol(pnl_sol, signed=True)] if pnl_sol is not None else [])
                     + ([fmt_money(pnl_usd, signed=True)] if pnl_usd is not None else []))
    fields = [
        {"name": "📈 PnL" if win else "📉 PnL", "value": pnl, "inline": True},
        {"name": "💵 MC", "value": f"{fmt_money(entry_mcap_usd) if entry_mcap_usd else '—'} → "
                                   f"{fmt_money(exit_mcap_usd) if exit_mcap_usd else '—'}", "inline": True},
    ]
    return {
        "title": f"{'✅' if win else '🔴'} {word} · 🪙 {_token_label(symbol, name)}"[:256],
        "description": "\n".join(_header(mint)),
        "color": COLOR_WIN if win else COLOR_LOSS,
        "fields": fields,
    }


COLOR_THESIS = 0xA78BFA  # violet


def thesis_card(*, mint: str, symbol: str, name: str | None = None, why: list | None = None,
                mcap_usd: float | None = None, invalidation: str | None = None,
                conviction: str | None = None, **_unused) -> dict:
    """Ledger's own call. Labelled 'Thesis' (no NFA boilerplate): 🧠 title,
    📋 CA + links, ≤3 short reasons, ❌ invalidation, 🎯 conviction."""
    lines = _header(mint)
    for w in (why or [])[:3]:
        t = _one_line(w, 90)
        if t:
            lines.append(f"• {t}")
    fields = []
    if mcap_usd:
        fields.append({"name": "💵 MC", "value": fmt_money(mcap_usd), "inline": True})
    if invalidation:
        fields.append({"name": "❌ Invalid if", "value": _one_line(invalidation, 60), "inline": True})
    if conviction:
        fields.append({"name": "🎯 Conviction", "value": conviction, "inline": True})
    return {
        "title": f"🧠 THESIS · 🪙 {_token_label(symbol, name)}",
        "description": "\n".join(lines),
        "color": COLOR_THESIS,
        "fields": fields,
    }


def card_to_text(card: dict) -> str:
    """Plain-text rendering (console log, journal, and the DISCORD_TRADE_FORMAT=text fallback)."""
    lines = [f"**{card['title']}**", card.get("description", "")]
    lines.append(" · ".join(f"{f['name']} {f['value']}" for f in card.get("fields", [])))
    return "\n".join(x for x in lines if x)
