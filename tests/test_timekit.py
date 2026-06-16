"""Unit tests for timekit — the single source of local-time truth.

The thing most likely to break here is DST: a fixed UTC offset would silently
be wrong half the year. These tests pin the winter/summer offsets and the
relative-time anchoring rule ("9pm at 1am means yesterday").
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services.timekit import (
    DEFAULT_TZ,
    get_user_now,
    now_block,
    part_of_day,
    resolve_local_time,
)

DETROIT = ZoneInfo("America/Detroit")


def _user(tz: str = "America/Detroit"):
    # timekit only touches .timezone, so a stub stands in for the ORM model.
    return SimpleNamespace(timezone=tz)


# --- DST correctness --------------------------------------------------------

def test_dst_offsets_differ_winter_vs_summer():
    winter = datetime(2026, 1, 15, 8, 0, tzinfo=DETROIT)
    summer = datetime(2026, 7, 15, 8, 0, tzinfo=DETROIT)
    assert winter.utcoffset() == timedelta(hours=-5)  # EST
    assert summer.utcoffset() == timedelta(hours=-4)  # EDT


def test_now_block_reflects_dst_offset_in_iso():
    summer = datetime(2026, 6, 15, 14, 30, tzinfo=DETROIT)
    block = now_block(_user(), now=summer)
    assert block["local_datetime"] == "2026-06-15T14:30:00-04:00"
    assert block["date"] == "2026-06-15"
    assert block["day_of_week"] == "Monday"
    assert block["local_time"] == "2:30 PM"
    assert block["part_of_day"] == "afternoon"
    assert block["is_weekend"] is False


def test_now_block_weekend_and_midnight_formatting():
    saturday_midnight = datetime(2026, 6, 13, 0, 5, tzinfo=DETROIT)
    block = now_block(_user(), now=saturday_midnight)
    assert block["is_weekend"] is True
    assert block["local_time"] == "12:05 AM"
    assert block["part_of_day"] == "night"


def test_get_user_now_uses_user_timezone():
    assert get_user_now(_user()).tzinfo == DETROIT


def test_blank_timezone_falls_back_to_default():
    assert get_user_now(_user(tz="")).tzinfo == ZoneInfo(DEFAULT_TZ)


# --- part_of_day ------------------------------------------------------------

def test_part_of_day_buckets():
    base = lambda h: datetime(2026, 6, 15, h, 0, tzinfo=DETROIT)
    assert part_of_day(base(6)) == "morning"
    assert part_of_day(base(13)) == "afternoon"
    assert part_of_day(base(19)) == "evening"
    assert part_of_day(base(23)) == "night"
    assert part_of_day(base(3)) == "night"


# --- relative-time anchoring ------------------------------------------------

def test_no_time_means_now():
    now = datetime(2026, 6, 15, 14, 30, tzinfo=DETROIT)
    assert resolve_local_time("", now) == now
    assert resolve_local_time("now", now) == now


def test_bare_clock_time_in_future_is_yesterday():
    # It's 1am and the user says "9pm" — that was last night, not tonight.
    now = datetime(2026, 6, 15, 1, 0, tzinfo=DETROIT)
    resolved = resolve_local_time("9pm", now)
    assert resolved == datetime(2026, 6, 14, 21, 0, tzinfo=DETROIT)


def test_bare_clock_time_in_past_is_today():
    now = datetime(2026, 6, 15, 23, 0, tzinfo=DETROIT)
    resolved = resolve_local_time("9pm", now)
    assert resolved == datetime(2026, 6, 15, 21, 0, tzinfo=DETROIT)


def test_24h_clock_time():
    now = datetime(2026, 6, 15, 23, 0, tzinfo=DETROIT)
    assert resolve_local_time("21:00", now) == datetime(2026, 6, 15, 21, 0, tzinfo=DETROIT)


def test_last_night():
    now = datetime(2026, 6, 15, 7, 0, tzinfo=DETROIT)
    assert resolve_local_time("rough night last night", now) == datetime(2026, 6, 14, 21, 0, tzinfo=DETROIT)


def test_this_morning_clamps_to_now_if_earlier():
    now = datetime(2026, 6, 15, 6, 0, tzinfo=DETROIT)  # before the 8am default
    assert resolve_local_time("this morning", now) == now


def test_yesterday_alone_keeps_clock_time():
    now = datetime(2026, 6, 15, 14, 30, tzinfo=DETROIT)
    assert resolve_local_time("yesterday", now) == now - timedelta(days=1)


def test_unrecognized_returns_none():
    now = datetime(2026, 6, 15, 14, 30, tzinfo=DETROIT)
    assert resolve_local_time("had pizza", now) is None
