# Memory 2.0 + Recommendation Intelligence — Implementation Plan

> Status: **PROPOSAL — not yet approved, nothing implemented.**
> Scope: turn the bot from "narrates today's numbers" into "remembers, recommends, checks whether the advice worked, and learns personal rules over time."
> Stack reality check: AI provider is **OpenAI Responses API** (`app/services/ai_client.py`, `settings.openai_model`), DB is **PostgreSQL on Railway**, no pgvector today. Single private user.

---

## 1. Current Architecture Summary

The app is a Telegram bot (`app/telegram/bot.py` registers handlers; webhook in `app/main.py`) backed by FastAPI + APScheduler + SQLAlchemy/Alembic.

**Data flow:** WHOOP/Withings → `app/jobs/daily_pull.py` (`pull_and_store`) → `metrics_normalizer.py` maps WHOOP cycles to the local waking date → `daily_metrics` table (one row per `(user_id, date)`).

**Computation layer ("backend computes"):** `app/services/baseline_engine.py` is the core. `build_daily_snapshot`, `build_weekly_snapshot`, and `build_qa_context` compute 7d/30d baselines, flags, streaks, sleep debt, HRV trend, workout effect, weight velocity, safety triggers — all as finished conclusions in dataclasses (`DailySnapshot`, `WeeklySnapshot`, `QAContext`).

**Payload layer:** `app/services/coach_payload_builder.py` (`build_daily_payload`, `build_weekly_payload`, `build_qa_payload`) turns those dataclasses into the exact JSON dict handed to the AI. Every number is pre-rounded; nothing is left for the model to compute.

**Narration layer ("AI explains"):** `app/services/ai_client.py` holds `SYSTEM_PROMPT`, `WEEKLY_SYSTEM_PROMPT`, `QA_SYSTEM_PROMPT`, `FOCUS_SYSTEM_PROMPT`, plus few-shot example arrays. `generate_daily_message`, `generate_weekly_message`, `generate_qa_response`, `generate_focus_response` make the calls. Two auxiliary extractor calls already exist: `classify_and_extract` (is this message a behavior log?) and `extract_user_facts` (pull persistent facts from a Q&A exchange).

**Pattern layer:** `app/services/observation_engine.py` correlates check-in tags with next-day metrics (`recalculate_observations`) and builds in-the-moment "closed loops" (`build_closed_loops`). Results live in the `observations` table with a status ladder: `watching → promising → stronger_signal` (and `weak`).

**Other engines:** `experiment_engine.py` (user-named A/B vs 14-day baseline → `experiments` table), `commitment_engine.py` (reads `events` where `event_type='commitment'`), `event_engine.py` (free-text log → `events` + rolls into journal tags), `sleep_optimizer.py` (bedtime profile, sleep debt, wake consistency, pre-sleep factor impact).

**Persistence of messages:** every generated message writes a `coach_messages` row with `summary_payload` (the exact JSON) + `ai_response`. This is already the audit trail.

**Scheduler (`app/main.py` lifespan):** four hourly-tick jobs gated on each user's local clock — `run_daily_message`, `run_supplement_reminder`, `run_weekly_message`, `run_proactive_check`.

**Tables today:** `users`, `oauth_connections`, `daily_metrics`, `journal_entries`, `coach_messages`, `observations`, `experiments`, `events`, `supplement_logs`, `message_log`. Migrations are sequential `0001`–`0010`; next is `0011`.

---

## 2. Existing Memory / Coach-Notes Flow

**Where it's stored:** `users.coach_notes` — a single `JSONB` blob (model: `app/models/user.py:26`, migration `0009`). Shape is a dict of list-valued keys: `supplements`, `medications`, `health_context`, `goals`, `lifestyle`, `other`.

**Where it's written:** `handlers.plain_text` → after every Q&A answer it fires `asyncio.create_task(_update_coach_notes(...))` (`handlers.py:1090`). That calls `ai_client.extract_user_facts` (`FACT_EXTRACTOR_SYSTEM_PROMPT`), which returns only *new* facts, then merges them into the blob (list-append with a dedup check). It "never lets background fact extraction crash anything" — swallows all exceptions.

**Where it's read / injected:**
- Daily message: `build_daily_payload` puts it under `about_you` (`coach_payload_builder.py:130`).
- Q&A: `build_qa_context` loads `user.coach_notes` → `build_qa_payload` emits `about_you` (`coach_payload_builder.py:238`).
- Both `SYSTEM_PROMPT` and `QA_SYSTEM_PROMPT` describe `about_you` as "your permanent memory."

**Commitments** are a parallel, semi-structured memory: `events(event_type='commitment')`, surfaced by `get_active_commitments` (last 7 days, max 2) into the daily payload's `commitments` key.

