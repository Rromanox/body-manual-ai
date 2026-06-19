"""Tests for wake-aware morning message timing."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

from app.config import parse_hhmm_to_minutes
from app.jobs import daily_message as dm

# 05:00 / 10:30 in minutes-since-midnight, grace 180 -> ceiling 13:30 (810).
START = 5 * 60          # 300
CUTOFF = 10 * 60 + 30   # 630
CEILING = CUTOFF + dm._FALLBACK_GRACE_MINUTES  # 810


def _decide(now, ready, already_sent=False):
    return dm.decide_morning_action(
        now, START, CUTOFF, ready=ready, already_sent=already_sent
    )


# --- decide_morning_action --------------------------------------------------

def test_before_window_skips():
    assert _decide(now=290, ready=True) == "skip"   # 04:50, before 05:00


def test_in_window_not_ready_waits():
    assert _decide(now=400, ready=False) == "wait"   # 06:40, sleep still pending


def test_in_window_ready_sends_full():
    assert _decide(now=400, ready=True) == "send_full"


def test_past_cutoff_not_ready_sends_degraded():
    assert _decide(now=650, ready=False) == "send_degraded"  # 10:50, fallback


def test_ready_after_cutoff_still_sends_full():
    # Slept until 11 — main sleep finalizes late but should still send the real message.
    assert _decide(now=700, ready=True) == "send_full"


def test_past_grace_ceiling_gives_up():
    assert _decide(now=CEILING + 1, ready=False) == "skip"
    assert _decide(now=CEILING + 1, ready=True) == "skip"  # too late even if ready


def test_already_sent_skips_regardless():
    assert _decide(now=400, ready=True, already_sent=True) == "skip"
    assert _decide(now=650, ready=False, already_sent=True) == "skip"


# --- _sleep_usable (readiness) ----------------------------------------------

def test_sleep_usable_none_row():
    # No row for today (still sleeping, or only naps which never create a row).
    assert dm._sleep_usable(None) is False


def test_sleep_usable_recovery_present():
    row = SimpleNamespace(recovery_score=62, sleep_hours=None)
    assert dm._sleep_usable(row) is True


def test_sleep_usable_sleep_present():
    row = SimpleNamespace(recovery_score=None, sleep_hours=7.4)
    assert dm._sleep_usable(row) is True


def test_sleep_usable_neither_present():
    row = SimpleNamespace(recovery_score=None, sleep_hours=None)
    assert dm._sleep_usable(row) is False


# --- morning_cron_minute_spec -----------------------------------------------

def test_cron_spec_30():
    assert dm.morning_cron_minute_spec(30) == "0,30"


def test_cron_spec_15():
    assert dm.morning_cron_minute_spec(15) == "0,15,30,45"


def test_cron_spec_60():
    assert dm.morning_cron_minute_spec(60) == "0"


def test_cron_spec_clamps_out_of_range():
    assert dm.morning_cron_minute_spec(0) == dm.morning_cron_minute_spec(1)  # clamps low
    assert dm.morning_cron_minute_spec(999) == "0"                          # clamps to 60


# --- parse_hhmm_to_minutes --------------------------------------------------

def test_parse_valid():
    assert parse_hhmm_to_minutes("05:00", "10:30") == 300
    assert parse_hhmm_to_minutes("09:45", "10:30") == 585


def test_parse_falls_back_to_default():
    assert parse_hhmm_to_minutes(None, "10:30") == 630
    assert parse_hhmm_to_minutes("", "10:30") == 630
    assert parse_hhmm_to_minutes("garbage", "10:30") == 630
    assert parse_hhmm_to_minutes("25:00", "10:30") == 630  # invalid hour


# --- /today manual command + helpers intact ---------------------------------

def test_today_manual_command_still_async():
    from app.telegram import handlers
    assert inspect.iscoroutinefunction(handlers.today)


def test_watcher_helpers_exist():
    assert callable(dm.decide_morning_action)
    assert callable(dm.has_sent_morning_message)
    assert callable(dm._sleep_usable)
    assert inspect.iscoroutinefunction(dm.run_daily_message)
