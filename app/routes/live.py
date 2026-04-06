from __future__ import annotations

from datetime import datetime

from flask import Blueprint, current_app, request

from ..extensions import db
from ..livekit_tokens import build_livekit_token, require_livekit_config
from ..models import LiveEvent, LiveRoom, RoomParticipant

live_bp = Blueprint("live", __name__)


def _ok(data: dict):
    return {"ok": True, "data": data}


def _error(message: str, status: int = 400):
    return {"ok": False, "error": {"message": message}}, status


def _serialize_pins(live_stream_id: str) -> list[dict]:
    events = (
        LiveEvent.query.filter_by(live_room_id=live_stream_id)
        .filter(LiveEvent.event_type.in_(["pin_listing", "unpin_listing", "activate_pin"]))
        .order_by(LiveEvent.created_at.asc(), LiveEvent.id.asc())
        .all()
    )
    items_by_listing: dict[str, dict] = {}
    active_listing_id: str | None = None

    for event in events:
        payload = event.payload_json or {}
        listing_id = str(payload.get("listingId") or "").strip()
        if not listing_id:
            continue

        if event.event_type == "pin_listing":
            items_by_listing[listing_id] = {
                "id": str(payload.get("id") or f"pin-{live_stream_id}-{listing_id}-{event.id}"),
                "liveId": live_stream_id,
                "listingId": listing_id,
                "pinnedAt": int(payload.get("pinnedAt") or int(event.created_at.timestamp() * 1000)),
                "active": True,
                "pinnedBy": payload.get("pinnedBy"),
            }
            active_listing_id = listing_id
            for other_listing_id, item in items_by_listing.items():
                item["active"] = other_listing_id == active_listing_id
        elif event.event_type == "activate_pin":
            if listing_id not in items_by_listing:
                continue
            active_listing_id = listing_id
            for other_listing_id, item in items_by_listing.items():
                item["active"] = other_listing_id == active_listing_id
        elif event.event_type == "unpin_listing":
            items_by_listing.pop(listing_id, None)
            if active_listing_id == listing_id:
                active_listing_id = None
                remaining = sorted(
                    items_by_listing.values(),
                    key=lambda item: int(item.get("pinnedAt") or 0),
                    reverse=True,
                )
                if remaining:
                    active_listing_id = str(remaining[0]["listingId"])
            for other_listing_id, item in items_by_listing.items():
                item["active"] = other_listing_id == active_listing_id

    return sorted(
        items_by_listing.values(),
        key=lambda item: int(item.get("pinnedAt") or 0),
        reverse=True,
    )


def _latest_summary(live_stream_id: str) -> dict | None:
    event = (
        LiveEvent.query.filter_by(live_room_id=live_stream_id, event_type="summary_report")
        .order_by(LiveEvent.created_at.desc(), LiveEvent.id.desc())
        .first()
    )
    if not event:
        return None
    payload = event.payload_json or {}
    if not isinstance(payload, dict):
        return None
    return payload


def _latest_event_payload(live_stream_id: str, event_type: str) -> dict | None:
    event = (
        LiveEvent.query.filter_by(live_room_id=live_stream_id, event_type=event_type)
        .order_by(LiveEvent.created_at.desc(), LiveEvent.id.desc())
        .first()
    )
    if not event or not isinstance(event.payload_json, dict):
        return None
    return event.payload_json


def _build_room_metadata(live_stream_id: str) -> dict:
    stream_patch = _latest_event_payload(live_stream_id, "stream_updated") or {}
    started_payload = _latest_event_payload(live_stream_id, "stream_started") or {}
    source = stream_patch or started_payload
    badges = source.get("badges")
    if not isinstance(badges, list):
        badges = []
    return {
        "themeId": source.get("themeId") or source.get("templateId"),
        "qualityMode": source.get("qualityMode"),
        "effectsPipeline": source.get("effectsPipeline"),
        "sellerTrustLabel": source.get("sellerTrustLabel"),
        "badges": [str(item) for item in badges if item],
    }


