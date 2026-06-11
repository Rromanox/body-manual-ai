# Body Manual AI — Product Spec

Working name: **Body Manual AI**

> An AI coach that figures out how your body actually works — and writes the manual.

This project combines WHOOP physiology data, Withings body-composition data, and short daily check-ins into a personal AI health coach. The goal is not to replace WHOOP or Withings — they already do dashboards, scores, and their own journal correlations well. The goal is **cross-device synthesis**: connecting recovery, body composition, and real-life context into one simple, conversational, long-term picture that neither platform can build alone.

## Product Philosophy

Do not build another dashboard. The app answers four questions:

1. What is happening with my body?
2. Why might it be happening?
3. What should I do today?
4. What patterns are we learning about me over time?

The north star is the **Personal Operating Manual** — a living document of evidence-backed patterns about this specific user that accumulates value over weeks and months. The daily message is the engagement loop; the manual is the reason to stay.

The MVP must prove one thing: *would the user miss the morning message and the manual if they stopped?*

## MVP Boundaries

Telegram-first, private, one user (the developer) first, then 2–3 friends.

**Non-goals for MVP:** native mobile apps, web dashboard (beyond OAuth redirect pages), custom recovery score, habit/streak scoring, social features, people-like-you engine, billing, nutrition tracking, complex ML, PDF reports, polished branding, cross-user data pooling.

---

## Features

### 1. Telegram Bot (the entire interface)

Commands:

- `/start` — onboarding + plain-English consent message
- `/connect_whoop` — begins WHOOP OAuth
- `/connect_withings` — begins Withings OAuth
- `/checkin` — manual trigger for the daily check-in (also prompted automatically with the morning message)
- `/today` — generate/resend today's coach message on demand
- `/weekly` — this week's summary on demand
- `/manual` — render the current Personal Operating Manual
- `/delete` — hard-deletes all of the user's data after confirmation

**There is no `/ask` command.** Any plain text message that is not a command is treated as a question to the coach (see §Chat Q&A). This is the natural Telegram interaction model.

Inline buttons are used for check-ins and confirmations.

### 2. WHOOP Integration (API v2)

OAuth 2.0 authorization-code flow. Scopes: `offline` (required — grants refresh tokens), `read:cycles`, `read:recovery`, `read:sleep`, `read:workout`. Request nothing else.

**Cycle mapping rule:** WHOOP data is organized around physiological cycles, not calendar days. All cycles, sleeps, recoveries, and workouts must be mapped to the user's **local date of waking** using `users.timezone`. `daily_metrics.date` represents the day the user woke up. This mapping logic gets unit tests before anything else does.

**Token handling:** access + refresh tokens stored per user in `oauth_connections`. Token refresh persists the new tokens atomically with the refresh call. A failed refresh marks the connection broken, alerts the admin, and sends the user a re-connect prompt.

### 3. Withings Integration

OAuth 2.0 flow against the Withings Public API. Target measures (all nullable — devices vary): weight, body fat %, muscle mass, fat-free mass, water %, bone mass, BMI. Map measurements to the local date they were taken.

**Critical gotcha — token rotation:** Withings refresh tokens are single-use. Every refresh returns a new refresh token and invalidates the old one. The new refresh token MUST be persisted in the same transaction as the refresh request; if persistence can fail after the call succeeds, the user's connection silently dies. Handle the invalid-refresh-token error path explicitly: mark connection broken, alert admin, prompt user to re-auth.

### 4. Daily Data Pull

A scheduled job pulls latest WHOOP and Withings data each morning per user.

**Pull-at-send-time with retry:** WHOOP recovery scores often finalize around when the user wakes. The morning-message job pulls fresh data at send time. If today's recovery is missing, retry every 30 minutes (max 4 retries) before sending a gracefully degraded message ("Your recovery score isn't in yet — here's what sleep and yesterday's strain suggest..."). Never send "data missing" as the headline on a normal day.

**Failure alerting:** any pull failure, API error streak, or auth failure sends a Telegram message to `ADMIN_TELEGRAM_ID`. The most likely failure mode of this entire project is a silently dead pipeline that nobody notices for a week.

### 5. Daily Check-In

The app's own context layer. WHOOP journal data is not available via the public API, so this check-in is the only source of life context — it is required, not optional polish.

