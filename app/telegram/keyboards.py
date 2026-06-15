from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

CHECKIN_TAGS: list[tuple[str, str]] = [
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


def confirm_delete_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, delete everything", callback_data="del_confirm"),
        InlineKeyboardButton("Cancel", callback_data="del_cancel"),
    ]])
