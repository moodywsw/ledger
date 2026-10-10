"""Emotions from real results: confidence, greed/fear (-1 fear .. +1 greed), tilt."""
import time


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def on_close(m: dict, pnl_sol: float, change_pct: float | None) -> dict:
    day = time.strftime("%Y-%m-%d", time.gmtime())
    if m.get("day") != day:
        m["day"], m["pnl_today_sol"] = day, 0.0
    m["pnl_today_sol"] = round(m.get("pnl_today_sol", 0.0) + (pnl_sol or 0.0), 6)
    win = (pnl_sol or 0) >= 0
    mag = min(abs(change_pct or 0) / 100.0, 1.0) if change_pct is not None else 0.2
    if win:
        m["win_streak"], m["loss_streak"] = m.get("win_streak", 0) + 1, 0
        m["confidence"] = _clamp(m["confidence"] + 0.05 + 0.1 * mag, 0, 1)
        m["greed_fear"] = _clamp(m["greed_fear"] + 0.1 + 0.1 * mag, -1, 1)
        m["tilt"] = _clamp(m["tilt"] - 0.15, 0, 1)
    else:
        m["loss_streak"], m["win_streak"] = m.get("loss_streak", 0) + 1, 0
        m["confidence"] = _clamp(m["confidence"] - 0.07 - 0.1 * mag, 0, 1)
        m["greed_fear"] = _clamp(m["greed_fear"] - 0.15 - 0.1 * mag, -1, 1)
        m["tilt"] = _clamp(m["tilt"] + 0.1 * m["loss_streak"], 0, 1)
    return m


def decay(m: dict, hours: float) -> dict:
    """Emotions drift back toward neutral over time."""
    f = 0.97 ** max(hours, 0)
    m["confidence"] = 0.5 + (m["confidence"] - 0.5) * f
    m["greed_fear"] *= f
    m["tilt"] *= f
    return m


def set_regime(m: dict, sol_change_24h_pct: float | None) -> dict:
    if sol_change_24h_pct is None:
        return m
    m["regime"] = "risk-on" if sol_change_24h_pct > 4 else "risk-off" if sol_change_24h_pct < -4 else "neutral"
    return m


def label(m: dict) -> str:
    if m.get("tilt", 0) > 0.5 or m.get("loss_streak", 0) >= 3:
        n = m.get("loss_streak", 0)
        return f"cautious after {n} losses" if n >= 2 else "a bit tilted, sizing down mentally"
    if m.get("win_streak", 0) >= 3:
        return f"locked in, {m['win_streak']} wins straight (staying humble)"
    gf = m.get("greed_fear", 0)
    if gf > 0.5:
        return "greedy — which means it's time to be careful"
    if gf < -0.5:
        return "fearful, cash feels good right now"
    if m.get("confidence", 0.5) > 0.7:
        return "confident"
    if m.get("confidence", 0.5) < 0.3:
        return "humbled"
    return "calm and patient"


def emoji(m: dict) -> str:
    l = label(m)
    return "🧊" if "cautious" in l or "tilt" in l else "🔥" if "locked" in l else "😬" if "greedy" in l else "🫥" if "fear" in l else "🧠"
