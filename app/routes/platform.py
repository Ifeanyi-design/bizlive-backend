from __future__ import annotations

import os
import uuid
from decimal import Decimal

from flask import Blueprint, request, send_from_directory
from werkzeug.utils import secure_filename

from ..extensions import db
from ..models import (
    Conversation,
    ListingRecord,
    MessageRecord,
    OrderRecord,
    RiskFlag,
    ServiceRequestRecord,
    TransactionRecord,
    User,
    VerificationCase,
    Wallet,
    WalletLedgerEntry,
)

platform_bp = Blueprint("platform", __name__)
HIGH_RISK_AMOUNT = Decimal("500000")


def _ok(data: dict):
    return {"ok": True, "data": data}


def _err(message: str, status: int = 400):
    return {"ok": False, "error": {"message": message}}, status


def _wallet_for(user_id: str | None, currency: str = "NGN") -> Wallet | None:
    if not user_id:
        return None
    wallet = Wallet.query.filter_by(user_id=user_id).first()
    if wallet:
        return wallet
    user = User.query.get(user_id)
    if not user:
        return None
    wallet = Wallet(user_id=user_id, currency=currency)
    db.session.add(wallet)
    db.session.flush()
    return wallet


def _add_ledger_entry(
    user_id: str,
    transaction_id: str | None,
    entry_kind: str,
    bucket: str,
    direction: str,
    amount: Decimal,
    currency: str,
    note: str,
    metadata: dict | None = None,
):
    db.session.add(
        WalletLedgerEntry(
            user_id=user_id,
            transaction_id=transaction_id,
            entry_kind=entry_kind,
            balance_bucket=bucket,
            direction=direction,
            amount=amount,
            currency=currency,
            note=note,
            metadata_json=metadata or {},
        )
    )


def _log_risk_flag(
    *,
    user_id: str | None = None,
    transaction_id: str | None = None,
    severity: str,
    reason: str,
    metadata: dict | None = None,
):
    db.session.add(
        RiskFlag(
            user_id=user_id,
            transaction_id=transaction_id,
            severity=severity,
            reason=reason,
            metadata_json=metadata or {},
        )
    )


def _serialize_message(row: MessageRecord) -> dict:
    metadata = row.metadata_json or {}
    return {
        "id": row.id,
        "conversationId": row.conversation_id,
        "senderId": row.sender_id,
        "messageType": row.message_type,
        "body": row.body,
        "metadata": {
            **metadata,
            "status": metadata.get("status") or "delivered",
        },
        "createdAt": row.created_at.isoformat(),
    }


def _normalize_participants(metadata: dict | None) -> set[str]:
    payload = metadata or {}
    participants = set()
    raw_ids = payload.get("participantIds") or []
    if isinstance(raw_ids, list):
        participants.update(str(item).strip() for item in raw_ids if str(item).strip())
    for key in ("buyerId", "sellerId", "title", "titleBuyer", "titleSeller"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            participants.add(value.strip())
    account_keys = payload.get("accountKeys") or []
    if isinstance(account_keys, list):
        participants.update(str(item).strip() for item in account_keys if str(item).strip())
    return participants


def _conversation_visible_to_user(
    row: Conversation,
    *,
    user_id: str | None,
    username: str | None,
) -> bool:
    if not user_id and not username:
        return True
    participants = _normalize_participants(row.metadata_json or {})
    if user_id and user_id in participants:
        return True
    if username and username in participants:
        return True

    recent_senders = (
        db.session.query(MessageRecord.sender_id)
        .filter(MessageRecord.conversation_id == row.id)
        .distinct()
        .limit(12)
        .all()
    )
    sender_ids = {sender_id for (sender_id,) in recent_senders if sender_id}
    if user_id and user_id in sender_ids:
        return True
    if username and username in sender_ids:
        return True
    return False


@platform_bp.post("/users/bootstrap")
def bootstrap_user():
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("id") or "").strip()
    username = str(payload.get("username") or user_id or "").strip()
    if not user_id or not username:
        return {"ok": False, "error": {"message": "id and username are required"}}, 400

    user = User.query.get(user_id)
    if not user:
        user = User(
            id=user_id,
            username=username,
            display_name=str(payload.get("displayName") or username),
            email=payload.get("email"),
            kyc_tier=str(payload.get("kycTier") or "tier_1"),
        )
        db.session.add(user)

    wallet = Wallet.query.filter_by(user_id=user_id).first()
    if not wallet:
        wallet = Wallet(user_id=user_id)
        db.session.add(wallet)

    db.session.commit()
    return _ok({"userId": user_id, "walletCreated": True})