def _build_moderation_summary(live_stream_id: str) -> dict:
    events = (
        LiveEvent.query.filter_by(live_room_id=live_stream_id, event_type="moderation_action")
        .order_by(LiveEvent.created_at.asc(), LiveEvent.id.asc())
        .all()
    )
    reports = (
        LiveEvent.query.filter_by(live_room_id=live_stream_id, event_type="report_submitted")
        .order_by(LiveEvent.created_at.asc(), LiveEvent.id.asc())
        .all()
    )
    blocked_users: set[str] = set()
    muted_users: set[str] = set()
    last_action_at: int | None = None
    for event in events:
        payload = event.payload_json or {}
        target_user_id = str(payload.get("targetUserId") or "").strip()
        action = str(payload.get("action") or "").strip()
        if action == "block" and target_user_id:
            blocked_users.add(target_user_id)
        if action == "unblock" and target_user_id:
            blocked_users.discard(target_user_id)
        if action == "mute" and target_user_id:
            muted_users.add(target_user_id)
        if action == "unmute" and target_user_id:
            muted_users.discard(target_user_id)
        last_action_at = int(event.created_at.timestamp() * 1000)
    return {
        "totalActions": len(events),
        "blockedUsers": len(blocked_users),
        "mutedUsers": len(muted_users),
        "reportsCount": len(reports),
        "lastActionAt": last_action_at,
    }


def _build_replay_summary(live_stream_id: str) -> dict:
    summary = _latest_summary(live_stream_id) or {}
    replay_payload = _latest_event_payload(live_stream_id, "replay_generated") or {}
    recording_payload = _latest_event_payload(live_stream_id, "recording_ready") or {}
    recording_url = (
        replay_payload.get("recordingUrl")
        or recording_payload.get("recordingUrl")
        or summary.get("recordingUrl")
    )
    clips = replay_payload.get("clips") or summary.get("topMoments")
    checkpoints = replay_payload.get("checkpoints") or summary.get("salesTimeline")
    return {
        "available": bool(summary or replay_payload or recording_payload),
        "recordingUrl": recording_url,
        "clipsCount": len(clips) if isinstance(clips, list) else 0,
        "checkpointsCount": len(checkpoints) if isinstance(checkpoints, list) else 0,
        "generatedAt": replay_payload.get("generatedAt") or recording_payload.get("generatedAt"),
    }


def _serialize_room_state(live_stream_id: str) -> dict:
    return {
        "metadata": _build_room_metadata(live_stream_id),
        "moderation": _build_moderation_summary(live_stream_id),
        "replay": _build_replay_summary(live_stream_id),
    }


def _serialize_room(room: LiveRoom) -> dict:
    joined_participants = RoomParticipant.query.filter_by(
        live_room_id=room.id,
        connection_status="joined",
    ).all()
    return {
        "liveStreamId": room.id,
        "hostId": room.host_id,
        "hostName": room.host_name,
        "title": room.title,
        "status": room.status,
        "scheduledTime": room.scheduled_time,
        "viewerCount": len([participant for participant in joined_participants if participant.role == "viewer"]),
        "participantCount": len(joined_participants),
        "participants": [
            {
                "userId": participant.user_id,
                "role": participant.role,
                "joined": participant.connection_status == "joined",
            }
            for participant in joined_participants
        ],
        "pins": _serialize_pins(room.id),
        "summary": _latest_summary(room.id),
        "roomState": _serialize_room_state(room.id),
        "updatedAt": int(room.updated_at.timestamp() * 1000) if room.updated_at else None,
        "createdAt": int(room.created_at.timestamp() * 1000) if room.created_at else None,
    }


