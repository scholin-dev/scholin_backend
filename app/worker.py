import time, os, re
import africastalking
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from brevo import Brevo
from brevo.core.api_error import ApiError

from app.core.database import SessionLocal
from app.models.message_queue import MessageQueue
from app.core.config import settings
from app.models.user import School

# ── Redis / Dramatiq (optional) ────────────────────────────────────────
import dramatiq
from dramatiq.brokers.redis import RedisBroker

africastalking.initialize(
    settings.AT_USERNAME,
    settings.AT_API_KEY,
)
print("🐛 worker.py is being imported", flush=True)

try:
    redis_broker = RedisBroker(url=settings.REDIS_URL)
    dramatiq.set_broker(redis_broker)
    print("✅ Redis broker connected")
except Exception as e:
    print(f"⚠️ Redis not available: {e}")
    print("   Falling back to ThreadPoolExecutor")


# ── Batch dispatchers ──────────────────────────────────────────────────
def send_email_batch(emails, message, school_id=None):
    for email in emails:
        try:
            send_single_email(email, message, school_id)
        except Exception as e:
            print(f"   ❌ Email failed for {email}: {e}")


def send_sms_batch(phones, message, school_id=None):
    db = SessionLocal()
    try:
        school = db.query(School).filter(School.id == school_id).first()
        if not school:
            raise Exception("School not found")

        for phone in phones:
            if (school.sms_bal or 0) <= 0:
                raise Exception("Low SMS balance")
            send_single_sms(phone, message)
            school.sms_bal -= 1

        db.commit()
    except Exception as e:
        print(f"❌ SMS batch failed: {e}")
    finally:
        db.close()


# ── Queue helpers ──────────────────────────────────────────────────────
def get_pending_messages(db, limit=50):
    return db.query(MessageQueue).filter(
        MessageQueue.status == 'pending',
        MessageQueue.retries < MessageQueue.max_retries,
    ).limit(limit).all()


def mark_as_sending(db, msg_id):
    db.query(MessageQueue).filter(MessageQueue.id == msg_id).update(
        {"status": "sending"}
    )
    db.commit()


def mark_as_sent(db, msg_id):
    db.query(MessageQueue).filter(MessageQueue.id == msg_id).update({
        "status": "sent",
        "sent_at": datetime.utcnow(),
    })
    db.commit()


def mark_as_failed(db, msg_id, error):
    msg = db.query(MessageQueue).filter(MessageQueue.id == msg_id).first()
    if msg:
        msg.status = 'failed'
        msg.retries += 1
        msg.error_message = str(error)[:500]
        db.commit()


# ── Brevo sender ───────────────────────────────────────────────────────
def send_single_email(to_email: str, message: str, school_id=None) -> bool:
    """Send a single email via Brevo (HTTPS — works on Railway)."""
    api_key = os.getenv("BREVO_API_KEY")
    sender_email = os.getenv("BREVO_SENDER_EMAIL")

    if not api_key or not sender_email:
        print("   ❌ BREVO_API_KEY or BREVO_SENDER_EMAIL not set")
        raise RuntimeError("Brevo credentials missing")

    html_body = f"""
    <html>
      <body style="font-family:Arial,sans-serif;padding:20px;background:#f9f9f9;">
        <div style="max-width:600px;margin:auto;background:#ffffff;padding:30px;
                    border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,0.05);">
          <h2 style="color:#1a237e;margin:0 0 16px;">School Announcement</h2>
          <p style="font-size:16px;line-height:1.6;color:#333;margin:0 0 16px;">
            {message.replace(chr(10), '<br>')}
          </p>
          <hr style="border:none;border-top:1px solid #ddd;margin:20px 0;">
          <p style="color:#999;font-size:12px;margin:0;">
            This is an automated message from your school.
          </p>
        </div>
      </body>
    </html>
    """

    try:
        client = Brevo(api_key=api_key)
        response = client.transactional_emails.send_transac_email(
            sender={"name": "Eduu School", "email": sender_email},
            to=[{"email": to_email}],
            subject="Message from Eduu School",
            html_content=html_body,
        )
        print(f"   ✅ Brevo: {to_email} (id={response.message_id})")
        return True
    except ApiError as e:
        print(f"   ❌ Brevo failed: {to_email} — {e.status_code} {e.body}")
        raise
    except Exception as e:
        print(f"   ❌ Brevo failed: {to_email} — {type(e).__name__}: {e}")
        raise


# ── Africa's Talking SMS ───────────────────────────────────────────────
def _normalize_phone(phone: str) -> str:
    """Convert Kenyan phone formats to +254XXXXXXXXX."""
    p = re.sub(r"[^\d+]", "", phone or "")
    if p.startswith("+254"):
        return p
    if p.startswith("254"):
        return "+" + p
    if p.startswith("0"):
        return "+254" + p[1:]
    if p.startswith("7") or p.startswith("1"):
        return "+254" + p
    return p


def send_single_sms(phone: str, message: str) -> bool:
    """Send a single SMS via Africa's Talking."""
    phone = _normalize_phone(phone)
    sms = africastalking.SMS
    try:
        sms.send(message, [phone])
        print(f"   ✅ SMS: {phone}")
        return True
    except Exception as e:
        print(f"   ❌ SMS failed for {phone}: {e}")
        raise


# ── Message processor ──────────────────────────────────────────────────
def process_single_message(msg):
    db = SessionLocal()
    try:
        mark_as_sending(db, msg.id)

        if msg.type == 'email' and msg.email:
            send_single_email(msg.email, msg.message, msg.school_id)
            mark_as_sent(db, msg.id)
            print(f"   ✅ Email sent to: {msg.email}")

        elif msg.type == 'sms' and msg.phone:
            send_single_sms(msg.phone, msg.message)
            mark_as_sent(db, msg.id)

    except Exception as e:
        mark_as_failed(db, msg.id, e)
        print(f"   ❌ Failed ({msg.type}): {e}")
    finally:
        db.close()


def process_batch(limit=50, max_workers=10):
    db = SessionLocal()
    try:
        messages = get_pending_messages(db, limit)
        if not messages:
            return 0

        print(f"\n📨 Processing {len(messages)} messages...")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            executor.map(process_single_message, messages)

        return len(messages)
    finally:
        db.close()


def get_queue_stats():
    db = SessionLocal()
    try:
        pending = db.query(MessageQueue).filter(MessageQueue.status == 'pending').count()
        sending = db.query(MessageQueue).filter(MessageQueue.status == 'sending').count()
        sent = db.query(MessageQueue).filter(MessageQueue.status == 'sent').count()
        failed = db.query(MessageQueue).filter(MessageQueue.status == 'failed').count()
        return pending, sending, sent, failed
    finally:
        db.close()


def run_worker():
    """Main worker loop."""
    last_stats_time = time.time()

    while True:
        try:
            processed = process_batch(limit=50, max_workers=10)

            if processed > 0:
                print(f"   ✅ Batch complete: {processed} messages")

            if time.time() - last_stats_time > 60:
                pending, sending, sent, failed = get_queue_stats()
                if pending + sending + sent + failed > 0:
                    print(
                        f"\n   📊 Queue Stats: {pending} pending | "
                        f"{sending} sending | {sent} sent | {failed} failed\n"
                    )
                last_stats_time = time.time()

            time.sleep(5)

        except KeyboardInterrupt:
            print("\n👋 Worker stopped")
            break
        except Exception as e:
            print(f"   ⚠️ Worker error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    run_worker()
