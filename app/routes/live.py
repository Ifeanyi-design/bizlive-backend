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


@live_bp.post("/streams/<live_stream_id>/validate")
def validate_token(live_stream_id: str):
    payload = request.get_json(silent=True) or {}
    token = payload.get("token")
    return _ok(
        {
            "liveStreamId": live_stream_id,
            "valid": bool(token),
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
