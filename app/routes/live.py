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
        "updatedAt": int(room.updated_at.timestamp() * 1000) if room.updated_at else None,
        "createdAt": int(room.created_at.timestamp() * 1000) if room.created_at else None,
    }


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


@live_bp.get("/streams/<live_stream_id>")
def get_live_stream(live_stream_id: str):
    room = _get_or_create_room(live_stream_id)
    return _ok(_serialize_room(room))


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
    db.session.add(
        LiveEvent(
            live_room_id=str(payload.get("liveStreamId") or ""),
            event_type="gift_sent",
            actor_id=str(payload.get("senderId") or ""),
            payload_json=payload,
        )
    )
    db.session.commit()
    return _ok({**payload, "sentAt": int(datetime.utcnow().timestamp() * 1000)})


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
            "updatedAt": int(datetime.utcnow().timestamp() * 1000),
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
