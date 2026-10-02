# ==========================================
# PAYSTACK PAYMENTS
# ==========================================
import os
import hmac
import hashlib
import httpx
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request, Header, Body
from sqlalchemy.orm import Session

from ....core.database import get_db
from ....core.session_auth import get_current_user
from ....models.user import User, School, SmsTopup, BookPurchase, Book

router = APIRouter(prefix="/paystack", tags=["paystack"])


def _paystack_secret() -> str:
    key = os.getenv("PAYSTACK_SECRET_KEY")
    if not key:
        raise HTTPException(500, "PAYSTACK_SECRET_KEY is not set")
    return key


def _paystack_base() -> str:
    return "https://api.paystack.co"


# ==========================================
# INITIALIZE
# ==========================================
@router.post("/initialize")
async def paystack_initialize(
    purpose: str = Body(..., description="'sms_topup' or 'book_purchase'"),
    phone_number: str = Body(...),
    amount: float = Body(...),
    sms_count: int | None = Body(None),
    book_id: int | None = Body(None),
    callback_url: str | None = Body(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user:
        raise HTTPException(401, "Not authenticated")
    if amount <= 0:
        raise HTTPException(400, "Amount must be > 0")

    phone_number = _normalize_phone(phone_number)

    # Unique reference — Paystack uses it for tracking and callbacks
    reference = f"PS-{purpose.upper()}-{user.id}-{int(datetime.utcnow().timestamp())}"

    # ── Create pending row ────────────────────────────────────
    if purpose == "sms_topup":
        if not sms_count or sms_count <= 0:
            raise HTTPException(400, "sms_count required for sms_topup")

        row = SmsTopup(
            school_id=user.school_id,
            user_id=user.id,
            phone_number=phone_number,
            amount=int(amount),
            sms_count=sms_count,
            status="pending",
            checkout_request_id=reference,   # stores Paystack reference
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        pending_id = row.id

    elif purpose == "book_purchase":
        if not book_id:
            raise HTTPException(400, "book_id required for book_purchase")

        book = db.query(Book).filter(Book.id == book_id).first()
        if not book:
            raise HTTPException(404, "Book not found")

        row = BookPurchase(
            user_id=user.id,
            book_id=book_id,
            phone_number=phone_number,
            amount=int(amount),
            status="pending",
            checkout_request_id=reference,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        pending_id = row.id

    else:
        raise HTTPException(400, f"Unknown purpose: {purpose}")

    # ── Call Paystack ─────────────────────────────────────────
    payload = {
        "email": user.email or f"user{user.id}@scholin.ke",
        "amount": int(amount * 100),
        "reference": reference,
        "currency": "KES",
        "mobile_money": {
            "phone": phone_number,
            "provider": "mpesa"
        },
        "metadata": {
            "purpose": purpose,
            "user_id": user.id,
            "pending_id": pending_id,
        },
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"{_paystack_base()}/charge",  # <--- CHANGED from /transaction/initialize
            json=payload,
            headers={...},
        )

    # 3. Handle the different response
    result = response.json()

    if response.status_code == 200 and result.get("status"):
        data = result["data"]
        
        # Paystack returns "pay_offline" when the STK push is sent
        if data.get("status") == "pay_offline":
            return {
                "success": True,
                "status": "pay_offline",
                "message": data.get("display_text") or "STK push sent. Enter PIN on your phone.",
                "reference": reference,
                "pending_id": pending_id,
            }

    data = result["data"]

    return {
        "success": True,
        "reference": data["reference"],
        "access_code": data["access_code"],
        "authorization_url": data["authorization_url"],
        "pending_id": pending_id,
        "amount": int(amount),
        "purpose": purpose,
    }


# ==========================================
# VERIFY (app can poll this)
# ==========================================
@router.get("/verify/{reference}")
async def paystack_verify(
    reference: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                f"{_paystack_base()}/transaction/verify/{reference}",
                headers={"Authorization": f"Bearer {_paystack_secret()}"},
            )
    except httpx.RequestError as e:
        raise HTTPException(502, f"Could not reach Paystack: {e}")

    result = response.json()

    if response.status_code != 200 or not result.get("status"):
        raise HTTPException(502, {
            "message": "Paystack verify failed",
            "response": result,
        })

    data = result["data"]
    status = data.get("status")

    if status == "success":
        _fulfill_if_pending(db, reference, data)

    return {
        "success": True,
        "reference": reference,
        "status": status,
        "amount": data.get("amount", 0) / 100,
        "currency": data.get("currency"),
        "paid_at": data.get("paid_at"),
        "channel": data.get("channel"),      # "card" | "mobile_money" | "bank"
        "customer_email": data.get("customer", {}).get("email"),
    }


# ==========================================
# WEBHOOK (Paystack → us)
# ==========================================
@router.post("/webhook")
async def paystack_webhook(
    request: Request,
    x_paystack_signature: str = Header(None),
    db: Session = Depends(get_db),
):
    body = await request.body()

    # Verify HMAC-SHA512 signature
    expected = hmac.new(
        _paystack_secret().encode("utf-8"),
        body,
        hashlib.sha512,
    ).hexdigest()

    if not hmac.compare_digest(expected, x_paystack_signature or ""):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = await request.json()
    except Exception:
        return {"status": "ignored"}

    event = payload.get("event")
    data = payload.get("data", {})
    reference = data.get("reference")

    if not reference:
        return {"status": "no_reference"}

    if event == "charge.success":
        _fulfill_if_pending(db, reference, data)
        print(f"✅ Paystack charge.success: {reference}")
    elif event in ("charge.failed", "transfer.failed"):
        _fail_if_pending(db, reference, data.get("gateway_response") or "failed")
        print(f"❌ Paystack {event}: {reference}")

    return {"status": "ok"}


# ==========================================
# HELPERS
# ==========================================
def _normalize_phone(phone: str) -> str:
    phone = phone.strip()
    if phone.startswith("07"):
        return "254" + phone[1:]
    if phone.startswith("+254"):
        return phone[1:]
    if phone.startswith("7"):
        return "254" + phone
    return phone


def _mark_failed(db: Session, purpose: str, pending_id: int, reason: str) -> None:
    if purpose == "sms_topup":
        row = db.query(SmsTopup).filter(SmsTopup.id == pending_id).first()
        if row:
            row.status = "failed"
            row.result_desc = reason[:500]
            db.commit()
    elif purpose == "book_purchase":
        row = db.query(BookPurchase).filter(BookPurchase.id == pending_id).first()
        if row:
            row.status = "failed"
            row.result_desc = reason[:500]
            db.commit()


def _fulfill_if_pending(db: Session, reference: str, data: dict) -> None:
    """
    Idempotent — safe to call from webhook, verify, and repeated retries.
    """

    # ── SMS top-up ───────────────────────────────────────────
    topup = db.query(SmsTopup).filter(
        SmsTopup.checkout_request_id == reference
    ).first()

    if topup:
        if topup.status == "success":
            return
        topup.status = "success"
        topup.mpesa_receipt = data.get("reference")
        school = db.query(School).filter(School.id == topup.school_id).first()
        if school:
            school.sms_bal = (school.sms_bal or 0) + topup.sms_count
        db.commit()
        print(f"✅ SMS topup #{topup.id} credited — channel: {data.get('channel')}")
        return

    # ── Book purchase ────────────────────────────────────────
    purchase = db.query(BookPurchase).filter(
        BookPurchase.checkout_request_id == reference
    ).first()

    if purchase:
        if purchase.status == "success":
            return
        purchase.status = "success"
        purchase.mpesa_receipt = data.get("reference")
        purchase.completed_at = datetime.utcnow()
        db.commit()
        print(f"✅ Book purchase #{purchase.id} unlocked — channel: {data.get('channel')}")
        return

    print(f"⚠️ No pending row matched reference: {reference}")


def _fail_if_pending(db: Session, reference: str, reason: str) -> None:
    topup = db.query(SmsTopup).filter(
        SmsTopup.checkout_request_id == reference
    ).first()
    if topup and topup.status == "pending":
        topup.status = "failed"
        topup.result_desc = reason[:500]
        db.commit()
        return

    purchase = db.query(BookPurchase).filter(
        BookPurchase.checkout_request_id == reference
    ).first()
    if purchase and purchase.status == "pending":
        purchase.status = "failed"
        purchase.result_desc = reason[:500]
        db.commit()
