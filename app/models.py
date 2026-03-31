from datetime import datetime

from .extensions import db


class LiveRoom(db.Model):
    __tablename__ = "live_rooms"

    id = db.Column(db.String(64), primary_key=True)
    host_id = db.Column(db.String(64), nullable=False, index=True)
    host_name = db.Column(db.String(120), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(32), nullable=False, default="setup")
    scheduled_time = db.Column(db.BigInteger, nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class RoomParticipant(db.Model):
    __tablename__ = "room_participants"

    id = db.Column(db.Integer, primary_key=True)
    live_room_id = db.Column(db.String(64), db.ForeignKey("live_rooms.id"), nullable=False, index=True)
    user_id = db.Column(db.String(64), nullable=False, index=True)
    role = db.Column(db.String(32), nullable=False, default="viewer")
    connection_status = db.Column(db.String(32), nullable=False, default="joined")
    joined_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    left_at = db.Column(db.DateTime, nullable=True)


class LiveEvent(db.Model):
    __tablename__ = "live_events"

    id = db.Column(db.Integer, primary_key=True)
    live_room_id = db.Column(db.String(64), db.ForeignKey("live_rooms.id"), nullable=False, index=True)
    event_type = db.Column(db.String(64), nullable=False, index=True)
    actor_id = db.Column(db.String(64), nullable=True, index=True)
    payload_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.String(64), primary_key=True)
    username = db.Column(db.String(120), nullable=False, unique=True, index=True)
    display_name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(255), nullable=True, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=True)
    auth_provider = db.Column(db.String(32), nullable=False, default="password")
    google_sub = db.Column(db.String(255), nullable=True, unique=True, index=True)
    kyc_tier = db.Column(db.String(16), nullable=False, default="tier_1")
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class Wallet(db.Model):
    __tablename__ = "wallets"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=False, unique=True, index=True)
    currency = db.Column(db.String(8), nullable=False, default="NGN")
    available_balance = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    escrow_balance = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    pending_balance = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    earnings_balance = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    available_coins = db.Column(db.Integer, nullable=False, default=0)
    pending_coins = db.Column(db.Integer, nullable=False, default=0)
    earnings_coins = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class WalletLedgerEntry(db.Model):
    __tablename__ = "wallet_ledger_entries"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=False, index=True)
    transaction_id = db.Column(db.String(64), db.ForeignKey("transactions.id"), nullable=True, index=True)
    entry_kind = db.Column(db.String(64), nullable=False, index=True)
    balance_bucket = db.Column(db.String(32), nullable=False, index=True)
    direction = db.Column(db.String(16), nullable=False)
    amount = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    currency = db.Column(db.String(8), nullable=False, default="NGN")
    note = db.Column(db.String(255), nullable=True)
    metadata_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class RiskFlag(db.Model):
    __tablename__ = "risk_flags"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=True, index=True)
    transaction_id = db.Column(db.String(64), db.ForeignKey("transactions.id"), nullable=True, index=True)
    severity = db.Column(db.String(16), nullable=False, default="low")
    reason = db.Column(db.String(255), nullable=False)
    metadata_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class TransactionRecord(db.Model):
    __tablename__ = "transactions"

    id = db.Column(db.String(64), primary_key=True)
    type = db.Column(db.String(64), nullable=False, index=True)
    buyer_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=True, index=True)
    seller_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=True, index=True)
    source_type = db.Column(db.String(32), nullable=True, index=True)
    source_id = db.Column(db.String(64), nullable=True, index=True)
    status = db.Column(db.String(32), nullable=False, index=True)
    escrow_status = db.Column(db.String(32), nullable=True, index=True)
    amount = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    currency = db.Column(db.String(8), nullable=False, default="NGN")
    metadata_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class Conversation(db.Model):
    __tablename__ = "conversations"

    id = db.Column(db.String(64), primary_key=True)
    title = db.Column(db.String(255), nullable=False, default="Conversation")
    metadata_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class MessageRecord(db.Model):
    __tablename__ = "messages"

    id = db.Column(db.String(64), primary_key=True)
    conversation_id = db.Column(
        db.String(64),
        db.ForeignKey("conversations.id"),
        nullable=False,
        index=True,
    )
    sender_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=False, index=True)
    message_type = db.Column(db.String(32), nullable=False, default="text")
    body = db.Column(db.Text, nullable=False, default="")
    metadata_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class OrderRecord(db.Model):
    __tablename__ = "orders"

    id = db.Column(db.String(64), primary_key=True)
    buyer_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=False, index=True)
    seller_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    amount = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    currency = db.Column(db.String(8), nullable=False, default="NGN")
    status = db.Column(db.String(32), nullable=False, default="created", index=True)
    payment_status = db.Column(db.String(32), nullable=False, default="unpaid")
    fulfillment_status = db.Column(db.String(32), nullable=False, default="pending")
    escrow_status = db.Column(db.String(32), nullable=False, default="none")
    source = db.Column(db.String(32), nullable=True)
    live_id = db.Column(db.String(64), nullable=True, index=True)
    metadata_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class UserSession(db.Model):
    __tablename__ = "user_sessions"

    id = db.Column(db.String(128), primary_key=True)
    user_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=False, index=True)
    provider = db.Column(db.String(32), nullable=False, default="password")
    device_label = db.Column(db.String(120), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_seen_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)


class VerificationCase(db.Model):
    __tablename__ = "verification_cases"

    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=False, index=True)
    tier = db.Column(db.String(16), nullable=False, default="tier_1")
    status = db.Column(db.String(32), nullable=False, default="draft", index=True)
    provider = db.Column(db.String(64), nullable=True)
    metadata_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class ListingRecord(db.Model):
    __tablename__ = "listings"

    id = db.Column(db.String(64), primary_key=True)
    seller_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    price = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    currency = db.Column(db.String(8), nullable=False, default="NGN")
    kind = db.Column(db.String(32), nullable=False, default="product")
    status = db.Column(db.String(32), nullable=False, default="draft", index=True)
    metadata_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class ServiceRequestRecord(db.Model):
    __tablename__ = "service_requests"

    id = db.Column(db.String(64), primary_key=True)
    requester_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=False, index=True)
    provider_id = db.Column(db.String(64), db.ForeignKey("users.id"), nullable=True, index=True)
    request_type = db.Column(db.String(32), nullable=False, index=True)
    status = db.Column(db.String(32), nullable=False, default="created", index=True)
    title = db.Column(db.String(255), nullable=False, default="Service request")
    amount = db.Column(db.Numeric(18, 2), nullable=False, default=0)
    currency = db.Column(db.String(8), nullable=False, default="NGN")
    metadata_json = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
