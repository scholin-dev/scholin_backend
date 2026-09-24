from datetime import date
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy import text
from app.models.user import School, FeeStructure


scheduler = AsyncIOScheduler(timezone="Africa/Nairobi")


def _current_term() -> tuple[int, int]:
    today = date.today()
    m = today.month
    term = 1 if m <= 4 else 2 if m <= 8 else 3
    return today.year, term


def _bill_current_term():
    """
    Runs daily. Creates missing Fee rows for the current term
    for every active student in every school that has a fee structure.
    Idempotent — does nothing if rows already exist.
    """
    from app.core.database import SessionLocal

    year, term = _current_term()
    with SessionLocal() as db:
        schools = db.query(School).all()
        total_created = 0

        for school in schools:
            struct = db.query(FeeStructure).filter(
                FeeStructure.school_id == school.id,
                FeeStructure.academic_year == year,
                FeeStructure.term_number == term,
            ).first()
            if not struct:
                continue

            result = db.execute(text("""
                INSERT INTO fees (
                    student_id, amount, paid, balance,
                    academic_year, term_number, term_name, status
                )
                SELECT s.user_id, :amount, 0, :amount,
                       :year, :term, :term_name, 'pending'
                FROM students s
                LEFT JOIN fees f
                  ON f.student_id = s.user_id
                 AND f.academic_year = :year
                 AND f.term_number = :term
                WHERE s.school_id = :school_id
                  AND s.is_active = true
                  AND f.id IS NULL
                ON CONFLICT (student_id, academic_year, term_number) DO NOTHING
            """), {
                "school_id": school.id,
                "year": year,
                "term": term,
                "term_name": struct.term_name,
                "amount": struct.amount,
            })
            db.commit()
            total_created += result.rowcount

        print(f"🔔 Scheduler: {year} T{term} — created {total_created} fee rows")


def start_scheduler():
    scheduler.add_job(
        _bill_current_term,
        CronTrigger(hour=6, minute=0),
        id="daily_term_billing",
        replace_existing=True,
    )
    scheduler.start()
    print("✅ Scheduler started (daily fee billing at 06:00 EAT)")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
