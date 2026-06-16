# Coach Feel — Audit & Design Proposal

> What turns a data reporter into a coach is **continuity**: it remembers what you
> told it, notices things on its own, and closes the loop on what you actually did.
> This document audits where the app stands against that bar, designs the path to it,
> and sequences the work. It proposes — it does not build.

Architecture principle held throughout: **the backend computes the connections
(gaps, deltas, baselines, timing, evidence counts); the AI only narrates them.**
The one new place the AI does real work is parsing free text into a *validated
schema* — it extracts, it never reasons about physiology.

---

## PART 1 — What the app actually does right now

### The user's journey, step by step

1. **`/start`** — Creates the user, sets timezone from the default, replies with a
   plain-English consent message (what's stored, that summaries go to OpenAI,
   that `/delete` erases everything). Honest and short.
2. **`/connect_whoop`** — Replies with a WHOOP OAuth link. After the user
   authorizes, the callback stores tokens and (per plan) backfills history so
   baselines exist immediately. `/connect_withings` does the same for the scale.
3. **Data flows in** — A pull happens at connect, on `/today`, and on the
   scheduled morning job. WHOOP cycles/sleep/recovery/workouts are mapped to the
   user's **local date of waking** and upserted into one wide `daily_metrics` row
   per day. Withings body-comp lands in the same row.
