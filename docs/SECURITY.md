# Mirko — security summary

## HTTP surface (api_server.py + security.py)
- **Production server:** waitress WSGI (`requirements.txt`), with Flask debug off. There is a 4 KB request body cap, 200 connections and a 30 s channel timeout.
- **Rate limits (per client IP, in memory):** public API 240/min, owner API 30/min, writes 10/min, static files 600/min. Over the limit returns 429 with `Retry-After`.
- **Client IP:** taken from `X-Real-IP`, or else the *last* `X-Forwarded-For` hop, so a client can't spoof it.
- **Token lockout:** after 5 failed admin-token attempts from one IP, that IP waits 30 s, then 60 s, 120 s and so on, up to 1 h. A success clears the counter.
- **Admin token:**
  - It's compared in constant time on bytes, and anything over 256 chars is rejected.
  - Generated tokens are 256-bit (`secrets.token_urlsafe(32)`) and saved with file mode 0600.
  - A `LEDGER_ADMIN_TOKEN` env var under 20 chars is ignored.
- **Owner-only endpoints:** every `/api/owner/*` endpoint and `POST /api/bot_switch` needs the admin token.
- **Headers:**
  - CSP: `default-src 'self'`, no inline scripts (only inline style *attributes*), `frame-ancestors 'none'`.
  - Also: `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, HSTS for 1 year, a restrictive Permissions-Policy, and COOP/CORP same-origin.
  - API responses get `Cache-Control: no-store`.
- **CORS:** none. The site is same-origin, so other sites can't read the API from a browser.
- **Input validation:**
  - Only GET/HEAD/POST/OPTIONS are allowed.
  - `limit` is bounded, strategy ids must match `^[a-f0-9]{6,32}$`, actions are whitelisted, and `enabled` must be a JSON boolean.
- **Errors:** handlers return generic JSON with no stack traces. Exception text is never echoed, because RPC URLs can carry API keys.
- **Static files:** served only from `site/` by `send_from_directory` (safe_join). `wallets.json`, state files and `DATA_DIR` are not reachable.

## Data privacy
- **Public responses:** `privacy.py` removes trader names, wallet addresses (Solana and EVM) and any trader-identifying fields from every public response and every persona post.
- **Private key:** `SOLANA_PRIVATE_KEY` is read only in `real_trading.py`, is never printed, and is never in any response. `persona/values.clean` also strips anything shaped like a key, webhook URL or seed.

## Known/accepted
- **Token in the boot log:** an auto-generated admin token is printed once per boot in the Railway log, which only the owner can see. That's how the owner gets it. To keep it out of logs, set `LEDGER_ADMIN_TOKEN` (32+ random chars) in Railway; the log then shows `(env)`.
- **Repo contents:** `wallets.json` is in the git repo. Make the GitHub repo private if trader wallets must stay secret.
- **Rate-limit scope:** limits are per process. Mirko runs as one process, which is fine.
- **Dependencies:** `pip-audit -r requirements.txt` found no known vulnerabilities (2026-10-10).
