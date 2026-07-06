"""Fix #3: health reminders must not depend on an active WHOOP connection."""
from __future__ import annotations

from datetime import date, timedelta

from app.services import health_reminder as hr
from tests.conftest import make_user

TODAY = date(2026, 6, 25)


def test_user_with_reminder_but_no_whoop_is_selected(mem_session):
    # user 1 has an active reminder and NO oauth/WHOOP connection at all
    make_user(mem_session, user_id=1)
    hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))
    # user 2 has no reminder
    make_user(mem_session, user_id=2)
    ids = [u.id for u in hr.users_with_active_reminders(mem_session)]
    assert ids == [1]


def test_stopped_reminder_user_excluded(mem_session):
    make_user(mem_session)
    hr.log_completion(mem_session, 1, TODAY - timedelta(days=6))
    hr.stop(mem_session, 1)
    assert hr.users_with_active_reminders(mem_session) == []
