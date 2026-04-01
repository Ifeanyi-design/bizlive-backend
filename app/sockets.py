from __future__ import annotations

from datetime import datetime

from flask import request
from flask_socketio import SocketIO, emit, join_room, leave_room

from .extensions import db
from .models import Conversation, MessageRecord, User, UserSession


def _normalize_participants(metadata: dict | None) -> set[str]:
    payload = metadata or {}
    participants = set()
    raw_ids = payload.get("participantIds") or []
    if isinstance(raw_ids, list):
        participants.update(str(item).strip() for item in raw_ids if str(item).strip())
    account_keys = payload.get("accountKeys") or []
    if isinstance(account_keys, list):
        participants.update(str(item).strip() for item in account_keys if str(item).strip())
    for key in ("buyerId", "sellerId", "title", "titleBuyer", "titleSeller"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            participants.add(value.strip())
    return participants


def _account_room(account_key: str) -> str:
    return f"account:{account_key}"


def _extract_socket_session(auth_payload: dict | None) -> UserSession | None:
    payload = auth_payload or {}
    token = str(payload.get("sessionToken") or request.args.get("sessionToken") or "").strip()
    if not token:
        return None
    session = UserSession.query.get(token)
    if not session or not session.is_active:
        return None
    if session.expires_at and session.expires_at < datetime.utcnow():
        session.is_active = False
        db.session.commit()
        return None
    session.last_seen_at = datetime.utcnow()
    db.session.commit()
    return session


def _socket_identity(auth_payload: dict | None) -> tuple[UserSession | None, User | None]:
    session = _extract_socket_session(auth_payload)
    if not session:
        return None, None
    user = User.query.get(session.user_id)
    return session, user


def _serialize_private_message(row: MessageRecord, sender: User | None) -> dict:
    metadata = row.metadata_json or {}
    return {
        "threadId": row.conversation_id,
        "id": row.id,
        "senderId": row.sender_id,
        "from": metadata.get("from")
        or (sender.display_name if sender and sender.display_name else None)
        or (sender.username if sender and sender.username else None)
        or row.sender_id,
        "text": row.body,
        "ts": int(row.created_at.timestamp() * 1000),
        "kind": metadata.get("kind") or row.message_type,
        "system": bool(
            row.message_type == "system"
            or metadata.get("system") is True
            or str(metadata.get("from") or row.sender_id) == "system"
        ),
        "meta": metadata.get("meta") if isinstance(metadata.get("meta"), dict) else metadata,
        "image": metadata.get("image"),
        "audio": metadata.get("audio"),
        "video": metadata.get("video"),
        "waveform": metadata.get("waveform"),
        "status": "delivered",
        "replyTo": metadata.get("replyTo"),
        "product": metadata.get("product"),
        "deal": metadata.get("deal"),
        "mentions": metadata.get("mentions"),
        "permanentUrl": metadata.get("permanentUrl"),
        "reactions": metadata.get("reactions"),
        "thread": metadata.get("thread"),
    }


def register_socket_handlers(socketio: SocketIO) -> None:
    @socketio.on("connect")
    def on_connect(auth=None):
        session, user = _socket_identity(auth)
        if session and user:
          join_room(_account_room(user.id))
          join_room(_account_room(user.username))
          emit(
              "system",
              {
                  "message": "connected",
                  "socketId": request.sid,
                  "userId": user.id,
                  "username": user.username,
                  "authenticated": True,
              },
          )
          return
        emit("system", {"message": "connected", "socketId": request.sid, "authenticated": False})

    @socketio.on("join_account")
    def on_join_account(payload):
        session, user = _socket_identity(payload)
        if not session or not user:
            emit("error", {"message": "Unauthorized"})
            return
        join_room(_account_room(user.id))
        join_room(_account_room(user.username))
        emit(
            "joined_account",
            {"userId": user.id, "username": user.username},
        )

    @socketio.on("join_live_room")
    def on_join_live_room(payload):
        room_id = str((payload or {}).get("liveStreamId") or "")
        user_id = str((payload or {}).get("userId") or "")
        if not room_id:
            emit("error", {"message": "liveStreamId is required"})
            return
        join_room(room_id)
        emit(
            "presence",
            {"liveStreamId": room_id, "userId": user_id, "status": "joined"},
            to=room_id,
        )

    @socketio.on("leave_live_room")
    def on_leave_live_room(payload):
        room_id = str((payload or {}).get("liveStreamId") or "")
        user_id = str((payload or {}).get("userId") or "")
        if not room_id:
            return
        leave_room(room_id)
        emit(
            "presence",
            {"liveStreamId": room_id, "userId": user_id, "status": "left"},
            to=room_id,
        )

    @socketio.on("live_message")
    def on_live_message(payload):
        live_room_id = str((payload or {}).get("liveStreamId") or (payload or {}).get("liveRoomId") or "")
        if not live_room_id:
            emit("error", {"message": "liveStreamId is required"})
            return
        outbound = {**(payload or {}), "liveStreamId": live_room_id}
        emit("live_message", outbound, to=live_room_id, include_self=True)

    @socketio.on("live_gift")
    def on_live_gift(payload):
        room_id = str((payload or {}).get("liveStreamId") or "")
        if not room_id:
            emit("error", {"message": "liveStreamId is required"})
            return
        emit("live_gift", payload, to=room_id, include_self=True)

    @socketio.on("private_message")
    def on_private_message(payload):
        session, user = _socket_identity(payload)
        if not session or not user:
            emit("error", {"message": "Unauthorized"})
            return

        thread_payload = payload.get("thread") or {}
        conversation_id = str(
            payload.get("threadId") or thread_payload.get("id") or ""
        ).strip()
        if not conversation_id:
            emit("error", {"message": "threadId is required"})
            return

        sender_id = str(payload.get("senderId") or user.id).strip()
        if sender_id != user.id and sender_id != user.username:
            emit("error", {"message": "Sender mismatch"})
            return

        normalized_thread_metadata = (
            {
                **thread_payload,
                "participantIds": list(_normalize_participants(thread_payload)),
            }
            if isinstance(thread_payload, dict)
            else {}
        )

        conversation = Conversation.query.get(conversation_id)
        if not conversation:
            conversation = Conversation(
                id=conversation_id,
                title=str(
                    normalized_thread_metadata.get("title")
                    or normalized_thread_metadata.get("titleBuyer")
                    or normalized_thread_metadata.get("titleSeller")
                    or "Conversation"
                ),
                metadata_json=normalized_thread_metadata,
            )
            db.session.add(conversation)
        elif normalized_thread_metadata:
            conversation.metadata_json = {
                **(conversation.metadata_json or {}),
                **normalized_thread_metadata,
            }
            conversation.title = str(
                normalized_thread_metadata.get("title")
                or conversation.title
                or "Conversation"
            )

        message = MessageRecord(
            id=str(payload.get("id") or ""),
            conversation_id=conversation_id,
            sender_id=user.id,
            message_type=str(payload.get("messageType") or payload.get("kind") or "text"),
            body=str(payload.get("body") or payload.get("text") or ""),
            metadata_json={
                **(payload.get("metadata") or {}),
                "status": "delivered",
            },
        )
        db.session.add(message)
        db.session.flush()
        conversation.updated_at = message.created_at
        db.session.commit()

        outbound = _serialize_private_message(message, user)
        participant_keys = _normalize_participants(conversation.metadata_json or {})
        participant_keys.update({user.id, user.username})
        emit(
            "message_ack",
            {
                "threadId": conversation_id,
                "messageId": message.id,
                "status": "delivered",
                "deliveredAt": int(message.created_at.timestamp() * 1000),
            },
            to=_account_room(user.id),
        )
        emit(
            "message_ack",
            {
                "threadId": conversation_id,
                "messageId": message.id,
                "status": "delivered",
                "deliveredAt": int(message.created_at.timestamp() * 1000),
            },
            to=_account_room(user.username),
        )
        emitted_rooms = set()
        for account_key in participant_keys:
            room = _account_room(account_key)
            if room in emitted_rooms:
                continue
            emit("private_message", outbound, to=room)
            emitted_rooms.add(room)

    @socketio.on("thread_read")
    def on_thread_read(payload):
        session, user = _socket_identity(payload)
        if not session or not user:
            emit("error", {"message": "Unauthorized"})
            return

        conversation_id = str((payload or {}).get("threadId") or "").strip()
        if not conversation_id:
            emit("error", {"message": "threadId is required"})
            return

        conversation = Conversation.query.get(conversation_id)
        if not conversation:
            emit("error", {"message": "Thread not found"})
            return

        read_at = int((payload or {}).get("readAt") or int(datetime.utcnow().timestamp() * 1000))
        metadata = conversation.metadata_json or {}
        read_receipts = dict(metadata.get("readReceipts") or {})
        read_receipts[user.id] = read_at
        read_receipts[user.username] = read_at
        metadata["readReceipts"] = read_receipts
        conversation.metadata_json = metadata
        db.session.commit()

        participant_keys = _normalize_participants(conversation.metadata_json or {})
        participant_keys.update({user.id, user.username})
        outbound = {
            "threadId": conversation_id,
            "readerId": user.id,
            "readerUsername": user.username,
            "readAt": read_at,
        }
        emitted_rooms = set()
        for account_key in participant_keys:
            room = _account_room(account_key)
            if room in emitted_rooms:
                continue
            emit("thread_read", outbound, to=room)
            emitted_rooms.add(room)
