from __future__ import annotations

import secrets
import json
from datetime import datetime, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from flask import Blueprint, current_app, redirect, request
from sqlalchemy import or_
from werkzeug.security import check_password_hash, generate_password_hash

from ..extensions import db
from ..models import User, UserSession, Wallet

auth_bp = Blueprint("auth", __name__)
google_state_store: dict[str, str] = {}


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


def _google_enabled() -> bool:
    return bool(
        current_app.config.get("GOOGLE_OAUTH_CLIENT_ID")
        and current_app.config.get("GOOGLE_OAUTH_CLIENT_SECRET")
        and current_app.config.get("GOOGLE_OAUTH_CALLBACK_URL")
    )


def _upsert_google_user(profile: dict) -> User:
    sub = str(profile.get("sub") or "").strip()
    email = str(profile.get("email") or "").strip().lower() or None
    name = str(profile.get("name") or profile.get("given_name") or "Google User").strip()
    username_seed = (
        str(profile.get("email") or "").split("@")[0]
        or str(profile.get("given_name") or "google").lower()
        or "google"
    )
    username = "".join(ch for ch in username_seed.lower() if ch.isalnum() or ch in {"_", "."})[:40] or "google"

    user = None
    if sub:
        user = User.query.filter_by(google_sub=sub).first()
    if not user and email:
        user = User.query.filter_by(email=email).first()
    if not user:
        suffix = secrets.token_hex(3)
        candidate = f"{username}_{suffix}"
        user = User(
            id=f"user-{secrets.token_hex(8)}",
            username=candidate,
            display_name=name or candidate,
            email=email,
            auth_provider="google",
            google_sub=sub or None,
        )
        db.session.add(user)
        db.session.flush()
        db.session.add(Wallet(user_id=user.id))
    else:
        user.display_name = name or user.display_name
        user.email = email or user.email
        user.auth_provider = "google"
        if sub:
            user.google_sub = sub
    db.session.commit()
    return user


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
        "Use /api/auth/google/start for browser OAuth or configure mobile auth session flow.",
        405,
    )


@auth_bp.get("/google/start")
def google_start():
    if not _google_enabled():
        return _error("Google OAuth is not configured on the backend", 501)

    mobile_redirect_uri = str(request.args.get("mobile_redirect_uri") or "").strip()
    if not mobile_redirect_uri:
        return _error("mobile_redirect_uri is required")

    state = secrets.token_urlsafe(24)
    google_state_store[state] = mobile_redirect_uri
    params = {
        "client_id": current_app.config["GOOGLE_OAUTH_CLIENT_ID"],
        "redirect_uri": current_app.config["GOOGLE_OAUTH_CALLBACK_URL"],
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
        "state": state,
    }
    return _ok(
        {
            "authUrl": f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}",
            "state": state,
        }
    )


@auth_bp.get("/google/callback")
def google_callback():
    if not _google_enabled():
        return _error("Google OAuth is not configured on the backend", 501)

    code = str(request.args.get("code") or "").strip()
    state = str(request.args.get("state") or "").strip()
    if not code or not state:
        return _error("Missing OAuth code or state")

    mobile_redirect_uri = google_state_store.pop(state, None)
    if not mobile_redirect_uri:
        return _error("OAuth state expired or is invalid", 400)

    try:
        token_payload = urlencode(
            {
                "code": code,
                "client_id": current_app.config["GOOGLE_OAUTH_CLIENT_ID"],
                "client_secret": current_app.config["GOOGLE_OAUTH_CLIENT_SECRET"],
                "redirect_uri": current_app.config["GOOGLE_OAUTH_CALLBACK_URL"],
                "grant_type": "authorization_code",
            }
        ).encode("utf-8")
        token_request = Request(
            "https://oauth2.googleapis.com/token",
            data=token_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urlopen(token_request) as response:
            token_response = json.loads(response.read().decode("utf-8"))

        access_token = str(token_response.get("access_token") or "").strip()
        if not access_token:
            raise ValueError("Google token exchange failed")

        profile_request = Request(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urlopen(profile_request) as response:
            profile = json.loads(response.read().decode("utf-8"))

        user = _upsert_google_user(profile)
        session = _issue_session(user, provider="google")
        params = urlencode(
            {
                "sessionToken": session.id,
                "userId": user.id,
                "username": user.username,
            }
        )
        return redirect(f"{mobile_redirect_uri}?{params}")
    except Exception as error:
        return redirect(
            f"{mobile_redirect_uri}?{urlencode({'error': str(error)})}"
        )
