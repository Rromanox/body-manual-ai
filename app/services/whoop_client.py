"""WHOOP v2 API client: OAuth (authorize URL, code exchange, refresh) and data pulls.

Token rule (CLAUDE.md gotcha 3/4): refreshed tokens are persisted in the same
transaction as the refresh call, before they are used anywhere. A rejected
refresh marks the connection broken — callers alert the admin and prompt re-auth.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models.oauth_connection import OAuthConnection

AUTHORIZE_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
API_BASE_URL = "https://api.prod.whoop.com/developer"
# offline is what grants refresh tokens — request these five scopes and nothing else
SCOPES = "offline read:cycles read:recovery read:sleep read:workout read:body_measurement"
PAGE_LIMIT = 25
REFRESH_MARGIN = timedelta(minutes=5)
STATE_TTL_SECONDS = 600
HTTP_TIMEOUT = 30.0


class WhoopAuthError(RuntimeError):
    """Authorization failed; the connection needs re-auth."""


class WhoopApiError(RuntimeError):
    """WHOOP API returned an unexpected error."""


@dataclass
class WhoopRawData:
    cycles: list[dict[str, Any]] = field(default_factory=list)
    sleeps: list[dict[str, Any]] = field(default_factory=list)
    recoveries: list[dict[str, Any]] = field(default_factory=list)
    workouts: list[dict[str, Any]] = field(default_factory=list)


def redirect_uri() -> str:
    return f"{settings.base_url}/auth/whoop/callback"


# --- OAuth state (stateless, HMAC-signed; survives app restarts) ---------------

def make_oauth_state(telegram_id: int) -> str:
    payload = f"{telegram_id}.{int(time.time())}"
    return f"{payload}.{_sign(payload)}"


def verify_oauth_state(state: str | None) -> int | None:
    """Returns the telegram_id embedded in a valid, unexpired state; else None."""
    if not state:
        return None
    parts = state.split(".")
    if len(parts) != 3:
        return None
    telegram_id_raw, issued_raw, signature = parts
    if not hmac.compare_digest(_sign(f"{telegram_id_raw}.{issued_raw}"), signature):
        return None
    try:
        telegram_id = int(telegram_id_raw)
        issued_at = int(issued_raw)
    except ValueError:
        return None
    if time.time() - issued_at > STATE_TTL_SECONDS:
        return None
    return telegram_id


def _sign(payload: str) -> str:
    return hmac.new(settings.secret_key.encode(), payload.encode(), hashlib.sha256).hexdigest()


# --- OAuth flow -----------------------------------------------------------------

def build_authorize_url(state: str) -> str:
    query = urlencode(
        {
            "client_id": settings.whoop_client_id,
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
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.whoop_client_id,
            "client_secret": settings.whoop_client_secret,
            "redirect_uri": redirect_uri(),
            "scope": "offline",
        }
    )


def apply_token_response(connection: OAuthConnection, token_data: dict[str, Any]) -> None:
    connection.access_token = token_data["access_token"]
    connection.refresh_token = token_data.get("refresh_token") or connection.refresh_token
    connection.expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=int(token_data.get("expires_in", 3600))
    )
    connection.status = "active"


async def ensure_fresh_access_token(session: Session, connection: OAuthConnection) -> str:
    now = datetime.now(timezone.utc)
    if connection.expires_at is not None and connection.expires_at - REFRESH_MARGIN > now:
        return connection.access_token
    try:
        token_data = await _token_request(
            {
                "grant_type": "refresh_token",
                "refresh_token": connection.refresh_token,
                "client_id": settings.whoop_client_id,
                "client_secret": settings.whoop_client_secret,
                "scope": "offline",
            }
        )
    except WhoopAuthError:
        connection.status = "broken"
        session.commit()
        raise
    apply_token_response(connection, token_data)
    session.commit()
    return connection.access_token


async def _token_request(data: dict[str, str]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(TOKEN_URL, data=data)
    if response.status_code in (400, 401):
        raise WhoopAuthError(f"WHOOP token request rejected ({response.status_code}): {response.text}")
    if response.status_code != 200:
        raise WhoopApiError(f"WHOOP token request failed ({response.status_code}): {response.text}")
    return response.json()


# --- Data pulls -------------------------------------------------------------------

async def pull_raw(access_token: str, start: datetime, end: datetime) -> WhoopRawData:
    return WhoopRawData(
        cycles=await fetch_collection(access_token, "/v2/cycle", start, end),
        sleeps=await fetch_collection(access_token, "/v2/activity/sleep", start, end),
        recoveries=await fetch_collection(access_token, "/v2/recovery", start, end),
        workouts=await fetch_collection(access_token, "/v2/activity/workout", start, end),
    )


async def fetch_collection(
    access_token: str, path: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    params: dict[str, Any] = {"limit": PAGE_LIMIT, "start": _iso_utc(start), "end": _iso_utc(end)}
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, base_url=API_BASE_URL, headers=headers) as client:
        while True:
            response = await client.get(path, params=params)
            if response.status_code == 401:
                raise WhoopAuthError(f"WHOOP rejected access token on GET {path}")
            if response.status_code != 200:
                raise WhoopApiError(f"GET {path} failed ({response.status_code}): {response.text}")
            body = response.json()
            records.extend(body.get("records", []))
            next_token = body.get("next_token")
            if not next_token:
                return records
            params["nextToken"] = next_token


async def fetch_body_measurement(access_token: str) -> dict[str, Any]:
    """Fetch the user's body measurement profile (height, weight, max HR) from WHOOP."""
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, base_url=API_BASE_URL, headers=headers) as client:
        response = await client.get("/v2/body_measurement")
    if response.status_code == 401:
        raise WhoopAuthError("WHOOP rejected access token on GET /v2/body_measurement")
    if response.status_code != 200:
        raise WhoopApiError(
            f"GET /v2/body_measurement failed ({response.status_code}): {response.text}"
        )
    return response.json()


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