@platform_bp.get("/wallets/<user_id>")
def get_wallet(user_id: str):
    wallet = Wallet.query.filter_by(user_id=user_id).first()
    if not wallet:
        return {"ok": False, "error": {"message": "Wallet not found"}}, 404

    return _ok(
        {
            "userId": user_id,
            "currency": wallet.currency,
            "availableBalance": float(wallet.available_balance),
            "escrowBalance": float(wallet.escrow_balance),
            "pendingBalance": float(wallet.pending_balance),
            "earningsBalance": float(wallet.earnings_balance),
            "availableCoins": wallet.available_coins,
            "pendingCoins": wallet.pending_coins,
            "earningsCoins": wallet.earnings_coins,
        }
    )


@platform_bp.get("/wallets/<user_id>/ledger")
def get_wallet_ledger(user_id: str):
    rows = (
        WalletLedgerEntry.query.filter_by(user_id=user_id)
        .order_by(WalletLedgerEntry.created_at.desc())
        .limit(200)
        .all()
    )
    return _ok(
        {
            "items": [
                {
                    "id": row.id,
                    "transactionId": row.transaction_id,
                    "entryKind": row.entry_kind,
                    "balanceBucket": row.balance_bucket,
                    "direction": row.direction,
                    "amount": float(row.amount),
                    "currency": row.currency,
                    "note": row.note,
                    "metadata": row.metadata_json,
                    "createdAt": row.created_at.isoformat(),
                }
                for row in rows
            ]
        }
    )


@platform_bp.post("/wallets/<user_id>/withdraw")
def withdraw_wallet(user_id: str):
    payload = request.get_json(silent=True) or {}
    amount = Decimal(str(payload.get("amount") or 0))
    if amount <= 0:
        return _err("amount must be greater than zero")

    wallet = _wallet_for(user_id)
    if not wallet:
        return _err("Wallet not found", 404)
    if Decimal(wallet.available_balance) < amount:
        return _err("Insufficient available balance", 409)

    wallet.available_balance = Decimal(wallet.available_balance) - amount
    wallet.pending_balance = Decimal(wallet.pending_balance) + amount
    _add_ledger_entry(
        user_id,
        None,
        "withdrawal_request",
        "available",
        "debit",
        amount,
        wallet.currency,
        "Withdrawal requested",
    )
    _add_ledger_entry(
        user_id,
        None,
        "withdrawal_request",
        "pending",
        "credit",
        amount,
        wallet.currency,
        "Withdrawal pending settlement",
    )
    db.session.commit()
    return _ok({"userId": user_id, "amount": float(amount), "status": "pending"})


@platform_bp.get("/threads")
def list_threads():
    rows = Conversation.query.order_by(Conversation.updated_at.desc()).limit(100).all()
    requested_kind = request.args.get("kind")
    user_id = request.args.get("userId")
    username = request.args.get("username")
    items = []
    for row in rows:
        if not _conversation_visible_to_user(row, user_id=user_id, username=username):
            continue
        metadata = row.metadata_json or {}
        kind = metadata.get("kind", "chat")
        if requested_kind and kind != requested_kind:
            continue
        last_message = (
            MessageRecord.query.filter_by(conversation_id=row.id)
            .order_by(MessageRecord.created_at.desc())
            .first()
        )
        items.append(
            {
                "id": row.id,
                "title": metadata.get("title") or row.title,
                "titleBuyer": metadata.get("titleBuyer"),
                "titleSeller": metadata.get("titleSeller"),
                "buyerId": metadata.get("buyerId"),
                "sellerId": metadata.get("sellerId"),
                "orderId": metadata.get("orderId"),
                "kind": kind,
                "last": last_message.body if last_message else metadata.get("last", ""),
                "lastTs": int(
                    (last_message.created_at if last_message else row.updated_at).timestamp() * 1000
                ),
                "unread": int(metadata.get("unread", 0) or 0),
                "muted": bool(metadata.get("muted", False)),
                "pinned": bool(metadata.get("pinned", False)),
                "verified": bool(metadata.get("verified", False)),
                "online": bool(metadata.get("online", False)),
                "lastSeenTs": metadata.get("lastSeenTs"),
                "activeOrderId": metadata.get("activeOrderId"),
                "tag": metadata.get("tag"),
                "live": metadata.get("live"),
            }
        )
    return _ok(
        {"items": items}
    )


