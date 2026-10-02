from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
from pathlib import Path
import os
import threading

from .core.config import settings
from .core.database import engine, Base, get_db
from .api.v1.routes import api_router
from .core.deps import require_admin


# ── Env detection ─────────────────────────────────────────────
IS_PROD = os.getenv("ENV", "dev").lower() == "prod"

# ── Paths ─────────────────────────────────────────────────────
# app/main.py → parent = app/ → parent.parent = project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LANDING_DIR = PROJECT_ROOT / "landing"
UPLOADS_DIR = PROJECT_ROOT / "uploads"


# ── Scheduler ─────────────────────────────────────────────────
scheduler = BackgroundScheduler()


def auto_expire_assignments():
    """Auto-expire assignments past their due date."""
    from .models.user import Assignment

    db = next(get_db())
    try:
        now = datetime.utcnow()
        assignments = db.query(Assignment).filter(
            Assignment.status == "Active",
            Assignment.due_date.isnot(None),
            Assignment.due_time.isnot(None),
        ).all()

        updated = 0
        for a in assignments:
            try:
                d = a.due_date.split('/')
                t = a.due_time.split(':')
                if len(d) == 3:
                    due = datetime(
                        int(d[2]), int(d[1]), int(d[0]),
                        int(t[0]) if t else 23,
                        int(t[1]) if len(t) > 1 else 59,
                    )
                    if now > due:
                        a.status = "Expired"
                        updated += 1
            except Exception as e:
                print(f"❌ Parse error for assignment {a.id}: {e}")

        if updated:
            db.commit()
            print(f"✅ Auto-expired {updated} assignments at {now}")
    except Exception as e:
        print(f"❌ auto-expire error: {e}")
        db.rollback()
    finally:
        db.close()


def start_scheduler():
    if scheduler.running:
        return
    scheduler.add_job(
        auto_expire_assignments,
        trigger=IntervalTrigger(minutes=120),
        id="auto_expire_assignments",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    print("✅ Scheduler started (every 2 hours)")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        print("✅ Scheduler stopped")


def start_worker():
    from .worker import run_worker
    threading.Thread(target=run_worker, daemon=True).start()
    print("✅ Worker started")


# ── Lifespan ──────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    UPLOADS_DIR.mkdir(exist_ok=True)
    start_worker()
    start_scheduler()
    print("🚀 Application started")
    yield
    stop_scheduler()
    print("👋 Application shutdown")


# ── App ───────────────────────────────────────────────────────
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url=None if IS_PROD else "/docs",
    redoc_url=None if IS_PROD else "/redoc",
    openapi_url=None if IS_PROD else "/openapi.json",
)


# ── CORS ──────────────────────────────────────────────────────
ALLOWED_ORIGINS = [
    "https://scholin.ke",
    "https://www.scholin.ke",
    "https://app.scholin.ke",
]
if not IS_PROD:
    ALLOWED_ORIGINS += [
        "http://localhost:3000",
        "http://localhost:8080",
        "http://10.0.2.2:8000",
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)


# ── API Routes ────────────────────────────────────────────────
app.include_router(api_router, prefix=f"/api/{settings.APP_VERSION}")


# ── Static mounts ─────────────────────────────────────────────
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")

if (LANDING_DIR / "images").exists():
    app.mount(
        "/images",
        StaticFiles(directory=str(LANDING_DIR / "images")),
        name="landing-images",
    )


# ── Landing pages ─────────────────────────────────────────────
def _read_landing(filename: str) -> str:
    path = LANDING_DIR / filename
    if not path.exists():
        return f"<h1>404 — {filename} not found</h1>"
    return path.read_text(encoding="utf-8")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing():
    return _read_landing("index.html")


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
def privacy():
    return _read_landing("privacy.html")


@app.get("/terms", response_class=HTMLResponse, include_in_schema=False)
def terms():
    return _read_landing("terms.html")


# ── Health ────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "status": "healthy",
        "scheduler_running": scheduler.running,
        "active_jobs": len(scheduler.get_jobs()),
    }


# ── Admin-only manual triggers ────────────────────────────────
@app.post("/api/v1/admin/expire-assignments")
def manual_expire_assignments(_=Depends(require_admin)):
    auto_expire_assignments()
    return {"message": "Expiry check completed"}


@app.get("/api/v1/admin/scheduler-status")
def get_scheduler_status(_=Depends(require_admin)):
    return {
        "scheduler_running": scheduler.running,
        "jobs": [
            {
                "id": j.id,
                "next_run": str(j.next_run_time) if j.next_run_time else None,
                "trigger": str(j.trigger),
            }
            for j in scheduler.get_jobs()
        ],
    }


# ── Run locally ───────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
