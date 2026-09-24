from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional

from ....core.database import get_db
from ....core.session_auth import get_current_user
from ....models.user import (
    User, Student, Teacher, Parent, Worker, School, ParentStudent,
    Timetable, StudentPerformance, Fee, Event, Announcement,
    Assignment, Activity
)

router = APIRouter(prefix="/me", tags=["Me"])



# ========== PROFILE ==========
@router.get("/profile")
def get_my_profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Returns profile based on role"""
    data = {"id": user.id, "username": user.username, "email": user.email,
            "full_name": user.full_name, "role": user.role, "phone": user.phone, "profile_picture": user.profile_picture}
    
    if user.role == "student":
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if student:
            data.update({"admission": student.admission_number, "class": student.class_name,
                         "gender": student.gender, "school": student.school_name,"profile_picture": student.profile_picture or user.profile_picture})
    elif user.role == "teacher":
        teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
        if teacher:
            data.update({"subject": teacher.subject, "qualification": teacher.qualification,
                         "experience": teacher.years_of_experience, "school": teacher.school_name, "profile_picture": teacher.profile_picture or user.profile_picture})
    elif user.role == "parent":
        parent = db.query(Parent).filter(Parent.user_id == user.id).first()
        if parent:
            data.update({"relationship": parent.relationship, "school": parent.school_name, "profile_picture": parent.profile_picture or user.profile_picture})
            # Get all children from junction table
            from app.models.user import ParentStudent
            links = db.query(ParentStudent).filter(ParentStudent.parent_id == parent.id).all()
            children = []
            for link in links:
                student = db.query(Student).filter(Student.id == link.student_id).first()
                if student:
                    child_user = db.query(User).filter(User.id == student.user_id).first()
                    children.append({
                        "student_id": student.id,
                        "name": child_user.full_name if child_user else None,
                        "class": student.class_name,
                        "relationship": link.relationship or parent.relationship
                    })
            data.update({"children": children, "total_children": len(children)})
    
    return {"data": data}

# ========== DASHBOARD (role-specific) ==========
@router.get("/dashboard")
def get_my_dashboard(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role == "student":
        return _student_dashboard(user, db)
    elif user.role == "teacher":
        return _teacher_dashboard(user, db)
    elif user.role == "parent":
        return _parent_dashboard(user, db)
    elif user.role == "admin":
        return _admin_dashboard(db)
    return {"message": "No dashboard for this role"}

def _student_dashboard(user, db):
    student = db.query(Student).filter(Student.user_id == user.id).first()
    if not student: raise HTTPException(status_code=404, detail="Student profile not found")
    
    performances = db.query(StudentPerformance).filter(StudentPerformance.student_id == user.id).all()
    subjects = list(set(p.subject for p in performances))
    fees = db.query(Fee).filter(Fee.student_id == user.id).all()
    total_fees = sum(f.amount for f in fees)
    total_paid = sum(f.paid for f in fees)
    
    return {
        "student": {"id": user.id, "name": user.full_name, "class": student.class_name,
                    "admission": student.admission_number, "school": student.school_name,
                    "gender": student.gender},
        "subjects": subjects,
        "performance": [{"subject": p.subject, "score": p.score, "term": p.term} for p in performances],
        "fees": {"total": total_fees, "paid": total_paid, "balance": total_fees - total_paid},
        "average": round(sum(p.score for p in performances) / len(performances), 1) if performances else 0,
    }

def _teacher_dashboard(user, db):
    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
    if not teacher: raise HTTPException(status_code=404, detail="Teacher profile not found")
    
    total_students = db.query(Student).filter(Student.school_id == teacher.school_id).count() if teacher.school_id else db.query(Student).filter(Student.school_name == teacher.school_name).count()
    performances = db.query(StudentPerformance).filter(StudentPerformance.subject == teacher.subject).all()
    avg_score = round(sum(p.score for p in performances) / len(performances), 1) if performances else 0
    
    return {
        "teacher": {"id": user.id, "name": user.full_name, "subject": teacher.subject,
                    "qualification": teacher.qualification},
        "total_students": total_students, "average_score": avg_score,
        "total_assignments": db.query(Assignment).filter(Assignment.teacher_id == user.id).count(),
        "attendance_rate": 94,
    }

def _parent_dashboard(user, db):
    parent = db.query(Parent).filter(Parent.user_id == user.id).first()
    if not parent: raise HTTPException(status_code=404, detail="Parent profile not found")
    
    students = db.query(Student).filter(Student.school_id == parent.school_id).all() if parent.school_id else db.query(Student).filter(Student.school_name == parent.school_name).all()
    children = []
    for student in students[:5]:
        child_user = db.query(User).filter(User.id == student.user_id).first()
        if child_user:
            perfs = db.query(StudentPerformance).filter(StudentPerformance.student_id == student.user_id).all()
            avg = round(sum(p.score for p in perfs) / len(perfs), 1) if perfs else 0
            fees = db.query(Fee).filter(Fee.student_id == student.user_id).all()
            total_fees = sum(f.amount for f in fees)
            total_paid = sum(f.paid for f in fees)
            children.append({
                "id": student.user_id, "name": child_user.full_name,
                "class": student.class_name, "average": avg,
                "fees_paid": total_paid, "fees_total": total_fees,
                "fees_percent": round(total_paid / total_fees * 100) if total_fees > 0 else 0,
            })
    
    return {
        "parent": {"id": user.id, "name": user.full_name, "phone": user.phone},
        "children": children, "school": parent.school_name,
        "total_children": len(children),
    }

def _admin_dashboard(db):
    return {
        "total_students": db.query(Student).count(),
        "total_teachers": db.query(Teacher).count(),
        "total_parents": db.query(Parent).count(),
        "total_schools": db.query(School).count(),
        "total_users": db.query(User).count(),
        "active_users": db.query(User).filter(User.is_active == True).count(),
    }

# ========== TIMETABLE (for current user) ==========
@router.get("/timetable")
def get_my_timetable(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    class_name = None
    
    if user.role == "student":
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if student: class_name = student.class_name
    elif user.role == "teacher":
        today = datetime.utcnow().strftime("%A")
        lessons = db.query(Timetable).filter(
            Timetable.teacher_id == user.id, Timetable.day_of_week == today
        ).order_by(Timetable.start_time).all()
        return {"data": [{"time": f"{l.start_time}-{l.end_time}", "subject": l.subject,
                          "class": l.class_name, "day": l.day_of_week} for l in lessons],
                "day": today}
    
    if class_name:
        today = datetime.utcnow().strftime("%A")
        lessons = db.query(Timetable).filter(
            Timetable.class_name == class_name, Timetable.day_of_week == today
        ).order_by(Timetable.start_time).all()
        return {"data": [{"time": f"{l.start_time}-{l.end_time}", "subject": l.subject,
                          "class": l.class_name, "teacher": "N/A", "is_break": l.is_break}
                         for l in lessons], "class": class_name, "day": today}
    
    return {"data": [], "message": "No class assigned"}

# ========== ASSIGNMENTS ==========
@router.get("/assignments")
def get_my_assignments(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.role == "teacher":
        assignments = db.query(Assignment).filter(Assignment.teacher_id == user.id).all()
        return {"data": [{"id": a.id, "title": a.title, "subject": a.subject, "class": a.class_name,
                          "dueDate": a.due_date, "status": a.status, "submitted": a.submitted_count,
                          "total": a.total_students} for a in assignments]}
    elif user.role == "student":
        return {"data": [
            {"title": "Mathematics Revision", "subject": "Mathematics", "due": "2026-07-15", "status": "Pending"},
            {"title": "English Essay", "subject": "English", "due": "2026-07-12", "status": "Submitted"},
            {"title": "Science Project", "subject": "Science", "due": "2026-07-20", "status": "Pending"},
        ]}
    return {"data": []}

# ========== EVENTS & ANNOUNCEMENTS (shared) ==========
@router.get("/events")
def get_my_events(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = datetime.utcnow()
    events = db.query(Event).filter(Event.event_date >= now).order_by(Event.event_date).limit(5).all()
    months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    return {"data": [{"id": e.id, "title": e.title, "description": e.description,
                      "date": f"{months[e.event_date.month-1]} {e.event_date.day}",
                      "time": e.time or "All Day"} for e in events]}

@router.get("/announcements")
def get_my_announcements(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    announcements = db.query(Announcement).order_by(Announcement.created_at.desc()).limit(5).all()
    result = []
    for a in announcements:
        diff = datetime.utcnow().replace(tzinfo=None) - a.created_at.replace(tzinfo=None)
        time_ago = f"{diff.days}d ago" if diff.days > 0 else f"{diff.seconds//3600}h ago" if diff.seconds > 3600 else "Just now"
        result.append({"id": a.id, "title": a.title, "description": a.description, "time": time_ago})
    return {"data": result}