@platform_bp.get("/transactions")
def list_transactions():
    user_id = request.args.get("userId")
    query = TransactionRecord.query.order_by(TransactionRecord.created_at.desc())
    if user_id:
        query = query.filter(
            (TransactionRecord.buyer_id == user_id) | (TransactionRecord.seller_id == user_id)
        )
    rows = query.limit(100).all()
    return _ok(
        {
            "items": [
                {
                    "id": row.id,
                    "type": row.type,
                    "buyerId": row.buyer_id,
                    "sellerId": row.seller_id,
                    "sourceType": row.source_type,
                    "sourceId": row.source_id,
                    "status": row.status,
                    "escrowStatus": row.escrow_status,
                    "amount": float(row.amount),
                    "currency": row.currency,
                    "metadata": row.metadata_json,
                    "createdAt": row.created_at.isoformat(),
                }
                for row in rows
            ]
        }
    )


@platform_bp.post("/transactions")
def create_transaction():
    payload = request.get_json(silent=True) or {}
    tx = TransactionRecord(
        id=str(payload.get("id")),
        type=str(payload.get("type") or "generic"),
        buyer_id=payload.get("buyerId"),
        seller_id=payload.get("sellerId"),
        source_type=payload.get("sourceType"),
        source_id=payload.get("sourceId"),
        status=str(payload.get("status") or "PENDING_PAYMENT"),
        escrow_status=payload.get("escrowStatus"),
        amount=Decimal(str(payload.get("amount") or 0)),
        currency=str(payload.get("currency") or "NGN"),
        metadata_json=payload.get("metadata") or {},
    )
    db.session.add(tx)
    if tx.amount >= HIGH_RISK_AMOUNT:
        _log_risk_flag(
            user_id=tx.buyer_id,
            transaction_id=tx.id,
            severity="medium",
            reason="High-value transaction created",
            metadata={"amount": float(tx.amount), "type": tx.type},
        )
    db.session.commit()
    return _ok({"transactionId": tx.id})


@platform_bp.put("/transactions/<transaction_id>")
def update_transaction(transaction_id: str):
    payload = request.get_json(silent=True) or {}
    tx = TransactionRecord.query.get(transaction_id)
    if not tx:
        return {"ok": False, "error": {"message": "Transaction not found"}}, 404

    if "status" in payload:
        tx.status = str(payload["status"])
    if "escrowStatus" in payload:
        tx.escrow_status = str(payload["escrowStatus"])
    if "metadata" in payload:
        tx.metadata_json = payload.get("metadata") or {}

    db.session.commit()
    return _ok({"transactionId": tx.id, "updated": True})


@platform_bp.post("/transactions/<transaction_id>/pay")
def pay_transaction(transaction_id: str):
    tx = TransactionRecord.query.get(transaction_id)
    if not tx:
        return _err("Transaction not found", 404)
    if not tx.buyer_id:
        return _err("Transaction buyer is missing")

    buyer_wallet = _wallet_for(tx.buyer_id, tx.currency)
    if not buyer_wallet:
        return _err("Buyer wallet not found", 404)

    amount = Decimal(tx.amount)
    if tx.status == "ESCROWED" or tx.escrow_status == "held":
        return _ok({"transactionId": tx.id, "status": tx.status, "escrowStatus": tx.escrow_status})
    if Decimal(buyer_wallet.available_balance) < amount:
        return _err("Insufficient available balance", 409)

    buyer_wallet.available_balance = Decimal(buyer_wallet.available_balance) - amount
    buyer_wallet.escrow_balance = Decimal(buyer_wallet.escrow_balance) + amount
    tx.status = "ESCROWED"
    tx.escrow_status = "held"
    tx.metadata_json = tx.metadata_json or {}
    _add_ledger_entry(
        tx.buyer_id,
        tx.id,
        "buyer_payment",
        "available",
        "debit",
        amount,
        tx.currency,
        "Transaction funded",
    )
    _add_ledger_entry(
        tx.buyer_id,
        tx.id,
        "escrow_hold",
        "escrow",
        "credit",
        amount,
        tx.currency,
        "Escrow funded",
    )
    if amount >= HIGH_RISK_AMOUNT:
        _log_risk_flag(
            user_id=tx.buyer_id,
            transaction_id=tx.id,
            severity="high",
            reason="High-value payment secured into escrow",
            metadata={"amount": float(amount)},
        )
    db.session.commit()
    return _ok({"transactionId": tx.id, "status": tx.status, "escrowStatus": tx.escrow_status})


