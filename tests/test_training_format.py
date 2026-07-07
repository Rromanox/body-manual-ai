"""Unit 7: formatting, date parsing, edit_session, keyboards, handler wiring."""
from __future__ import annotations

from datetime import date

from app.models.training_session import TrainingSession
from app.services import training_format as fmt
from app.services import training_gate as gate
from app.services import training_plan as tp
from app.telegram import keyboards
from tests.conftest import make_user

TODAY = date(2026, 7, 15)  # Wednesday, week 1


def test_parse_plan_date():
    assert fmt.parse_plan_date("today", TODAY) == TODAY
    assert fmt.parse_plan_date("yesterday", TODAY) == date(2026, 7, 14)
    assert fmt.parse_plan_date("tomorrow", TODAY) == date(2026, 7, 16)
    assert fmt.parse_plan_date("2026-08-15", TODAY) == date(2026, 8, 15)
    assert fmt.parse_plan_date("saturday", TODAY) == date(2026, 7, 18)
    assert fmt.parse_plan_date("sun", TODAY) == date(2026, 7, 19)
    assert fmt.parse_plan_date("Aug 15", TODAY) == date(2026, 8, 15)
    assert fmt.parse_plan_date("15 Aug", TODAY) == date(2026, 8, 15)
    assert fmt.parse_plan_date("gibberish", TODAY) is None


def _row(**kw):
    base = dict(user_id=1, week=1, phase="base", title="X", session_type="z2", status="pending")
    base.update(kw)
    return TrainingSession(**base)


def test_status_icons_and_week_format():
    rows = [
        _row(date=date(2026, 7, 13), session_type="rest", title="Rest"),
        _row(date=date(2026, 7, 14), session_type="intervals", title="3×10 SS", duration_min=60, status="completed"),
        _row(date=date(2026, 7, 18), session_type="long_ride", title="90 min", duration_min=90, status="skipped", priority="high"),
    ]
    text = fmt.format_week(rows, 1)
    assert "Week 1 of 12 — Base" in text
    assert "💤" in text and "✅" in text and "⏭" in text
    assert "3×10 SS" in text


def test_week_and_plan_flag_pre_start():
    before = date(2026, 7, 6)  # plan starts Mon Jul 13
    rows = [_row(date=date(2026, 7, 14), session_type="intervals", title="3×10 SS", duration_min=60)]
    wk_text = fmt.format_week(rows, 1, today=before)
    assert "starts Mon Jul 13" in wk_text  # not mistaken for the current week

    ov = {"current_week": None, "current_phase": None, "completed_sessions": 0,
          "total_sessions": 56, "completion_pct": 0, "critical_done": 0,
          "critical_total": 3, "critical_remaining": 3}
    plan_text = fmt.format_plan(ov, today=before)
    assert "hasn't started yet" in plan_text
    assert "Monday, Jul 13" in plan_text
    # A current week does NOT get the "starts" note.
    assert "starts" not in fmt.format_week(rows, 1, today=date(2026, 7, 15))


def test_format_today_block_matches_spec_shape():
    row = _row(date=date(2026, 9, 1), week=8, phase="build",
               session_type="intervals", title="2×20 min Sweet Spot", duration_min=75)
    g = gate.GateResult(zone="green", recovery=71, session_type="intervals")
    ov = {"current_week": 8, "current_phase": "build", "critical_remaining": 3, "critical_total": 3}
    block = fmt.format_today_block(row, g, ov)
    assert "🚴 TODAY — Week 8, Build" in block
    assert "2×20 min Sweet Spot (75 min)" in block
    assert "Recovery: 71% (green) → as written" in block
    assert "Critical rides remaining: 3 of 3" in block


def test_edit_session_changes_and_validates(mem_session):
    make_user(mem_session)
    tp.upsert_session(mem_session, 1, TODAY, week=1, phase="base",
                      session_type="intervals", title="SS", duration_min=60)
    tp.edit_session(mem_session, 1, TODAY, duration_min=75, source="command")
    assert tp.get_session(mem_session, 1, TODAY).duration_min == 75
    tp.edit_session(mem_session, 1, TODAY, session_type="z2", source="command")
    assert tp.get_session(mem_session, 1, TODAY).session_type == "z2"
    from app.models.training_log import TrainingLog
    assert mem_session.query(TrainingLog).filter_by(action="edited").count() == 2


def test_keyboard_callback_data():
    iso = "2026-09-05"
    gk = keyboards.gate_keyboard(iso).inline_keyboard
    assert gk[0][0].callback_data == f"tr_gate:accept:{iso}"
    assert gk[0][1].callback_data == f"tr_gate:ride:{iso}"
    assert len(keyboards.cant_keyboard(iso).inline_keyboard) == 5
    assert len(keyboards.less_time_keyboard(iso).inline_keyboard[0]) == 3
    crit = keyboards.critical_choice_keyboard(iso).inline_keyboard
    assert crit[0][0].callback_data == f"tr_crit:sunday:{iso}"
    swap = keyboards.move_swap_keyboard("2026-07-15", "2026-07-17").inline_keyboard
    assert swap[0][0].callback_data == "tr_move:swap:2026-07-15:2026-07-17"


def test_handlers_and_registration_import():
    # The training handlers and callback dispatcher import cleanly.
    from app.telegram import training_handlers
    assert callable(training_handlers.week_command)
    assert callable(training_handlers.training_callback)
