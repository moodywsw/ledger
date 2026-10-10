"""Mirko's living persona: mood, memory, beliefs, voice and autoposting.

Runs as a daemon thread inside the bot. It tails journal.jsonl (everything
the bot already says/does), so the trading code needs no changes beyond
calling start_persona() once. Every outlet is silently off when its env
vars are missing; text falls back to rule-based templates when no free LLM
key (GEMINI_API_KEY / GROQ_API_KEY) is set.
"""
from .engine import start_persona, feed, Persona  # noqa: F401
