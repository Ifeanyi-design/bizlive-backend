from flask import Flask

from .config import Config
from .extensions import cors, db, migrate, socketio
from .routes.auth import auth_bp
from .routes.live import live_bp
from .routes.platform import platform_bp
from .sockets import register_socket_handlers


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    migrate.init_app(app, db)
    cors.init_app(app, resources={r"/api/*": {"origins": app.config["CORS_ORIGINS"]}})
    socketio.init_app(
        app,
        cors_allowed_origins=app.config["CORS_ORIGINS"],
        async_mode=app.config["SOCKETIO_ASYNC_MODE"],
        path=app.config["SOCKETIO_PATH"],
        ping_interval=app.config["SOCKETIO_PING_INTERVAL"],
        ping_timeout=app.config["SOCKETIO_PING_TIMEOUT"],
    )

    app.register_blueprint(auth_bp, url_prefix="/api/auth")
    app.register_blueprint(live_bp, url_prefix="/api/live")
    app.register_blueprint(platform_bp, url_prefix="/api")
    register_socket_handlers(socketio)

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "service": "bizlive-live-backend",
            "async_mode": app.config["SOCKETIO_ASYNC_MODE"],
            "socketio": {
                "path": app.config["SOCKETIO_PATH"],
                "pingInterval": app.config["SOCKETIO_PING_INTERVAL"],
                "pingTimeout": app.config["SOCKETIO_PING_TIMEOUT"],
            },
            "livekit": {
                "urlConfigured": bool(app.config.get("LIVEKIT_URL")),
                "apiKeyConfigured": bool(app.config.get("LIVEKIT_API_KEY")),
                "apiSecretConfigured": bool(app.config.get("LIVEKIT_API_SECRET")),
            },
        }

    return app
