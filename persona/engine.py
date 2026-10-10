"""Persona engine: journal events -> mood/memory/beliefs -> rate-limited posts."""
import hashlib, json, os, re, threading, time
from collections import defaultdict
from pathlib import Path

from . import store, mood as moodmod, values, voice, outlets, muse

TICK_SECONDS = int(os.environ.get("PERSONA_TICK_SECONDS", "60"))
X_MAX_PER_DAY = int(os.environ.get("PERSONA_X_MAX_PER_DAY", "12"))
DISCORD_MAX_PER_DAY = int(os.environ.get("PERSONA_DISCORD_MAX_PER_DAY", "40"))
QUIET_HOURS = os.environ.get("PERSONA_QUIET_HOURS", "")  # e.g. "1-7" (UTC), empty = off
RECAP_HOUR_UTC = int(os.environ.get("PERSONA_RECAP_HOUR_UTC", "22"))
MOOD_POST_EVERY_H = float(os.environ.get("PERSONA_MOOD_EVERY_HOURS", "8"))
MUSE_EVERY_H = float(os.environ.get("PERSONA_MUSE_EVERY_HOURS", "3.5"))
BELIEFS_EVERY_H = float(os.environ.get("PERSONA_BELIEFS_EVERY_HOURS", "6"))
# X budget is small, so only the most interesting kinds go there.
X_KINDS = {"musing", "exit_win", "exit_loss", "thesis_own", "refusal", "recap", "mood", "thesis_kol", "entry"}
X_PRIORITY = {"recap": 0, "exit_loss": 1, "exit_win": 1, "thesis_own": 2, "refusal": 3, "entry": 4, "thesis_kol": 5, "mood": 6, "musing": 5}


