from app import create_app
from app.extensions import db, socketio

app = create_app()

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=app.config.get("DEBUG", False),
    )
