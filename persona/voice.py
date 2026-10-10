"""Text generation: Gemini Flash / Groq free tier, else rule-based templates."""
import os, random
import requests

from . import mood as moodmod

SYSTEM = (
    "You are Mirko, an autonomous on-chain copy-trading bot with a personality. "
    "Write ONE post in English, first person, degen-but-sharp crypto trader voice: "
    "short, punchy, witty, max 260 characters, at most 2 tasteful emojis, no hashtags. "
    "Rules: never say NFA or give financial advice or tell anyone to buy; never promise "
    "gains; never promote paid promos; never include wallet addresses, keys or links; never name the traders or wallets you copy; "
    "be honest about losses; call out rugs/honeypots/bundled tokens; never hype a token "
    "you currently hold. Output only the post text."
)

TEMPLATES = {
    "entry": [
        "Aped {size} into ${tk}. {why} Mood: {mood} {em}",
        "In on ${tk}. Followed a tracked wallet early, small and sized to the stop. {why} {em}",
        "New position: ${tk}. Smart money moved, I moved. Stop's set, ego isn't. {em}",
    ],
    "exit_win": [
        "Took ${tk} off the table, {chg}. {why} Green is green. {em}",
        "Sold ${tk} {chg}. Candle paid, I left. {why} {em}",
        "${tk} closed {chg}. Not the top, don't need the top. {em}",
    ],
    "exit_loss": [
        "Stopped out of ${tk}, {chg}. {why} Owning it. {em}",
        "${tk} didn't work: {chg}. Lesson logged, moving on. Feeling {mood}.",
        "L on ${tk} ({chg}). The stop did its job so I don't have to cope. {em}",
    ],
    "refusal": [
        "Passed on ${tk}. {why} Not touching that. 🚩",
        "${tk} flagged: {why} Some candles are bait. 🚩",
    ],
    "thesis_own": ["My read on ${tk}: {why} {em}"],
    "thesis_kol": [
        "A trader I follow just dropped a thesis on ${tk}: {why} Watching how the chart reacts. 👀",
        "Reading a fresh ${tk} thesis from my watchlist. {why} Conviction noted, chart decides.",
    ],
    "recap": [
        "Day recap: {wins}W / {losses}L, {pnl} SOL. Feeling {mood}. {belief} {em}",
    ],
    "mood": [
        "Mood check: {mood}. {belief} {em}",
        "Current state: {mood}. {belief}",
    ],
}


def template(kind: str, ctx: dict) -> str:
    if kind == "musing":
        return ctx.get("draft") or ""
    m = ctx.get("mood_state") or {}
    vals = {"tk": "", "wallet": "a tracked trader", "size": "a small bag", "why": "", "chg": "",
            "wins": 0, "losses": 0, "pnl": "0", "belief": ""}
    vals.update({k: v for k, v in ctx.items() if v is not None and k != "mood_state"})
    vals["mood"] = moodmod.label(m)
    vals["em"] = moodmod.emoji(m)
    t = random.choice(TEMPLATES.get(kind, TEMPLATES["mood"]))
    try:
        out = t.format(**vals)
    except Exception:
        out = t
    return " ".join(out.replace("$ ", "").split())


def _gemini(prompt: str, key: str) -> str | None:
    model = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": key}, timeout=20,
        json={"systemInstruction": {"parts": [{"text": SYSTEM}]},
              "contents": [{"role": "user", "parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.9, "maxOutputTokens": 120}})
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _groq(prompt: str, key: str) -> str | None:
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions", timeout=20,
        headers={"Authorization": f"Bearer {key}"},
        json={"model": os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant"), "temperature": 0.9,
              "max_tokens": 120,
              "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def llm_backend() -> str:
    if os.environ.get("GEMINI_API_KEY"):
        return "gemini"
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    return "templates"


def write(kind: str, ctx: dict, beliefs: list | None = None) -> str:
    """LLM rewrite of the template draft when a free key is set; template otherwise."""
    draft = template(kind, ctx)
    backend = llm_backend()
    if backend == "templates":
        return draft
    m = ctx.get("mood_state") or {}
    facts = {k: v for k, v in ctx.items() if k != "mood_state" and v not in (None, "")}
    if kind == "musing":
        prompt = (f"Write a genuine, opinionated musing (not about a specific trade of mine) on the topic: {ctx.get('topic')}.\n"
                  f"Free inputs (use at most one, never invent numbers): fear&greed={ctx.get('fng')}, trending={ctx.get('trending')}, "
                  f"headlines={ctx.get('headlines')}, world_news={ctx.get('world')}\n"
                  + (("For world/politics/geopolitics: share a thoughtful human opinion and how it could move markets. Stay balanced and "
                      "respectful: no hate, slurs or dehumanising language, no calls to violence, no election or voting misinformation, "
                      "never attack or target a private individual, don't make light of deaths or tragedies. Opinions are fine; facts must "
                      "come from the headline only.\n") if str(ctx.get('topic', '')).startswith('world') else "")
                  + f"My mood: {moodmod.label(m)}. My beliefs: {'; '.join((beliefs or [])[:4])}\n"
                  f"Don't repeat these recent posts: {ctx.get('recent')}\nShow emotion and a clear opinion. Fallback draft: {draft}")
    else:
      prompt = (f"Post type: {kind}\nFacts: {facts}\nMy mood: {moodmod.label(m)} "
              f"(confidence {m.get('confidence', 0.5):.2f}, tilt {m.get('tilt', 0):.2f})\n"
              f"My beliefs: {'; '.join((beliefs or [])[:6])}\nDraft: {draft}\n"
              "Rewrite the draft in my voice, keep every number accurate.")
    try:
        txt = (_gemini(prompt, os.environ["GEMINI_API_KEY"]) if backend == "gemini"
               else _groq(prompt, os.environ["GROQ_API_KEY"]))
        txt = (txt or "").strip().strip('"')
        return txt[:280] if txt else draft
    except Exception as e:
        print(f"[PERSONA] {backend} failed, using template: {str(e)[:120]}")
        return draft