4. **The morning message** — Once a day, in the user's local send window, the bot
   pulls fresh data (retrying if recovery hasn't scored yet), builds a payload,
   and sends a short coach message, immediately followed by a check-in prompt
   ("How was yesterday? Tap any that apply").
5. **The check-in** — Inline buttons: alcohol, late meal, high stress, sick,
   travel, hard day, late caffeine, dehydrated, big meal → "None" / "Save". Tags
   are stored against **yesterday's** date. The reply is "Saved ✓."
6. **On demand** — `/today` (regenerate), `/weekly` (7-day-vs-30-day summary),
   `/focus` (one action item), `/manual` (baselines + patterns + experiments),
   `/history` (last 7 daily messages), `/experiment` (start/track a self-test),
   `/goal`, `/timezone`, `/backfill`, `/chatlog`, `/delete`.
7. **Plain text** — Any non-command message is treated as a **question** and
   answered conversationally, with the last 5 Q&A turns threaded in as memory and
   a fat context payload (recent daily data, 7/30-day averages, recent tags,
   observations).

This is well **beyond** the SPEC's Week 1–2 MVP. Withings, Q&A, weekly, manual,
observations, experiments, goals, and full chat logging all exist and work.

### What the daily message actually knows and says

The payload it narrates is genuinely rich:

- Recovery, sleep hours, resting HR, HRV — each as **today vs 7-day vs 30-day
  baseline, with a flag** computed against the user's own normal.
- Yesterday's strain band and workout count/minutes.
- Yesterday's check-in tags.
- Sleep timing, sleep stages (REM/deep/light), and full body composition +
  weight trend (overnight change, weekly trend, water-spike flag).
- Check-in streak (only surfaced at 3+).
- The `now` block (real local time, day, part of day, weekend).
- A hard-coded safety caution appended by the backend when RHR/recovery/weight
  triggers fire — never written by the AI.

The system prompt is good: warm, anti-jargon, anti-template, "lead with the data,"
no exclamation marks, hedged language. On a normal day it says one or two
sentences; on a flagged day it explains. That part is solid.

### Where it still feels like a data reporter, not a coach (blunt)

The prompt is coachy, but the **architecture has no continuity**. Specifically:

1. **The morning message has no memory.** Every morning is a cold open. It never
   references what it told you yesterday, and it never references what you replied.
   It cannot say "you felt wrecked yesterday — better today?" because that data is
   never put in front of it. This is the single biggest gap, and the SPEC's whole
   north star is continuity. (Note: Q&A *does* have short-term memory — the daily
   message simply doesn't reuse the same trick.)

2. **It reacts to data, never to *you*.** It sees yesterday's tags but it does not
   **close the loop**: it won't say "you logged a late meal, and your sleep
   efficiency came in under your normal." The machinery to find that connection
   exists (`observation_engine`) but it only surfaces weeks later in `/manual` as
   an aggregate — never in the moment, never tied to the specific night.

3. **The check-in is a dead end.** You tap "late meal, high stress," you get
   "Saved ✓," and nothing ever comes back. The tap silently feeds the observation
   counter. The user gets zero signal that logging mattered, which is exactly the
   behavior loop that makes people stop logging.

4. **You can't just tell it what happened.** Say "had pizza at 9pm" and the bot
   treats it as a *question* and answers it like one. There is no path to log an
   event in the moment. (It also requires WHOOP to be connected before it will
   respond at all.)

5. **It only speaks when spoken to.** Apart from the one scheduled morning
   message, the bot is silent. A 3rd straight low-recovery day passes without
   comment unless it crosses the hard safety threshold (very low, 7+ days).

6. **It has little point of view.** On a flagged day it gives a measured "keep it
   easy." It rarely makes a direct call — "skip the hard workout, your body isn't
   ready." A coach has opinions; this one mostly narrates.

7. **Commitments evaporate.** If you say "I'm going to protect my bedtime this
   week," nothing remembers it, so nothing follows up.

### What's stubbed, half-built, or quietly broken

- **`feel_score` is unreachable.** The callback handler processes `ci_feel:` and
  `ci_feel_skip`, but `checkin_keyboard()` renders no feel buttons — so the 1–5
  feel score the SPEC calls for can never actually be entered. Dead code on one
  side, missing UI on the other.
- **`free_text` on check-ins is never captured.** The column exists; no handler
  writes to it.
- **The observation engine can only ever find what *hurts* you.** `_is_supporting`
  counts a pattern only when the next-day metric is **worse** than baseline, and
  the tag vocabulary is all negatives (alcohol, late meal, stress…). There is no
  positive vocabulary (early dinner, meditated, good hydration) and no
  "this helped" detection. So the manual is structurally incapable of a "What
  Helps" section — only "What Hurts."
- **Weekly is on-demand only.** SPEC says "Sent Sunday evening." There's no
  scheduled weekly job yet (the hourly scheduler only runs the morning message).
- **The manual isn't quite the SPEC's document.** It renders baselines +
  observation buckets (Stronger Signals / Emerging / Watching) + experiments. The
  SPEC's "Confirmed Patterns / Hypotheses / Needs More Data" and a true narrative
  "Your Sleep / Your Recovery / What Helps / What Hurts" structure aren't there.
- **Conversational journaling, proactive check-ins, commitment tracking** — none
  exist. They're the subject of Part 2.

---

## PART 2 — Making it feel like a real coach

Everything below clusters around continuity. The flagship is the richest
expression of it; the rest are cheaper levers on the same principle.

### FLAGSHIP — In-the-moment event logging with consequence follow-up

> You tell the bot things as they happen, in plain language. The next morning,
> after your sleep data lands, the coach connects what you did to what your body
> did — and that closed loop becomes evidence in your manual.

#### (a) The data model — reconcile events with the yesterday-checkbox journal

The current `journal_entries` table is a **per-day aggregate** keyed to the date a
behavior occurred (always "yesterday" at prompt time): one row, a list of tags, an
(unused) feel score, an (unused) free-text field. Events are the opposite shape:
**timestamped, individual, happening "now."** Forcing events into the daily
aggregate would conflate two meanings, so I'd add a sibling table rather than
overload the existing one — but with a **defined contract** that keeps the working
observation engine fed (this is the "don't just bolt one on" requirement).

**New table: `events`**

| column | meaning |
|---|---|
| `id` | pk |
| `user_id` | fk |
| `occurred_at` | tz-aware timestamp of when the thing happened (resolved via `timekit`) |
| `local_date` | the **behavior date** for that user (the bridge to journal/observations) |
| `event_type` | `meal` / `alcohol` / `caffeine` / `stress` / `exercise` / `sleep_problem` / `note` |
| `raw_text` | exactly what the user typed — never discarded, even if unparsed |
| `structured` | JSONB: quantity, units, time qualifier, anything parsed |
| `confidence` | `clean` / `needs_confirmation` |
| `source` | `chat` / `checkin` |
| `created_at` | audit |

**The reconciliation contract.** Events do not replace the check-in — they become
a richer *source* for the same tag→metric engine. A small **roll-up** runs when
the day closes (or lazily, the next morning before the loop is computed): it
converts the day's events into the existing tag vocabulary the observation engine
already consumes. A `meal` event whose `occurred_at` is within ~2–3 hours of the
user's typical sleep onset becomes the `late_meal` tag; any `alcohol` event
becomes `alcohol`; etc. The checkbox check-in stays as a fallback and a
confirmation surface. Net effect: **one correlation engine, two input methods**,
and the engine code barely changes.

This also means the in-the-moment event ("pizza, 9pm") and the morning checkbox
("late meal, yesterday") are reconciled by `local_date` + the roll-up, not by two
parallel systems fighting over the truth.

#### (b) Parsing — and handling ambiguity without lying

- **Use the AI as a structured extractor in a separate, cheap call** — not the
  narration call. Input: the raw text + the `now` block. Output: a strict JSON
  list of events (`type`, `occurred_at`, `quantity`, `confidence`). The backend
  validates the schema and resolves relative times with the **already-built**
  `timekit.resolve_local_time` ("9pm" at 1am → yesterday 9pm). The AI never
  touches physiology here; it only turns words into rows.
- **Ambiguity → ask, never guess.** "had a few" has no number → store the event
  as `needs_confirmation`, preserve the raw text, and the bot asks **one**
  clarifying question ("a few what — drinks?"). A wrong confident guess ("logged 3
  beers") erodes trust far worse than a single question. When the type itself is
  unclear, keep it as a loose `note` rather than mis-filing it.
- **Statement vs. question.** Today `plain_text` assumes every message is a
  question. We need a lightweight classifier (cheap AI call, or a heuristic on
  past-tense self-report) to split "had pizza at 9pm" (log) from "is 58 recovery
  bad?" (question). When genuinely ambiguous, answer as a question **and** offer
  to log it — never silently drop a possible log.

#### (c) When the loop closes — the async part, and where it surfaces

The event is logged at night; the sleep/recovery data that gives it meaning
doesn't land until morning. So the loop is inherently asynchronous, and the right
answer is to **close it inside the morning message**, not as its own ping.

- The backend, right after the morning WHOOP pull (where the pipeline already
  lives), computes the connection: meal time vs **actual sleep onset** for that
  night, the resulting gap, sleep quality vs the user's baseline, and the running
  count ("3rd time this month"). It writes a `closed_loops` block into the daily
  payload.
- The AI narrates it as one natural line of the morning note: *"Eating about two
  hours before bed again last night, and your sleep efficiency came in 10% under
  your normal — third time this month."*
- **Why fold it into the morning message instead of a separate ping:** the morning
  message is already an expected, opted-in touch. Adding the loop there costs zero
  extra notifications. A separate "your pizza hurt your sleep" ping is exactly the
  kind of unprompted noise that gets the bot muted (see Part 3's bar).
- Each closed loop writes a per-instance evidence record that the observation
  engine aggregates — the loop-in-the-morning and the pattern-in-the-manual become
  the same data at two zoom levels.

**One implementation note worth flagging now:** computing the meal→bed gap
precisely needs the night's **sleep onset as a real timestamp**. The normalized
`sleep_start_local` column is only `"HH:MM"` (no date), but the full UTC onset
timestamp is preserved in `daily_metrics.raw_whoop_json["sleeps"][…]["start"]`.
The gap computation should read the raw timestamp (or we reconstruct the onset
datetime from `sleep_start_local` + the prior local date). Either works; it just
shouldn't be hand-waved.

### Beyond the flagship

**Memory in the daily message (the cheapest big win).** Give the morning message
what Q&A already has. Add to the daily payload: `yesterday_message` (what the
coach said), `yesterday_reply` (the tags/feel the user gave back), and any
`open_commitments`. Everything needed is already in `coach_messages` and
`journal_entries` — it's a payload-assembly change, not new infrastructure. This
alone unlocks "you were wrecked yesterday — you're back to normal today" and is
the highest coach-feel-per-hour change in the whole document.

**Commitment tracking.** When the user voices an intention ("I'll protect my
bedtime this week"), capture it (an `event_type: commitment`, or a tiny
`commitments` table). The backend computes adherence ("bedtime before 11 on 4 of
6 nights"); the AI references it. Real coach behavior, but it depends on the
event-parsing layer existing first, so it rides behind the flagship.

**Conversational journaling.** The statement-vs-question classifier and the event
extractor (both built for the flagship) directly enable "just talk to it." The
gap-filling case is mostly a **backend** decision: when recovery is flagged low
**and** there's no journal entry/event for yesterday, the morning message asks
("rough recovery — travel, drinks, or just a bad night?"). The backend decides
*when* to ask; the AI only phrases it.

**A point of view.** Largely a prompt change: on clearly-flagged days, let the
coach make the direct call ("skip the hard session today") instead of a hedged
suggestion, scaled by `goal` (a performance user wants the blunt call; a
general-health user wants gentler framing). Cheap, and it sharpens the persona
immediately.

**Reflection (weekly look-back that asks first).** A weekly variant that opens
with a question — "how did this week feel to you?" — and only delivers the data
read after the user answers. Needs the scheduled weekly job plus a two-turn
conversational flow, so it's a later item.

---

## PART 3 — Evaluate and sequence

### The map

| Idea | Coach-feel | Effort | Phase | Main risk |
|---|---|---|---|---|
| **Daily-message memory** (thread yesterday's msg + reply into payload) | **High** | **Low** | **Now** | Almost none — reuses existing data |
| **Close the check-in loop in the next morning message** (checkbox-only, no parser) | **High** | **Low–Med** | **Now** | Over-claiming from one night → keep it hedged + evidence-counted |
| Point of view on flagged days (prompt change) | Med | Low | Now | Being bossy on weak evidence; gate to strong flags |
| Free-text event logging + extractor (flagship core) | High | High | Week 2–3 | Mis-parsing erodes trust; statement/question split is fiddly |
| Consequence follow-up from free-text events (full flagship) | High | Med (on top of events) | Week 2–3 | Needs precise sleep-onset timestamp; async timing |
| Conversational gap-filling ("logged nothing, ask why") | Med–High | Med | Week 2–3 | Becomes nagging if it asks too often |
| Commitment tracking | Med–High | Med | Week 3+ | Feels like surveillance if heavy-handed |
| Proactive check-ins (unprompted pings) | High **if rare**, negative if not | Med | Week 3+ (gated) | **The big one — fatigue/mute. See bar below** |
| Scheduled weekly + reflection flow | Med | Med | Later | Two-turn flow complexity |
| Positive-pattern detection ("What Helps") | Med | Med | Later | Needs new vocabulary + helped-detection logic |

MVP discipline: **do not build this all at once.** The table is a backlog, not a
sprint.

### The bar for unprompted messages (design for restraint)

Proactivity is where this gets annoying fast — an unprompted ping carries the same
fatigue risk as the morning message, but worse, because it's unexpected. Before
the bot is *ever* allowed to message unprompted, all of these must hold:

1. **Hard cap: at most one unprompted message per day**, on top of the morning
   message. No exceptions.
2. **Fires on a genuinely notable, newly-detected state — once.** "3rd straight
   low-recovery day" fires *that day only*, not every day after. A persistent
   state must not re-alert.
3. **Quiet hours, via the `now` block.** Never at night; respect `part_of_day`.
4. **Skip if the user already interacted today** — they've already had their
   touch; don't pile on.
5. **Always actionable or a real question.** Never "just checking in." If there's
   nothing to do or ask, stay silent.
6. **One-tap mute** (`/quiet`, or "stop nudging me"). The evening bedtime nudge is
   **opt-in, default off.**

The backend owns *whether* and *when* to fire (strict, computed thresholds); the
AI only writes the words. That keeps proactivity from drifting into chattiness.

### The one or two things to do next

**1. Daily-message memory.** Thread yesterday's coach message and the user's
check-in reply into the daily payload and prompt. Lowest effort in the document,
highest continuity gain, reuses data you already store. This is the single change
that most makes the bot feel like it *remembers you*.

**2. Close the check-in loop in the next morning message — checkbox version.**
You already collect tags; the backend already knows how to compare a tag day to
the next-day metric (that's the observation engine's core). Surface the
**per-instance** connection in the next morning's message ("you logged a late meal
— sleep efficiency came in under your normal, third time this month this has lined
up"). This delivers the flagship's emotional payoff — *the coach noticed what I
did and told me what it cost me* — using **only data that already exists and no
natural-language parser**.

Together these two are cheap, low-risk, and reuse the existing pipeline — and they
turn the daily message from a cold-open dashboard into something with memory and
follow-through. The full free-text event logger (the flagship's richest form) is
the natural Week 2–3 build *after* these prove the loop is worth it, because it
carries the real parsing/trust risk and should be earned, not rushed.
