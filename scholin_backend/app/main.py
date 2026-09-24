from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from .core.config import settings
from .core.database import engine, Base, get_db
from .api.v1.routes import api_router
import multiprocessing, uvicorn, threading
from app.worker import run_worker
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from app.scheduler import start_scheduler, stop_scheduler
from datetime import datetime
import os

# Create tables
Base.metadata.create_all(bind=engine)

# Initialize app
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc"
)

@app.on_event("startup")
def _start():
    start_scheduler()

@app.on_event("shutdown")
def _stop():
    stop_scheduler()

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Change to specific origins in production
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include API routes with version prefix
app.include_router(api_router, prefix=f"/api/{settings.APP_VERSION}")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

# ========== APSCHEDULER SETUP ==========
scheduler = BackgroundScheduler()

def auto_expire_assignments():
    """Auto-expire assignments that are past their due date"""
    from .api.v1.models import Assignment  # Import your Assignment model
    from sqlalchemy.orm import Session
    
    db = next(get_db())
    try:
        now = datetime.utcnow()
        
        # Get all active assignments with due dates
        assignments = db.query(Assignment).filter(
            Assignment.status == "Active",
            Assignment.due_date.isnot(None),
            Assignment.due_time.isnot(None)
        ).all()
        
        updated = 0
        for assignment in assignments:
            try:
                # Parse due date "DD/MM/YYYY"
                date_parts = assignment.due_date.split('/')
                # Parse due time "HH:MM"
                time_parts = assignment.due_time.split(':')
                
                if len(date_parts) == 3:
                    day = int(date_parts[0])
                    month = int(date_parts[1])
                    year = int(date_parts[2])
                    
                    hour = int(time_parts[0]) if time_parts else 23
                    minute = int(time_parts[1]) if len(time_parts) > 1 else 59
                    
                    due_datetime = datetime(year, month, day, hour, minute)
                    
                    if now > due_datetime:
                        assignment.status = "Expired"
                        updated += 1
                        print(f"⏰ Expired: {assignment.title} (was due {assignment.due_date} {assignment.due_time})")
            except Exception as e:
                print(f"❌ Error parsing date for assignment {assignment.id}: {e}")
        
        if updated > 0:
            db.commit()
            print(f"✅ Auto-expired {updated} assignments at {now}")
    
    except Exception as e:
        print(f"❌ Error in auto-expire: {e}")
        db.rollback()
    finally:
        db.close()


def start_scheduler():
    """Start the APScheduler for auto-expiring assignments"""
    try:
        scheduler.add_job(
            auto_expire_assignments,
            trigger=IntervalTrigger(minutes=120),  # Run every 1 minute
            id='auto_expire_assignments',
            replace_existing=True,
            max_instances=1  # Prevent overlapping runs
        )
        scheduler.start()
        print("✅ Auto-expire scheduler started (runs every 1 minute)")
    except Exception as e:
        print(f"❌ Failed to start scheduler: {e}")


def stop_scheduler():
    """Stop the scheduler"""
    if scheduler.running:
        scheduler.shutdown()
        print("✅ Scheduler stopped")

# ========== START WORKER ==========
def start_worker():
    """Start the message queue worker in a background thread"""
    from .worker import run_worker
    worker_thread = threading.Thread(target=run_worker, daemon=True)
    worker_thread.start()
    print("✅ Message Queue Worker started in background")


# ========== APP LIFECYCLE EVENTS ==========
@app.on_event("startup")
def startup_event():
    """Run on app startup"""
    start_worker()
    start_scheduler()
    print("🚀 Application started with all services")

@app.on_event("shutdown")
def shutdown_event():
    """Run on app shutdown"""
    stop_scheduler()
    print("👋 Application shutdown complete")


# Root endpoint
@app.get("/")
def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "status": "running",
        "docs": "/docs"
    }

# Health check
@app.get("/health")
def health():
    return {
        "status": "healthy",
        "scheduler_running": scheduler.running if scheduler else False,
        "active_jobs": len(scheduler.get_jobs()) if scheduler else 0
    }

# Manual trigger endpoint (for testing)
@app.post("/api/v1/admin/expire-assignments")
def manual_expire_assignments():
    """Manually trigger assignment expiry check"""
    auto_expire_assignments()
    return {"message": "Assignment expiry check completed"}

# Get scheduler status
@app.get("/api/v1/admin/scheduler-status")
def get_scheduler_status():
    """Get scheduler status"""
    jobs = []
    if scheduler:
        for job in scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "next_run": str(job.next_run_time) if job.next_run_time else None,
                "trigger": str(job.trigger)
            })
    
    return {
        "scheduler_running": scheduler.running if scheduler else False,
        "jobs": jobs
    }


# ========== START APPLICATION ==========
if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
    )
else:
    # When imported by uvicorn, check if we're the main process
    if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        print("✅ App imported, services will start on startup event")
