"""Discord webhook + X API v2 (OAuth 1.0a user context). Silent when unconfigured."""
import base64, hashlib, hmac, os, secrets, time, urllib.parse
import requests

X_URL = "https://api.twitter.com/2/tweets"


def discord_enabled() -> bool:
    return bool(os.environ.get("LEDGER_PERSONA_WEBHOOK"))


def x_enabled() -> bool:
    return all(os.environ.get(k) for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"))


def post_discord(text: str) -> bool:
    url = os.environ.get("LEDGER_PERSONA_WEBHOOK")
    if not url:
        return False
    try:
        r = requests.post(url, json={"content": text, "username": "Ledger",
                                     "allowed_mentions": {"parse": []}}, timeout=10)
        return r.status_code < 300
    except Exception as e:
        print(f"[PERSONA] discord failed: {str(e)[:100]}")
        return False


def _q(s: str) -> str:
    return urllib.parse.quote(str(s), safe="~-._")


def oauth1_header(method: str, url: str, ck: str, cs: str, tk: str, ts_: str,
                  nonce: str | None = None, timestamp: str | None = None) -> str:
    p = {"oauth_consumer_key": ck, "oauth_nonce": nonce or secrets.token_hex(16),
         "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": timestamp or str(int(time.time())),
         "oauth_token": tk, "oauth_version": "1.0"}
    # JSON body is not part of the signature base string
    base = "&".join([method.upper(), _q(url), _q("&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(p.items())))])
    key = f"{_q(cs)}&{_q(ts_)}".encode()
    p["oauth_signature"] = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_q(k)}="{_q(v)}"' for k, v in sorted(p.items()))


def post_x(text: str) -> bool:
    if not x_enabled():
        return False
    e = os.environ
    try:
        h = oauth1_header("POST", X_URL, e["X_API_KEY"], e["X_API_SECRET"], e["X_ACCESS_TOKEN"], e["X_ACCESS_SECRET"])
        r = requests.post(X_URL, json={"text": text[:280]}, headers={"Authorization": h}, timeout=15)
        if r.status_code >= 300:
            print(f"[PERSONA] X post failed {r.status_code}: {r.text[:160]}")
        return r.status_code < 300
    except Exception as ex:
        print(f"[PERSONA] X failed: {str(ex)[:100]}")
        return False