def _find_active_room_for_host(host_id: str) -> LiveRoom | None:
    return (
        LiveRoom.query.filter_by(host_id=host_id)
        .filter(LiveRoom.status.in_(["setup", "preview", "live", "scheduled"]))
        .order_by(LiveRoom.updated_at.desc(), LiveRoom.created_at.desc(), LiveRoom.id.desc())
        .first()
    )


def _get_or_create_room(live_stream_id: str, payload: dict | None = None) -> LiveRoom:
    room = LiveRoom.query.get(live_stream_id)
    if room:
        return room
    payload = payload or {}
    room = LiveRoom(
        id=live_stream_id,
        host_id=payload.get("hostId", "host"),
        host_name=payload.get("hostName", "Host"),
        title=payload.get("title", "Live now"),
        status=payload.get("status", "setup"),
        scheduled_time=payload.get("scheduledTime"),
    )
    db.session.add(room)
    db.session.commit()
    return room


@live_bp.post("/streams")
def create_live_stream():
    payload = request.get_json(silent=True) or {}
    live_stream_id = str(payload.get("id") or payload.get("liveStreamId") or "").strip()
    if not live_stream_id:
        return _error("liveStreamId is required")
    room = _get_or_create_room(live_stream_id, payload)
    return _ok({"liveStreamId": room.id, "status": room.status})


@live_bp.get("/runtime-status")
def live_runtime_status():
    livekit_configured = True
    try:
        require_livekit_config()
    except Exception:
        livekit_configured = False
    return _ok(
        {
            "livekitConfigured": livekit_configured,
            "livekitUrlConfigured": bool(current_app.config.get("LIVEKIT_URL", "")),
            "apiKeyConfigured": bool(current_app.config.get("LIVEKIT_API_KEY", "")),
            "apiSecretConfigured": bool(current_app.config.get("LIVEKIT_API_SECRET", "")),
        }
    )


@live_bp.get("/streams/<live_stream_id>")
def get_live_stream(live_stream_id: str):
    room = _get_or_create_room(live_stream_id)
    return _ok(_serialize_room(room))


@live_bp.get("/hosts/<host_id>/active")
def get_active_live_for_host(host_id: str):
    normalized_host_id = str(host_id or "").strip()
    if not normalized_host_id:
        return _error("hostId is required")
    room = _find_active_room_for_host(normalized_host_id)
    if not room:
        return _ok({"active": None})
    return _ok({"active": _serialize_room(room)})


@live_bp.patch("/streams/<live_stream_id>")
def update_live_stream(live_stream_id: str):
    payload = request.get_json(silent=True) or {}
    room = _get_or_create_room(live_stream_id)
    if "hostId" in payload and payload.get("hostId"):
        room.host_id = str(payload.get("hostId"))
    if "hostName" in payload and payload.get("hostName"):
        room.host_name = str(payload.get("hostName"))
    if "title" in payload and payload.get("title"):
        room.title = str(payload.get("title"))
    if "status" in payload and payload.get("status"):
        room.status = str(payload.get("status"))
    if "scheduledTime" in payload:
        room.scheduled_time = payload.get("scheduledTime")
    db.session.add(
        LiveEvent(
            live_room_id=room.id,
            event_type="stream_updated",
            actor_id=str(payload.get("hostId") or room.host_id),
            payload_json=payload,
        )
    )
    db.session.commit()
    return _ok(_serialize_room(room))


@live_bp.post("/schedule")
def schedule_live():
    payload = request.get_json(silent=True) or {}
    live_stream_id = str(payload.get("liveStreamId") or "").strip()
    if not live_stream_id:
        return _error("liveStreamId is required")
    room = _get_or_create_room(live_stream_id, payload | {"status": "scheduled"})
    room.status = "scheduled"
    room.scheduled_time = payload.get("scheduledTime")
    db.session.commit()
    return _ok(
        {
            "liveStreamId": room.id,
            "hostId": room.host_id,
            "hostName": room.host_name,
            "title": room.title,
            "scheduledTime": room.scheduled_time,
            "status": "scheduled",
            "confirmationId": f"schedule-{room.id}",
        }
    )