@platform_bp.post("/transactions/<transaction_id>/release")
def release_transaction_escrow(transaction_id: str):
    tx = TransactionRecord.query.get(transaction_id)
    if not tx:
        return _err("Transaction not found", 404)
    if not tx.buyer_id or not tx.seller_id:
        return _err("Transaction buyer or seller is missing")

    buyer_wallet = _wallet_for(tx.buyer_id, tx.currency)
    seller_wallet = _wallet_for(tx.seller_id, tx.currency)
    if not buyer_wallet or not seller_wallet:
        return _err("Wallet not found", 404)

    amount = Decimal(tx.amount)
    if tx.status == "COMPLETED" or tx.escrow_status == "released":
        return _ok({"transactionId": tx.id, "status": tx.status, "escrowStatus": tx.escrow_status})
    if Decimal(buyer_wallet.escrow_balance) < amount:
        return _err("Insufficient escrow balance", 409)

    buyer_wallet.escrow_balance = Decimal(buyer_wallet.escrow_balance) - amount
    seller_wallet.available_balance = Decimal(seller_wallet.available_balance) + amount
    seller_wallet.earnings_balance = Decimal(seller_wallet.earnings_balance) + amount
    tx.status = "COMPLETED"
    tx.escrow_status = "released"
    _add_ledger_entry(
        tx.buyer_id,
        tx.id,
        "escrow_release",
        "escrow",
        "debit",
        amount,
        tx.currency,
        "Escrow released",
    )
    _add_ledger_entry(
        tx.seller_id,
        tx.id,
        "seller_payout",
        "available",
        "credit",
        amount,
        tx.currency,
        "Escrow released to seller available balance",
    )
    _add_ledger_entry(
        tx.seller_id,
        tx.id,
        "seller_payout",
        "earnings",
        "credit",
        amount,
        tx.currency,
        "Escrow released to seller earnings",
    )
    db.session.commit()
    return _ok({"transactionId": tx.id, "status": tx.status, "escrowStatus": tx.escrow_status})


@platform_bp.post("/transactions/<transaction_id>/refund")
def refund_transaction(transaction_id: str):
    payload = request.get_json(silent=True) or {}
    tx = TransactionRecord.query.get(transaction_id)
    if not tx:
        return _err("Transaction not found", 404)
    if not tx.buyer_id:
        return _err("Transaction buyer is missing")

    buyer_wallet = _wallet_for(tx.buyer_id, tx.currency)
    if not buyer_wallet:
        return _err("Buyer wallet not found", 404)

    amount = Decimal(str(payload.get("refundAmount") or tx.amount or 0))
    if tx.status in {"REFUNDED", "PARTIALLY_REFUNDED"} or tx.escrow_status == "refunded":
        return _ok(
            {
                "transactionId": tx.id,
                "status": tx.status,
                "escrowStatus": tx.escrow_status,
                "refundAmount": float(amount),
            }
        )
    if amount <= 0:
        return _err("refundAmount must be greater than zero")
    if Decimal(buyer_wallet.escrow_balance) < amount:
        return _err("Insufficient escrow balance", 409)

    buyer_wallet.escrow_balance = Decimal(buyer_wallet.escrow_balance) - amount
    buyer_wallet.available_balance = Decimal(buyer_wallet.available_balance) + amount
    tx.status = "PARTIALLY_REFUNDED" if amount < Decimal(tx.amount) else "REFUNDED"
    tx.escrow_status = "refunded"
    _add_ledger_entry(
        tx.buyer_id,
        tx.id,
        "escrow_refund",
        "escrow",
        "debit",
        amount,
        tx.currency,
        "Escrow refunded",
    )
    _add_ledger_entry(
        tx.buyer_id,
        tx.id,
        "escrow_refund",
        "available",
        "credit",
        amount,
        tx.currency,
        "Refund returned to available balance",
    )
    db.session.commit()
    return _ok(
        {
            "transactionId": tx.id,
            "status": tx.status,
            "escrowStatus": tx.escrow_status,
            "refundAmount": float(amount),
        }
    )


