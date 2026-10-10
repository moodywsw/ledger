"""Guardrails ('good vs evil'). Every outgoing post passes through clean()."""
import re

SCAM_WORDS = ("rug", "honeypot", "bundle", "bundled", "dev dump", "dev sold", "dev selling",
              "mint authority", "freeze authority", "wash", "scam", "drained", "lp pulled")

_BANNED = [
    r"\bn\.?f\.?a\.?\b", r"not financial advice", r"financial advice", r"\bdyor\b",
    r"guarantee[ds]?", r"risk[- ]free", r"\b100x\b", r"\b1000x\b", r"can'?t lose",
    r"buy (it )?now", r"ape (in )?now", r"don'?t miss", r"last chance", r"get in (early|now)",
    r"\bsponsored\b", r"\bpaid promo\b", r"\b#?ad\b", r"use (my|code|referral)", r"ref(erral)? link",
]
_SECRET = [
    r"[1-9A-HJ-NP-Za-km-z]{80,90}",        # base58 private keys (64-byte)
    r"\b(seed|mnemonic|private key|secret key|api[_ ]?key|token=)\S*", r"\b0x[0-9a-fA-F]{64}\b",
    r"https?://\S*(webhook|discord\.com/api)\S*",
]
_SOL_ADDR = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def is_scam_reason(text: str) -> bool:
    t = (text or "").lower()
    return any(w in t for w in SCAM_WORDS)


def clean(text: str, allow_addresses: bool = False) -> str | None:
    """Return a safe version of text, or None if it can't be made safe."""
    if not text:
        return None
    t = text
    for p in _SECRET:
        if re.search(p, t, re.I):
            t = re.sub(p, "", t, flags=re.I)
    if not allow_addresses:  # never leak wallet addresses (ours or traders')
        t = _SOL_ADDR.sub("", t)
    for p in _BANNED:
        t = re.sub(p, "", t, flags=re.I)
    t = re.sub(r"[ \t]{2,}", " ", t).strip(" -·|\n")
    return t if len(t) >= 8 else None
