import time, os
import smtplib, africastalking
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from app.core.database import SessionLocal
from app.models.message_queue import MessageQueue
from app.core.config import settings

from app.models.user import School

# Redis/Dramatiq imports
import dramatiq
from dramatiq.brokers.redis import RedisBroker

africastalking.initialize(
    settings.AT_USERNAME,
    settings.AT_API_KEY,
)
print("🐛 worker.py is being imported", flush=True)
# Configure Redis broker
try:
    redis_broker = RedisBroker(url=settings.REDIS_URL)
    dramatiq.set_broker(redis_broker)
    print("✅ Redis broker connected")
except Exception as e:
    print(f"⚠️ Redis not available: {e}")
    print("   Falling back to ThreadPoolExecutor")
def send_email_batch(emails, message, school_id):
    for email in emails:
        try:
            send_single_email(email, message, school_id)
        except Exception as e:
            print(f"Email failed for {email}: {e}")


def send_sms_batch(phones, message, school_id):
    db = SessionLocal()
    try:
        school = db.query(School).filter(School.id == school_id).first()
        if not school:
            raise Exception("School not found")
            
        for phone in phones:
            if school.sms_bal <=0:
                raise Exception("Low SMS balance")
            phone = (
                phone.strip()
                    .replace(" ", "")
                    .replace("-", "")
                    .replace("(", "")
                    .replace(")", "")
            )
            send_single_sms(phone, message)
            school.sms_bal -= 1
        db.commit()
    except Exception as e:
        print(f"Exception {e} Occured")
    finally:
        db.close()

def get_pending_messages(db, limit=50):
    """Get pending messages from queue"""
    return db.query(MessageQueue).filter(
        MessageQueue.status == 'pending',
        MessageQueue.retries < MessageQueue.max_retries
    ).limit(limit).all()


def mark_as_sending(db, msg_id):
    db.query(MessageQueue).filter(MessageQueue.id == msg_id).update({"status": "sending"})
    db.commit()


def mark_as_sent(db, msg_id):
    db.query(MessageQueue).filter(MessageQueue.id == msg_id).update({
        "status": "sent", "sent_at": datetime.utcnow()
    })
    db.commit()


def mark_as_failed(db, msg_id, error):
    msg = db.query(MessageQueue).filter(MessageQueue.id == msg_id).first()
    if msg:
        msg.status = 'failed'
        msg.retries += 1
        msg.error_message = str(error)[:500]
        db.commit()


import resend

def send_single_email(to_email: str, message: str, school_id) -> bool:
    """Send a single email using Resend"""
    try:
        resend.api_key = os.getenv("RESEND_API_KEY")
        
        params = {
            "from": f"{settings.FROM_NAME} <{settings.FROM_EMAIL}>",
            "to": [to_email],
            "subject": f"Message from {settings.FROM_NAME}",
            "html": f"""
            <html><body style="font-family: Arial, sans-serif; padding: 20px;">
                <div style="max-width: 600px; margin: auto; background: #f9f9f9; padding: 30px; border-radius: 10px;">
                    <h2 style="color: #1a237e;">School Announcement</h2>
                    <p style="font-size: 16px; line-height: 1.6; color: #333;">{message.replace(chr(10), '<br>')}</p>
                    <hr style="border: 1px solid #ddd; margin: 20px 0;">
                    <p style="color: #999; font-size: 12px;">This is an automated message from your school.</p>
                </div>
            </body></html>
            """
        }
        
        r = resend.Emails.send(params)
        print(f"   ✅ Resend: {to_email} (ID: {r['id']})")
        return True
        
    except Exception as e:
        print(f"   ❌ Resend failed: {to_email} - {e}")
        raise


def send_single_sms(phone: str, message: str) -> bool:
    """Send a single SMS using Africa's Talking"""
    if phone[2:] != '+25':
        phone = '+254'+ phone[1:]
    sms = africastalking.SMS
    try:
        response = sms.send(
            message,
            [phone],
        )
        
        return True

    except Exception as e:
        print(f"❌ SMS failed for {phone}")
        print(e)
        raise


def process_single_message(msg):
    """Process a single message from the queue"""
    db = SessionLocal()
    try:
        mark_as_sending(db, msg.id)
        
        if msg.type == 'email' and msg.email:
            send_single_email(msg.email, msg.message)
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
    """Process a batch of pending messages"""
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
    """Get current queue statistics"""
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
    """Main worker loop - runs forever"""
    
    last_stats_time = time.time()
    
    while True:
        try:
            processed = process_batch(limit=50, max_workers=10)
            
            if processed > 0:
                print(f"   ✅ Batch complete: {processed} messages")
            
            # Show stats every 60 seconds
            if time.time() - last_stats_time > 60:
                pending, sending, sent, failed = get_queue_stats()
                if pending + sending + sent + failed > 0:
                    print(f"\n   📊 Queue Stats: {pending} pending | {sending} sending | {sent} sent | {failed} failed\n")
                last_stats_time = time.time()
            
            time.sleep(5)
            
        except KeyboardInterrupt:
            print("\n👋 Worker stopped")
            break
        except Exception as e:
            print(f"   ⚠️ Worker error: {e}")
            time.sleep(10)


# Start worker if run directly
if __name__ == "__main__":
    run_worker()
