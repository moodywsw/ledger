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


def _build_client():
    import discord
    import ask

    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())

    @client.event
    async def on_ready():
        print(f"[DISCORD-BOT] Mirko is live as {client.user} · answers on @mention, DMs and #{ASK_CHANNEL}")

    @client.event
    async def on_message(message):
        if message.author.bot or message.author == client.user:
            return
        is_dm = isinstance(message.channel, discord.DMChannel)
        mentioned = client.user in message.mentions
        in_channel = getattr(message.channel, "name", "").lower() == ASK_CHANNEL
        if not (is_dm or mentioned or in_channel):
            return
        text = message.content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
        if not text:
            return
        async with message.channel.typing():
            res = await asyncio.get_running_loop().run_in_executor(None, ask.answer, text, str(message.author.id), "discord")
        await message.reply(res["answer"][:1900], mention_author=False)

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

    def run():
        try:
            asyncio.run(client.start(DISCORD_BOT_TOKEN))
        except Exception as e:   # bad token / missing intent -> log, never crash the trading loop
            print(f"[DISCORD-BOT] stopped: {type(e).__name__}: {str(e)[:160]}")
    threading.Thread(target=run, name="discord-bot", daemon=True).start()


if __name__ == "__main__":
    if not DISCORD_BOT_TOKEN:
        raise RuntimeError("Set DISCORD_BOT_TOKEN env var first.")
    asyncio.run(_build_client().start(DISCORD_BOT_TOKEN))
