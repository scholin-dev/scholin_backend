from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
import os
import threading

from .core.config import settings
from .core.database import engine, Base, get_db
from .api.v1.routes import api_router
from .core.deps import require_admin  # see note below

# ── Env detection ─────────────────────────────────────────────
IS_PROD = os.getenv("ENV", "dev").lower() == "prod"
DOCS_USER = os.getenv("DOCS_USER")
DOCS_PASS = os.getenv("DOCS_PASS")

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
        trigger=IntervalTrigger(minutes=120),  # every 2 hours
        id='auto_expire_assignments',
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


# ── Lifespan (replaces on_event) ──────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    Base.metadata.create_all(bind=engine)
    start_worker()
    start_scheduler()
    print("🚀 Application started")
    yield
    # Shutdown
    stop_scheduler()
    print("👋 Application shutdown")


# ── App ───────────────────────────────────────────────────────
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    # Hide interactive docs in prod
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
    # Dev only
    ALLOWED_ORIGINS += [
        "http://localhost:3000",
        "http://localhost:8080",
        "http://10.0.2.2:8000",  # Android emulator
    ]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
)

# ── Routes ────────────────────────────────────────────────────
app.include_router(api_router, prefix=f"/api/{settings.APP_VERSION}")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")


# ── Root / Health ─────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
    }


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