@platform_bp.post("/transactions/<transaction_id>/dispute")
def dispute_transaction(transaction_id: str):
    payload = request.get_json(silent=True) or {}
    tx = TransactionRecord.query.get(transaction_id)
    if not tx:
        return _err("Transaction not found", 404)

    tx.status = "DISPUTED"
    tx.escrow_status = tx.escrow_status or "held"
    tx.metadata_json = {
        **(tx.metadata_json or {}),
        "dispute": payload or {},
    }
    _log_risk_flag(
        user_id=tx.buyer_id,
        transaction_id=tx.id,
        severity="medium",
        reason="Transaction dispute opened",
        metadata=payload or {},
    )
    db.session.commit()
    return _ok({"transactionId": tx.id, "status": tx.status, "escrowStatus": tx.escrow_status})


@platform_bp.get("/orders")
def list_orders():
    user_id = request.args.get("userId")
    query = OrderRecord.query.order_by(OrderRecord.created_at.desc())
    if user_id:
        query = query.filter((OrderRecord.buyer_id == user_id) | (OrderRecord.seller_id == user_id))
    rows = query.limit(100).all()
    return _ok(
        {
            "items": [
                {
                    "id": row.id,
                    "buyerId": row.buyer_id,
                    "sellerId": row.seller_id,
                    "title": row.title,
                    "amount": float(row.amount),
                    "currency": row.currency,
                    "status": row.status,
                    "paymentStatus": row.payment_status,
                    "fulfillmentStatus": row.fulfillment_status,
                    "escrowStatus": row.escrow_status,
                    "source": row.source,
                    "liveId": row.live_id,
                    "metadata": row.metadata_json,
                    "createdAt": row.created_at.isoformat(),
                }
                for row in rows
            ]
        }
    )


@platform_bp.post("/orders")
def create_order():
    payload = request.get_json(silent=True) or {}
    order = OrderRecord(
        id=str(payload.get("id")),
        buyer_id=str(payload.get("buyerId")),
        seller_id=str(payload.get("sellerId")),
        title=str(payload.get("title") or "Order"),
        amount=Decimal(str(payload.get("amount") or 0)),
        currency=str(payload.get("currency") or "NGN"),
        status=str(payload.get("status") or "created"),
        payment_status=str(payload.get("paymentStatus") or "unpaid"),
        fulfillment_status=str(payload.get("fulfillmentStatus") or "pending"),
        escrow_status=str(payload.get("escrowStatus") or "none"),
        source=payload.get("source"),
        live_id=payload.get("liveId"),
        metadata_json=payload.get("metadata") or {},
    )
    db.session.add(order)
    db.session.commit()
    return _ok({"orderId": order.id})


@platform_bp.put("/orders/<order_id>")
def update_order(order_id: str):
    payload = request.get_json(silent=True) or {}
    order = OrderRecord.query.get(order_id)
    if not order:
        return {"ok": False, "error": {"message": "Order not found"}}, 404

    if "status" in payload:
        order.status = str(payload["status"])
    if "paymentStatus" in payload:
        order.payment_status = str(payload["paymentStatus"])
    if "fulfillmentStatus" in payload:
        order.fulfillment_status = str(payload["fulfillmentStatus"])
    if "escrowStatus" in payload:
        order.escrow_status = str(payload["escrowStatus"])
    if "metadata" in payload:
        order.metadata_json = payload.get("metadata") or {}

    db.session.commit()
    return _ok({"orderId": order.id, "updated": True})


@platform_bp.get("/conversations/<conversation_id>/messages")
def get_messages(conversation_id: str):
    conversation = Conversation.query.get(conversation_id)
    if not conversation:
        return _err("Thread not found", 404)
    user_id = request.args.get("userId")
    username = request.args.get("username")
    if not _conversation_visible_to_user(conversation, user_id=user_id, username=username):
        return _err("Thread not found", 404)
    rows = (
        MessageRecord.query.filter_by(conversation_id=conversation_id)
        .order_by(MessageRecord.created_at.desc())
        .limit(200)
        .all()
    )
    return _ok({"items": [_serialize_message(row) for row in rows]})


