"""Bug #1: reta reminder fires daily; confirmation must persist and quiet it.

Policy (c): once/day until confirmed. These tests pin the due-date behavior and
the new confirmation paths (Taken button -> log_completion; a bare "yes" shortly
after a reminder logs it, but a random "yes" does not).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.services import health_reminder as hr
from tests.conftest import make_user

# 2026-06-25 10:00 UTC; a shot logged on 06-19 with the default 6-day interval
# is due exactly on 06-25.
NOW = datetime(2026, 6, 25, 10, 0, tzinfo=timezone.utc)
TODAY = NOW.date()


# --- reproduction: due-date behavior (policy c) -----------------------------

def test_not_due_two_days_after_completion(mem_session):
    make_user(mem_session)
    hr.log_completion(mem_session, 1, TODAY - timedelta(days=2))  # next due = today+4
    assert hr.due_reminders(mem_session, 1, TODAY) == []


def test_due_on_the_sixth_day(mem_session):
    make_user(mem_session)
    hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))  # next due = today
    assert len(hr.due_reminders(mem_session, 1, TODAY)) == 1


def test_quiet_after_confirmation(mem_session):
    make_user(mem_session)
    hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))
    assert len(hr.due_reminders(mem_session, 1, TODAY)) == 1
    hr.log_completion(mem_session, 1, TODAY)  # confirm today -> next due today+6
    assert hr.due_reminders(mem_session, 1, TODAY) == []


def test_refires_next_day_until_confirmed(mem_session):
    """Policy (c): once per day. Not twice the same day, but again the next day."""
    make_user(mem_session)
    r = hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))
    assert len(hr.due_reminders(mem_session, 1, TODAY)) == 1
    hr.mark_reminded(mem_session, r.id, TODAY, now=NOW)
    assert hr.due_reminders(mem_session, 1, TODAY) == []                     # same day: quiet
    assert len(hr.due_reminders(mem_session, 1, TODAY + timedelta(days=1))) == 1  # next day: again


# --- awaiting_confirmation (the "within a few hours" gate) -------------------

def test_awaiting_confirmation_recent_reminder(mem_session):
    make_user(mem_session)
    r = hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))
    hr.mark_reminded(mem_session, r.id, TODAY, now=NOW)
    assert hr.awaiting_confirmation(mem_session, 1, NOW) is not None
    assert hr.awaiting_confirmation(mem_session, 1, NOW + timedelta(hours=2)) is not None


def test_awaiting_confirmation_expires(mem_session):
    make_user(mem_session)
    r = hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))
    hr.mark_reminded(mem_session, r.id, TODAY, now=NOW)
    assert hr.awaiting_confirmation(mem_session, 1, NOW + timedelta(hours=7)) is None


def test_awaiting_confirmation_none_without_reminder(mem_session):
    make_user(mem_session)
    hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))  # never reminded
    assert hr.awaiting_confirmation(mem_session, 1, NOW) is None


def test_awaiting_confirmation_none_after_completed_today(mem_session):
    make_user(mem_session)
    r = hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))
    hr.mark_reminded(mem_session, r.id, TODAY, now=NOW)
    hr.log_completion(mem_session, 1, TODAY)  # took it -> no longer awaiting
    assert hr.awaiting_confirmation(mem_session, 1, NOW) is None


# --- bare-confirmation phrasing ---------------------------------------------

def test_bare_confirmations_recognized():
    for msg in ["yes", "Yep", "yeah", "done", "Taken", "took it", "did it", "👍", "confirmed"]:
        assert hr.is_bare_confirmation(msg) is True, msg


def test_non_confirmations_rejected():
    for msg in ["no", "yesterday I felt great", "yes but why is my recovery low today",
                "not yet", "what is retatrutide?"]:
        assert hr.is_bare_confirmation(msg) is False, msg


# --- explicit NL still works + "taken" now recognized -----------------------

def test_explicit_reta_still_logs():
    assert hr.detect_reta_message("I took my retatrutide shot today", TODAY) == {"action": "log", "date": TODAY}
    assert hr.detect_reta_message("I've taken my retatrutide", TODAY)["action"] == "log"  # 'taken' now works


# --- combined: yes-after-reminder logs; random yes does not -----------------

def test_yes_after_reminder_confirms_but_random_yes_does_not(mem_session):
    make_user(mem_session, user_id=1)
    make_user(mem_session, user_id=2)
    # user 1 was just reminded; user 2 was not
    r = hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))
    hr.mark_reminded(mem_session, r.id, TODAY, now=NOW)
    # user 1: "yes" is a valid confirmation (recent reminder + bare confirm)
    assert hr.is_bare_confirmation("yes") and hr.awaiting_confirmation(mem_session, 1, NOW) is not None
    # user 2: same "yes" has nothing to confirm -> no completion
    assert hr.awaiting_confirmation(mem_session, 2, NOW) is None