@live_bp.post("/streams/<live_stream_id>/start")
def start_live_stream(live_stream_id: str):
    payload = request.get_json(silent=True) or {}
    room = _get_or_create_room(live_stream_id, payload | {"status": "live"})
    room.status = "live"
    db.session.add(
        LiveEvent(
            live_room_id=room.id,
            event_type="stream_started",
            actor_id=payload.get("hostId"),
            payload_json=payload,
        )
    )
    db.session.commit()
    try:
        require_livekit_config()
        token = build_livekit_token(
            identity=str(payload.get("hostId") or room.host_id),
            name=str(payload.get("hostName") or room.host_name),
            room_name=room.id,
            can_publish=True,
            can_subscribe=True,
        )
        server_url = current_app.config["LIVEKIT_URL"]
    except Exception as error:
        current_app.logger.exception("Failed to start live stream %s", live_stream_id)
        return _error(str(error), 500)

    return _ok(
        {
            "liveStreamId": room.id,
            "status": "live",
            "serverUrl": server_url,
            "token": token,
        }
    )


@live_bp.get("/streams/<live_stream_id>/pins")
def get_live_pins(live_stream_id: str):
    _get_or_create_room(live_stream_id)
    return _ok({"liveStreamId": live_stream_id, "items": _serialize_pins(live_stream_id)})


@live_bp.get("/streams/<live_stream_id>/summary")
def get_live_summary(live_stream_id: str):
    _get_or_create_room(live_stream_id)
    return _ok({"liveStreamId": live_stream_id, "summary": _latest_summary(live_stream_id)})


@live_bp.get("/streams/<live_stream_id>/state")
def get_live_room_state(live_stream_id: str):
    _get_or_create_room(live_stream_id)
    return _ok({"liveStreamId": live_stream_id, **_serialize_room_state(live_stream_id)})


@live_bp.post("/streams/<live_stream_id>/summary")
def save_live_summary(live_stream_id: str):
    payload = request.get_json(silent=True) or {}
    _get_or_create_room(live_stream_id)
    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type="summary_report",
            actor_id=str(payload.get("hostId") or payload.get("host") or ""),
            payload_json=payload,
        )
    )
    db.session.commit()
    return _ok({"liveStreamId": live_stream_id, "summary": payload})


@live_bp.post("/streams/<live_stream_id>/replay")
def save_live_replay(live_stream_id: str):
    payload = request.get_json(silent=True) or {}
    _get_or_create_room(live_stream_id)
    event_type = str(payload.get("eventType") or "replay_generated")
    if event_type not in {"replay_generated", "recording_ready"}:
        return _error("eventType must be replay_generated or recording_ready")
    replay_payload = {
        "recordingUrl": payload.get("recordingUrl"),
        "clips": payload.get("clips"),
        "checkpoints": payload.get("checkpoints"),
        "generatedAt": int(payload.get("generatedAt") or int(datetime.utcnow().timestamp() * 1000)),
        "status": payload.get("status") or ("ready" if payload.get("recordingUrl") else "pending"),
    }
    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type=event_type,
            actor_id=str(payload.get("actorId") or payload.get("hostId") or payload.get("host") or ""),
            payload_json=replay_payload,
        )
    )
    db.session.commit()
    return _ok(
        {
            "liveStreamId": live_stream_id,
            "replay": _build_replay_summary(live_stream_id),
        }
    )