@platform_bp.put("/threads/<conversation_id>")
def upsert_thread(conversation_id: str):
    payload = request.get_json(silent=True) or {}
    metadata = payload if isinstance(payload, dict) else {}
    metadata["participantIds"] = list(_normalize_participants(metadata))
    conversation = Conversation.query.get(conversation_id)
    if not conversation:
        conversation = Conversation(
            id=conversation_id,
            title=str(payload.get("title") or "Conversation"),
            metadata_json=metadata,
        )
        db.session.add(conversation)
    else:
        conversation.title = str(payload.get("title") or conversation.title or "Conversation")
        conversation.metadata_json = {
            **(conversation.metadata_json or {}),
            **metadata,
        }
    db.session.commit()
    return _ok({"threadId": conversation.id, "updated": True})


@platform_bp.post("/threads/<conversation_id>/presence")
def update_thread_presence(conversation_id: str):
    payload = request.get_json(silent=True) or {}
    conversation = Conversation.query.get(conversation_id)
    if not conversation:
        return _err("Thread not found", 404)
    conversation.metadata_json = {
        **(conversation.metadata_json or {}),
        "online": bool(payload.get("online", False)),
        "lastSeenTs": payload.get("lastSeenTs"),
    }
    db.session.commit()
    return _ok({"threadId": conversation.id, "updated": True})


@platform_bp.post("/threads/<conversation_id>/moderation")
def moderate_thread(conversation_id: str):
    payload = request.get_json(silent=True) or {}
    conversation = Conversation.query.get(conversation_id)
    if not conversation:
        return _err("Thread not found", 404)
    moderation = {
        **((conversation.metadata_json or {}).get("moderation") or {}),
        **payload,
    }
    conversation.metadata_json = {
        **(conversation.metadata_json or {}),
        "moderation": moderation,
    }
    db.session.commit()
    return _ok({"threadId": conversation.id, "moderation": moderation})


@platform_bp.post("/conversations/<conversation_id>/messages")
def post_message(conversation_id: str):
    payload = request.get_json(silent=True) or {}
    thread_payload = payload.get("thread") or {}
    normalized_thread_metadata = (
        {**thread_payload, "participantIds": list(_normalize_participants(thread_payload))}
        if isinstance(thread_payload, dict)
        else {}
    )
    conversation = Conversation.query.get(conversation_id)
    if not conversation:
        conversation = Conversation(
            id=conversation_id,
            title=str(payload.get("title") or "Conversation"),
            metadata_json=normalized_thread_metadata,
        )
        db.session.add(conversation)
    elif normalized_thread_metadata:
        conversation.metadata_json = {
            **(conversation.metadata_json or {}),
            **normalized_thread_metadata,
        }
        conversation.title = str(
            normalized_thread_metadata.get("title") or conversation.title or "Conversation"
        )

    metadata = payload.get("metadata") or {}
    if isinstance(metadata, dict):
        metadata = {
            **metadata,
            "status": "delivered",
        }

    message = MessageRecord(
        id=str(payload.get("id")),
        conversation_id=conversation_id,
        sender_id=str(payload.get("senderId")),
        message_type=str(payload.get("messageType") or "text"),
        body=str(payload.get("body") or ""),
        metadata_json=metadata,
    )
    db.session.add(message)
    db.session.flush()
    conversation.updated_at = message.created_at
    db.session.commit()
    return _ok({"messageId": message.id})


@platform_bp.post("/threads/<conversation_id>/read")
def mark_thread_read(conversation_id: str):
    payload = request.get_json(silent=True) or {}
    conversation = Conversation.query.get(conversation_id)
    if not conversation:
        return _err("Thread not found", 404)

    user_id = str(payload.get("userId") or "").strip()
    username = str(payload.get("username") or "").strip()
    reader_key = user_id or username
    if not reader_key:
        return _err("userId or username is required")

    metadata = conversation.metadata_json or {}
    read_receipts = dict(metadata.get("readReceipts") or {})
    read_at = int(payload.get("readAt") or int(datetime.utcnow().timestamp() * 1000))
    read_receipts[reader_key] = read_at
    metadata["readReceipts"] = read_receipts
    conversation.metadata_json = metadata
    db.session.commit()
    return _ok({"threadId": conversation_id, "readerId": reader_key, "readAt": read_at})


