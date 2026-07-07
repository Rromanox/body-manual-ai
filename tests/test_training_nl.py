"""Unit 8: natural-language routing — detection, command parity, bare-done window."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.models.training_log import TrainingLog
from app.services import training_format as fmt
from app.services import training_nl as nl
from app.services import training_plan as tp
from app.services import training_rules as rules
from scripts.seed_training_plan import seed
from tests.conftest import make_user

WED = date(2026, 7, 15)  # a Wednesday in week 1


def test_detect_skip_move_soften_complete():
    assert nl.detect_training_message("I skipped yesterday", WED) == {"action": "skip", "date": date(2026, 7, 14)}
    assert nl.detect_training_message("move Saturday's ride to Sunday", WED) == {
        "action": "move", "from": date(2026, 7, 18), "to": date(2026, 7, 19),
    }
    assert nl.detect_training_message("make tomorrow easier", WED) == {"action": "soften", "date": date(2026, 7, 16)}
    assert nl.detect_training_message("did my ride today", WED) == {"action": "complete", "date": WED}


def test_detect_constraints():
    assert nl.detect_training_message("only have 30 minutes", WED) == {
        "action": "cant", "constraint": "less_time", "minutes": 30,
    }
    assert nl.detect_training_message("only have 40 minutes", WED)["minutes"] == 45  # snapped
    assert nl.detect_training_message("no bike today", WED)["constraint"] == "no_bike"
    assert nl.detect_training_message("stuck at the front desk", WED)["constraint"] == "cant_leave"
    assert nl.detect_training_message("feeling beat", WED)["constraint"] == "feeling_beat"
    assert nl.detect_training_message("I can't today", WED)["constraint"] == "prompt"


def test_questions_and_chatter_ignored():
    assert nl.detect_training_message("what's my recovery today?", WED) is None
    assert nl.detect_training_message("I love riding my bike", WED) is None
    assert nl.detect_training_message("", WED) is None


def test_nl_skip_parity_with_command(mem_session):
    make_user(mem_session, 1)
    make_user(mem_session, 2)
    seed(mem_session, 1)
    seed(mem_session, 2)
    yesterday = date(2026, 7, 14)

    # NL path: detector resolves the date, then calls the shared op.
    intent = nl.detect_training_message("I skipped yesterday", WED)
    assert intent["date"] == fmt.parse_plan_date("yesterday", WED)  # same date the command parses
    rules.skip_session(mem_session, 1, intent["date"], source="natural_language")

    # Command path: same shared op with the command-parsed date.
    rules.skip_session(mem_session, 2, fmt.parse_plan_date("yesterday", WED), source="command")

    s1 = tp.get_session(mem_session, 1, yesterday)
    s2 = tp.get_session(mem_session, 2, yesterday)
    assert s1.status == s2.status == "skipped"
    r1 = mem_session.query(TrainingLog).filter_by(user_id=1, action="skipped").one()
    r2 = mem_session.query(TrainingLog).filter_by(user_id=2, action="skipped").one()
    assert r1.detail["rule"] == r2.detail["rule"] == "normal_no_reschedule"


def _now(hour=10):
    return datetime(2026, 7, 15, hour, 0, tzinfo=timezone.utc)


def test_bare_done_within_window_completes(mem_session):
    make_user(mem_session)
    tp.upsert_session(mem_session, 1, WED, week=1, phase="base",
                      session_type="intervals", title="3x10 SS", duration_min=60)
    tp.mark_presented(mem_session, 1, WED, now=_now(9))  # session shown at 9:00

    row = nl.awaiting_done_confirmation(mem_session, 1, _now(10))  # replying at 10:00
    assert row is not None
    assert nl.is_bare_done("done, felt great") is True
    tp.mark_completed(mem_session, 1, WED, source="natural_language")
    assert tp.get_session(mem_session, 1, WED).status == "completed"


def test_bare_done_without_reminder_does_not_complete(mem_session):
    make_user(mem_session)
    tp.upsert_session(mem_session, 1, WED, week=1, phase="base",
                      session_type="intervals", title="3x10 SS", duration_min=60)
    # No mark_presented -> not awaiting, so a random "done" must not complete it.
    assert nl.awaiting_done_confirmation(mem_session, 1, _now(10)) is None
    assert tp.get_session(mem_session, 1, WED).status == "pending"


def test_bare_done_outside_window_expires(mem_session):
    make_user(mem_session)
    tp.upsert_session(mem_session, 1, WED, week=1, phase="base",
                      session_type="intervals", title="3x10 SS", duration_min=60)
    tp.mark_presented(mem_session, 1, WED, now=_now(2))  # shown at 2:00
    assert nl.awaiting_done_confirmation(mem_session, 1, _now(10)) is None  # 8h later, window is 6h


def test_bare_done_not_rematched_after_completion(mem_session):
    make_user(mem_session)
    tp.upsert_session(mem_session, 1, WED, week=1, phase="base",
                      session_type="intervals", title="3x10 SS", duration_min=60)
    tp.mark_presented(mem_session, 1, WED, now=_now(9))
    tp.mark_completed(mem_session, 1, WED)
    # Already completed -> no longer awaiting a bare "done".
    assert nl.awaiting_done_confirmation(mem_session, 1, _now(10)) is None
