# BizLive Backend

---
title: BizLive Backend
emoji: 🐍
colorFrom: "#FF0000"
colorTo: "#00FF00"
sdk: docker
sdk_version: "20.10.24"
app_file: Dockerfile
pinned: false
---

Separate Flask backend for BizLive app and live-room testing.

## Stack

- Flask
- Flask-SocketIO
- `gevent` async runtime
- Flask-SQLAlchemy
- LiveKit token generation
- PostgreSQL-ready configuration

## Why `gevent`

This backend is configured for `Flask-SocketIO` with `gevent`, which is usually the safer fast async option for this stack. It gives you websocket support and concurrency without forcing a full ASGI rewrite.

## Quick Start

1. Create a venv.
2. Install requirements.
3. Copy `.env.example` to `.env`.
4. Set `DATABASE_URL` to your Neon/Postgres connection string.
5. Fill in your LiveKit URL, API key, and secret.
6. Run `python run.py`.

Example local setup:

```powershell
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python run.py
```

## Frontend Connection

Set this in your Expo environment when you want the app to hit this backend instead of the mock live API:

`EXPO_PUBLIC_LIVE_API_BASE_URL=http://YOUR_HOST:5000`

For app-wide auth and other backend-backed platform calls, also set:

`EXPO_PUBLIC_API_BASE_URL=http://YOUR_HOST:5000`

## Endpoints

- `GET /api/health`
- `POST /api/auth/register`
- `POST /api/auth/login`
- `POST /api/auth/logout`
- `GET /api/auth/me`
- `POST /api/auth/google` (placeholder until OAuth provider config is added)
- `POST /api/users/bootstrap`
- `GET /api/wallets/<user_id>`
- `GET /api/wallets/<user_id>/ledger`
- `POST /api/wallets/<user_id>/withdraw`
- `GET /api/transactions`
- `POST /api/transactions`
- `PUT /api/transactions/<id>`
- `POST /api/transactions/<id>/pay`
- `POST /api/transactions/<id>/release`
- `POST /api/transactions/<id>/refund`
- `POST /api/transactions/<id>/dispute`
- `GET /api/orders`
- `POST /api/orders`
- `PUT /api/orders/<id>`
- `GET /api/threads`
- `GET /api/threads/<id>/messages`
- `POST /api/threads/<id>/messages`
- `GET /api/conversations/<id>/messages`
- `POST /api/conversations/<id>/messages`
- `POST /api/live/streams`
- `POST /api/live/schedule`
- `POST /api/live/streams/<id>/start`
- `POST /api/live/streams/<id>/validate`
- `POST /api/live/streams/<id>/join`
- `POST /api/live/streams/<id>/leave`
- `POST /api/live/cohosts/invite`
- `POST /api/live/cohosts/respond`
- `POST /api/live/gifts`
- `POST /api/live/push`
- `POST /api/live/streams/<id>/end`

## Socket.IO Events

Client emits:

- `join_live_room`
- `leave_live_room`
- `live_message`
- `live_gift`

Server emits:

- `system`
- `presence`
- `live_message`
- `live_gift`
- `error`

## PostgreSQL Notes

- Neon is a good fit for this backend and works well with GitHub Student benefits.
- Use a connection string like:

```text
postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require
```

- The config automatically normalizes older `postgres://` URLs.
- This backend currently uses `db.create_all()` for bootstrap. That is fine for early testing, but production should move to Alembic migrations.

## Hosting

If you want to copy `backend/` into a separate repo and deploy from there, that is a good idea.

Recommended structure for the backend-only repo:

```text
bizlive-backend/
  app/
  run.py
  requirements.txt
  Dockerfile
  .env.example
  README.md
```

Recommended hosting path:

1. Create a new GitHub repo just for the backend.
2. Copy the contents of `backend/` into that repo root.
3. Add your `.env` values in the host platform secrets.
4. Deploy with Docker on Hugging Face Spaces, Railway, Render, or Fly.io.

For Hugging Face Docker Space:

1. Create a new Space.
2. Choose `Docker`.
3. Push the backend-only repo to the Space-linked GitHub repo, or push directly to the Space remote.
4. Set secrets for `DATABASE_URL`, `SECRET_KEY`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET`.
5. Expose port `5000`.

For the frontend app:

- `EXPO_PUBLIC_API_BASE_URL=https://your-backend-host`
- `EXPO_PUBLIC_LIVE_API_BASE_URL=https://your-backend-host`

## Notes

- This is the first backend test scaffold, not the full production fraud/risk system.
- SQLite works for local testing. For real hosting, point `DATABASE_URL` at Postgres. Neon is a good fit here and works well with GitHub Student credits.
- For real auth, sessions, and money data, Postgres is the right target. Neon is a strong free starting point, especially with GitHub Student benefits.
- LiveKit media still runs through your LiveKit server. This Flask app issues tokens and handles room-side app events.
- Google sign-in is possible, but not fully wired yet. The backend placeholder exists, and the frontend still needs the real Expo OAuth flow plus Google credentials.