@live_bp.post("/streams/<live_stream_id>/pins")
def pin_live_listing(live_stream_id: str):
    payload = request.get_json(silent=True) or {}
    listing_id = str(payload.get("listingId") or "").strip()
    if not listing_id:
        return _error("listingId is required")
    _get_or_create_room(live_stream_id)
    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type="pin_listing",
            actor_id=str(payload.get("pinnedBy") or payload.get("actorId") or ""),
            payload_json={
                "id": str(payload.get("id") or f"pin-{live_stream_id}-{listing_id}-{int(datetime.utcnow().timestamp() * 1000)}"),
                "listingId": listing_id,
                "pinnedBy": payload.get("pinnedBy"),
                "pinnedAt": int(payload.get("pinnedAt") or int(datetime.utcnow().timestamp() * 1000)),
            },
        )
    )
    db.session.commit()
    return _ok({"liveStreamId": live_stream_id, "items": _serialize_pins(live_stream_id)})


@live_bp.post("/streams/<live_stream_id>/pins/<listing_id>/activate")
def activate_live_pin(live_stream_id: str, listing_id: str):
    _get_or_create_room(live_stream_id)
    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type="activate_pin",
            actor_id="system",
            payload_json={"listingId": listing_id},
        )
    )
    db.session.commit()
    return _ok({"liveStreamId": live_stream_id, "items": _serialize_pins(live_stream_id)})


@live_bp.delete("/streams/<live_stream_id>/pins")
@live_bp.delete("/streams/<live_stream_id>/pins/<listing_id>")
def unpin_live_listing(live_stream_id: str, listing_id: str | None = None):
    target_listing_id = str(listing_id or "").strip()
    if not target_listing_id:
        current_items = _serialize_pins(live_stream_id)
        active_item = next((item for item in current_items if item.get("active")), None)
        target_listing_id = str(active_item.get("listingId") if active_item else "").strip()
    if not target_listing_id:
        return _ok({"liveStreamId": live_stream_id, "items": _serialize_pins(live_stream_id)})
    _get_or_create_room(live_stream_id)
    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type="unpin_listing",
            actor_id="system",
            payload_json={"listingId": target_listing_id},
        )
    )
    db.session.commit()
    return _ok({"liveStreamId": live_stream_id, "items": _serialize_pins(live_stream_id)})


@live_bp.post("/streams/<live_stream_id>/validate")
def validate_token(live_stream_id: str):
    room = _get_or_create_room(live_stream_id)
    livekit_configured = True
    try:
        require_livekit_config()
    except Exception:
        livekit_configured = False
    return _ok(
        {
            "liveStreamId": live_stream_id,
            "valid": livekit_configured and room.status in ("preview", "live"),
            "status": room.status,
            "hostId": room.host_id,
            "hostName": room.host_name,
            "title": room.title,
            "livekitConfigured": livekit_configured,
            "checkedAt": int(datetime.utcnow().timestamp() * 1000),
        }
    )


@live_bp.post("/streams/<live_stream_id>/join")
def join_live_stream(live_stream_id: str):
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("userId") or "").strip()
    if not user_id:
        return _error("userId is required")

    room = _get_or_create_room(live_stream_id)
    participant = RoomParticipant.query.filter_by(
        live_room_id=room.id,
        user_id=user_id,
    ).first()
    if not participant:
        participant = RoomParticipant(
            live_room_id=room.id,
            user_id=user_id,
            role=str(payload.get("role") or "viewer"),
            connection_status="joined",
        )
        db.session.add(participant)
    else:
        participant.connection_status = "joined"
        participant.left_at = None

    db.session.add(
        LiveEvent(
            live_room_id=room.id,
            event_type="viewer_joined",
            actor_id=user_id,
            payload_json=payload,
        )
    )
    db.session.commit()
    try:
        require_livekit_config()
        token = build_livekit_token(
            identity=user_id,
            name=str(payload.get("userName") or user_id),
            room_name=room.id,
            can_publish=bool(payload.get("canPublish") or False),
            can_subscribe=True,
        )
        server_url = current_app.config["LIVEKIT_URL"]
    except Exception as error:
        current_app.logger.exception("Failed to join live stream %s for user %s", live_stream_id, user_id)
        return _error(str(error), 500)

    return _ok(
        {
            "liveStreamId": room.id,
            "userId": user_id,
            "joinedAt": int(datetime.utcnow().timestamp() * 1000),
            "serverUrl": server_url,
            "token": token,
        }
    )


