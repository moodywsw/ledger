# Ledger's living persona

`persona/` runs as a background thread inside the bot on Railway. It reads everything the bot already logs (`journal.jsonl`), updates its **mood**, **memory** and **beliefs**, and posts in its own voice to Discord, X and the website. It costs **~$0/month**: free LLM tiers are optional, and without them it uses built-in templates.

## What it does
- **Voice:** English, first person, a degen-but-sharp trader. Short posts with few emojis. Every post goes through `persona/values.py`, which removes "NFA", financial-advice wording, shilling and paid-promo phrases, and any wallet address, key or webhook URL.
- **Mood:** confidence, greed/fear and tilt, updated from real closes, win/loss streaks and the SOL/BTC regime, and slowly drifting back to neutral. It shows up in posts, e.g. "feeling cautious after 3 losses".
- **Memory:** a lesson for every closed trade (trader, setup, result), plus facts from Fomo theses (when `FOMO_API_KEY` is set), its own theses, safety refusals and market data. Every 6 h these are boiled down into ≤8 **beliefs**, which are used in prompts and shown on the site.
- **Values:** it posts call-outs for tokens refused as rugs, honeypots, bundled or dev-dumps. It never hypes its own bags and posts losses the same way as wins.
- **Autoposts:** entries, exits (with reasoning), its own theses, reactions to tracked KOL theses, a daily recap and mood checks. Posts are rate-limited and deduplicated, and quiet hours are optional.
- State is stored in `$DATA_DIR/persona_state.json`.

## Env vars (Railway → service → Variables)
Each outlet stays off until its variables are set.

| Variable | Purpose |
|---|---|
| `GEMINI_API_KEY` | Gemini Flash free tier for writing posts (preferred) |
| `GROQ_API_KEY` | Groq free tier (used if no Gemini key) |
| `LEDGER_PERSONA_WEBHOOK` | Discord webhook URL for the channel it talks in |
| `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET` | X API v2, OAuth 1.0a user context (all 4 required) |
| `FOMO_API_KEY` | (already supported) lets it read and react to Fomo theses |

Optional tuning: `PERSONA_ENABLED` (default `true`), `PERSONA_X_MAX_PER_DAY` (12), `PERSONA_DISCORD_MAX_PER_DAY` (40), `PERSONA_QUIET_HOURS` (e.g. `1-7`, UTC, default off), `PERSONA_RECAP_HOUR_UTC` (22), `PERSONA_MOOD_EVERY_HOURS` (8), `PERSONA_BELIEFS_EVERY_HOURS` (6), `PERSONA_KOL_MIN_SCORE` (0.6), `GEMINI_MODEL` (`gemini-2.0-flash`), `GROQ_MODEL` (`llama-3.1-8b-instant`).

## Getting the free keys
### Gemini (free)
1. Go to https://aistudio.google.com and sign in with a Google account.
2. Click **Get API key** → **Create API key** (create it in a new project if asked).
3. Copy it into Railway as `GEMINI_API_KEY`.

### Groq (free, alternative)
1. Go to https://console.groq.com, sign up, then **API Keys** → **Create API Key**.
2. Save it as `GROQ_API_KEY`.

### X / Twitter (free tier, about 500 posts/month)
1. Create (or log into) the X account Ledger will post as.
2. Go to https://developer.x.com → **Developer Portal** and sign up for the **Free** plan (describe the use as "automated posts from my own trading bot account").
3. In the default **Project → App**, open **User authentication settings** → **Set up**: set App permissions to **Read and write**, Type of App to **Web App, Automated App or Bot**, and put any URL (e.g. your Ledger site) as Callback URI and Website. Save.
4. Go to **Keys and tokens**:
   - **API Key and Secret** → Regenerate → `X_API_KEY`, `X_API_SECRET`.
   - **Access Token and Secret** → Generate (do this *after* setting Read and write, or the token stays read-only) → `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`.
5. Add all four to Railway. The startup log will show `[PERSONA] alive — ... x=True`.

### Discord webhook (free)
1. In your Discord server, open the channel → ⚙️ **Edit Channel** → **Integrations** → **Webhooks** → **New Webhook**.
2. Name it "Ledger", optionally add an avatar, then **Copy Webhook URL**.
3. Save it as `LEDGER_PERSONA_WEBHOOK`. This is separate from `DISCORD_WEBHOOK_URL`, which keeps posting trade cards.

## Website
`GET /api/persona/feed?limit=30` returns:
```json
{"name":"Ledger","mood":{"label":"calm and patient","emoji":"🧠","confidence":0.5,"greed_fear":0,"tilt":0,"win_streak":0,"loss_streak":0,"regime":"neutral","pnl_today_sol":0},
 "beliefs":["..."],"posts":[{"ts":1760000000,"kind":"exit_win","text":"...","outlets":["discord","x"]}],
 "lessons_count":12,"outlets":{"discord":true,"x":false,"llm":"gemini"}}
```
Every post appears in the feed even when no outlet is configured, or when the X budget for the day is used up.

## Cost & limits
- Gemini Flash / Groq free tiers: Ledger makes ~20–60 calls/day, far below the free daily limits. If a call fails, it uses a template instead.
- The X free tier allows roughly 500 posts/month, so the default cap of 12/day stays under it. The last 3 daily slots are kept for recaps, exits and its own theses. The free tier is write-only, so it doesn't reply to mentions.
- Discord webhooks are free, though Discord rate-limits them per channel (fine at this volume).
- Fomo theses need `FOMO_API_KEY` (fomoapi.io, unofficial, has terms risk).

## Musings
Every `PERSONA_MUSE_EVERY_HOURS` (default 3.5h) Ledger posts a non-trade thought (market, culture, narratives, KOLs, macro, AI, bot life, lessons, headlines, trending), rotating topics with no repeat in the last 5. Free inputs: alternative.me Fear & Greed, CoinGecko trending, RSS (`PERSONA_RSS_FEEDS`, default CoinDesk/Decrypt/The Block). LLM writes freely if GEMINI/GROQ key set, otherwise templates. Same safety filter and daily caps.
