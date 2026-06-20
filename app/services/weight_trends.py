"""Deterministic weight-trend audit (Weight Trend Audit Guard).

The AI kept reconstructing weight history from memory — moving readings to the
wrong dates and treating a 7-day AVERAGE as the weight 7 days ago. This computes
every trend from the actual stored dated readings and exposes the exact rows,
dates, method, and math so the AI can only quote them, never invent them.

Pure functions over a list of (date, lbs) — trivially testable, no DB/AI.
Key rule: a window's start weight is always a real dated reading, never an average.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

_WINDOWS = (3, 7, 14, 30)
_MIN_RATE_POINTS = 4  # points needed to prefer linear regression for the selected rate


def _clean(weights: list[tuple[date, float | None]]) -> list[tuple[date, float]]:
    pts = [(d, round(float(w), 1)) for d, w in weights if w is not None]
    pts.sort(key=lambda x: x[0])
    return pts


def _endpoint_trend(window_pts: list[tuple[date, float]], window_days: int) -> dict[str, Any] | None:
    if len(window_pts) < 2:
        return None
    start_d, start_w = window_pts[0]
    end_d, end_w = window_pts[-1]
    days = (end_d - start_d).days
    if days <= 0:
        return None
    lbs_per_day = (end_w - start_w) / days
    n = len(window_pts)
    summary = {
        "method": "endpoint_change",
        "window_days": window_days,
        "start_date": str(start_d),
        "start_weight": round(start_w, 1),
        "end_date": str(end_d),
        "end_weight": round(end_w, 1),
        "change_lbs": round(end_w - start_w, 1),
        "days_elapsed": days,
        "lbs_per_day": round(lbs_per_day, 3),
        "lbs_per_week": round(lbs_per_day * 7, 2),
        "data_points_used": n,
        "data_dates_used": [str(d) for d, _ in window_pts],
    }
    summary["confidence"] = "low" if n < 3 else ("medium" if n < 5 else "high")
    if n < 3:
        summary["note"] = "sparse data — based on only the endpoints"
    return summary


def _linreg_lbs_per_day(window_pts: list[tuple[date, float]]) -> float | None:
    """Least-squares slope in lbs/day (negative = losing). None if undefined."""
    n = len(window_pts)
    if n < 2:
        return None
    base = window_pts[0][0]
    xs = [(d - base).days for d, _ in window_pts]
    ys = [w for _, w in window_pts]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denom
    return slope


def _window_points(pts: list[tuple[date, float]], window_days: int) -> list[tuple[date, float]]:
    cutoff = pts[-1][0] - timedelta(days=window_days)
    return [(d, w) for d, w in pts if d >= cutoff]


def _select_rate(pts: list[tuple[date, float]], windows: dict[int, dict]) -> dict[str, Any] | None:
    """Choose the rate the projection should use. Prefer linear regression over a
    stable longer window; fall back to the endpoint trend of the best window."""
    for win in (14, 7, 30, 3):
        wp = _window_points(pts, win)
        if len(wp) >= _MIN_RATE_POINTS:
            slope = _linreg_lbs_per_day(wp)
            if slope is not None:
                return {
                    "rate_lbs_per_week": round(slope * 7, 2),
                    "window_days": win,
                    "method": "linear_regression",
                    "data_points_used": len(wp),
                }
    # Fallback: endpoint of the longest window we have.
    for win in (30, 14, 7, 3):
        s = windows.get(win)
        if s:
            return {
                "rate_lbs_per_week": s["lbs_per_week"],
                "window_days": s["window_days"],
                "method": "endpoint_change",
                "data_points_used": s["data_points_used"],
            }
    return None


def build_weight_trend_audit(
    weights: list[tuple[date, float | None]], as_of: date
) -> dict[str, Any] | None:
    """Full audit: per-window endpoint trends + a selected projection rate +
    the exact known dated weights. None when there are fewer than 2 readings."""
    pts = _clean(weights)
    if len(pts) < 2:
        return None
    windows: dict[int, dict] = {}
    for win in _WINDOWS:
        wp = _window_points(pts, win)
        trend = _endpoint_trend(wp, win)
        if trend is not None:
            windows[win] = trend
    selected = _select_rate(pts, windows)
    return {
        "current_weight": pts[-1][1],
        "current_date": str(pts[-1][0]),
        "selected": selected,
        # JSON keys must be strings — window sizes as "3d"/"7d"/... for payloads.
        "windows": {f"{k}d": v for k, v in windows.items()},
        "known_weights": {str(d): w for d, w in pts},
    }


def _fmt_date(iso: str) -> str:
    try:
        d = date.fromisoformat(iso)
        return f"{d.strftime('%b')} {d.day}"
    except (ValueError, TypeError):
        return iso


def format_audit(audit: dict[str, Any] | None) -> str:
    """Deterministic plain-text summary — used as the guard fallback."""
    if not audit:
        return "I don't have enough weight readings to break down your trend yet."
    sel = audit.get("selected")
    lines = []
    if sel:
        lines.append(
            f"Selected rate: {abs(sel['rate_lbs_per_week'])} lb/week "
            f"({sel['method'].replace('_', ' ')} over the last {sel['window_days']} days, "
            f"{sel['data_points_used']} readings)."
        )
    for label in ("3d", "7d", "14d", "30d"):
        w = (audit.get("windows") or {}).get(label)
        if not w:
            continue
        lines.append(
            f"{w['window_days']}-day: {w['start_weight']} ({w['start_date']}) -> "
            f"{w['end_weight']} ({w['end_date']}) = {w['change_lbs']} lb over "
            f"{w['days_elapsed']} days = {w['lbs_per_week']} lb/week."
        )
    return "\n".join(lines) if lines else "Not enough weight data to compute a trend."


def format_weight_audit_answer(
    audit: dict[str, Any] | None,
    question_text: str | None = None,
    projection: dict[str, Any] | None = None,
) -> str:
    """Full user-facing answer for a weight data-audit question, built ENTIRELY
    from backend data — no AI. Lists only actual stored readings (never invented
    or re-dated), names the selected rate's window/method, shows the per-window
    math, and adds NO generic advice."""
    if not audit:
        return "I don't have enough weight readings yet to break down your trend."

    lines: list[str] = []
    sel = audit.get("selected")
    if sel:
        rate = sel["rate_lbs_per_week"]
        verb = "losing" if rate < 0 else ("gaining" if rate > 0 else "holding at")
        lines.append(
            f"That {abs(rate)} lb/week is the backend {sel['method'].replace('_', ' ')} trend over the "
            f"last {sel['window_days']} days ({sel['data_points_used']} readings) - you're {verb} weight. "
            f"It is NOT a simple 7-day average, and I don't use an average as a starting weight."
        )

    # Exact readings — only what's actually stored.
    known = audit.get("known_weights") or {}
    if known:
        lines.append("")
        lines.append("Weight readings I have:")
        for iso in sorted(known.keys(), reverse=True):
            lines.append(f"- {_fmt_date(iso)}: {known[iso]} lb")

    # Per-window endpoint math.
    windows = audit.get("windows") or {}
    shown = [windows[k] for k in ("3d", "7d", "14d", "30d") if k in windows]
    if shown:
        lines.append("")
        lines.append("Trend by window (endpoint to endpoint):")
        for w in shown:
            lines.append(
                f"- {w['window_days']}-day: {w['start_weight']} lb ({_fmt_date(w['start_date'])}) -> "
                f"{w['end_weight']} lb ({_fmt_date(w['end_date'])}) = {w['change_lbs']} lb over "
                f"{w['days_elapsed']} days = {w['lbs_per_week']} lb/week"
            )

    # Optional projection line (only when it's a real projection).
    if projection and projection.get("status") == "projected":
        from app.services.weight_projection import format_projection
        lines.append("")
        lines.append(format_projection(projection))

    return "\n".join(lines).strip()
