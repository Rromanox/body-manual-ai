from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CHECKIN_TAGS: list[tuple[str, str]] = [
    # Positive habits
    ("🥗 Early dinner", "early_dinner"),
    ("😴 Early bedtime", "early_bedtime"),
    ("💧 Well hydrated", "well_hydrated"),
    ("🧘 Meditated", "meditated"),
    # Disruptors
    ("🍺 Alcohol", "alcohol"),
    ("🌙 Late meal", "late_meal"),
    ("😰 High stress", "high_stress"),
    ("🤒 Sick", "sick"),
    ("✈️ Travel", "travel"),
    ("💪 Hard day", "hard_day"),
    ("☕ Late caffeine", "late_caffeine"),
    ("💧 Dehydrated", "dehydrated"),
    ("🍽️ Big meal", "big_meal"),
]

GOALS: list[tuple[str, str]] = [
    ("General health", "general_health"),
    ("Performance", "performance"),
    ("Weight loss", "weight_loss"),
]


def checkin_keyboard(selected: set[str]) -> InlineKeyboardMarkup:
    rows = []
    for label, tag in CHECKIN_TAGS:
        prefix = "✓ " if tag in selected else ""
        rows.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"ci_tag:{tag}")])
    rows.append([
        InlineKeyboardButton("None of these", callback_data="ci_none"),
        InlineKeyboardButton("Save ✓", callback_data="ci_done"),
    ])
    return InlineKeyboardMarkup(rows)


def goal_keyboard(current_goal: str | None) -> InlineKeyboardMarkup:
    rows = []
    for label, value in GOALS:
        prefix = "✓ " if value == current_goal else ""
        rows.append([InlineKeyboardButton(f"{prefix}{label}", callback_data=f"goal:{value}")])
    return InlineKeyboardMarkup(rows)


def feel_keyboard() -> InlineKeyboardMarkup:
    """Second, optional step of check-in (SPEC §5: feel score 1-5, free-text note)."""
    rows = [[InlineKeyboardButton(str(n), callback_data=f"ci_feel:{n}") for n in range(1, 6)]]
    rows.append([InlineKeyboardButton("📝 Add a note", callback_data="ci_feel_note")])
    rows.append([InlineKeyboardButton("Skip", callback_data="ci_feel_skip")])
    return InlineKeyboardMarkup(rows)


def supplement_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Took it", callback_data="supp_take"),
        InlineKeyboardButton("Not yet", callback_data="supp_skip"),
    ]])


def reta_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Taken ✓", callback_data="reta_taken"),
    ]])


# --- training plan ----------------------------------------------------------
# callback_data format: "tr_<kind>:<arg>:<isodate>[:<isodate2>]" — parsed by
# handlers.training_callback.

def gate_keyboard(iso_date: str) -> InlineKeyboardMarkup:
    """Accept the recovery gate's adjustment, or ride as written."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Accept adjustment", callback_data=f"tr_gate:accept:{iso_date}"),
        InlineKeyboardButton("Ride as written", callback_data=f"tr_gate:ride:{iso_date}"),
    ]])


def critical_choice_keyboard(iso_date: str) -> InlineKeyboardMarkup:
    """The two never-drop options for a missed critical ride (rule 3)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Shift to Sunday", callback_data=f"tr_crit:sunday:{iso_date}"),
        InlineKeyboardButton("Take next Saturday", callback_data=f"tr_crit:nextsat:{iso_date}"),
    ]])


def cant_keyboard(iso_date: str) -> InlineKeyboardMarkup:
    from app.services.training_substitution import CONSTRAINT_BUTTONS
    rows = [
        [InlineKeyboardButton(label, callback_data=f"tr_cant:{value}:{iso_date}")]
        for label, value in CONSTRAINT_BUTTONS
    ]
    return InlineKeyboardMarkup(rows)


def less_time_keyboard(iso_date: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"{n} min", callback_data=f"tr_time:{n}:{iso_date}")
        for n in (30, 45, 60)
    ]])


def edit_keyboard(iso_date: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Duration", callback_data=f"tr_edit:duration:{iso_date}"),
        InlineKeyboardButton("Intensity/Type", callback_data=f"tr_edit:type:{iso_date}"),
    ]])


def move_swap_keyboard(from_iso: str, to_iso: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Confirm swap", callback_data=f"tr_move:swap:{from_iso}:{to_iso}"),
        InlineKeyboardButton("Cancel", callback_data=f"tr_move:cancel:{from_iso}"),
    ]])


def confirm_delete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, delete everything", callback_data="del_confirm"),
        InlineKeyboardButton("Cancel", callback_data="del_cancel"),
    ]])
