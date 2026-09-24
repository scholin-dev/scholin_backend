from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import random
import string
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import threading

from ....core.database import get_db
from ....core.security import get_password_hash
from ....core.config import settings
from ....models.user import User, PasswordReset
from ....schemas.user import (
    ForgotPasswordRequest,
    VerifyCodeRequest,
    ResetPasswordRequest,
    PasswordResetResponse
)

router = APIRouter(prefix="/password-reset", tags=["Password Reset"])

def generate_code() -> str:
    return ''.join(random.choices(string.digits, k=6))

def send_email_sync(to_email: str, subject: str, html_body: str):
    """Send email in a separate thread"""
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f"{settings.FROM_NAME} <{settings.FROM_EMAIL}>"
        msg['To'] = to_email
        msg.attach(MIMEText(html_body, 'html'))
        
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
        
        print(f"✅ Email sent to {to_email}")
    except Exception as e:
        print(f"❌ Email failed: {e}")

def send_password_reset_email(email: str, code: str, full_name: str = "User"):
    """Send beautiful password reset email"""
    subject = "Password Reset - Eduu School"
    
    html_body = f"""
    <!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Password Reset</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="style.css">

<style>

    :root{{
--primary:#198754;
--secondary:#20c997;
--dark:#0f5132;
--light:#eaf7ef;
--text:#666;
}}

*{{
margin:0;
padding:0;
box-sizing:border-box;
}}

body{{
background:linear-gradient(135deg,var(--light),#d7f3df);
font-family:'Poppins',sans-serif;
padding:40px 20px;
color:#222;
}}

.container{{
max-width:620px;
margin:auto;
background:white;
border-radius:30px;
overflow:hidden;
box-shadow:0 25px 70px rgba(0,0,0,.12);
animation:show .8s ease;
}}

@keyframes show{{
from{{
opacity:0;
transform:translateY(40px);
}}
to{{
opacity:1;
transform:translateY(0);
}}
}}

.header{{
position:relative;
padding:60px 40px;
text-align:center;
background:linear-gradient(270deg,#0f5132,#198754,#20c997,#0f5132);
background-size:400% 400%;
animation:gradientMove 8s ease infinite;
color:white;
overflow:hidden;
}}

@keyframes gradientMove{{
0%{{
background-position:0% 50%;
}}
50%{{
background-position:100% 50%;
}}
100%{{
background-position:0% 50%;
}}
}}

.header:before,
.header:after{{
content:"";
position:absolute;
border-radius:50%;
background:rgba(255,255,255,.1);
animation:float 5s infinite alternate;
}}

.header:before{{
width:300px;
height:300px;
right:-100px;
top:-120px;
}}

.header:after{{
width:180px;
height:180px;
left:-70px;
bottom:-70px;
}}

@keyframes float{{
from{{
transform:translateY(0);
}}
to{{
transform:translateY(25px);
}}
}}

.logo{{
width:100px;
height:100px;
margin:auto;
border-radius:50%;
background:white;
display:flex;
align-items:center;
justify-content:center;
padding:15px;
margin-bottom:20px;
animation:floatLogo 3s ease-in-out infinite;
}}

.logo img{{
width:100%;
height:100%;
object-fit:contain;
}}

@keyframes floatLogo{{
0%,100%{{
transform:translateY(0);
}}
50%{{
transform:translateY(-15px);
}}
}}

.header h1{{
font-size:34px;
font-weight:700;
}}

.header p{{
margin-top:10px;
opacity:.9;
}}

.content{{
padding:45px;
}}

.content h2{{
font-size:24px;
margin-bottom:20px;
}}

.content p{{
line-height:1.8;
color:var(--text);
}}

.code-card{{
margin:40px 0;
padding:35px;
text-align:center;
border-radius:25px;
background:linear-gradient(180deg,#fff,#f9fdfb);
box-shadow:0 12px 40px rgba(0,0,0,.08);
border:1px solid #e8f5eb;
}}

.code{{
font-size:55px;
font-weight:700;
font-family:monospace;
letter-spacing:15px;
color:var(--primary);
animation:otpGlow 2s infinite alternate;
}}

@keyframes otpGlow{{
from{{
text-shadow:0 0 5px #20c997;
}}
to{{
text-shadow:0 0 25px #198754;
}}
}}

.expiry{{
margin-top:15px;
color:#888;
}}

.timer{{
color:#dc3545;
font-weight:700;
animation:timerPulse 1s infinite;
}}

@keyframes timerPulse{{
0%,100%{{
opacity:1;
text-shadow:0 0 5px red;
}}
50%{{
opacity:.4;
text-shadow:0 0 20px red;
}}
}}

.progress{{
height:8px;
background:#eee;
border-radius:20px;
margin-top:20px;
overflow:hidden;
}}

#progress-bar{{
height:100%;
width:100%;
background:linear-gradient(90deg,#198754,#20c997);
transition:1s linear;
}}

.button{{
display:inline-block;
margin-top:25px;
padding:16px 45px;
border-radius:50px;
background:linear-gradient(135deg,var(--primary),var(--secondary));
color:white;
text-decoration:none;
font-weight:600;
transition:.4s ease;
position:relative;
overflow:hidden;
}}

.button:hover{{
transform:translateY(-8px) scale(1.05);
background:linear-gradient(135deg,#20c997,#198754);
box-shadow:0 15px 35px rgba(25,135,84,.5);
letter-spacing:1px;
}}

.button:hover::after{{
content:"→";
margin-left:10px;
animation:arrowMove .6s infinite alternate;
}}

@keyframes arrowMove{{
from{{
transform:translateX(0);
}}
to{{
transform:translateX(8px);
}}
}}

.copy-btn{{
margin-top:20px;
padding:10px 20px;
border:none;
border-radius:30px;
background:#f1f8f4;
color:#198754;
font-weight:600;
cursor:pointer;
transition:.3s;
}}

.copy-btn:hover{{
background:#198754;
color:white;
transform:scale(1.05);
}}

.note{{
display:flex;
gap:15px;
padding:20px;
background:#fff8e8;
border-left:5px solid orange;
border-radius:15px;
animation:slideIn 1s ease;
}}

@keyframes slideIn{{
from{{
opacity:0;
transform:translateX(-50px);
}}
to{{
opacity:1;
transform:translateX(0);
}}
}}

.note-icon{{
font-size:30px;
}}

.note h3{{
color:#b45309;
}}

.note p{{
font-size:14px;
color:#8b5e00;
}}

.footer{{
padding:35px;
text-align:center;
background:#fafafa;
}}

.social{{
display:flex;
justify-content:center;
gap:15px;
}}

.social a{{
width:45px;
height:45px;
display:flex;
align-items:center;
justify-content:center;
background:var(--primary);
color:white;
border-radius:50%;
text-decoration:none;
font-size:20px;
animation:bounce 2s infinite;
transition:.3s;
}}

.social a:nth-child(1){{
animation-delay:0s;
}}

.social a:nth-child(2){{
animation-delay:.2s;
}}

.social a:nth-child(3){{
animation-delay:.4s;
}}

.social a:nth-child(4){{
animation-delay:.6s;
}}

@keyframes bounce{{
0%,100%{{
transform:translateY(0);
}}
50%{{
transform:translateY(-12px);
}}
}}

.social a:hover{{
transform:scale(1.2);
background:var(--secondary);
}}

@media(max-width:600px){{
.content{{
padding:30px;
}}

.code{{
font-size:35px;
letter-spacing:8px;
}}

.header h1{{
font-size:28px;
}}
}}
</style>

</head>
<body>
<div class="container">
<div class="header">
<div class="logo">
<img src="img/ logo.png" alt="Eduu Logo">
</div>
<h1>Password Reset</h1>
<p>Eduu School Management System</p>
</div>
<div class="content">
<h2>Hello, David Ouma 👋</h2>
<p>We received a request to reset the password for your Eduu School account. Use the secure verification code below to continue.</p>

<div class="code-card">
<div class="code" id="otp">
483920
</div>
<button class="copy-btn" onclick="copyCode()">
📋 Copy Code
</button>
<div class="expiry">
⏳ Expires in <strong id="timer" class="timer">15:00</strong>
</div>
<div class="progress">
<div id="progress-bar"></div>
</div>
<a href="#" class="button" id="verifyBtn" onclick="verifyAccount()">
Verify Account →
</a
</div>

<div class="note">
<div class="note-icon">🛡️</div>
<div>
<h3>Security Notice</h3>
<p>Never share this verification code with anyone. Eduu School staff will never ask for your code. If you didn't request this password reset, ignore this email.</p>
</div>
</div>
</div>
<div class="footer">
<p>© 2026 Eduu School Management<br>Secure • Reliable • Trusted</p>
<div class="social">
<a href="#">🌐</a>
<a href="#">📘</a>
<a href="#">🐦</a>
<a href="#">📧</a>
</div>
</div>
</div>
</body>
<script src="script.js">

let time = 15 * 60;

let timer = document.getElementById("timer");
let bar = document.getElementById("progress-bar");

let countdown = setInterval(()=>{{

let minutes = Math.floor(time / 60);
let seconds = time % 60;

seconds = seconds < 10 ? "0" + seconds : seconds;

timer.textContent = minutes + ":" + seconds;

let percentage = (time / (15 * 60)) * 100;

bar.style.width = percentage + "%";


if(time <= 60){{

timer.style.color="red";

}}


if(time <= 0){{

clearInterval(countdown);

timer.textContent="Expired";

document.getElementById("verifyBtn").style.pointerEvents="none";

document.getElementById("verifyBtn").textContent="Code Expired";

bar.style.width="0%";

}}

time--;

}},1000);



function copyCode(){{

let code=document.getElementById("otp").innerText;

navigator.clipboard.writeText(code);

document.querySelector(".copy-btn").innerHTML="✓ Copied";

setTimeout(()=>{{

document.querySelector(".copy-btn").innerHTML="📋 Copy Code";

}},2000);

}}

function verifyAccount(){{

let btn=document.getElementById("verifyBtn");

btn.innerHTML="⏳ Verifying...";

setTimeout(()=>{{

btn.innerHTML="✓ Verified";

btn.style.background="#198754";

}},2000);

}}

</script>
</html>
    """
    
    # Send in background thread so API doesn't wait
    thread = threading.Thread(target=send_email_sync, args=(email, subject, html_body))
    thread.start()


