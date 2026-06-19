"""Tests for the retatrutide (/reta) recurring reminder."""
from __future__ import annotations

import inspect
import pathlib
from datetime import date, timedelta

from app.services import health_reminder as hr
from tests.conftest import make_user

TODAY = date(2026, 6, 19)


# --- data ops ---------------------------------------------------------------

def test_log_creates_reminder_with_default_interval(mem_session):
    make_user(mem_session)
    r = hr.log_completion(mem_session, 1, TODAY)
    assert r.interval_days == 6
    assert r.last_completed_date == TODAY
    assert r.next_due_date == TODAY + timedelta(days=6)
    assert r.is_active is True


def test_next_due_is_logged_date_plus_interval(mem_session):
    make_user(mem_session)
    hr.set_interval(mem_session, 1, 6)
    r = hr.log_completion(mem_session, 1, TODAY)
    assert r.next_due_date == TODAY + timedelta(days=6)


def test_late_log_recalculates_from_actual_date(mem_session):
    make_user(mem_session)
    # due was Jun 25; actually logged Jun 26 -> next due Jul 2 (interval 6)
    hr.set_interval(mem_session, 1, 6)
    hr.log_completion(mem_session, 1, date(2026, 6, 19))  # next due Jun 25
    r = hr.log_completion(mem_session, 1, date(2026, 6, 26))  # logged late
    assert r.next_due_date == date(2026, 7, 2)


def test_set_interval_without_log_leaves_next_due_none(mem_session):
    make_user(mem_session)
    r = hr.set_interval(mem_session, 1, 6)
    assert r.interval_days == 6 and r.next_due_date is None


def test_set_interval_with_existing_log_recomputes_due(mem_session):
    make_user(mem_session)
    hr.log_completion(mem_session, 1, TODAY)  # interval 6 -> due +6
    r = hr.set_interval(mem_session, 1, 7)
    assert r.interval_days == 7
    assert r.next_due_date == TODAY + timedelta(days=7)


def test_change_interval_six_to_seven(mem_session):
    make_user(mem_session)
    hr.set_interval(mem_session, 1, 6)
    r = hr.set_interval(mem_session, 1, 7)
    assert r.interval_days == 7


def test_set_interval_rejects_bad_value(mem_session):
    make_user(mem_session)
    for bad in (0, -3, 999):
        try:
            hr.set_interval(mem_session, 1, bad)
            assert False, f"expected ValueError for {bad}"
        except ValueError:
            pass


def test_stop_disables(mem_session):
    make_user(mem_session)
    hr.log_completion(mem_session, 1, TODAY)
    r = hr.stop(mem_session, 1)
    assert r.is_active is False
    assert hr.stop(mem_session, 2) is None  # no reminder for user 2


# --- due_reminders ----------------------------------------------------------

def test_due_when_past_due_and_not_reminded_or_completed_today(mem_session):
    make_user(mem_session)
    hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))  # due today
    due = hr.due_reminders(mem_session, 1, TODAY)
    assert len(due) == 1


def test_not_due_before_due_date(mem_session):
    make_user(mem_session)
    hr.log_completion(mem_session, 1, TODAY)  # due TODAY+6
    assert hr.due_reminders(mem_session, 1, TODAY) == []


def test_not_due_if_logged_today(mem_session):
    make_user(mem_session)
    # was due today, but user logged today -> not due (handled by completed-today)
    hr.set_interval(mem_session, 1, 6)
    hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))  # next due today
    hr.log_completion(mem_session, 1, TODAY)  # logs today -> next due +6, last_completed today
    assert hr.due_reminders(mem_session, 1, TODAY) == []


def test_reminder_sends_only_once_per_day(mem_session):
    make_user(mem_session)
    hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))  # due today
    due = hr.due_reminders(mem_session, 1, TODAY)
    assert len(due) == 1
    hr.mark_reminded(mem_session, due[0].id, TODAY)
    assert hr.due_reminders(mem_session, 1, TODAY) == []  # already reminded today


def test_inactive_not_due(mem_session):
    make_user(mem_session)
    hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))
    hr.stop(mem_session, 1)
    assert hr.due_reminders(mem_session, 1, TODAY) == []


# --- natural-language detection ---------------------------------------------

def test_nl_took_shot_today():
    assert hr.detect_reta_message("I took my retatrutide shot today.", TODAY) == {"action": "log", "date": TODAY}


def test_nl_took_reta_today():
    assert hr.detect_reta_message("I took reta today", TODAY)["date"] == TODAY


def test_nl_did_shot_this_morning():
    assert hr.detect_reta_message("I did my shot this morning", TODAY) == {"action": "log", "date": TODAY}


def test_nl_took_yesterday():
    assert hr.detect_reta_message("I took retatrutide yesterday", TODAY)["date"] == TODAY - timedelta(days=1)


def test_nl_forgot_yesterday_but_took_today():
    d = hr.detect_reta_message("I forgot my shot yesterday but took it today", TODAY)
    assert d == {"action": "log", "date": TODAY}


def test_nl_set_interval():
    assert hr.detect_reta_message("Remind me every 6 days for reta", TODAY) == {"action": "set_interval", "interval_days": 6}


def test_nl_question_ignored():
    assert hr.detect_reta_message("what is retatrutide?", TODAY) is None
    assert hr.detect_reta_message("should I take my shot?", TODAY) is None


def test_nl_unrelated_ignored():
    assert hr.detect_reta_message("had pizza at 9pm", TODAY) is None
    assert hr.detect_reta_message("I took a shot of espresso", TODAY) is None  # no reta / "my shot"


# --- status / formatting ----------------------------------------------------

def test_status_not_set(mem_session):
    make_user(mem_session)
    assert "not set" in hr.format_status(hr.get(mem_session, 1)).lower()


def test_status_shows_last_and_due(mem_session):
    make_user(mem_session)
    hr.log_completion(mem_session, 1, TODAY)
    text = hr.format_status(hr.get(mem_session, 1))
    assert "Retatrutide" in text and "next due" in text.lower() and "every 6 days" in text.lower()


def test_format_logged_says_today(mem_session):
    make_user(mem_session)
    r = hr.log_completion(mem_session, 1, TODAY)
    assert "today" in hr.format_logged(r, today=TODAY).lower()


# --- guard: no /shot anywhere -----------------------------------------------

def test_no_shot_command_in_help_or_sources():
    assert "/shot" not in hr.RETA_HELP
    root = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("app/services/health_reminder.py", "app/jobs/health_reminder_job.py",
                "app/telegram/handlers.py", "app/telegram/bot.py", "app/main.py"):
        assert "/shot" not in (root / rel).read_text(encoding="utf-8"), rel


def test_reta_command_registered():
    from app.telegram import handlers
    assert inspect.iscoroutinefunction(handlers.reta_command)
    assert "/reta" in handlers.HELP_TEXT
