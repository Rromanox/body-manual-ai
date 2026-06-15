"""Withings API client: OAuth and body composition measurement pulls.

CRITICAL (CLAUDE.md gotcha 3): Withings refresh tokens are single-use and
rotate on every use. Persist new tokens in the SAME transaction as the
refresh call. A rejected refresh marks the connection broken.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.oauth_connection import OAuthConnection

AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"
TOKEN_URL = "https://wbsapi.withings.net/v2/oauth2"
MEASURE_URL = "https://wbsapi.withings.net/measure"
NOTIFY_URL = "https://wbsapi.withings.net/notify"
SCOPES = "user.metrics"
NOTIFY_APPLI_BODY_COMPOSITION = 1  # Weight & body composition
REFRESH_MARGIN = timedelta(minutes=5)
HTTP_TIMEOUT = 30.0

# Withings measurement type IDs → DailyMetric column name
# Standard body composition types from Withings Body+ / Body Cardio scales:
#   1=Weight, 5=Fat Free Mass, 6=Fat Ratio%, 76=Muscle Mass,
#   77=Hydration%, 88=Bone Mass
MEAS_TYPES: dict[int, str] = {
    1: "weight",
    5: "fat_free_mass",
    6: "body_fat_pct",
    76: "muscle_mass",
    77: "water_pct",
    88: "bone_mass",
}

logger = logging.getLogger(__name__)


class WithingsAuthError(RuntimeError):
    """Authorization failed; the connection needs re-auth."""


class WithingsApiError(RuntimeError):
    """Withings API returned an unexpected error."""


def redirect_uri() -> str:
    return f"{settings.base_url}/auth/withings/callback"


def build_authorize_url(state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.withings_client_id,
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


async def exchange_code(code: str) -> dict[str, Any]:
    return await _token_request(
        {
            "action": "requesttoken",
            "grant_type": "authorization_code",
            "client_id": settings.withings_client_id,
            "client_secret": settings.withings_client_secret,
            "code": code,
            "redirect_uri": redirect_uri(),
        }
    )


def apply_token_response(connection: OAuthConnection, token_data: dict[str, Any]) -> None:
    """Write tokens from the response body dict into the OAuthConnection row."""
    connection.access_token = token_data["access_token"]
    connection.refresh_token = token_data["refresh_token"]
    connection.expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(token_data.get("expires_in", 10800))
    )
    connection.scopes = token_data.get("scope", SCOPES)
    connection.status = "active"


async def ensure_fresh_access_token(session: Session, connection: OAuthConnection) -> str:
    """Return a valid access token, refreshing (and persisting) if needed."""
    now = datetime.now(timezone.utc)
    if connection.expires_at is not None and connection.expires_at - REFRESH_MARGIN > now:
        return connection.access_token

    try:
        token_data = await _token_request(
            {
                "action": "requesttoken",
                "grant_type": "refresh_token",
                "client_id": settings.withings_client_id,
                "client_secret": settings.withings_client_secret,
                "refresh_token": connection.refresh_token,
            }
        )
    except WithingsAuthError:
        connection.status = "broken"
        session.commit()
        raise

    apply_token_response(connection, token_data)
    session.commit()
    return connection.access_token


async def _token_request(data: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(TOKEN_URL, data=data)

    if response.status_code not in (200, 201):
        raise WithingsApiError(
            f"Withings token request HTTP error ({response.status_code}): {response.text}"
        )

    body = response.json()
    status = body.get("status", -1)
    if status == 401 or status == 100:
        raise WithingsAuthError(
            f"Withings token rejected (status={status}): {body}"
        )
    if status != 0:
        raise WithingsApiError(
            f"Withings token request failed (status={status}): {body}"
        )

    return body["body"]


async def pull_body_measurements(
    access_token: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    """Fetch all body measurement groups between start and end (UTC datetimes)."""
    params = {
        "action": "getmeas",
        "meastype": ",".join(str(t) for t in MEAS_TYPES),
        "category": "1",
        "startdate": str(int(start.timestamp())),
        "enddate": str(int(end.timestamp())),
    }
    headers = {"Authorization": f"Bearer {access_token}"}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.get(MEASURE_URL, params=params, headers=headers)

    if response.status_code == 401:
        raise WithingsAuthError("Withings rejected access token on GET /measure")
    if response.status_code != 200:
        raise WithingsApiError(
            f"GET /measure failed ({response.status_code}): {response.text}"
        )

    body = response.json()
    status = body.get("status", -1)
    if status == 401:
        raise WithingsAuthError("Withings returned status 401 on GET /measure")
    if status != 0:
        raise WithingsApiError(f"GET /measure returned error status={status}: {body}")

    return body.get("body", {}).get("measuregrps", [])


def normalize_measurements(
    groups: list[dict[str, Any]],
    timezone_str: str,
) -> dict[date, dict[str, float]]:
    """
    Convert raw Withings measurement groups into a per-date dict of metric values.
    When multiple measurements exist for the same date, keep the latest (by timestamp).
    """
    tz = ZoneInfo(timezone_str)
    # date -> {metric_col: value, "__ts": timestamp}
    by_date: dict[date, dict[str, Any]] = {}

    for grp in groups:
        ts = grp.get("date", 0)
        dt = datetime.fromtimestamp(ts, tz=tz)
        meas_date = dt.date()

        row = by_date.setdefault(meas_date, {"__ts": ts})
        if ts < row["__ts"]:
            continue  # older group for same date — skip
        row["__ts"] = ts

        for measure in grp.get("measures", []):
            mtype = measure.get("type")
            col = MEAS_TYPES.get(mtype)
            if col is None:
                continue
            raw_value = measure.get("value", 0)
            unit = measure.get("unit", 0)
            value = raw_value * (10 ** unit)
            row[col] = round(value, 2)

    return {d: {k: v for k, v in row.items() if k != "__ts"} for d, row in by_date.items()}


def _webhook_url() -> str:
    return f"{settings.base_url}/webhooks/withings"


async def subscribe_notifications(access_token: str) -> None:
    """Register our webhook with Withings for body composition updates (appli=1)."""
    headers = {"Authorization": f"Bearer {access_token}"}
    data = {
        "action": "subscribe",
        "callbackurl": _webhook_url(),
        "appli": str(NOTIFY_APPLI_BODY_COMPOSITION),
        "comment": "Body Manual AI",
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(NOTIFY_URL, data=data, headers=headers)

    if response.status_code != 200:
        raise WithingsApiError(
            f"Withings notify subscribe failed ({response.status_code}): {response.text}"
        )
    body = response.json()
    status = body.get("status", -1)
    if status not in (0, 294):  # 294 = already subscribed, treat as success
        raise WithingsApiError(
            f"Withings notify subscribe returned error status={status}: {body}"
        )
    logger.info("Withings notification subscription active for appli=%s", NOTIFY_APPLI_BODY_COMPOSITION)


async def unsubscribe_notifications(access_token: str) -> None:
    """Remove the webhook subscription — called when a connection is deleted."""
    headers = {"Authorization": f"Bearer {access_token}"}
    data = {
        "action": "revoke",
        "callbackurl": _webhook_url(),
        "appli": str(NOTIFY_APPLI_BODY_COMPOSITION),
    }
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(NOTIFY_URL, data=data, headers=headers)

    if response.status_code != 200:
        logger.warning("Withings notify revoke returned HTTP %s", response.status_code)
        return
    body = response.json()
    if body.get("status", -1) != 0:
        logger.warning("Withings notify revoke returned status %s", body.get("status"))
