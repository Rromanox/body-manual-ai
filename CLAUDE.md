# Body Manual AI

An AI health coach that combines WHOOP data, Withings body composition, and daily check-ins to figure out how the user's body works — and writes their personal operating manual. Telegram-first private MVP.

**Read `docs/SPEC.md` before designing or modifying any feature.** It contains the full product spec, schema, prompting rules, and roadmap. This file is operational only.

## Current Phase

**Week 1 only.** Telegram `/start` → WHOOP OAuth → daily pull → `daily_metrics` → `/today` coach message via OpenAI API. Do NOT build Withings, check-ins, weekly summaries, or the manual until Week 1 success criteria pass (see SPEC §Roadmap).

## Stack

Python 3.12, FastAPI, PostgreSQL, SQLAlchemy + Alembic, python-telegram-bot, APScheduler, OpenAI API (Responses API). Docker Compose for local Postgres. Config via `.env` (python-dotenv). No frontend — the only web routes are OAuth redirects.

## Core Architecture Principle

**The backend calculates. The AI explains.** All baselines, deltas, trends, flags, and evidence counts are computed in Python/SQL. The AI receives a structured JSON payload of pre-computed conclusions and narrates them in plain English. The AI never derives numbers, never sees raw history, and is never trusted with arithmetic.

## Project Structure

```
app/
  main.py            # FastAPI app + startup
  config.py          # env config
  db.py              # session/engine
  models/            # one file per table (user, oauth_connection, daily_metric, journal_entry, coach_message, observation)
  services/
    whoop_client.py  # WHOOP v2 API only
    withings_client.py
    ai_client.py     # OpenAI calls + system prompt assembly
    metrics_normalizer.py   # provider data -> daily_metrics rows
    baseline_engine.py      # averages, deltas, flags
    coach_payload_builder.py
    manual_engine.py
  telegram/          # bot.py, handlers.py, keyboards.py
  routes/            # whoop_oauth.py, withings_oauth.py
  jobs/              # daily_pull.py, daily_message.py, weekly_summary.py
docs/SPEC.md
migrations/
```

## Critical Gotchas (do not skip)

1. **WHOOP is cycle-based, not calendar-day based.** Map cycles/sleep/recovery to the user's local date-of-waking using `users.timezone`. `daily_metrics.date` = the local day the user woke up.
2. **Pull at send time, with retry.** WHOOP recovery often finalizes around wake-up. The daily message job pulls fresh data first; if today's recovery is null, retry every 30 min (max 4) before sending a degraded message.
3. **Withings refresh tokens are single-use and rotate.** Persist the new refresh token in the same transaction as the refresh call. Handle invalid-refresh-token by marking the connection broken and prompting re-auth via Telegram. Same atomic-persist pattern for WHOOP.
4. **WHOOP `offline` scope is required** — it's what grants refresh tokens. Request only: `offline`, `read:cycles`, `read:recovery`, `read:sleep`, `read:workout`.
5. **Check-in tags always refer to YESTERDAY.** Prompted with the morning message; `journal_entries.date` = the date the behavior occurred (yesterday's local date). Never ambiguous.
6. **Any job failure or auth failure sends an alert to the admin Telegram chat** (`ADMIN_TELEGRAM_ID` in env). Silent pipeline death is the #1 project risk.
7. **`/delete` hard-deletes all user rows.** No soft delete. The consent message promises this.
8. **No `/ask` command.** Any non-command text message to the bot is treated as a question to the coach.

## AI Output Rules (enforced in system prompt — see SPEC §Prompting)

Never diagnose. Never recommend supplements, medications, or aggressive deficits. Never claim certainty from weak evidence — every pattern statement carries evidence counts and hedged language ("early signal", "may", never "confirmed"). Short messages on normal days (~1–2 sentences); detail only when something is unusual. Sustained anomalies (RHR elevated 5+ days, recovery cratered 7+ days, rapid unexplained weight loss) trigger a hard-coded "worth talking to a medical professional" message from the backend, not the LLM.

## Dev Conventions

Working code over perfect architecture, but keep provider clients, token handling, normalization, prompt building, and Telegram handlers strictly separated. Alembic for every schema change. Log the exact AI payload sent with every generated message (`coach_messages.summary_payload`) — it's the only way to debug bad outputs. Type hints everywhere. No tests required for Week 1 except the cycle→calendar-day mapping logic, which MUST have unit tests (it's the easiest thing to get subtly wrong).