@platform_bp.get("/threads/<conversation_id>/messages")
def get_thread_messages_alias(conversation_id: str):
    return get_messages(conversation_id)


@platform_bp.post("/threads/<conversation_id>/messages")
def post_thread_message_alias(conversation_id: str):
    return post_message(conversation_id)


@platform_bp.post("/threads/<conversation_id>/messages/<message_id>/reactions")
def react_to_message(conversation_id: str, message_id: str):
    payload = request.get_json(silent=True) or {}
    message = MessageRecord.query.filter_by(
        id=message_id,
        conversation_id=conversation_id,
    ).first()
    if not message:
        return _err("Message not found", 404)

    emoji = str(payload.get("emoji") or "").strip()
    user = str(payload.get("user") or "").strip()
    if not emoji or not user:
        return _err("emoji and user are required")

    metadata = message.metadata_json or {}
    reactions = dict(metadata.get("reactions") or {})
    existing = list(reactions.get(emoji) or [])
    if user in existing:
        reactions[emoji] = [item for item in existing if item != user]
    else:
        reactions[emoji] = [*existing, user]
    metadata["reactions"] = reactions
    message.metadata_json = metadata
    db.session.commit()
    return _ok({"messageId": message.id, "reactions": reactions})


@platform_bp.get("/kyc/cases")
def list_verification_cases():
    user_id = request.args.get("userId")
    query = VerificationCase.query.order_by(VerificationCase.updated_at.desc())
    if user_id:
        query = query.filter(VerificationCase.user_id == user_id)
    rows = query.limit(100).all()
    return _ok(
        {
            "items": [
                {
                    "id": row.id,
                    "userId": row.user_id,
                    "tier": row.tier,
                    "status": row.status,
                    "provider": row.provider,
                    "metadata": row.metadata_json,
                    "createdAt": row.created_at.isoformat(),
                    "updatedAt": row.updated_at.isoformat(),
                }
                for row in rows
            ]
        }
    )


@platform_bp.post("/kyc/cases")
def create_verification_case():
    payload = request.get_json(silent=True) or {}
    case = VerificationCase(
        id=str(payload.get("id") or f"kyc-{secrets.token_hex(8)}"),
        user_id=str(payload.get("userId") or ""),
        tier=str(payload.get("tier") or "tier_1"),
        status=str(payload.get("status") or "submitted"),
        provider=payload.get("provider"),
        metadata_json=payload.get("metadata") or {},
    )
    if not case.user_id:
        return _err("userId is required")
    db.session.add(case)
    db.session.commit()
    return _ok({"caseId": case.id})


@platform_bp.put("/kyc/cases/<case_id>")
def update_verification_case(case_id: str):
    payload = request.get_json(silent=True) or {}
    case = VerificationCase.query.get(case_id)
    if not case:
        return _err("Verification case not found", 404)
    if "status" in payload:
        case.status = str(payload["status"])
    if "metadata" in payload:
        case.metadata_json = payload.get("metadata") or {}
    db.session.commit()
    return _ok({"caseId": case.id, "updated": True})


@platform_bp.get("/listings")
def list_listings():
    seller_id = request.args.get("sellerId")
    query = ListingRecord.query.order_by(ListingRecord.updated_at.desc())
    if seller_id:
        query = query.filter(ListingRecord.seller_id == seller_id)
    rows = query.limit(200).all()
    return _ok(
        {
            "items": [
                {
                    "id": row.id,
                    "sellerId": row.seller_id,
                    "title": row.title,
                    "price": float(row.price),
                    "currency": row.currency,
                    "kind": row.kind,
                    "status": row.status,
                    "metadata": row.metadata_json,
                    "createdAt": row.created_at.isoformat(),
                    "updatedAt": row.updated_at.isoformat(),
                }
                for row in rows
            ]
        }
    )


@platform_bp.get("/listings/<listing_id>")
def get_listing(listing_id: str):
    row = ListingRecord.query.get(listing_id)
    if not row:
        return _err("Listing not found", 404)
    return _ok(
        {
            "id": row.id,
            "sellerId": row.seller_id,
            "title": row.title,
            "price": float(row.price),
            "currency": row.currency,
            "kind": row.kind,
            "status": row.status,
            "metadata": row.metadata_json,
            "createdAt": row.created_at.isoformat(),
            "updatedAt": row.updated_at.isoformat(),
        }
    )


