from __future__ import annotations

import datetime
from typing import Iterable

from flask import current_app
from livekit import api


def build_livekit_token(
    *,
    identity: str,
    name: str,
    room_name: str,
    can_publish: bool,
    can_subscribe: bool,
    can_publish_data: bool = True,
) -> str:
    token = api.AccessToken(
        current_app.config["LIVEKIT_API_KEY"],
        current_app.config["LIVEKIT_API_SECRET"],
    )
    token.identity = identity
    token.name = name
    token.ttl = datetime.timedelta(
        seconds=int(current_app.config["LIVEKIT_TOKEN_TTL_SECONDS"])
    )
    token.with_grants(
        api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=can_publish,
            can_subscribe=can_subscribe,
            can_publish_data=can_publish_data,
        )
    )
    return token.to_jwt()


def require_livekit_config() -> None:
    required: Iterable[str] = (
        current_app.config.get("LIVEKIT_URL", ""),
        current_app.config.get("LIVEKIT_API_KEY", ""),
        current_app.config.get("LIVEKIT_API_SECRET", ""),
    )
    if not all(required):
        raise RuntimeError("LiveKit environment variables are not configured")
