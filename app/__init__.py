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
        }

    return app