@live_bp.post("/streams/<live_stream_id>/leave")
def leave_live_stream(live_stream_id: str):
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("userId") or "").strip()
    if not user_id:
        return _error("userId is required")

    participant = RoomParticipant.query.filter_by(
        live_room_id=live_stream_id,
        user_id=user_id,
    ).first()
    if participant:
        participant.connection_status = "left"
        participant.left_at = datetime.utcnow()

    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type="viewer_left",
            actor_id=user_id,
            payload_json=payload,
        )
    )
    db.session.commit()
    return _ok({"liveStreamId": live_stream_id, "userId": user_id, "leftAt": int(datetime.utcnow().timestamp() * 1000)})


@live_bp.post("/cohosts/invite")
def invite_cohost():
    payload = request.get_json(silent=True) or {}
    live_stream_id = str(payload.get("liveStreamId") or "").strip()
    if not live_stream_id:
        return _error("liveStreamId is required")
    _get_or_create_room(live_stream_id)
    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type="cohost_invited",
            actor_id=str(payload.get("actorId") or payload.get("userId") or ""),
            payload_json=payload,
        )
    )
    db.session.commit()
    return _ok(
        {
            "liveStreamId": payload.get("liveStreamId"),
            "userId": payload.get("userId"),
            "invitedAt": int(datetime.utcnow().timestamp() * 1000),
        }
    )


@live_bp.post("/cohosts/respond")
def respond_to_invite():
    payload = request.get_json(silent=True) or {}
    live_stream_id = str(payload.get("liveStreamId") or "").strip()
    user_id = str(payload.get("userId") or "").strip()
    if not live_stream_id:
        return _error("liveStreamId is required")
    if not user_id:
        return _error("userId is required")
    _get_or_create_room(live_stream_id)
    participant = RoomParticipant.query.filter_by(
        live_room_id=live_stream_id,
        user_id=user_id,
    ).first()
    if payload.get("status") == "accepted":
        if not participant:
            participant = RoomParticipant(
                live_room_id=live_stream_id,
                user_id=user_id,
                role="cohost",
                connection_status="accepted",
            )
            db.session.add(participant)
        else:
            participant.role = "cohost"
            participant.connection_status = "accepted"
            participant.left_at = None
    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type="cohost_response",
            actor_id=user_id,
            payload_json=payload,
        )
    )
    db.session.commit()
    return _ok(
        {
            "liveStreamId": payload.get("liveStreamId"),
            "userId": payload.get("userId"),
            "status": payload.get("status"),
            "respondedAt": int(datetime.utcnow().timestamp() * 1000),
        }
    )


@live_bp.post("/gifts")
def send_gift():
    payload = request.get_json(silent=True) or {}
    live_stream_id = str(payload.get("liveStreamId") or "").strip()
    sender_id = str(payload.get("senderId") or "").strip()
    client_request_id = str(payload.get("clientRequestId") or "").strip()
    if not live_stream_id:
        return _error("liveStreamId is required")
    if not sender_id:
        return _error("senderId is required")
    if client_request_id:
        existing_events = (
            LiveEvent.query.filter_by(
                live_room_id=live_stream_id,
                event_type="gift_sent",
                actor_id=sender_id,
            )
            .order_by(LiveEvent.id.desc())
            .limit(50)
            .all()
        )
        existing = next(
            (
                event
                for event in existing_events
                if isinstance(event.payload_json, dict)
                and str(event.payload_json.get("clientRequestId") or "").strip() == client_request_id
            ),
            None,
        )
        if existing:
            existing_payload = existing.payload_json or {}
            return _ok(
                {
                    **existing_payload,
                    "clientRequestId": client_request_id,
                    "ackId": f"gift-event-{existing.id}",
                    "deduped": True,
                    "sentAt": int(existing.created_at.timestamp() * 1000),
                }
            )

    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type="gift_sent",
            actor_id=sender_id,
            payload_json=payload,
        )
    )
    db.session.commit()
    latest_event = (
        LiveEvent.query.filter_by(
            live_room_id=live_stream_id,
            event_type="gift_sent",
            actor_id=sender_id,
        )
        .order_by(LiveEvent.id.desc())
        .first()
    )
    return _ok(
        {
            **payload,
            "clientRequestId": client_request_id or None,
            "ackId": f"gift-event-{latest_event.id}" if latest_event else None,
            "deduped": False,
            "sentAt": int(datetime.utcnow().timestamp() * 1000),
        }
    )


