"""Withings push notification webhook.

Withings POSTs application/x-www-form-urlencoded to this endpoint whenever new
body composition data is available (appli=1). We trigger a fresh pull for the
matching user so daily_metrics is always up to date.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Form, Response
from sqlalchemy import select

from app.db import SessionLocal
from app.models.oauth_connection import OAuthConnection
from app.services.alerts import send_admin_alert

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/webhooks/withings")
async def withings_webhook(
    userid: str = Form(default=""),
    appli: int = Form(default=0),
    startdate: int = Form(default=0),
    enddate: int = Form(default=0),
) -> Response:
    """
    Withings sends: userid, appli, startdate, enddate.
    We find the matching user by Withings user ID and trigger a pull.
    Always return 200 so Withings doesn't retry.
    """
    import asyncio
    from app.routes.withings_oauth import pull_withings_and_store

    if appli != 1:
        # Not body composition — nothing to do
        return Response(status_code=200)

    if not userid:
        logger.warning("Withings webhook received with no userid")
        return Response(status_code=200)

    # Find the user whose Withings connection has this userid stored in scopes
    # Fall back to pulling for ALL active Withings connections (safe for single-user MVP)
    with SessionLocal() as session:
        connections = session.scalars(
            select(OAuthConnection).where(
                OAuthConnection.provider == "withings",
                OAuthConnection.status == "active",
            )
        ).all()
        user_ids = [c.user_id for c in connections]

    if not user_ids:
        logger.info("Withings webhook: no active connections, ignoring")
        return Response(status_code=200)

    logger.info(
        "Withings webhook: appli=%s userid=%s — triggering pull for %s user(s)",
        appli, userid, len(user_ids),
    )

    async def _pull_all() -> None:
        for uid in user_ids:
            try:
                written = await pull_withings_and_store(uid, days=3)
                logger.info("Withings webhook pull: %s days updated for user %s", written, uid)
            except Exception as exc:
                logger.exception("Withings webhook pull failed for user %s", uid)
                await send_admin_alert(f"Withings webhook pull failed for user {uid}: {exc}")

    asyncio.create_task(_pull_all())
    return Response(status_code=200)