@router.post("/forgot", response_model=PasswordResetResponse)
def forgot_password(request: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == request.email).first()
    
    if not user:
        return {"message": "If the email exists, a reset code has been sent.", "success": True}
    
    # Invalidate old unused codes
    db.query(PasswordReset).filter(
        PasswordReset.email == request.email,
        PasswordReset.is_used == False
    ).update({"is_used": True})
    
    code = generate_code()
    expires_at = datetime.utcnow() + timedelta(minutes=15)
    
    reset_entry = PasswordReset(email=request.email, code=code, expires_at=expires_at)
    db.add(reset_entry)
    db.commit()
    
    # Send real email
    send_password_reset_email(email=user.email, code=code, full_name=user.full_name)
    print(f"🔑 Code for {request.email}: {code}")  # Also print for debugging
    
    return {"message": "If the email exists, a reset code has been sent.", "success": True}


@router.post("/verify", response_model=PasswordResetResponse)
def verify_code(request: VerifyCodeRequest, db: Session = Depends(get_db)):
    reset_entry = db.query(PasswordReset).filter(
        PasswordReset.email == request.email,
        PasswordReset.code == request.code,
        PasswordReset.is_used == False,
        PasswordReset.expires_at > datetime.utcnow()
    ).first()
    
    if not reset_entry:
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    
    return {"message": "Code verified.", "success": True}


@router.post("/reset", response_model=PasswordResetResponse)
def reset_password(request: ResetPasswordRequest, db: Session = Depends(get_db)):
    reset_entry = db.query(PasswordReset).filter(
        PasswordReset.email == request.email,
        PasswordReset.code == request.code,
        PasswordReset.is_used == False,
        PasswordReset.expires_at > datetime.utcnow()
    ).first()
    
    if not reset_entry:
        raise HTTPException(status_code=400, detail="Invalid or expired code")
    
    user = db.query(User).filter(User.email == request.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    user.hashed_password = get_password_hash(request.new_password)
    reset_entry.is_used = True
    db.commit()
    
    return {"message": "Password reset successfully!", "success": True}
