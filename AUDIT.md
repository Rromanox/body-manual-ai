# Body Manual AI — Audit (investigation only, no fixes yet)

> Status: **findings for review.** No code changed. Awaiting approval on the fix list at the bottom.

## Stack reality check (the prompt assumed a different stack)

This app is **Python 3.12 / FastAPI / PostgreSQL / SQLAlchemy+Alembic / python-telegram-bot / APScheduler / OpenAI**, deployed on **Railway**. It is **not** MongoDB/Node/PM2/Render. So "MongoDB messages collection", "PM2 logs", "unhandled promise rejections" don't apply — the equivalents are the `message_log` Postgres table, Railway logs, and Python `except` blocks. Findings below are mapped to the real stack.

---

## Step 0 — Evidence access

**Can I see the chat logs / message history?** They **exist**, but I **cannot reach them from this environment.**

- **Conversation history IS persisted** in Postgres:
  - `message_log` — every incoming + outgoing message with `direction`, `message_type`, `content`, `created_at` (written by `chat_logger.log_incoming/log_outgoing`; viewable in-app via `/chatlog`). **This is the primary evidence for Bug #1** — it records each outgoing `"Retatrutide shot is due today."` (message_type `system`) and each of your confirmation replies.
  - `coach_messages` — AI daily/weekly/Q&A messages + the exact `summary_payload`.
  - `health_reminders` — current reminder state (`last_completed_date`, `next_due_date`, `last_reminded_date`, `is_active`).
