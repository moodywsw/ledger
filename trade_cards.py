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


def _token_label(symbol: str | None, name: str | None) -> str:
    # No "$" prefix on purpose: "$TICKER" triggers other bots in the server.
    sym = symbol or "?"
    return f"{name} ({sym})" if name and name.upper() != sym.upper() else sym


def _header(mint: str) -> list:
    return [f"📋 `{mint}`", token_links(mint)]


COLOR_STOP = 0xFB7185    # rose


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _chain_label(chain: str | None) -> str:
    return {"solana": "◎ Solana", "base": "🔵 Base", "bsc": "🟡 BNB Chain", "eth": "⟠ Ethereum"}.get((chain or "solana").lower(), chain or "◎ Solana")


def entry_card(*, mint: str, symbol: str, name: str | None = None, price_usd: float | None = None,
               mcap_usd: float | None = None, size_sol: float | None = None, size_usd: float | None = None,
               thesis: str | None = None, chain: str | None = None, source: str | None = None, **_unused) -> dict:
    """Green BUY card: token + ticker, chain, size, entry price/MC, CA in code, links, why/signal."""
    size = " · ".join(x for x in (f"**{fmt_money(size_usd)}** USDC" if size_usd is not None else None,
                                  fmt_sol(size_sol) if size_sol is not None else None) if x) or "—"
    fields = [
        {"name": "💰 Size", "value": size, "inline": True},
        {"name": "💵 Entry", "value": fmt_price(price_usd), "inline": True},
        {"name": "🏷️ Market cap", "value": fmt_money(mcap_usd) if mcap_usd else "—", "inline": True},
        {"name": "⛓️ Chain", "value": _chain_label(chain), "inline": True},
    ]
    if source:
        fields.append({"name": "📡 Signal", "value": _one_line(source, 60), "inline": True})
    desc = []
    t = _one_line(thesis)
    if t:
        desc.append(f"> 🧠 {t}")
    desc += [f"**CA** `{mint}`", f"🔗 {token_links(mint)}"]
    return {
        "author": {"name": "Mirko · new position"},
        "title": f"🟢 BUY · {_token_label(symbol, name)}"[:256],
        "url": f"https://dexscreener.com/solana/{mint}",
        "description": "\n".join(desc),
        "color": COLOR_WIN,
        "fields": fields,
        "footer": {"text": "Mirko · live on-chain trade"},
        "timestamp": _now_iso(),
    }


def exit_card(*, mint: str, symbol: str, name: str | None = None, partial_fraction: float | None = None,
              entry_mcap_usd: float | None = None, exit_mcap_usd: float | None = None,
              pnl_sol: float | None = None, pnl_usd: float | None = None, pnl_pct: float | None = None,
              received_usd: float | None = None, reason: str | None = None, remaining_fraction: float | None = None,
              **_unused) -> dict:
    """partial_fraction=None/1.0 → full exit; 0<f<1 → partial. Stop-outs get their own label/colour."""
    ref = pnl_usd if pnl_usd is not None else (pnl_sol if pnl_sol is not None else (pnl_pct or 0))
    win = ref >= 0
    is_trim = partial_fraction is not None and 0 < partial_fraction < 0.999
    stop = bool(reason) and any(k in str(reason).lower() for k in ("stop", "sl", "rug", "trailing"))
    if stop and not win:
        word, emoji, color = "STOP LOSS", "🛑", COLOR_STOP
    elif is_trim:
        word, emoji, color = (f"TAKE PROFIT {partial_fraction:.0%}" if win else f"TRIM {partial_fraction:.0%}"), ("✅" if win else "🔴"), (COLOR_WIN if win else COLOR_LOSS)
    else:
        word, emoji, color = ("FULL EXIT" if win else "EXIT"), ("✅" if win else "🔴"), (COLOR_WIN if win else COLOR_LOSS)
    pnl = " · ".join([f"**{fmt_pct(pnl_pct)}**"]
                     + ([fmt_money(pnl_usd, signed=True)] if pnl_usd is not None else [])
                     + ([fmt_sol(pnl_sol, signed=True)] if pnl_sol is not None else []))
    fields = [
        {"name": "📈 PnL" if win else "📉 PnL", "value": pnl, "inline": True},
        {"name": "💵 Received", "value": f"{fmt_money(received_usd)} USDC" if received_usd is not None else "—", "inline": True},
        {"name": "🏷️ MC", "value": f"{fmt_money(entry_mcap_usd) if entry_mcap_usd else '—'} → "
                                   f"{fmt_money(exit_mcap_usd) if exit_mcap_usd else '—'}", "inline": True},
    ]
    rem = remaining_fraction if remaining_fraction is not None else ((1 - partial_fraction) if is_trim else 0.0)
    fields.append({"name": "🎒 Remaining", "value": f"{rem:.0%} still riding" if rem > 0.001 else "position closed", "inline": True})
    return {
        "author": {"name": "Mirko · " + ("partial exit" if is_trim else "position closed")},
        "title": f"{emoji} {word} · {_token_label(symbol, name)}"[:256],
        "url": f"https://dexscreener.com/solana/{mint}",
        "description": "\n".join([f"**CA** `{mint}`", f"🔗 {token_links(mint)}"]),
        "color": color,
        "fields": fields,
        "footer": {"text": "Mirko · live on-chain trade" + (f" · {reason}" if reason else "")},
        "timestamp": _now_iso(),
    }


COLOR_THESIS = 0xA78BFA  # violet


def thesis_card(*, mint: str, symbol: str, name: str | None = None, why: list | None = None,
                mcap_usd: float | None = None, invalidation: str | None = None,
                conviction: str | None = None, **_unused) -> dict:
    """Mirko's own call. Labelled 'Thesis' (no NFA boilerplate): 🧠 title,
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
