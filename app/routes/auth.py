from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from flask import Blueprint, current_app, request
from sqlalchemy import or_
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db
from ..models import User, UserSession, Wallet

auth_bp = Blueprint("auth", __name__)


def _ok(data: dict):
    return {"ok": True, "data": data}


def _error(message: str, status: int = 400):
    return {"ok": False, "error": {"message": message}}, status


def _serialize_user(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "displayName": user.display_name,
        "email": user.email,
        "kycTier": user.kyc_tier,
        "verifiedAccount": user.kyc_tier in {"tier_2", "tier_3"},
        "verifiedCategories": [],
        "extraVerifiedProductClasses": [],
        "verification": {
            "phone": {"level": "phone", "status": "verified" if user.kyc_tier != "tier_1" else "unverified"},
            "identity": {
                "level": "identity",
                "status": "verified" if user.kyc_tier in {"tier_2", "tier_3"} else "unverified",
            },
            "business": {
                "level": "business",
                "status": "verified" if user.kyc_tier == "tier_3" else "unverified",
            },
        },
    }


def _issue_session(user: User, provider: str = "password") -> UserSession:
    session = UserSession(
        id=secrets.token_urlsafe(32),
        user_id=user.id,
        provider=provider,
        is_active=True,
        last_seen_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=30),
    )
    db.session.add(session)
    db.session.commit()
    return session


def _extract_session() -> UserSession | None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.replace("Bearer ", "", 1).strip()
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


@auth_bp.post("/register")
def register_user():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username") or "").strip().lower()
    password = str(payload.get("password") or "")
    email = str(payload.get("email") or "").strip().lower() or None
    display_name = str(payload.get("displayName") or username).strip()

    if len(username) < 3:
        return _error("Username must be at least 3 characters")
    if len(password) < 6:
        return _error("Password must be at least 6 characters")

    filters = [User.username == username]
    if email:
        filters.append(User.email == email)
    existing = User.query.filter(or_(*filters)).first()
    if existing:
        return _error("User already exists", 409)

    user = User(
        id=f"user-{secrets.token_hex(8)}",
        username=username,
        display_name=display_name or username,
        email=email,
        password_hash=generate_password_hash(password),
        auth_provider="password",
    )
    db.session.add(user)
    db.session.flush()
    db.session.add(Wallet(user_id=user.id))
    db.session.commit()

    session = _issue_session(user)
    return _ok({"user": _serialize_user(user), "sessionToken": session.id})


@auth_bp.post("/login")
def login_user():
    payload = request.get_json(silent=True) or {}
    identifier = str(payload.get("identifier") or payload.get("username") or "").strip().lower()
    password = str(payload.get("password") or "")
    if not identifier or not password:
        return _error("Identifier and password are required")

    user = User.query.filter(
        (User.username == identifier) | (User.email == identifier)
    ).first()
    if not user or not user.password_hash:
        return _error("Invalid credentials", 401)
    if not check_password_hash(user.password_hash, password):
        return _error("Invalid credentials", 401)

    session = _issue_session(user)
    return _ok({"user": _serialize_user(user), "sessionToken": session.id})


@auth_bp.post("/logout")
def logout_user():
    session = _extract_session()
    if not session:
        return _error("Session not found", 401)
    session.is_active = False
    db.session.commit()
    return _ok({"loggedOut": True})


@auth_bp.get("/me")
def me():
    session = _extract_session()
    if not session:
        return _error("Unauthorized", 401)
    user = User.query.get(session.user_id)
    if not user:
        return _error("User not found", 404)
    return _ok({"user": _serialize_user(user), "sessionToken": session.id})


@auth_bp.post("/google")
def google_login():
    return _error(
        "Google sign-in backend is not configured yet. Add Google OAuth client credentials first.",
        501,
    )
