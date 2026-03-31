from flask import request
from flask_socketio import SocketIO, emit, join_room, leave_room


def register_socket_handlers(socketio: SocketIO) -> None:
    @socketio.on("connect")
    def on_connect():
        emit("system", {"message": "connected", "socketId": request.sid})

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
        room_id = str((payload or {}).get("liveStreamId") or "")
        if not room_id:
            emit("error", {"message": "liveStreamId is required"})
            return
        emit("live_message", payload, to=room_id, include_self=True)

    @socketio.on("live_gift")
    def on_live_gift(payload):
        room_id = str((payload or {}).get("liveStreamId") or "")
        if not room_id:
            emit("error", {"message": "liveStreamId is required"})
            return
        emit("live_gift", payload, to=room_id, include_self=True)