@live_bp.post("/streams/<live_stream_id>/presence")
def update_live_presence(live_stream_id: str):
    payload = request.get_json(silent=True) or {}
    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type="presence_update",
            actor_id=str(payload.get("userId") or ""),
            payload_json=payload,
        )
    )
    db.session.commit()
    return _ok(
        {
            "liveStreamId": live_stream_id,
            "userId": payload.get("userId"),
            "presence": payload,
            "updatedAt": int(datetime.utcnow().timestamp() * 1000),
        }
    )


@live_bp.post("/streams/<live_stream_id>/moderation")
def moderate_live_user(live_stream_id: str):
    payload = request.get_json(silent=True) or {}
    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type="moderation_action",
            actor_id=str(payload.get("actorId") or ""),
            payload_json=payload,
        )
    )
    db.session.commit()
    return _ok(
        {
            "liveStreamId": live_stream_id,
            "targetUserId": payload.get("targetUserId"),
            "action": payload.get("action"),
            "moderation": _build_moderation_summary(live_stream_id),
            "updatedAt": int(datetime.utcnow().timestamp() * 1000),
        }
    )


@live_bp.post("/streams/<live_stream_id>/report")
def report_live_room(live_stream_id: str):
    payload = request.get_json(silent=True) or {}
    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type="report_submitted",
            actor_id=str(payload.get("actorId") or payload.get("userId") or ""),
            payload_json=payload,
        )
    )
    db.session.commit()
    return _ok(
        {
            "liveStreamId": live_stream_id,
            "reportId": f"report-{live_stream_id}-{int(datetime.utcnow().timestamp() * 1000)}",
            "moderation": _build_moderation_summary(live_stream_id),
            "reportedAt": int(datetime.utcnow().timestamp() * 1000),
        }
    )


@live_bp.post("/streams/<live_stream_id>/reactions")
def react_in_live_room(live_stream_id: str):
    payload = request.get_json(silent=True) or {}
    db.session.add(
        LiveEvent(
            live_room_id=live_stream_id,
            event_type="reaction_sent",
            actor_id=str(payload.get("userId") or ""),
            payload_json=payload,
        )
    )
    db.session.commit()
    return _ok(
        {
            "liveStreamId": live_stream_id,
            "userId": payload.get("userId"),
            "emoji": payload.get("emoji"),
            "sentAt": int(datetime.utcnow().timestamp() * 1000),
        }
    )


@live_bp.post("/push")
def push_live_notification():
    payload = request.get_json(silent=True) or {}
    return _ok(
        {
            **payload,
            "deliveredAt": int(datetime.utcnow().timestamp() * 1000),
            "channel": "flask-socketio",
        }
    )


@live_bp.post("/streams/<live_stream_id>/end")
def end_live_stream(live_stream_id: str):
    room = _get_or_create_room(live_stream_id)
    room.status = "ended"
    db.session.add(
        LiveEvent(
            live_room_id=room.id,
            event_type="stream_ended",
            actor_id=room.host_id,
            payload_json={},
        )
    )
    db.session.commit()
    return _ok({"liveStreamId": room.id, "status": "ended"})
