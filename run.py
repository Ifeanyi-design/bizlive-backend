from app import create_app
from app.extensions import db, socketio
import os

app = create_app()

if os.getenv("AUTO_CREATE_TABLES", "1") == "1":
    with app.app_context():
        db.create_all()

@app.route("/")
def indexes():
    return "BizLive backend running!"

if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=7860,
        debug=app.config.get("DEBUG", False),
    )