@platform_bp.post("/listings")
def create_listing():
    payload = request.get_json(silent=True) or {}
    listing = ListingRecord(
        id=str(payload.get("id") or f"listing-{secrets.token_hex(8)}"),
        seller_id=str(payload.get("sellerId") or ""),
        title=str(payload.get("title") or "Listing"),
        price=Decimal(str(payload.get("price") or 0)),
        currency=str(payload.get("currency") or "NGN"),
        kind=str(payload.get("kind") or "product"),
        status=str(payload.get("status") or "draft"),
        metadata_json=payload.get("metadata") or {},
    )
    if not listing.seller_id:
        return _err("sellerId is required")
    db.session.add(listing)
    db.session.commit()
    return _ok({"listingId": listing.id})


@platform_bp.get("/service-requests")
def list_service_requests():
    user_id = request.args.get("userId")
    query = ServiceRequestRecord.query.order_by(ServiceRequestRecord.updated_at.desc())
    if user_id:
        query = query.filter(
            (ServiceRequestRecord.requester_id == user_id)
            | (ServiceRequestRecord.provider_id == user_id)
        )
    rows = query.limit(200).all()
    return _ok(
        {
            "items": [
                {
                    "id": row.id,
                    "requesterId": row.requester_id,
                    "providerId": row.provider_id,
                    "requestType": row.request_type,
                    "status": row.status,
                    "title": row.title,
                    "amount": float(row.amount),
                    "currency": row.currency,
                    "metadata": row.metadata_json,
                    "createdAt": row.created_at.isoformat(),
                    "updatedAt": row.updated_at.isoformat(),
                }
                for row in rows
            ]
        }
    )


@platform_bp.post("/service-requests")
def create_service_request():
    payload = request.get_json(silent=True) or {}
    record = ServiceRequestRecord(
        id=str(payload.get("id") or f"svc-{secrets.token_hex(8)}"),
        requester_id=str(payload.get("requesterId") or ""),
        provider_id=payload.get("providerId"),
        request_type=str(payload.get("requestType") or "delivery"),
        status=str(payload.get("status") or "created"),
        title=str(payload.get("title") or "Service request"),
        amount=Decimal(str(payload.get("amount") or 0)),
        currency=str(payload.get("currency") or "NGN"),
        metadata_json=payload.get("metadata") or {},
    )
    if not record.requester_id:
        return _err("requesterId is required")
    db.session.add(record)
    db.session.commit()
    return _ok({"serviceRequestId": record.id})


@platform_bp.put("/service-requests/<request_id>")
def update_service_request(request_id: str):
    payload = request.get_json(silent=True) or {}
    record = ServiceRequestRecord.query.get(request_id)
    if not record:
        return _err("Service request not found", 404)
    if "status" in payload:
        record.status = str(payload["status"])
    if "providerId" in payload:
        record.provider_id = payload.get("providerId")
    if "metadata" in payload:
        record.metadata_json = payload.get("metadata") or {}
    db.session.commit()
    return _ok({"serviceRequestId": record.id, "updated": True})


# ── Media upload ──────────────────────────────────────────────────────────────

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "mp4", "mov", "webm",
                      "m4a", "aac", "caf", "mp3", "wav"}

# Stored one level above the package: backend/uploads/
_UPLOAD_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "uploads")
)


@platform_bp.post("/media/upload")
def upload_media():
    """Accept a multipart/form-data upload and return a permanent URL."""
    if "file" not in request.files:
        return _err("No file field in request", 400)

    f = request.files["file"]
    if not f or not f.filename:
        return _err("Empty file", 400)

    ext = secure_filename(f.filename).rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        return _err(f"File type '.{ext}' is not allowed", 415)

    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    server_name = f"{uuid.uuid4()}.{ext}"
    dest = os.path.join(_UPLOAD_DIR, server_name)
    f.save(dest)

    # Build absolute URL so the client can fetch it directly
    base = request.host_url.rstrip("/")
    url = f"{base}/uploads/{server_name}"
    return _ok({"url": url, "filename": server_name})


@platform_bp.get("/uploads/<path:filename>")
def serve_upload(filename: str):
    """Serve a previously uploaded file."""
    safe = secure_filename(filename)
    if not safe:
        return _err("Invalid filename", 400)
    return send_from_directory(_UPLOAD_DIR, safe)
