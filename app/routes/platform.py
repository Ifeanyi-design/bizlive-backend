from __future__ import annotations

from decimal import Decimal

from flask import Blueprint, request

from ..extensions import db
from ..models import (
    Conversation,
    MessageRecord,
    OrderRecord,
    RiskFlag,
    TransactionRecord,
    User,
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
    items = []
    for row in rows:
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
    if Decimal(buyer_wallet.escrow_balance) < amount:
        return _err("Insufficient escrow balance", 409)

    buyer_wallet.escrow_balance = Decimal(buyer_wallet.escrow_balance) - amount
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
    rows = (
        MessageRecord.query.filter_by(conversation_id=conversation_id)
        .order_by(MessageRecord.created_at.desc())
        .limit(200)
        .all()
    )


@platform_bp.put("/threads/<conversation_id>")
def upsert_thread(conversation_id: str):
    payload = request.get_json(silent=True) or {}
    conversation = Conversation.query.get(conversation_id)
    if not conversation:
        conversation = Conversation(
            id=conversation_id,
            title=str(payload.get("title") or "Conversation"),
            metadata_json=payload,
        )
        db.session.add(conversation)
    else:
        conversation.title = str(payload.get("title") or conversation.title or "Conversation")
        conversation.metadata_json = {
            **(conversation.metadata_json or {}),
            **payload,
        }
    db.session.commit()
    return _ok({"threadId": conversation.id, "updated": True})
    return _ok(
        {
            "items": [
                {
                    "id": row.id,
                    "conversationId": row.conversation_id,
                    "senderId": row.sender_id,
                    "messageType": row.message_type,
                    "body": row.body,
                    "metadata": row.metadata_json,
                    "createdAt": row.created_at.isoformat(),
                }
                for row in rows
            ]
        }
    )


@platform_bp.post("/conversations/<conversation_id>/messages")
def post_message(conversation_id: str):
    payload = request.get_json(silent=True) or {}
    conversation = Conversation.query.get(conversation_id)
    if not conversation:
        conversation = Conversation(
            id=conversation_id,
            title=str(payload.get("title") or "Conversation"),
            metadata_json=payload.get("thread") or {},
        )
        db.session.add(conversation)
    elif payload.get("thread"):
        conversation.metadata_json = {
            **(conversation.metadata_json or {}),
            **(payload.get("thread") or {}),
        }
        conversation.title = str(
            (payload.get("thread") or {}).get("title") or conversation.title or "Conversation"
        )

    message = MessageRecord(
        id=str(payload.get("id")),
        conversation_id=conversation_id,
        sender_id=str(payload.get("senderId")),
        message_type=str(payload.get("messageType") or "text"),
        body=str(payload.get("body") or ""),
        metadata_json=payload.get("metadata") or {},
    )
    db.session.add(message)
    db.session.commit()
    return _ok({"messageId": message.id})


@platform_bp.get("/threads/<conversation_id>/messages")
def get_thread_messages_alias(conversation_id: str):
    return get_messages(conversation_id)


@platform_bp.post("/threads/<conversation_id>/messages")
def post_thread_message_alias(conversation_id: str):
    return post_message(conversation_id)
