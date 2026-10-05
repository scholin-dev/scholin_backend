from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
import random
import string
import os

from brevo import Brevo
from brevo.core.api_error import ApiError

from ....core.database import get_db
from ....core.security import get_password_hash
from ....models.user import User, PasswordReset
from ....schemas.user import (
    ForgotPasswordRequest,
    VerifyCodeRequest,
    ResetPasswordRequest,
    PasswordResetResponse,
)

router = APIRouter(prefix="/password-reset", tags=["Password Reset"])


def generate_code() -> str:
    return ''.join(random.choices(string.digits, k=6))


def build_password_reset_html(code: str, full_name: str) -> str:
    """Return the HTML body for the password reset email."""
    return f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Password Reset</title>
    </head>
    <body style="margin:0;padding:40px 20px;background:linear-gradient(135deg,#eaf7ef,#d7f3df);
                 font-family:'Poppins',Arial,sans-serif;color:#222;">
      <div style="max-width:620px;margin:auto;background:#ffffff;border-radius:30px;
                  overflow:hidden;box-shadow:0 25px 70px rgba(0,0,0,0.12);">

        <!-- Header -->
        <div style="padding:60px 40px;text-align:center;
                    background:linear-gradient(270deg,#0f5132,#198754,#20c997,#0f5132);
                    color:#ffffff;">
          <h1 style="margin:0;font-size:34px;font-weight:700;">Password Reset</h1>
          <p style="margin-top:10px;opacity:0.9;">Eduu School Management System</p>
        </div>

        <!-- Content -->
        <div style="padding:45px;">
          <h2 style="font-size:24px;margin:0 0 20px;">Hello, {full_name} 👋</h2>
          <p style="line-height:1.8;color:#666;">
            We received a request to reset the password for your Eduu School account.
            Use the secure verification code below to continue.
          </p>

          <!-- Code card -->
          <div style="margin:40px 0;padding:35px;text-align:center;border-radius:25px;
                      background:linear-gradient(180deg,#ffffff,#f9fdfb);
                      box-shadow:0 12px 40px rgba(0,0,0,0.08);
                      border:1px solid #e8f5eb;">
            <div style="font-size:55px;font-weight:700;font-family:monospace;
                        letter-spacing:15px;color:#198754;">
              {code}
            </div>
            <div style="margin-top:15px;color:#888;">
              ⏳ Expires in <strong style="color:#dc3545;">15:00</strong> minutes
            </div>
          </div>

          <!-- Security notice -->
          <div style="display:flex;gap:15px;padding:20px;background:#fff8e8;
                      border-left:5px solid orange;border-radius:15px;">
            <div style="font-size:30px;">🛡️</div>
            <div>
              <h3 style="color:#b45309;margin:0 0 8px;">Security Notice</h3>
              <p style="font-size:14px;color:#8b5e00;margin:0;">
                Never share this verification code with anyone. Eduu School staff
                will never ask for your code. If you didn't request this password
                reset, ignore this email.
              </p>
            </div>
          </div>
        </div>

        <!-- Footer -->
        <div style="padding:35px;text-align:center;background:#fafafa;">
          <p style="margin:0;color:#999;font-size:13px;">
            © 2026 Eduu School Management<br>Secure • Reliable • Trusted
          </p>
        </div>
      </div>
    </body>
    </html>
    """


def send_password_reset_email(to_email: str, code: str, full_name: str = "User"):
    """
    Send password reset email via Brevo API (HTTPS — works on Railway).
    Runs synchronously; call via BackgroundTasks so the route returns fast.
    """
    api_key = os.getenv("BREVO_API_KEY")
    sender_email = os.getenv("BREVO_SENDER_EMAIL")

    if not api_key or not sender_email:
        print("❌ BREVO_API_KEY or BREVO_SENDER_EMAIL not set — skipping send")
        return

    html_body = build_password_reset_html(code, full_name)

    try:
        client = Brevo(api_key=api_key)
        response = client.transactional_emails.send_transac_email(
            sender={"name": "Eduu School", "email": sender_email},
            to=[{"email": to_email}],
            subject="Password Reset - Eduu School",
            html_content=html_body,
        )
        print(f"✅ Brevo sent to {to_email} (message_id={response.message_id})")
    except ApiError as e:
        print(f"❌ Brevo API error: {e.status_code} — {e.body}")
    except Exception as e:
        print(f"❌ Brevo unexpected error: {type(e).__name__}: {e}")


@router.post("/forgot", response_model=PasswordResetResponse)
def forgot_password(
    request: ForgotPasswordRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.email == request.email).first()

    # Always return the same response to prevent user enumeration
    generic_response = {
        "message": "If the email exists, a reset code has been sent.",
        "success": True,
    }

    if not user:
        return generic_response

    # Invalidate old unused codes
    db.query(PasswordReset).filter(
        PasswordReset.email == request.email,
        PasswordReset.is_used.is_(False),
    ).update({"is_used": True})

    code = generate_code()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=15)

    reset_entry = PasswordReset(
        email=request.email,
        code=code,
        expires_at=expires_at,
    )
    db.add(reset_entry)
    db.commit()

    # Fire-and-forget: FastAPI runs this after the response is sent
    background_tasks.add_task(
        send_password_reset_email, user.email, code, user.full_name
    )

    print(f"🔑 Code for {request.email}: {code}")
    return generic_response


@router.post("/verify", response_model=PasswordResetResponse)
def verify_code(request: VerifyCodeRequest, db: Session = Depends(get_db)):
    reset_entry = db.query(PasswordReset).filter(
        PasswordReset.email == request.email,
        PasswordReset.code == request.code,
        PasswordReset.is_used.is_(False),
        PasswordReset.expires_at > datetime.now(timezone.utc),
    ).first()

    if not reset_entry:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    return {"message": "Code verified.", "success": True}


@router.post("/reset", response_model=PasswordResetResponse)
def reset_password(request: ResetPasswordRequest, db: Session = Depends(get_db)):
    reset_entry = db.query(PasswordReset).filter(
        PasswordReset.email == request.email,
        PasswordReset.code == request.code,
        PasswordReset.is_used.is_(False),
        PasswordReset.expires_at > datetime.now(timezone.utc),
    ).first()

    if not reset_entry:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    user = db.query(User).filter(User.email == request.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if len(request.new_password) < 6:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 6 characters",
        )

    user.hashed_password = get_password_hash(request.new_password)
    reset_entry.is_used = True
    db.commit()

    return {"message": "Password reset successfully!", "success": True}