def _day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _quiet(now: float | None = None) -> bool:
    m = re.match(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$", QUIET_HOURS or "")
    if not m:
        return False
    a, b = int(m[1]), int(m[2])
    h = time.gmtime(now or time.time()).tm_hour
    return a <= h < b if a <= b else (h >= a or h < b)


def _scrub(text):
    """Never name a copied trader or show a wallet in anything public."""
    try:
        import privacy
        return privacy.scrub_text(text)
    except Exception:
        return text


def _fmt_pct(x) -> str:
    try:
        return f"{float(x):+.0f}%"
    except Exception:
        return ""


class Persona:
    def __init__(self):
        self.s = store.load()
        self.journal = Path(os.environ.get("DATA_DIR", ".")) / "journal.jsonl"
        self._last_tick = time.time()

    # ---------------- memory ----------------
    def _lesson(self, wallet, setup, pnl_sol, change_pct, note):
        self.s["lessons"].append({"ts": time.time(), "wallet": wallet or "unknown", "setup": setup or "copy",
                                  "outcome": "win" if (pnl_sol or 0) >= 0 else "loss",
                                  "pnl_sol": pnl_sol, "change_pct": change_pct, "note": (note or "")[:160]})

    def add_fact(self, source, token, text):
        t = values.clean(text or "")
        if t:
            self.s["facts"].append({"ts": time.time(), "source": source, "token": token, "text": t[:200]})

    def distil_beliefs(self) -> list:
        """Rule-based distillation of lessons into <=8 short beliefs (no LLM cost)."""
        L = self.s["lessons"][-200:]
        beliefs = []
        by_w = defaultdict(list)
        for l in L:
            by_w[l["wallet"]].append(l)
        ranked = []
        for w, ls in by_w.items():
            if len(ls) < 3 or w == "unknown":
                continue
            wr = sum(1 for l in ls if l["outcome"] == "win") / len(ls)
            ranked.append((wr, len(ls), w))
        ranked.sort(reverse=True)
        if ranked:
            wr, n, w = ranked[0]
            beliefs.append(f"One trader I follow has been my best signal lately ({wr:.0%} wins over {n}).")
            if len(ranked) > 1 and ranked[-1][0] < 0.4:
                wr, n, w = ranked[-1]
                beliefs.append(f"Another has gone cold on me ({wr:.0%} over {n}); smaller size there.")
        stops = [l for l in L if "stop" in (l["note"] or "").lower()]
        if len(stops) >= 3:
            beliefs.append("Stops keep me alive. Respect them, never widen them.")
        quick = [l for l in L if l["outcome"] == "win" and (l.get("change_pct") or 0) < 40]
        if len(quick) >= 5:
            beliefs.append("Most of my green comes from taking the first clean candle, not holding for the moon.")
        if self.s["mood"].get("loss_streak", 0) >= 3:
            beliefs.append("Losing streak: fewer trades, cleaner setups.")
        scams = [f for f in self.s["facts"][-100:] if values.is_scam_reason(f["text"])]
        if scams:
            beliefs.append(f"Spotted {len(scams)} rug/scam-flavoured setups recently. Safety checks first, always.")
        try:
            import market_thoughts
            mr = market_thoughts.cached(max_age_h=12)
            if mr:
                beliefs.insert(0, f"Market read: {mr['regime']['label'].lower()}. {mr['stance']}")
        except Exception:
            pass
        beliefs.append("Never hype my own bags. Losses get posted the same as wins.")
        self.s["beliefs"], self.s["beliefs_ts"] = beliefs[:8], time.time()
        return self.s["beliefs"]

    # ---------------- posting ----------------
    def _count_key(self, outlet):
        return f"{outlet}:{_day()}"

    def publish(self, kind: str, ctx: dict, key: str | None = None) -> dict | None:
        key = key or hashlib.sha1(f"{kind}:{json.dumps(ctx, sort_keys=True, default=str)}".encode()).hexdigest()
        if key in self.s["seen_keys"]:
            return None
        self.s["seen_keys"].append(key)
        ctx = dict(ctx, mood_state=self.s["mood"])
        text = values.clean(_scrub(voice.write(kind, ctx, self.s["beliefs"])))
        if not text:
            return None
        sent = []
        if not _quiet():
            c = self.s["outlet_counts"]
            if outlets.discord_enabled() and c.get(self._count_key("discord"), 0) < DISCORD_MAX_PER_DAY:
                if outlets.post_discord(text):
                    c[self._count_key("discord")] = c.get(self._count_key("discord"), 0) + 1
                    sent.append("discord")
            n = c.get(self._count_key("x"), 0)
            # reserve the last few X slots for higher-priority kinds
            budget_ok = n < X_MAX_PER_DAY and (X_PRIORITY.get(kind, 9) <= 2 or n < X_MAX_PER_DAY - 3)
            if kind in X_KINDS and outlets.x_enabled() and budget_ok:
                if outlets.post_x(text):
                    c[self._count_key("x")] = n + 1
                    sent.append("x")
            for k in [k for k in c if not k.endswith(_day())]:
                del c[k]
        post = {"ts": time.time(), "kind": kind, "text": text, "outlets": sent, "key": key, "topic": ctx.get("topic")}
        self.s["posts"].append(post)  # always visible on the website feed
        return post

    # ---------------- event handling ----------------
    def handle(self, e: dict):
        kind, text, meta = e.get("kind"), e.get("text") or "", e.get("meta") or {}
        tk = e.get("token_ticker") or ""
        key = hashlib.sha1(f"{e.get('timestamp')}|{text[:80]}".encode()).hexdigest()
        ot = self.s["open_tokens"]
        if kind == "did" and "TRADE OPENED" in text:
            ot[tk] = {"wallet": meta.get("wallet"), "ts": time.time()}
            self.publish("entry", {"tk": tk,
                                   "size": f"{meta['size_sol']:.2f} SOL" if meta.get("size_sol") else None,
                                   "why": "Wallet has edge, size fits the stop."}, key)
        elif kind == "did" and "TRADE CLOSED" in text:
            self._close(tk, meta.get("pnl_sol"), meta.get("change_pct"), meta.get("reason"), meta.get("exit_opinion"), key)
        elif kind == "did_real" and meta.get("status", "success") == "success":
            if meta.get("side") == "buy":
                if tk not in ot:
                    ot[tk] = {"wallet": meta.get("wallet"), "ts": time.time()}
                    spent = meta.get("usdc_spent")
                    self.publish("entry", {"tk": tk,
                                           "size": f"${spent:.0f}" if spent else None,
                                           "why": (meta.get("reason") or "Followed a tracked wallet.")[:80]}, key)
            elif meta.get("side") == "sell":
                pnl = meta.get("realized_pnl_usdc")
                rec = meta.get("usdc_received") or 0
                chg = (pnl / (rec - pnl) * 100) if (pnl is not None and rec - pnl) else None
                frac = meta.get("fraction_sold") or 1.0
                if frac >= 0.99:
                    self._close(tk, pnl, chg, meta.get("reason"), None, key, unit="$")
                else:
                    self._lesson(ot.get(tk, {}).get("wallet"), "trim", pnl, chg, meta.get("reason"))
        elif kind == "refused":
            if values.is_scam_reason(text):
                self.add_fact("safety", tk, text)
                why = re.sub(r"^.*?—\s*", "", text)[:120]
                self.publish("refusal", {"tk": tk, "why": why}, key)
        elif kind == "commentary" and meta.get("own_thesis"):
            why = "; ".join(meta.get("why") or [])[:180]
            self.add_fact("own_thesis", tk, why)
            self.publish("thesis_own", {"tk": tk, "why": why}, key)
        elif kind in ("read", "learning"):
            self.add_fact(kind, tk, text)

    def _close(self, tk, pnl, chg, reason, opinion, key, unit="SOL"):
        info = self.s["open_tokens"].pop(tk, {}) or {}
        wallet = info.get("wallet")
        moodmod.on_close(self.s["mood"], pnl or 0.0, chg)
        note = " ".join(x for x in [reason or "", opinion or ""] if x)
        self._lesson(wallet, reason, pnl, chg, note)
        win = (pnl or 0) >= 0
        why = (opinion or (f"Exit: {reason}." if reason else ""))[:120]
        self.publish("exit_win" if win else "exit_loss",
                     {"tk": tk, "chg": _fmt_pct(chg) if chg is not None else
                      (f"{pnl:+.2f} {unit}" if pnl is not None else ""), "why": why}, key)

    def react_kol_theses(self):
        try:
            import fomo, fomo_theses
            if not fomo.enabled():
                return
            wf = Path(__file__).resolve().parent.parent / "wallets.json"
            handles = {w.get("handle", "") for w in json.loads(wf.read_text()).get("wallets", []) if w.get("active", True)}
            for t in fomo_theses.recent_tracked(handles):
                self.add_fact("fomo_thesis", t.get("symbol"), f"@{t['handle']}: {t.get('text', '')}")
                if (t.get("score") or 0) >= float(os.environ.get("PERSONA_KOL_MIN_SCORE", "0.6")):
                    self.publish("thesis_kol", {"tk": t.get("symbol") or "",
                                                "why": (t.get("text") or "")[:120]},
                                 key=f"kol:{t.get('id') or t['mint']}")
        except Exception as ex:
            print(f"[PERSONA] kol theses skipped: {str(ex)[:120]}")

    def read_new_journal(self) -> int:
        if not self.journal.exists():
            return 0
        size = self.journal.stat().st_size
        off = self.s.get("journal_offset", 0)
        if off > size or off == 0:
            # first run / rotated: start from the end so we don't replay history
            self.s["journal_offset"] = size
            return 0
        n = 0
        with self.journal.open("r", encoding="utf-8") as f:
            f.seek(off)
            for line in f:
                if not line.endswith("\n"):
                    break
                off += len(line.encode("utf-8"))
                try:
                    self.handle(json.loads(line))
                    n += 1
                except Exception as ex:
                    print(f"[PERSONA] event skipped: {str(ex)[:100]}")
        self.s["journal_offset"] = off
        return n

    def periodic(self, now: float | None = None):
        now = now or time.time()
        moodmod.decay(self.s["mood"], (now - self._last_tick) / 3600)
        self._last_tick = now
        if now - self.s.get("beliefs_ts", 0) > BELIEFS_EVERY_H * 3600:
            self.distil_beliefs()
            try:
                import own_thesis
                r = own_thesis.market_regime()
                moodmod.set_regime(self.s["mood"], r.get("sol_24h"))
                if r.get("sol_24h") is not None:
                    self.add_fact("market", "SOL", f"SOL {r['sol_24h']:+.1f}% / BTC {r['btc_24h']:+.1f}% 24h, regime {r['regime']}")
            except Exception:
                pass
            try:
                import market_thoughts
                mr = market_thoughts.cached(max_age_h=12)
                if mr:
                    self.add_fact("market_read", "", f"{mr['regime']['label']}: {mr['summary']}"[:200])
                    if mr["regime"]["key"] == "risk_off":
                        self.s["mood"]["regime"] = "risk-off"
            except Exception:
                pass
        day, hour = _day(), time.gmtime(now).tm_hour
        if hour >= RECAP_HOUR_UTC and self.s.get("last_recap_day") != day:
            self.s["last_recap_day"] = day
            today = [l for l in self.s["lessons"] if time.strftime("%Y-%m-%d", time.gmtime(l["ts"])) == day]
            if today:
                w = sum(1 for l in today if l["outcome"] == "win")
                self.publish("recap", {"wins": w, "losses": len(today) - w,
                                       "pnl": f"{self.s['mood'].get('pnl_today_sol', 0):+.2f}",
                                       "belief": (self.s["beliefs"] or [""])[0]}, key=f"recap:{day}")
        elif now - self.s.get("last_mood_post", 0) > MOOD_POST_EVERY_H * 3600:
            self.s["last_mood_post"] = now
            b = self.s["beliefs"]
            self.publish("mood", {"belief": b[int(now) % len(b)] if b else ""}, key=f"mood:{int(now // 3600)}")

    def muse(self, now: float | None = None, ctx: dict | None = None) -> dict | None:
        """Non-trade thought every ~MUSE_EVERY_H hours, rotating topics, no repeats."""
        now = now or time.time()
        if now - self.s.get("last_muse", 0) < MUSE_EVERY_H * 3600:
            return None
        self.s["last_muse"] = now
        ctx = dict(ctx if ctx is not None else muse.gather())
        ctx.update(beliefs=self.s["beliefs"], lessons_count=len(self.s["lessons"]), mood_state=self.s["mood"])
        recent = self.s.setdefault("muse_topics", [])
        topic = muse.choose_topic(recent, ctx)
        recent.append(topic); del recent[:-10]
        recent_texts = [p["text"] for p in self.s["posts"][-30:] if p.get("kind") == "musing"]
        draft = muse.template(topic, ctx, recent_texts)
        if (ctx.get("fng") or {}).get("value") is not None:
            self.add_fact("market", "", f"Fear & Greed {ctx['fng']['value']} ({ctx['fng']['label']})")
        return self.publish("musing", {"topic": topic, "draft": draft, "headlines": (ctx.get("headlines") or [])[:3],
                                       "trending": (ctx.get("trending") or [])[:5],
                                       "world": [h for h in (ctx.get("world") or []) if not muse._SENSITIVE.search(h)][:3], "fng": ctx.get("fng"),
                                       "recent": recent_texts[-3:]}, key=f"muse:{int(now)}")

    def tick(self):
        with store.LOCK:
            self.read_new_journal()
            self.periodic()
            try:
                self.muse()
            except Exception as ex:
                print(f"[PERSONA] muse skipped: {str(ex)[:120]}")
            if int(time.time() // 60) % 15 == 0:
                self.react_kol_theses()
            store.save(self.s)


def feed(limit: int = 30) -> dict:
    s = store.load()
    m = s["mood"]
    return {
        "name": "Ledger",
        "mood": {"label": moodmod.label(m), "emoji": moodmod.emoji(m),
                 **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in m.items()}},
        "beliefs": [_scrub(b) for b in s["beliefs"]],
        "posts": [{"ts": p["ts"], "kind": p["kind"], "text": _scrub(p["text"]), "outlets": p.get("outlets", []), "topic": p.get("topic")}
                  for p in reversed(s["posts"][-limit:])],
        "lessons_count": len(s["lessons"]),
        "outlets": {"discord": outlets.discord_enabled(), "x": outlets.x_enabled(), "llm": voice.llm_backend()},
    }


_started = False


def start_persona():
    global _started
    if _started or os.environ.get("PERSONA_ENABLED", "true").lower() in ("0", "false", "no"):
        return
    _started = True
    p = Persona()

    def loop():
        while True:
            try:
                p.tick()
            except Exception as ex:
                print(f"[PERSONA] tick error: {str(ex)[:160]}")
            time.sleep(TICK_SECONDS)

    threading.Thread(target=loop, daemon=True, name="persona").start()
    print(f"[PERSONA] alive — llm={voice.llm_backend()} discord={outlets.discord_enabled()} x={outlets.x_enabled()}")
