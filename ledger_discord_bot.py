"""
ledger_discord_bot.py — Mirko answers in your Discord server.

Uses the SAME answer function as the website's "Ask Mirko" (ask.answer): same brain/context,
same per-user limits (5/min, 30/day), global daily cap, abuse + prompt-injection guards.
He replies when @mentioned, when DM'd, or to every message in the channel named
DISCORD_ASK_CHANNEL (default "ask-mirko"). He never executes anything from chat.

Runs inside the main service (ledger_bot.py calls start()) when DISCORD_BOT_TOKEN is set.
Needs: Discord Developer Portal -> Bot -> "Message Content Intent" ON; bot invited with
View Channels, Send Messages, Read Message History (and Embed Links optional).
"""
import asyncio
import os
import threading

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ASK_CHANNEL = os.environ.get("DISCORD_ASK_CHANNEL", "ask-mirko").lstrip("#").lower()
_started = False


STATS = {"started": False, "ready": False, "guilds": 0, "seen": 0, "answered": 0, "gated": 0, "dm_ignored": 0, "no_content": 0, "errors": 0, "last_error": None, "last_msg": None}
_limited: dict = {}


def _dump():
    try:
        import json, os
        from pathlib import Path
        Path(os.environ.get("DATA_DIR", "."), "discord_bot_status.json").write_text(json.dumps(STATS))
    except Exception:
        pass


def _build_client(content: bool = True):
    import discord
    import ask

    intents = discord.Intents.default()
    intents.message_content = content
    STATS["content_intent"] = content
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())

    @client.event
    async def on_ready():
        STATS["ready"] = True; STATS["guilds"] = len(client.guilds); _dump()
        print(f"[DISCORD-BOT] Mirko is live as {client.user} · answers on @mention, DMs and #{ASK_CHANNEL}")

    _seen = set()
    _denied = {}

    @client.event
    async def on_message(message):
        if message.author.bot or message.author == client.user or message.id in _seen:
            return
        _seen.add(message.id)
        if len(_seen) > 2000:
            _seen.clear()
        is_dm = isinstance(message.channel, discord.DMChannel)
        mentioned = client.user in message.mentions
        in_channel = getattr(message.channel, "name", "").lower() == ASK_CHANNEL
        if not (is_dm or mentioned or in_channel):
            return
        import access, time as _t
        STATS["seen"] += 1; STATS["last_msg"] = _t.time()
        if is_dm:   # DMs only from members of a server Mirko shares with them
            if not any(g.get_member(message.author.id) for g in client.guilds):
                STATS["dm_ignored"] += 1
                return
        if not access.discord_allowed(message.author):
            STATS["gated"] += 1
            if not is_dm and _denied.get(message.author.id, 0) < _t.time() - 3600:   # at most one note per hour per user
                _denied[message.author.id] = _t.time()
                await message.reply("Mirko chat is invite-only here — ask the owner for the Mirko Access role.", mention_author=False, delete_after=20)
            return
        text = message.content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
        if not text:
            if mentioned and not message.content:
                STATS["no_content"] += 1
                await message.reply("I can see the ping but not your words — owner: enable Message Content Intent for the bot.", mention_author=False)
            return
        try:   # another live instance (deploy overlap) may already have answered
            async for m in message.channel.history(limit=15, after=message):
                if m.author == client.user and m.reference and m.reference.message_id == message.id:
                    return
        except Exception:
            pass
        try:
            async with message.channel.typing():
                res = await asyncio.get_running_loop().run_in_executor(None, ask.answer, text, "dc:" + str(message.author.id), "discord", None, None)
        except Exception as e:
            STATS["errors"] += 1; STATS["last_error"] = f"{type(e).__name__}: {str(e)[:120]}"
            print(f"[DISCORD-BOT] answer failed {STATS['last_error']}")
            res = {"answer": "Head's buried in charts — ask me again in a minute."}
        try:
            async for m in message.channel.history(limit=15, after=message):
                if m.author == client.user and m.reference and m.reference.message_id == message.id:
                    return
        except Exception:
            pass
        if res.get("limited"):
            if _limited.get(message.author.id, 0) > _t.time() - 3600:
                return   # reply once per hour about limits
            _limited[message.author.id] = _t.time()
        await message.reply(res["answer"][:1990], mention_author=False)
        STATS["answered"] += 1; _dump()

    return client


def start():
    """Start the bot on a background thread (no-op without a token or discord.py)."""
    global _started
    if _started or not DISCORD_BOT_TOKEN:
        if not DISCORD_BOT_TOKEN:
            print("[DISCORD-BOT] DISCORD_BOT_TOKEN not set; chat bot off")
        return
    try:
        client = _build_client()
    except Exception as e:
        print(f"[DISCORD-BOT] disabled: {type(e).__name__}")
        return
    _started = True
    STATS["started"] = True; _dump()

    def run():
        nonlocal client
        try:
            asyncio.run(client.start(DISCORD_BOT_TOKEN))
        except Exception as e:   # bad token / missing intent -> log, never crash the trading loop
            STATS["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"; _dump()
            print(f"[DISCORD-BOT] stopped: {STATS['last_error']}")
            if "PrivilegedIntents" in type(e).__name__:   # portal toggle off: mentions + DMs still carry content
                print("[DISCORD-BOT] Message Content Intent disabled in portal; retrying in mention/DM-only mode")
                try:
                    client = _build_client(content=False)
                    asyncio.run(client.start(DISCORD_BOT_TOKEN))
                except Exception as e2:
                    STATS["last_error"] = f"{type(e2).__name__}: {str(e2)[:160]}"; _dump()
    threading.Thread(target=run, name="discord-bot", daemon=True).start()


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("Set DISCORD_BOT_TOKEN env var first.")
    asyncio.run(_build_client().start(DISCORD_BOT_TOKEN))