- **No dedicated job-execution log table.** Reminder fires are only recorded as (a) `logger.info(...)` lines in Railway logs and (b) the outgoing `message_log` row. So "when did the reminder actually fire" is answerable from `message_log` + Railway logs, not a structured job log.
- **I have no DB connectivity or Railway log access from this sandbox** (`DATABASE_URL` points at Railway/localhost; no local Postgres running; multipart isn't even installed locally). I cannot run the queries myself.

**What to export and hand me (to confirm Bug #1 empirically):**
1. From Railway Postgres, the last ~3 weeks of these rows (CSV or `pg_dump`):
   - `SELECT created_at, direction, message_type, content FROM message_log WHERE content ILIKE '%retatrutide%' OR content ILIKE '%shot%' ORDER BY created_at;`
   - `SELECT * FROM health_reminders;`
2. The last ~2 weeks of **Railway app logs** (grep `health_reminder` / `Sent health reminder`).

That said — I found the bug by code trace and am confident without the export; the export just confirms the firing pattern.

---

## Bug #1 (priority): Retatrutide reminder fires ~daily instead of every 6 days

**Root cause: TWO compounding defects**, both in the `/reta` system I built. Neither is a scheduler-duplication, timezone, or in-memory-state issue (those hypotheses are ruled out below).

### Cause A — the due check re-fires every day after the due date
`app/services/health_reminder.py:123` `due_reminders()`:
```python
HealthReminder.next_due_date <= today          # line 131  (NOT == today)
... and r.last_reminded_date != today ...       # line 136
```
Once `next_due_date` is in the past, the reminder is "due" **every day** until a *new completion* advances `next_due_date`. `last_reminded_date != today` only prevents **multiple sends on the same day** — it does **not** stop day-after-day reminders. So a single un-advanced due date produces one reminder **per day, indefinitely**.

### Cause B — common confirmations don't persist, so `next_due_date` never advances
The reminder that goes out (`app/jobs/health_reminder_job.py`, `_maybe_remind`) is a **plain text message with no confirm button**:
```python
text = f"{name} shot is due today."
await bot.send_message(chat_id=telegram_id, text=text)   # no reply_markup
```
(Contrast `supplement_reminder.py`, which sends `reply_markup=supplement_keyboard()` so creatine can be confirmed with one tap.)

To log the shot, your reply must pass `detect_reta_message` (`health_reminder.py:161`), which requires **both** a reta/shot keyword **and** a past-tense verb:
```python
_RETA_SIGNAL_RE = r"\b(reta|retatrutide)\b|\b(my|the)\s+shot\b"   # line 152
_TAKEN_RE       = r"\b(took|did|had|injected|done)\b"             # line 156
```
So natural replies to the reminder **fail to log**:
- `"took it"` → has a verb but no reta/"my shot" keyword → **not logged**
- `"done"` / `"yep"` / `"taken"` / `"yes"` → `"taken"` isn't even in the verb list; none have the keyword → **not logged**

When the confirmation isn't logged, `log_completion()` never runs, `next_due_date` stays in the past, and **Cause A nags you every day forever.**

**Combined effect:** exactly your symptom — the shot is on a 6-day cycle, but because the reminder can't be easily confirmed and re-fires daily once overdue, it "reminds nearly every day."

### Hypotheses explicitly ruled out
- **Confirmation not persisted (partial — this IS Cause B):** `/reta` and the *exact* phrases ("I took my retatrutide shot today") DO persist via `log_completion` (`health_reminder.py:49`, writes `last_completed_date` + `next_due_date`, committed). The gap is only the *reminder-reply* phrasings above.
- **Wrong comparison / static anchor:** No. `next_due_date = completed_date + interval_days` is correct; anchor is the real last completion.
- **Timezone/date-boundary:** No. Reminder dates are all **local** `date` objects from `get_user_today(user)` (per-user IANA tz); the job gates on `get_user_now(u).hour`. No naive/aware mixing in this path. You're US-Eastern (`America/Detroit` default = same offset as Orlando).
- **Duplicate schedulers:** No. `main.py` registers each job once per process with a fixed `id`, and APScheduler uses the default in-memory jobstore (no persistence across restarts), so jobs don't accumulate on redeploy.
- **State reset on restart:** No. Reminder state lives in the `health_reminders` **table**, not memory.

### Proposed fix (for approval — not yet applied)
1. **Add a one-tap confirm button** to the reminder (an inline `Taken ✓` keyboard + callback that calls `log_completion(today)`), mirroring the creatine flow. This closes Cause B with certainty.
2. **Broaden `detect_reta_message`** confirmation verbs/context (add `taken`, and treat a bare affirmative — `yes/yep/done/took it` — as confirmation **when a reta reminder was sent in the last ~2 days**, tracked via `last_reminded_date`).
3. **Stop the daily nag (Cause A):** after the due date, cap follow-up reminders (e.g. remind on the due date and at most once/day for N days, or switch to "due today" only on the exact date plus a single overdue nudge). Decide the policy with you.
4. **Reproduction tests first:** `last_completed = today-2d, interval 6` → `due_reminders` returns **nothing**; `= today-6d` → returns the reminder; after a button-confirm, `next_due = today+6` and it goes quiet.

---

## Full audit — findings by area

Severity: 🔴 bug · 🟡 risk · 🔵 improvement

### Scheduling & reminders
- 🔴 **Bug #1** above.
- 🟡 **All hourly jobs (reminders included) only run for users with an ACTIVE WHOOP connection.** Every job joins `oauth_connections` on `provider='whoop', status='active'` (`daily_message.py`, `supplement_reminder.py`, `weekly_message.py`, `proactive_check.py`, `health_reminder_job.py`). If WHOOP disconnects or is marked `broken`, **your medication reminder silently stops** — a med reminder shouldn't depend on a fitness token. (Fine for you today; real coupling risk.)
- 🟡 **Missed-tick / idle risk.** APScheduler uses the in-memory jobstore with `misfire_grace_time=600`. If Railway idles or is mid-redeploy during the only qualifying tick, a run is skipped. For the morning message the wake-watcher re-checks; for reminders, Cause A's `<= today` accidentally provides catch-up — but once Cause A is fixed, verify the reminder still catches up after a missed day.
- 🔵 **Idempotency is generally good** (daily message via `coach_messages` row; supplement via `noon/evening_reminder_sent` flags; reta via `last_reminded_date`).

### Date & timezone handling
- 🔵 **Actually solid.** `timekit.py` centralizes local time from `users.timezone` (IANA names → DST-correct via `zoneinfo`). Calendar dates (daily_metrics.date, reminder dates) are **local**; absolute instants (OAuth `expires_at`, API windows) are **UTC-aware** (`datetime.now(timezone.utc)`). No naive/aware mixing found in hot paths.
- 🔵 Default tz is `America/Detroit`; you're in Orlando — **same Eastern offset/DST**, so correct, but the label is slightly off. Confirm your `users.timezone` is set to `America/New_York`.

### State & persistence
- 🟡 In-memory module state exists but is all **transient/ephemeral**, none of it reminder due-state: `daily_message._in_flight` (re-entry guard), `withings_webhook._last_webhook_pull` (debounce), `withings_client._token_refresh_locks`. Safe to lose on restart. ✅ No stale-state reminder bug.
- 🟡 **`_update_coach_notes` swallows all errors silently** (`handlers.py:1540 pass  # never let ... crash`). Coach-notes extraction can fail invisibly. Low impact (structured `user_memories` is the newer tier), but it's a silent failure.
- 🔵 Confirmation writes are awaited and committed (`/reta`, check-ins, `/creatine`). Good.

### WHOOP & Withings ingestion
- 🟡 **WHOOP token refresh has no concurrency lock, but Withings does.** `withings_client.ensure_fresh_access_token` guards refresh with a per-user `asyncio.Lock` (because Withings refresh tokens are single-use/rotating). `whoop_client.ensure_fresh_access_token` (`whoop_client.py:122`) has **no lock** — two concurrent refreshes (e.g. `/today` + morning job) could race, and if WHOOP rotates refresh tokens, one gets invalidated → **silent sync stop until reconnect**. Recovery path exists (`status='broken'` + admin alert + reconnect prompt), but the race is avoidable.
- 🔵 **Token expiry handling is otherwise correct:** refresh with margin, atomic persist of new access+refresh tokens (`apply_token_response`), `WhoopAuthError → status='broken' + commit + raise` (surfaced by the daily job which alerts admin and prompts `/connect_whoop`).
- 🔵 **Missing-data handling is good:** the wake-aware morning job only sends once `_sleep_usable` (recovery OR sleep present), else waits/sends a degraded message — it does **not** silently reuse yesterday's data as today's.
- 🔵 **Duplicate ingestion is safe:** `metrics_normalizer` upserts on unique `(user_id, date)`; the Withings webhook has a 60s debounce.

### Telegram layer
- 🟡 **Send failures aren't specifically handled** in reminder/morning sends (`bot.send_message` without try/except at the call site). A Telegram rate-limit/network error inside a job bubbles to the job's `except Exception` → admin alert, but the message is **lost** (no retry). Low frequency; note for reliability.
- 🟡 **Intent fall-through:** the free-text router has grown many detectors (correction, data-audit, status-memory, constraint, reta, follow-through, then log-vs-question, then Q&A). Reasonable messages can land in an unexpected branch. Bug #1's Cause B is a concrete example (reminder replies fall through to Q&A). Worth an intent-coverage table.
- 🔵 **Two fast messages:** `_in_flight` guards the morning job; Q&A has no per-user lock but each message is independent (worst case: two AI answers). Low risk.

### LLM layer
- 🔵 "Today" is passed correctly — `now_block(user, now)` injects the user's real local datetime; payloads are pre-computed (backend-computes principle). No stale-day injection found.
- 🔵 API errors: `ai_client._respond` logs + falls back across the model chain, then re-raises; callers alert admin / send a graceful error. No crash, no infinite loop.
- 🟡 **Cost:** every Q&A now fires **two** background `EXTRACT` calls (legacy `coach_notes` via `extract_user_facts` **and** structured `user_memories` via `memory_extractor`) plus a recommendation-extraction call. Non-blocking, but 2–3 extra LLM calls per message. Retiring the legacy `coach_notes` path removes one.

### Memory architecture
- 🟡 **Two overlapping tiers, both written and read, partially redundant:**
  - `users.coach_notes` (blob) — written by `_update_coach_notes`, read as `about_you` in payloads. **Live.**
  - `user_memories` (typed) — written by `memory_extractor` + `/memory` + the (unrun) migration; read by `memory_retriever` (`for_qa/for_morning/...`) + `/memory`. **Live.**
  - No tier is write-only/read-before-write, but the **manual `coach_notes → user_memories` migration was never run**, so structured memory is sparse and the two systems run in parallel (extra cost, possible divergence). Planned retirement of `coach_notes` (Memory 2.0 Phase 8) would resolve this.

### General
- 🟡 **Bare-ish `except Exception:` blocks** are mostly intentional crash-proofing and log before swallowing; the one that swallows **silently** is `_update_coach_notes` (`handlers.py:1540`).
- 🔵 **Secrets:** no secrets in committed code; `.env.example` is the template. **Action:** confirm `.env` is in `.gitignore` and never committed (it exists locally in the repo dir).
- 🔵 **Dead/duplicated code:** `DailySnapshot.weight_velocity` is declared **twice** (`baseline_engine.py:92-93`) — harmless, long-standing TODO. Two weight-trend code paths coexist (Q&A `weight_trend_audit` vs morning `_build_weight_trend`) — can quote different rates; worth unifying.
- 🔵 **No job-execution audit log** (the gap you called out).

---

## Proposed fix list (ordered) — awaiting your approval

| # | Sev | Fix | Files |
|---|-----|-----|-------|
| 1 | 🔴 | **Bug #1**: add one-tap `Taken ✓` button to the reta reminder + callback → `log_completion`; broaden confirmation NL (add `taken`, accept a bare affirmative shortly after a reminder); cap the daily overdue re-fire per an agreed policy. **Test-first.** | `health_reminder_job.py`, `keyboards.py`, `handlers.py`, `health_reminder.py`, migration? (no schema change expected) |
| 2 | 🔵 | **Job-execution log** — a small `job_runs` table (job name, started/finished, outcome, counts) written by each scheduled job, so "when did X fire" is answerable. | new model + migration, `jobs/*` |
| 3 | 🟡 | **Decouple medication reminders from the WHOOP-active gate** (reta shouldn't stop if WHOOP disconnects). | `health_reminder_job.py` |
| 4 | 🟡 | **Add the per-user refresh lock to WHOOP** (match Withings) to avoid rotating-token races. | `whoop_client.py` |
| 5 | 🟡 | **Stop silent failure** in `_update_coach_notes` (log the exception). | `handlers.py` |
| 6 | 🔵 | **Reliability:** wrap job `bot.send_message` with a bounded retry on Telegram rate-limit/network. | `jobs/*` |
| 7 | 🔵 | Cleanup: remove duplicate `weight_velocity` field; plan weight-trend path unification; (later) retire `coach_notes` to drop an LLM call. | `baseline_engine.py`, memory phase |

**Bug #1 is the only 🔴.** I recommend doing #1 first (test-first, its own commit), then #2 (so we can *see* reminder history going forward), then the 🟡s. I will not touch anything until you approve the list (and pick the overdue-reminder policy in #1).
