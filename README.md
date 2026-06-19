# Body Manual AI — Week 1

Telegram `/start` → WHOOP OAuth → daily pull → `daily_metrics` → `/today` coach
message via the OpenAI API. See [SPEC.md](SPEC.md) for the full product spec.

## Setup

1. **Postgres**: `docker compose up -d`
2. **Python** (3.12): `python -m venv .venv`, activate it, then
   `pip install -r requirements.txt`
3. **Config**: copy `.env.example` to `.env` and fill it in:
   - `TELEGRAM_BOT_TOKEN` from @BotFather; `ADMIN_TELEGRAM_ID` is your own
     Telegram user id (failure alerts go there).
   - WHOOP: create an app at <https://developer.whoop.com> with scopes
     `offline read:cycles read:recovery read:sleep read:workout` and redirect
     URI `{BASE_URL}/auth/whoop/callback` (default:
     `http://localhost:8000/auth/whoop/callback`).
   - OpenAI: `OPENAI_API_KEY` from <https://platform.openai.com/api-keys>.
     `OPENAI_MODEL` picks the model (default `gpt-4o-mini`) — it always comes
     from env config, never from code. See **Model routing** below to give
     different task tiers different models.
   - `DEFAULT_TIMEZONE` must be YOUR IANA timezone (e.g. `America/New_York`) —
     it's how WHOOP cycles get mapped to calendar days.
4. **Migrations**: `alembic upgrade head`
5. **Run**: `uvicorn app.main:app --port 8000`

## Connecting WHOOP locally

`/connect_whoop` sends you an authorization link. The redirect lands on
`BASE_URL`, so:

- **Desktop browser on the same machine as the app**: works as-is with
  `BASE_URL=http://localhost:8000`.
- **From your phone**: your phone can't reach your laptop's localhost. Run
  `ngrok http 8000` (or `cloudflared tunnel`), set `BASE_URL` to the tunnel
  URL, restart the app, and register that callback URL in the WHOOP dashboard.

## Model routing

AI calls are grouped into named **routes**, each resolving to a model from env
config (never hard-coded). With only `OPENAI_MODEL` set, every route uses it —
identical to before. To give a tier its own model, set the matching env var:

| Route | Used for | Env var |
|-------|----------|---------|
| `fast` | log-vs-question / intent classification | `OPENAI_MODEL_FAST` |
| `extract` | structured JSON extraction (events, user facts) | `OPENAI_MODEL_EXTRACT` |
| `coach` | `/today`, Q&A, `/focus` | `OPENAI_MODEL_COACH` |
| `deep` | weekly summary (future: rules, `/manual`) | `OPENAI_MODEL_DEEP` |
| `quality_gate` | future anti-generic advice check | `OPENAI_MODEL_QUALITY_GATE` |

Resolution: route-specific var → `OPENAI_MODEL` → built-in default (`gpt-4o-mini`).
Routing lives in `app/services/model_router.py`; every OpenAI call goes through a
single helper (`ai_client._respond`) that logs route, model, token usage, and
latency. On **Railway**, set these as service variables (e.g. `OPENAI_MODEL_DEEP`)
and redeploy — no code change needed.

## Tests

`pytest` — covers the WHOOP cycle → local-calendar-day mapping (required Week 1
tests), the memory store, and model routing.

## Week 1 scope

Withings, check-ins, weekly summaries, Q&A, and `/manual` are deliberately not
built yet (`/delete` ships with the Week 3 consent-flow hardening). See
SPEC §Roadmap.