**Semantics (never ambiguous):** the check-in is prompted as part of the morning message, and the tags always refer to **yesterday**. `journal_entries.date` = the local date the behavior occurred (yesterday's date). The correlation engine depends on this being consistent.

Maximum 5–6 taps. Inline buttons:

- Alcohol
- Late meal
- High stress
- Sick
- Travel
- Hard day
- None of these

Optional: feel score 1–5, free-text note. Target completion time: under 20 seconds. It must feel lighter than WHOOP's journal, not heavier.

### 6. Daily Coach Message

Generated each morning from: today's recovery, sleep, HRV, resting HR, strain, workouts, weight trend, body-fat trend, yesterday's check-in tags, and 7/30-day baselines.

The backend computes everything and sends the AI a structured payload of conclusions:

```json
{
  "user_goal": "fat_loss_muscle_maintenance",
  "recovery": {"today": 54, "baseline_30d": 71, "flag": "low_vs_baseline"},
  "sleep_hours": {"today": 5.8, "baseline_30d": 7.4, "flag": "short_vs_baseline"},
  "resting_hr": {"today": 62, "baseline_30d": 55, "flag": "elevated"},
  "hrv": {"today": 42, "baseline_30d": 58, "flag": "below_baseline"},
  "yesterday_strain": "high",
  "weight": {"trend_per_week": -0.4, "overnight_change": 2.1, "flag": "spike_likely_water"},
  "body_fat": {"trend_per_week": -0.2, "flag": "improving"},
  "yesterday_tags": ["late_meal", "high_stress"],
  "data_days_available": 41
}
```

The AI narrates. Example target output for the payload above:

> Your body looks more stressed than usual today. The main clues: lower recovery, shorter sleep, and a higher resting heart rate — and yesterday was a high-strain, high-stress day with a late meal. Not a failure, just a day for easy training, water, and an earlier bedtime. Your weight is up 2 lbs overnight, but your body-fat trend is still improving, so that's almost certainly water, not fat.

**Message variability is a hard requirement.** On normal days the message is 1–2 sentences ("Green light. Nothing unusual — train normally."). Detail is reserved for days when something is genuinely off. A repetitive daily message is the #1 retention killer.

### 7. Personal Operating Manual

The north star. Rendered by `/manual` from the `observations` table. MVP version is plain text:

```
Kevin's Body Manual

Confirmed Patterns:
- Not enough data yet.

Hypotheses Under Observation:
- Late meals may reduce next-day recovery.
  Evidence: 4 of 6 logged late-meal days. Early signal, not confirmed.
- High-stress days may raise resting heart rate.
  Evidence: 3 of 5 logged high-stress days. Needs more data.

Needs More Data:
- Alcohol impact
- Travel impact
- Hard evening workouts
```

Rules:

- Every line carries an evidence count. No exceptions.
- Hedged language only: "may", "appears", "early signal", "worth watching".
- The status `confirmed` does not exist in MVP. Strongest status is `stronger_signal`.
- The manual visible from day one as a work in progress is itself the retention mechanic — watching it fill in IS the product.

### 8. Weekly Summary

Sent Sunday evening. Recovery trend, sleep trend, strain trend, weight trend, body-fat trend, muscle trend (if available), biggest positive signal, biggest opportunity, one focus for next week. Same payload→narration architecture as the daily message. Short.

### 9. Chat Q&A

Any non-command text message is a question. The handler builds a fatter payload (30-day summary table, recent journal tags, current observations) and answers conversationally. Same system prompt and safety rules. No RAG — at this scale, structured summaries in context are sufficient.

---

## Database Schema

Postgres, Alembic migrations from day one.

**users**: id, telegram_id, first_name, username, timezone, goal, created_at, updated_at. *(No `deleted_at` — see /delete rule below.)*

**oauth_connections**: id, user_id, provider (`whoop`|`withings`), access_token, refresh_token, expires_at, scopes, status (`active`|`broken`), created_at, updated_at. Token columns isolated so encryption-at-rest can be added before friends onboard.

**daily_metrics** — one row per user per local date, wide and nullable:
id, user_id, date, recovery_score, hrv_ms, resting_heart_rate, respiratory_rate, spo2, skin_temp, sleep_hours, sleep_efficiency, sleep_performance, sleep_consistency, strain, workout_count, total_workout_minutes, weight, body_fat_pct, muscle_mass, fat_free_mass, water_pct, bone_mass, bmi, raw_whoop_json (JSONB), raw_withings_json (JSONB), created_at, updated_at. Never fail when a provider is missing data.

**journal_entries**: id, user_id, date *(= date behavior occurred, i.e. yesterday at prompt time)*, tags (JSONB array), feel_score (nullable int), free_text (nullable), created_at, updated_at.

**coach_messages**: id, user_id, date, message_type (`daily`|`weekly`|`manual`|`q_and_a`), summary_payload (JSONB — the exact payload sent to the AI, always logged), ai_response (text), created_at.

**observations**: id, user_id, pattern_key, pattern_description, trigger_tag, outcome_metric, occurrence_count, supporting_count, opposing_count, first_seen, last_seen, status (`watching`|`promising`|`weak`|`stronger_signal`|`archived`), notes, created_at, updated_at.

**`/delete` is a hard delete.** It removes every row belonging to the user across all tables after an inline-button confirmation. No soft-delete column, no retention. The consent message at `/start` promises this and the code honors it literally.

## Baseline Logic

Compute on the fly with SQL window functions — no caching until it's slow.

- 7-day average, 30-day average, today-vs-30-day delta, weekly trend, monthly trend.
- Data maturity gates: <7 days → "still building your baseline" framing only; 14 days → early baseline language; 30 days → trend language; 60–90 days → manual insights eligible for `promising`/`stronger_signal`.
- Never over-personalize before the data exists. "We're still learning your patterns" is an honest and acceptable message.

## AI Prompting System

Three layers:

1. **System prompt** — coach persona + hard rules below.
2. **Structured payload** — pre-computed conclusions only (see §Daily Coach Message). The AI never receives raw history and never does arithmetic.
3. **Few-shot examples** — 4–6 payload→message pairs covering: normal day, bad-recovery day, recomposition week, water-weight spike, not-enough-data-yet. Few-shots control tone more than instructions do.

### System prompt hard rules

The coach must: speak simply; avoid jargon unless explaining it; never diagnose; never claim certainty from weak evidence; never recommend medications, supplements, or extreme diets; never recommend aggressive calorie deficits; never shame the user; normalize normal fluctuation; distinguish correlation from causation; use personal-baseline language ("higher than YOUR normal"), not population claims; translate every metric into an action; keep normal-day messages to 1–2 sentences; vary structure day to day.

Tone target:

> Green light today. Nothing looks unusual — train normally and keep your bedtime consistent.

Tone to never produce:

> Your autonomic nervous system is exhibiting sympathetic dominance due to reduced RMSSD.

## Safety Rules (backend-enforced, not LLM-discretionary)

Hard-coded backend triggers that override/append to the AI message:

- Resting HR elevated 5+ consecutive days
- Recovery very low 7+ consecutive days with no logged explanation
- Rapid unexplained weight loss
- Repeated very poor sleep
- User message contains concerning symptoms (chest pain, fainting, severe dizziness, etc.)

Response pattern: "This pattern is worth taking seriously. I can't diagnose anything, but if this continues or you feel unwell, it would be smart to talk with a medical professional." The app is a wellness coach, not a doctor, and never pretends otherwise.

Weight-loss guardrails: never praise rapid loss; never push deficits; rapid loss triggers the same caution path as other anomalies.

## Privacy Rules

- Plain-English consent at `/start`: what's stored, that summaries are sent to an AI provider to generate messages, and that `/delete` erases everything.
- `/delete` hard-deletes (see schema section).
- OAuth tokens isolated in one module; encryption at rest added before any friend onboards.
- Every user's data is fully separate. No pooling, no cross-user features, no "people like you" in MVP.

---

## Roadmap

### Week 1 — "First WHOOP-based message"

1. FastAPI app + config + Docker Compose Postgres
2. Schema + Alembic migrations
3. Telegram bot + `/start` with consent message
4. WHOOP OAuth (routes + token storage)
5. WHOOP v2 pull: cycles, recovery, sleep, workouts
6. Cycle → local-calendar-day mapping (with unit tests)
7. `daily_metrics` storage
8. Baseline engine + payload builder
9. OpenAI API call (Responses API) with system prompt + few-shots
10. `/today` sends a real coach message
11. Admin failure alerts

**Success criteria:** one real user connects WHOOP; real data lands in `daily_metrics`; `/today` produces a message the developer would actually read.

### Week 2 — "Full daily loop"

1. Withings OAuth + measurement pull (token rotation handled correctly)
2. Body-composition columns populated
3. `/checkin` inline buttons (yesterday semantics)
4. Scheduled morning message with pull-at-send-time retry
5. Plain-text Q&A handler
6. `/weekly`
7. Minimal `/manual` (reads observations table, even if mostly empty)

**Success criteria:** daily message arrives automatically every morning; check-in takes <20 seconds; weekly summary works; manual renders.

### Week 3+ — "Friends"

1. Onboarding polish + `/delete` + consent flow hardening
2. Token-refresh edge cases + re-auth prompts
3. Message variety improvements
4. Observation tracking: simple counter updates from journal-tag → next-day-metric pairs
5. Manual evidence counts populating for real

### Later (not now)

Experiments engine (with "supports the hypothesis" language, never "confirmed"), Apple Health, nutrition integrations, web/mobile clients, custom composite scores.

## Acceptance Test

After the first working version: *would the developer personally read this message every morning for 30 days?* If no, fix message quality before adding any feature. The product lives or dies on the daily message and the manual feeling personally true.