**Observations** are the closest thing to "learned rules": evidence-counted, status-laddered, but limited to **single hardcoded tag→metric pairs** (`TRACKED_PAIRS`).

---

## 3. Gaps (current → Memory 2.0)

| Capability wanted | Today | Gap |
|---|---|---|
| Structured, typed memory | One JSONB blob, 6 freeform keys | No IDs, no types beyond those 6, no per-fact confidence/timestamps/status, no correction, weak dedup |
| Queryable / relevance-filtered memory | Whole blob dumped into every call | No retrieval layer; irrelevant facts always included; grows unbounded |
| User can inspect/correct memory | None | No `/memory` commands; can't see, edit, delete, or confirm a fact |
| Recommendation ledger | Nothing | Advice is free text inside `ai_response`; never extracted or structured |
| Did the advice work? | Nothing (closest: `build_closed_loops`, but that's behavior→metric, not advice→outcome) | No checkpoint creation or evaluation |
| Testable predictions | Nothing | No prediction storage/checking |
| Personal rules (compound) | `observations` (single pair only) | No multi-condition rules ("HRV down >15% **and** RHR up >5"); no rule lifecycle commands |
| Anti-generic quality gate | Style enforced only by prompt + few-shots | No post-generation check / regenerate loop |
| Memory in weekly review | Weekly summary has metric trends + tag patterns | No "here's what I learned / keep-edit-delete" loop |
| Disliked advice / constraints | None | Can't record "don't give me hydration advice" and have it suppress future advice |

---

## 4. Proposed Database Schema

Design principles: **extend existing tables where they already do the job** (observations, events), add new tables only for genuinely new concepts. Keep everything single-user-clean but FK'd to `users` with `ondelete=CASCADE` (so `/delete` still nukes everything in one statement — see `handlers.delete_callback`). All new migrations follow the `0011`, `0012`… pattern.

### 4.1 `user_memories` (new) — the structured replacement/superset of `coach_notes`

Purpose: one row per discrete remembered fact/preference/constraint/goal/context, with type, confidence, lifecycle, and provenance.

| field | type | notes |
|---|---|---|
| `id` | int PK | |
| `user_id` | FK users CASCADE, indexed | |
| `memory_type` | str(32) | enum-in-code: `stable_fact, preference, constraint, goal, commitment, context_event, disliked_advice, hypothesis, confirmed_rule, training_preference, schedule_pattern, recovery_trigger, weight_context, food_pattern, sleep_pattern` |
| `content` | Text | canonical human-readable statement ("Prefers blunt, direct coaching") |
| `structured` | JSONB | optional machine fields (e.g. `{"window":"after_21:00","metric":"recovery"}`) |
| `status` | str(16) | `active, watching, archived, superseded` (default `active`) |
| `source` | str(16) | `user_stated, ai_extracted, derived` (derived = promoted from data) |
| `confidence` | str(12) | `low, medium, high` (see §13) |
| `tags` | JSONB (list[str]) | for keyword retrieval ("sleep","alcohol","travel") |
| `evidence_count` | int | how many times reinforced/observed (default 1) |
| `expires_at` | Date null | set for `context_event` / `commitment`; null = permanent |
| `last_seen_at` | Date | last time reinforced or referenced |
| `superseded_by` | int null (self-FK) | for corrections/merges |
| `created_at` / `updated_at` | timestamptz | |

Indexes: `(user_id, status)`, `(user_id, memory_type)`, GIN on `tags` (optional; keyword filtering can be Python-side at single-user scale).

Lifecycle: `active` → `archived` (user deletes/ignores) or `superseded` (corrected/merged). `context_event`/`commitment` auto-archive past `expires_at`.

### 4.2 `recommendation_ledger` (new) — every meaningful piece of advice

Purpose: structured record of advice given, why, and what to check later.

| field | type | notes |
|---|---|---|
| `id` | int PK | |
| `user_id` | FK users CASCADE, indexed | |
| `coach_message_id` | FK coach_messages null | link back to the message it came from |
| `created_date` | Date indexed | local date advice was given |
| `source_type` | str(16) | `daily, qa, weekly, focus` |
| `recommendation` | Text | the action, normalized ("Keep strain under 10 today") |
| `category` | str(24) | `training, sleep, nutrition, recovery, stress, hydration, other` |
| `rationale` | Text | why ("HRV 16% below baseline, RHR +5") |
| `trigger_snapshot` | JSONB | the numbers that triggered it (copied from payload) |
| `expected_outcome` | Text | "recovery stabilizes/improves tomorrow" |
| `checkpoint_metric` | str(24) null | `recovery_score, hrv_ms, sleep_hours, weight…` |
| `checkpoint_date` | Date null | when to evaluate (deterministic, set at creation) |
| `checkpoint_direction` | str(8) null | `up, down, stable` (expected move) |
| `baseline_value` | float null | metric value at creation, for comparison |
| `followed` | str(12) | `unknown, yes, no, partial` (default `unknown`) |
| `outcome` | str(14) | `pending, improved, worsened, inconclusive, not_checkable` (default `pending`) |
| `outcome_detail` | JSONB null | `{"from":42,"to":61,"delta":19}` |
| `linked_rule_id` | FK personal_rules null | if a rule generated it |
| `created_at` / `updated_at` | timestamptz | |

Indexes: `(user_id, outcome)` (find pending), `(user_id, checkpoint_date)` (find due), `(user_id, created_date)`.

> Note: `recommendation_checkpoints` and `prediction_checks` from your brief are **folded into this table** (the checkpoint fields). A separate checkpoints table would be over-normalized for one checkpoint per recommendation. If a recommendation ever needs *multiple* checkpoints, split later — flagged as a deliberate simplification.

### 4.3 `personal_rules` (new) — compound, promoted rules

Purpose: multi-condition "if X then Y" rules that `observations` can't express. Observations stay as the single-pair detector; rules are the promoted, possibly-compound, user-confirmable layer.

| field | type | notes |
|---|---|---|
| `id` | int PK | |
| `user_id` | FK users CASCADE, indexed | |
| `title` | str(160) | "Avoid high strain when HRV is down and RHR is up" |
| `description` | Text | plain-English |
| `trigger_conditions` | JSONB | machine form: `[{"metric":"hrv_ms","op":"pct_below_baseline","value":15},{"metric":"resting_heart_rate","op":"above_baseline_bpm","value":5}]` |
| `recommended_action` | Text | "keep day strain under 10" |
| `related_metrics` | JSONB (list) | |
| `related_tags` | JSONB (list) | |
| `evidence_count` | int | supporting days |
| `contradicting_count` | int | |
| `confidence` | str(12) | `low/medium/high` derived from counts (see §13) |
| `status` | str(12) | `watching, emerging, confirmed, retired` |
| `origin` | str(12) | `derived, user_stated` |
| `source_observation_key` | str(64) null | if promoted from an `observations.pattern_key` |
| `supporting_examples` | JSONB | list of `{date, snapshot}` (capped, e.g. last 10) |
| `last_validated` | Date null | last day evidence re-confirmed it |
| `created_at` / `updated_at` | timestamptz | |

Indexes: `(user_id, status)`.

### 4.4 `memory_reviews` (new, lightweight) — weekly review state

Purpose: track which candidate memories were proposed to the user and their disposition, so the weekly "keep/edit/delete" loop is idempotent and auditable.

| field | type | notes |
|---|---|---|
| `id` | int PK | |
| `user_id` | FK users CASCADE, indexed | |
| `review_date` | Date | |
| `candidate_memory_ids` | JSONB (list[int]) | memories surfaced for review |
| `disposition` | JSONB | `{memory_id: "kept"/"edited"/"deleted"}` |
| `status` | str(12) | `pending, completed` |
| `created_at` | timestamptz | |

> `memory_events` from your brief (an append-only log of memory changes) is **deferred**. The `superseded_by` chain + `updated_at` give us correction history cheaply; a full event log is enterprise-grade and not needed for one user. Flagged as a conscious cut.

### 4.5 Relationship to existing tables

- `events(event_type='commitment')` stays the **immutable raw capture**. Active commitments are *also* represented as `user_memories(memory_type='commitment', expires_at=...)` so they get lifecycle (kept/broken, expiry). The commitment engine is rewritten to read memories, not events directly (events remain the source for the extractor).
- `observations` unchanged. `personal_rules` references promoted ones via `source_observation_key`.
- `coach_messages` gains an optional back-link from `recommendation_ledger.coach_message_id` (no schema change to coach_messages needed).

---

## 5. Services / Modules to Add

All new services live in `app/services/`. They follow the existing convention: pure-ish functions taking a `Session`, deterministic math in Python/SQL, AI only for language tasks.

### 5.1 `memory_store.py` — `MemoryStore`
- **Responsibility:** CRUD + dedup/merge for `user_memories`. The only writer of that table.
- **Key fns:** `add_memory(...)`, `get_active(user_id, types=?, tags=?)`, `supersede(old_id, new_id)`, `archive(id)`, `confirm(id)` (bumps confidence to `high`, source→`user_stated`), `merge_duplicates(user_id)` (semantic-ish dedup by type + normalized content; AI-assisted only when keyword overlap is ambiguous).
- **Runs:** on demand from handlers, extractor, retriever, weekly job.

### 5.2 `memory_extractor.py` — `MemoryExtractor`
- **Responsibility:** turn a Q&A exchange (and optionally a check-in note) into typed memory candidates. **Replaces** `extract_user_facts`'s blob output with typed rows.
- **In:** `user_message`, `ai_response`, existing memories (for "don't repeat"). **Out:** `list[MemoryCandidate]` with `memory_type`, `content`, `tags`, `confidence`, `expires_at?`.
- **Runs:** background task after Q&A (same slot as today's `_update_coach_notes`), never crashes the turn.

### 5.3 `recommendation_ledger.py` — `RecommendationLedgerService`
- **Responsibility:** write/read `recommendation_ledger`; extract structured recommendations from a generated message.
- **`extract_from_message(message_text, payload, source_type)`** → uses an AI extractor prompt to pull `[{recommendation, category, rationale, expected_outcome, checkpoint_metric, checkpoint_direction}]`; backend fills `checkpoint_date`, `baseline_value`, `trigger_snapshot` deterministically from the payload + date.
- **Runs:** background, right after `generate_daily_message` / `generate_qa_response` persist their `coach_messages` row.

### 5.4 `checkpoint_service.py` — `RecommendationCheckpointService` (also covers prediction checks)
- **Responsibility:** **deterministic** evaluation. Find ledger rows where `outcome='pending'` and `checkpoint_date <= today`; compare the metric now vs `baseline_value` in the expected `checkpoint_direction`; set `outcome` ∈ `improved/worsened/inconclusive/not_checkable` and `outcome_detail`.
- **In:** `Session, user_id, today`. **Out:** list of just-evaluated results (for the morning payload).
- **Runs:** at the **start** of `daily_message._do_send_for_user`, after the pull, before payload build — so today's message can say "yesterday's call worked."

### 5.5 `personal_rule_engine.py` — `PersonalRuleEngine`
- **Responsibility:** **deterministic** promotion. (a) Promote mature `observations` into `personal_rules` when thresholds met. (b) Run a small set of **compound detectors** (hardcoded condition templates, e.g. "low HRV + high RHR → bad next-day recovery") over `daily_metrics` history, counting supporting vs contradicting days. (c) Recompute `confidence`/`status`; retire rules that stop validating.
- **In:** `Session, user_id, today`. **Out:** none (writes rules). 
- **Runs:** weekly (in `weekly_message`) and on `/manual` (cheap recompute, like `recalculate_observations` is called on `/manual` today).
- **AI usage:** none for math; optional AI only to phrase `title`/`description` nicely.

### 5.6 `memory_retriever.py` — `MemoryRetriever`
- **Responsibility:** the relevance layer. Select the *right* memories/rules/recent-recs for each task and return a compact, token-budgeted dict for the payload builder.
- **Fns:** `for_daily(...)`, `for_qa(question, ...)`, `for_weekly(...)`, `for_manual(...)`, `for_focus(...)`.
- **Strategy (hybrid, no embeddings v1):** SQL/Python filter by `memory_type` relevance per task + `status='active'` + recency + confidence; for Q&A, **keyword overlap** scoring between the question tokens and `memory.content`+`memory.tags` (+ recommendation category) to rank, take top-N. Hard cap counts per section (e.g. ≤6 memories, ≤3 rules, ≤3 pending recs). Embeddings/pgvector noted as a **future upgrade** if keyword recall proves weak.

### 5.7 `advice_quality.py` — `AntiGenericAdviceEvaluator`
- **Responsibility:** gate generated daily/Q&A messages before sending.
- **Deterministic checks first (cheap):** does the text reference at least one number that appears in the payload? does it contain an actionable verb/target? is it within length bounds? does it avoid a banned-phrase list ("stay hydrated", "prioritize sleep" with no number, "listen to your body")? does it respect `disliked_advice` memories (e.g. contains "hydration" when user dislikes hydration advice)?
- **Optional AI critic** (one cheap call) only when deterministic checks are borderline.
- **Action:** if it fails, **regenerate once** with an appended instruction ("be specific, cite the number, give one concrete action"); if it still fails, send anyway but log it for debugging.
- **Runs:** inside `generate_daily_message`/`generate_qa_response` flow (or wrapping them in handlers/jobs).

### 5.8 Migration helper `migrate_coach_notes.py` (one-shot)
- Convert each `coach_notes` key into typed `user_memories` rows (see §15).

---

## 6. Prompting Changes (`app/services/ai_client.py`)

New prompt constants + extended existing ones. Keep the "AI narrates pre-computed facts" rule everywhere.

**New extractor prompts (JSON-out, like `EVENT_EXTRACTOR_SYSTEM_PROMPT`):**

- `MEMORY_EXTRACTOR_SYSTEM_PROMPT` — typed memory extraction. Example shape:
  ```
  Output ONLY JSON: {"memories":[{"memory_type":"preference","content":"Prefers blunt, direct coaching","tags":["style"],"confidence":"medium","expires_days":null}]}
  Rules: only what the user explicitly stated or clearly implied about lasting preferences/constraints/goals.
  Use context_event (with expires_days) for temporary situations ("busy season travel until March").
  Never invent. Return {"memories":[]} if nothing lasting.
  ```
- `RECOMMENDATION_EXTRACTOR_SYSTEM_PROMPT` — pull structured advice from a message the bot just sent. Example:
  ```
  Input: {"message": "...", "available_metrics":["recovery_score","hrv_ms",...]}
  Output ONLY JSON: {"recommendations":[{"recommendation":"Keep day strain under 10","category":"training","rationale":"HRV 16% below baseline","expected_outcome":"recovery stabilizes tomorrow","checkpoint_metric":"recovery_score","checkpoint_direction":"up"}]}
  Only extract concrete, checkable advice. Skip pleasantries. Return [] if none.
  ```

**Extended narration prompts:**
- `SYSTEM_PROMPT` (daily) — add documentation for new payload keys: `checkpoint_results` ("yesterday's advice and whether it worked — mention the most relevant one, plainly: 'told you to stay under 10, you did, recovery climbed to 61'"), `active_rules` ("personal rules that apply today"), `relevant_memories` (preferences/constraints/goals — **obey style preferences**, e.g. blunt vs gentle, short vs detailed), `disliked_advice` ("never give this kind of advice").
- `QA_SYSTEM_PROMPT` — add `relevant_memories`, `relevant_rules`, `related_past_recommendations` ("if you've advised on this before and it worked/didn't, reference it"). Reinforce: obey style/constraint memories.
- `WEEKLY_SYSTEM_PROMPT` — add `recommendations_this_week` (kept/worked counts), `rules_updated`, `memory_candidates` (what was learned).
- New `MEMORY_REVIEW_SYSTEM_PROMPT` — summarize candidate memories into a friendly "here's what I think I learned — keep / edit / delete?" message.
- (Anti-generic critic) `ADVICE_CRITIC_SYSTEM_PROMPT` — returns `{"specific":true/false,"cites_data":true/false,"has_action":true/false,"reason":"..."}`.

**Few-shots:** add 1–2 daily few-shots showing a checkpoint callback ("yesterday I told you… you did… recovery improved") and a rule-driven recommendation, mirroring the existing `FEW_SHOTS` style.

---

## 7. Morning Message Changes (`/today` + `daily_message.py`)

Exact changes to `_do_send_for_user` (job) and `handlers.today`:

1. **After pull, before payload:** call `RecommendationCheckpointService.evaluate_due(session, user_id, today)` → returns yesterday's evaluated recs.
2. **Before payload:** call `MemoryRetriever.for_daily(...)` → active commitments, current goal(s), applicable rules, pending/just-evaluated recs, recent context events, **style preferences**, disliked-advice list.
3. **`build_daily_payload` gains keys:** `checkpoint_results`, `active_rules`, `relevant_memories`, `disliked_advice`. (Existing `commitments` becomes sourced from memories.)
4. **After `generate_daily_message`:** run `AntiGenericAdviceEvaluator`; regenerate once if needed.
5. **After persisting the message:** background `RecommendationLedgerService.extract_from_message(...)` writes today's new recs with their checkpoint dates.

The message should now be able to answer: *what's my body saying* (existing), *what to do / avoid* (rules + recs), *what am I testing* (active experiments/hypotheses — one line), *did yesterday's advice work* (checkpoint_results). **Anti-overload guard:** the prompt already enforces brevity on normal days; cap injected extras (≤1 checkpoint callback, ≤1 rule, ≤2 commitments) so the message doesn't balloon. The backend decides what's worth surfacing; the AI mentions at most the top one or two.

---

## 8. Q&A Changes (`handlers.plain_text` + `build_qa_context`)

1. Keep the existing `classify_and_extract` log-vs-question split.
2. For questions: `MemoryRetriever.for_qa(question, ...)` → keyword-ranked memories + applicable rules + related past recommendations (same category/keywords).
3. `build_qa_payload` gains `relevant_memories`, `relevant_rules`, `related_past_recommendations`. (Keep `about_you` during transition, then retire — see §15.)
4. After answering, if the answer contained concrete advice, `RecommendationLedgerService.extract_from_message(..., source_type='qa')` so Q&A advice also gets checkpointed.
5. Replace the background `_update_coach_notes` with `MemoryExtractor` writing typed rows via `MemoryStore` (same fire-and-forget, crash-proof slot).
6. Run the anti-generic gate on the Q&A answer too.

---

## 9. Weekly Summary Changes (`weekly_message.py` + `build_weekly_payload`)

Add to the weekly payload:
- `recommendations_this_week`: counts of given / followed / improved (deterministic from the ledger).
- `rules_updated`: rules that changed status this week (emerging→confirmed, etc.).
- `patterns_changed`: observations whose status strengthened/weakened.
- `focus_next_week`: the top current lever (biggest-confidence rule or worst-trending metric).
- `memory_candidates`: new memories learned this week → drives the **weekly memory review** message (a second message, or appended section, asking keep/edit/delete and creating a `memory_reviews` row).

Run `PersonalRuleEngine.recompute(...)` as part of the weekly job (it's the natural cadence).

---

## 10. `/manual` Changes (`handlers.manual`)

Current sections: 30-day baselines, What Helps / What Hurts (from observations), Experiments. Add:
- **Confirmed rules** (personal_rules `status='confirmed'`) — "Your operating rules."
- **Emerging hypotheses** (rules `watching/emerging` + memory `hypothesis`).
- **Constraints & preferences** (memories: `constraint`, `preference`, `disliked_advice`) — "What I know about how you like to be coached."
- **Active commitments** (memory `commitment`, not expired).
Keep calling `recalculate_observations` on open; also call `PersonalRuleEngine.recompute` (cheap) so the manual is always fresh.

---

## 11. Telegram Commands (`bot.py` + `handlers.py` + `main.py` set_my_commands)

New `/memory` command with subcommands parsed from `context.args` (same pattern as `/experiment`):

| Command | Behavior |
|---|---|
| `/memory` | Active memories grouped by type (compact) |
| `/memory recent` | Memories learned in last 7 days |
| `/memory rules` | Personal rules by status (alias `/rules`) |
| `/memory commitments` | Active (non-expired) commitments |
| `/memory delete <id>` | Archive a memory (`status='archived'`) |
| `/memory edit <id> <text>` | Edit content (or prompt for new text via `context.user_data`) |
| `/memory confirm <id>` | Confirm → confidence `high`, source `user_stated` |
| `/memory ignore <id>` | Mark `disliked`/archived so it stops surfacing |
| `/memory review` | Bot summarizes "what I think I know" (AI via `MEMORY_REVIEW_SYSTEM_PROMPT`) |

Optional later: `/why` (explain the last recommendation from the ledger), `/plan` (force a structured Q&A planning answer), `/train` & `/sleep` (focused views). Inline buttons for review use a `mem_` `CallbackQueryHandler` (mirror `ci_`/`goal:` patterns). Memory IDs shown to the user are the row PKs.

Update `set_my_commands` in `app/main.py` and `HELP_TEXT` in `handlers.py`.

---

## 12. Data Lifecycle

- **context_event / commitment memories** carry `expires_at`; a daily sweep (cheap, in the daily job) flips expired ones to `archived`. Commitments also close when evaluated as kept/broken.
- **Commitment closure:** at weekly review, compare commitment to the relevant metric/behavior over its window → mark kept/broken in `structured`, archive.
- **Wrong memories:** `/memory edit`/`delete`/`ignore`, or auto-supersede when the extractor sees a contradicting user statement (new row, old row → `superseded_by`).
- **Hypothesis → rule:** a `hypothesis` memory or `watching` observation graduates to a `personal_rules` row once evidence thresholds (§13) are met; status climbs `watching→emerging→confirmed`.
- **Rule retirement:** if `contradicting_count` rises or `last_validated` goes stale (e.g. >60 days with no support), status → `retired` (kept for history, not injected).
- **Dedup/merge:** `MemoryStore.merge_duplicates` runs in the weekly job; merges same-type near-duplicate content, summing `evidence_count`.

---

## 13. Confidence & Evidence Model

**Memories:**
- `ai_extracted` → `low`. `user_stated` → `medium`. `user_confirmed` (via `/memory confirm`) → `high`.
- `evidence_count` increments each time the same fact is restated/re-observed; reaching a small threshold (e.g. 3) bumps `low→medium`.

**Rules / observations (reuse the existing ladder in `observation_engine._compute_status`):**
- `< 4` occurrences → `watching`.
- `≥ 4` and support rate `≥ 0.5` → `emerging` (≈ today's `promising`).
- `≥ 10` and support rate `≥ 0.6` → `confirmed` (≈ today's `stronger_signal`).
- support rate `< 0.3` at `≥ 10` → `retired`/`weak`.
- **Compound rules** need a higher floor (e.g. ≥ 6 supporting days) because the trigger is rarer and overfitting is easier.

**Anti-overclaim:** only `confirmed` rules get assertive language; `watching/emerging` are always hedged ("early signal", "might"), matching the existing prompt rules. The backend passes the status; the AI mirrors the certainty. Never present a rule the engine hasn't promoted.

---

## 14. Rollout Plan (phased)

Each phase ends shippable (commit + Railway deploy), preserving all current features.

**Phase 1 — Schema + structured memory store + coach_notes migration**
- New files: `app/models/user_memory.py`, migration `0011_user_memories.py`, `app/services/memory_store.py`, `app/services/migrate_coach_notes.py`.
- Tests: memory CRUD, dedup, coach_notes→memories conversion.
- Acceptance: `user_memories` table live; existing `coach_notes` backfilled; nothing user-facing changed yet.

**Phase 2 — Memory extraction + retrieval, wired into Q&A & daily (read path)**
- New: `memory_extractor.py`, `memory_retriever.py`. Touch: `handlers.plain_text` (swap `_update_coach_notes`), `coach_payload_builder` (add `relevant_memories`), `ai_client` prompts (`MEMORY_EXTRACTOR_SYSTEM_PROMPT`, prompt doc updates).
- Tests: extraction typing, noisy-message-ignored, retrieval relevance (includes relevant / excludes irrelevant).
- Acceptance: typed memories created from chat; daily/Q&A payloads carry only relevant memories; `about_you` still present as fallback.

**Phase 3 — Recommendation ledger (write + extract)**
- New: `app/models/recommendation.py`, migration `0012`, `recommendation_ledger.py`, `RECOMMENDATION_EXTRACTOR_SYSTEM_PROMPT`.
- Touch: `daily_message.py`, `handlers.today`, `handlers.plain_text` (background extract after send).
- Tests: extraction from a sample message; checkpoint_date/baseline set deterministically.
- Acceptance: every daily/Q&A message with concrete advice produces a ledger row with a pending checkpoint.

**Phase 4 — Checkpoint / prediction evaluation + morning callback**
- New: `checkpoint_service.py`. Touch: `daily_message._do_send_for_user` (evaluate due before payload), `coach_payload_builder` (`checkpoint_results`), `SYSTEM_PROMPT` + a few-shot.
- Tests: improved/worsened/inconclusive/not_checkable classification; "advice worked" appears in payload.
- Acceptance: morning message can reference whether yesterday's advice worked.

**Phase 5 — Personal rules**
- New: `app/models/personal_rule.py`, migration `0013`, `personal_rule_engine.py`.
- Touch: `weekly_message.py`, `handlers.manual`, payloads (`active_rules`/`relevant_rules`).
- Tests: rule created only after evidence threshold; compound detector counts correctly; retirement.
- Acceptance: confirmed rules appear in `/manual` and drive recommendations.

**Phase 6 — Telegram memory commands + weekly review**
- New: migration `0014_memory_reviews.py`, `app/models/memory_review.py`, `memory_review_service.py`, handlers + `mem_` callbacks.
- Touch: `bot.py`, `main.py` (commands/help), `weekly_message.py` (review message).
- Tests: `/memory delete` archives; `/memory confirm` raises confidence; review disposition recorded.
- Acceptance: user can inspect/correct memory; weekly "keep/edit/delete" works.

**Phase 7 — Anti-generic quality gate**
- New: `advice_quality.py`, `ADVICE_CRITIC_SYSTEM_PROMPT`. Touch: daily/Q&A generation paths.
- Tests: vague response flagged; specific response passes; regenerate-once path.
- Acceptance: generic messages get caught + regenerated; failures logged.

**Phase 8 — Integration polish, retire `coach_notes`, docs/tests cleanup**
- Remove `about_you` once memories fully cover it; update `CLAUDE.md`/`SPEC.md`; broaden tests.

---

## 15. Migration Strategy (coach_notes → user_memories)

One-shot `migrate_coach_notes.py`, run once (or guarded so it's idempotent — skip users that already have memories):
- `supplements[]` → `stable_fact` (tags `["supplement"]`, confidence `medium`).
- `medications[]` → `stable_fact` (tags `["medication"]`).
- `health_context[]` → `stable_fact` (tags `["health"]`).
- `goals[]` → `goal`.
- `lifestyle[]` → `stable_fact`/`training_preference`/`schedule_pattern` (keyword routed; default `stable_fact`).
- `other[]` → `context_event` if it reads temporary, else `stable_fact`.
Keep `coach_notes` column intact and keep emitting `about_you` until Phase 8 so there's a rollback path; only drop after memories are proven in production.

---

## 16. Testing Strategy

Follow existing pytest style (`tests/test_*.py`, synthetic fixtures). **Decision (locked): SQLite + JSON shim.** New models use a JSON-variant column type that compiles to `JSONB` on Postgres (prod) and `JSON` on SQLite (tests), so the suite runs with zero infra — matching the no-Docker dev machine. Accept the small risk of prod/test JSON-semantics drift (e.g. ordering, containment operators); any query relying on Postgres-specific JSONB operators gets its own Postgres-gated test or is kept in Python. The shared shim type lives in `app/db.py` (e.g. a `JSONColumn = JSONB().with_variant(JSON(), "sqlite")` helper) and is reused by all new models.

Unit/integration tests to add:
- Memory extraction: lasting fact extracted; noisy "thanks" → no memory.
- Commitment extracted with correct `expires_at`; expires/archives on sweep.
- Recommendation extracted from a sample daily message; checkpoint_date = next day; baseline captured.
- Checkpoint eval next morning: recovery up → `improved`; missing data → `not_checkable`.
- Personal rule: created only at threshold; not created below; compound detector counts supporting/contradicting; retirement on stale.
- Retrieval: relevant memory included, irrelevant excluded, count caps respected.
- `/memory delete` archives; `/memory confirm` → confidence `high`.
- Anti-generic: vague text flagged; specific text passes.
- **Keep the existing required test** (`metrics_normalizer` cycle→day) green.

---

## 17. Risks & Edge Cases

- **Storing wrong memories** → confidence tiers + `/memory` correction + supersede chain; never let `low`-confidence drive assertive advice.
- **Duplicates** → weekly `merge_duplicates`; extractor sees existing memories to avoid restating.
- **Stale memories** → `expires_at` sweep; `last_validated` retirement for rules.
- **Overfitting on little data** → evidence thresholds (higher for compound rules); reuse maturity gates (`MIN_DAYS_FOR_FLAGS`, `MIN_DAYS_FOR_LOOPS`).
- **AI inventing rules** → rules are **backend-promoted only**; AI never creates a rule, only phrases it.
- **Long prompt payloads** → retriever caps + token budget; that's the whole point of the relevance layer.
- **Morning message overload** → surface ≤1–2 extras; brevity rule already in `SYSTEM_PROMPT`.
- **Recommendation fatigue** → at most one checkpoint callback/day; don't re-advise the same thing while a checkpoint is pending.
- **Missing WHOOP/Withings data** → checkpoint `not_checkable`; never penalize the user.
- **User ignores recommendations** → `followed='unknown'`; outcome still measured on the metric, just less attributable.
- **Contradictory user info** → newest user_stated supersedes; surface at weekly review if confidence conflict.
- **Checkpoint attribution is correlational, not causal** → keep hedged language ("that was probably the right call"), never "caused."

---

## 18. Recommended First Implementation (what I'd build first, and why)

> **Locked decisions (from review):** (1) Tests use **SQLite + a JSON shim** column type, no Postgres needed. (2) The `coach_notes → user_memories` migration is a **manual one-shot** you trigger after deploy (not auto-run on startup), so you can watch it run against real data and verify before relying on it.

**Build Phase 1 first** — the structured `user_memories` table, `MemoryStore`, and the `coach_notes` migration — because:
1. It's the foundation every other phase reads from (recs link to memories, rules reference them, retrieval queries them).
2. It's **zero user-facing risk**: the read path keeps using `about_you` until Phase 2, so nothing changes for you until the data model is proven.
3. It immediately de-risks the biggest unknown (Postgres JSONB testing, dedup behavior, migration of your real `coach_notes`) before any AI or scheduling complexity is layered on.

Then Phase 3 (recommendation ledger) + Phase 4 (checkpoints) are the **highest-value pair** — that's the "did the advice work?" loop that makes this feel like a real coach, and it builds cleanly on the message-persistence (`coach_messages`) you already have.

---

## Phase 1 — Task list (for approval)

1. **Model + migration:** `app/models/user_memory.py` + `migrations/versions/0011_user_memories.py` (table per §4.1, FK CASCADE, indexes). Register in `app/models/__init__.py`.
2. **`app/services/memory_store.py`:** `add_memory`, `get_active`, `archive`, `confirm`, `supersede`, `merge_duplicates` (deterministic dedup first; AI-assisted merge deferred).
3. **`app/services/migrate_coach_notes.py`:** idempotent one-shot converting each `coach_notes` key → typed memory rows (§15); safe to run on real data; leaves `coach_notes` intact. **Triggered manually** (e.g. a guarded `/debug/migrate_memories` route or a small CLI entry) — not run automatically on deploy.
4. **JSON shim in `app/db.py`:** add the shared `JSONB`-with-SQLite-`JSON`-variant column helper so the new model (and all later memory tables) use it; tests run on SQLite.
5. **Tests:** `tests/test_memory_store.py` — add/get/archive/confirm/dedup + coach_notes conversion mapping, running on SQLite. Confirm the existing suite stays green.
6. **No behavior change yet:** do **not** touch payloads, prompts, or handlers in Phase 1. `about_you` keeps working unchanged.

**Acceptance:** migration applies on Railway; `user_memories` populated from existing `coach_notes`; new tests pass; `/today`, `/manual`, Q&A behave exactly as before.
