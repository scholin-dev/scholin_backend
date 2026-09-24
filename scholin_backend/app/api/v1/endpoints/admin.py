from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form, Request, status, Body, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse, Response
from sqlalchemy.orm import Session
from sqlalchemy import case, func, desc, and_, or_, desc
from datetime import datetime, timedelta, timezone
from ....models.user import Session as Refresh
from typing import Optional, List, Tuple, Dict, Any
from datetime import datetime
from ....core.config import settings
from weasyprint import HTML
from PIL import Image
import os, io, re, json, asyncio, time, smtplib, threading, uuid, shutil, subprocess, base64, httpx, csv, traceback
from google import genai
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import pandas as pd

from ....models.message_queue import MessageQueue
from concurrent.futures import ThreadPoolExecutor

from ....core.security import verify_password, get_password_hash
from ....core.database import get_db, SessionLocal
from ....core.session_auth import get_current_user, get_current_school_id
from ....models.user import (
    User, Student, Teacher, Parent, School,
    StudentPerformance, Fee, FeePayment, Event, Announcement, Activity, Timetable, Worker, WorkerAttendance, WorkerPerformance, Assignment, Job, Book, Class, ClassSubjectTeacher, ParentStudent, StudentAttendance, RevisionMaterial, TeacherEmploymentHistory, AdminEmploymentHistory, WorkerEmploymentHistory, ParentUpload, View, Lastpage, Rating, Applicant, Alert, Chat, ApplicationDocument, FeeTransaction, FeeStructure, AssignmentSubmission, Group, GroupMember, GroupMessage, SmsTopup, BookPurchase
)

from app.worker import send_sms_batch, send_email_batch
from pydantic import BaseModel, Field
class ParentData(BaseModel):
    parent_name: Optional[str] = ""
    email: Optional[str] = ""
    phone: Optional[str] = ""

class ImportParentRequest(BaseModel):
    parents: List[ParentData]
    school_id: int
    student_name: Optional[str] = ""
    admission_number: Optional[str] = ""
    class_name: Optional[str] = ""

class FeeStructureCreate(BaseModel):
    academic_year: int
    term_number: int
    amount: int

class FeeStructureUpdate(BaseModel):
    amount: int
    
class GroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=500)
    type: str = Field('individual', description="individual or class")
    members: Optional[List[int]] = Field(default_factory=list)
    class_name: Optional[str] = Field(None, max_length=50)
    created_by: Optional[int] = None
    creator_role: Optional[str] = 'admin'

class GroupMessageCreate(BaseModel):
    message: str = Field(..., min_length=1)
    reply_to_id: Optional[int] = None

router = APIRouter(prefix="/admin", tags=["Admin"])
load_dotenv()

def _grade_from_percent(pct: float) -> str:
    if pct >= 80:
        return "A"
    if pct >= 75:
        return "A-"
    if pct >= 70:
        return "B+"
    if pct >= 65:
        return "B"
    if pct >= 60:
        return "B-"
    if pct >= 55:
        return "C+"
    if pct >= 50:
        return "C"
    if pct >= 45:
        return "C-"
    if pct >= 40:
        return "D+"
    if pct >= 35:
        return "D"
    if pct >= 30:
        return "D-"
    return "E"

# Replace the stats endpoint at the top of admin.py
@router.get("/stats/{school_id}")
def get_admin_stats(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    school_id: Optional[int] = Depends(get_current_school_id)
):
    if user.role not in ['admin', 'school'] and user.school_id != school_id:
      raise HTTPException(status_code=403, detail="Not Authorized")
    # Base queries
    student_query = db.query(Student)
    teacher_query = db.query(Teacher)
    worker_query = db.query(Worker)
    
    # Filter by school if school_id is set
    if school_id:
        student_query = student_query.filter(Student.school_id == school_id)
        teacher_query = teacher_query.filter(Teacher.school_id == school_id)
        worker_query = worker_query.filter(Worker.school_id == school_id)
    
    total_students = student_query.count()
    total_teachers = teacher_query.count()
    total_schools = db.query(School).count()
    total_workers = worker_query.count()
    
    return {
        "total_students": total_students,
        "total_teachers": total_teachers,
        "total_workers": total_workers,
        "school_id": school_id
    }

# ========== STUDENTS ==========
@router.get("/students")
def get_students(
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    school_id: Optional[int] = Depends(get_current_school_id),
    current_user: User = Depends(get_current_user)
):
    query = db.query(Student, User).join(User, Student.user_id == User.id)
    
    if school_id:
        query = query.filter(Student.school_id == school_id)
    if current_user.role == "teacher":
        teacher = db.query(Teacher).filter(Teacher.user_id == current_user.id).first()
        if teacher:
            cst_class_ids = [row[0] for row in db.query(ClassSubjectTeacher.class_id).filter(
                ClassSubjectTeacher.teacher_id == teacher.id,
                ClassSubjectTeacher.is_active == True
            ).all()]
            
            if cst_class_ids:
                query = query.filter(Student.class_id.in_(cst_class_ids))
            else:
                raise HTTPException(status_code=404, detail="no classes found")
    
    if search:
        query = query.filter(
            (User.full_name.ilike(f"%{search}%")) |
            (User.email.ilike(f"%{search}%")) |
            (Student.admission_number.ilike(f"%{search}%"))
        )
    if status == "active":
        query = query.filter(User.is_active == True)
    elif status == "inactive":
        query = query.filter(User.is_active == False)
    
    total = query.count()
    results = query.offset((page - 1) * limit).limit(limit).all()
    
    student_ids = [student.id for student, _ in results]
    class_ids = list(set(student.class_id for student, _ in results if student.class_id))
    
    # Bulk fetch class teachers
    class_teacher_map = {}
    if class_ids:
        class_teachers = db.query(Class.id, User.full_name)\
            .join(Teacher, Class.class_teacher_id == Teacher.id)\
            .join(User, Teacher.user_id == User.id)\
            .filter(Class.id.in_(class_ids)).all()
        class_teacher_map = {ct[0]: ct[1] for ct in class_teachers}
    
    # Bulk fetch parents
    parent_map = {}
    if student_ids:
        parent_rows = db.query(ParentStudent.student_id, User.full_name, ParentStudent.relation_type)\
            .join(Parent, ParentStudent.parent_id == Parent.id)\
            .join(User, Parent.user_id == User.id)\
            .filter(ParentStudent.student_id.in_(student_ids)).all()
        for student_id, parent_name, relation in parent_rows:
            parent_map.setdefault(student_id, []).append({"name": parent_name, "relation": relation})
    
    # ✅ FIX: Use user from the JOIN, don't re-query!
    students = []
    for student, user in results:
        students.append({
            "id": student.id,
            "student_user_id": student.user_id,
            "admission_number": student.admission_number,
            "name": user.full_name,
            "email": user.email,
            "phone": user.phone,
            "class": student.class_name,
            "gender": student.gender,
            "school": student.school_name,
            "class_teacher": class_teacher_map.get(student.class_id),
            "parents": parent_map.get(student.id, []),
            "status": "Active" if user.is_active else "Inactive",
            "created_at": str(user.created_at),
            "profile_picture": user.profile_picture
        })
    
    return {"data": students, "total": total, "page": page, "pages": (total + limit - 1) // limit}
    
# ========== STUDENTS ==========
@router.get("/parents/students-for-linking")
def get_students_for_linking(
    school_id: int = Query(...),
    class_id: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get students available for parent linking.
    Filters by school_id and optionally class_id.
    """
    query = db.query(Student, User).join(User, Student.user_id == User.id)
    
    query = query.filter(Student.school_id == school_id)
    
    if class_id:
        query = query.filter(Student.class_id == class_id)
    
    if search:
        query = query.filter(
            (User.full_name.ilike(f"%{search}%")) |
            (Student.admission_number.ilike(f"%{search}%"))
        )
    
    # Get results
    results = query.limit(100).all()
    
    students = []
    for student, user in results:
        students.append({
            "id": student.id,
            "student_id": student.id,
            "user_id": user.id,
            "admission_number": student.admission_number,
            "name": user.full_name,
            "email": user.email,
            "phone": user.phone,
            "class": student.class_name,
            "class_name": student.class_name,
            "class_id": student.class_id,
            "gender": student.gender,
            "school": student.school_name,
            "school_id": student.school_id,
            "status": "Active" if user.is_active else "Inactive",
            "profile_picture": user.profile_picture
        })
    
    return {
        "data": students,
        "total": len(students),
        "page": 1,
        "pages": 1
    }
    

# ========== TEACHERS ==========
@router.get("/teachers")
def get_teachers(
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    school_id = get_user_school_id(user, db)
    query = db.query(Teacher, User).join(User, Teacher.user_id == User.id)
    if school_id:
        query = query.filter(Teacher.school_id == school_id)
    if search:
        query = query.filter(
            (User.full_name.ilike(f"%{search}%")) |
            (User.email.ilike(f"%{search}%")) |
            (Teacher.subject.ilike(f"%{search}%"))
        )
    if status == "active":
        query = query.filter(User.is_active == True)
    elif status == "inactive":
        query = query.filter(User.is_active == False)
    
    total = query.count()
    results = query.offset((page - 1) * limit).limit(limit).all()
    
    
    # Bulk fetch school names
    school_ids = list(set(t.school_id for t, _ in results if t.school_id))
    school_map = {}
    if school_ids:
        schools = db.query(School.id, School.school_name).filter(School.id.in_(school_ids)).all()
        school_map = {s[0]: s[1] for s in schools}
    
    # Bulk fetch classes taught (via ClassSubjectTeacher)
    teacher_ids = [t.id for t, _ in results]
    class_map = {}
    if teacher_ids:
        cst_rows = db.query(
            ClassSubjectTeacher.teacher_id, 
            Class.name
        ).join(Class, ClassSubjectTeacher.class_id == Class.id)\
         .filter(ClassSubjectTeacher.teacher_id.in_(teacher_ids)).all()
        for tid, cname in cst_rows:
            if tid not in class_map:
                class_map[tid] = []
            if cname not in class_map[tid]:
                class_map[tid].append(cname)
    
    # Bulk fetch class_teacher_of
    class_teacher_map = {}
    if teacher_ids:
        ct_rows = db.query(Class.class_teacher_id, Class.name)\
            .filter(Class.class_teacher_id.in_(teacher_ids)).all()
        for ctid, cname in ct_rows:
            class_teacher_map[ctid] = cname
    
    teachers = []
    for teacher, user in results:
        teachers.append({
            "id": user.id,
            "name": user.full_name,
            "email": user.email,
            "phone": user.phone,
            "subject": teacher.subject,
            "qualification": teacher.qualification,
            "experience": teacher.years_of_experience,
            "school": school_map.get(teacher.school_id),
            "classes": class_map.get(teacher.id, []),
            "class_teacher_of": class_teacher_map.get(teacher.id),
            "status": "Active" if user.is_active else "Inactive",
            "created_at": str(user.created_at),
            "photo": user.profile_picture
        })
    return {"data": teachers, "total": total, "page": page, "pages": (total + limit - 1) // limit}

# ========== PERFORMANCE ==========
@router.get("/performance/{school_id}")
def get_performance(
    school_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get top performing classes for a school"""
    
    # ✅ Join with Class table to get class names
    query = db.query(
        Class.name.label('class_name'),
        func.avg(StudentPerformance.score).label('average'),
        func.count(func.distinct(StudentPerformance.student_id)).label('student_count')
    ).join(
        Student, StudentPerformance.student_id == Student.user_id
    ).join(
        Class, Student.class_id == Class.id
    ).filter(
        Student.school_id == school_id
    ).group_by(
        Class.name
    ).order_by(
        desc('average')
    ).limit(5).all()
    
    top_classes = []
    for row in query:
        avg = round(row.average, 1) if row.average else 0
        top_classes.append({
            "class": row.class_name,
            "average": avg,
            "performance": f"{avg}%",
            "students": row.student_count
        })
    
    return {"data": top_classes}

# ========== FEES ==========
    
@router.get("/fees/payment-methods")
async def get_payment_methods_stats(
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Get payment method breakdown for the school"""
    if user.role not in ['admin', 'school']:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    # Get all transactions for this school
    transactions = db.query(FeeTransaction).join(
        Student, FeeTransaction.student_id == Student.user_id
    ).filter(
        Student.school_id == user.school_id,
        FeeTransaction.amount > 0
    ).all()
    
    # Calculate totals per payment method
    method_totals = {}
    total_amount = 0
    
    for tx in transactions:
        method = tx.payment_provider or 'Other'
        amount = tx.amount
        method_totals[method] = method_totals.get(method, 0) + amount
        total_amount += amount
    
    # Calculate percentages
    methods = []
    for method, amount in method_totals.items():
        percentage = (amount / total_amount * 100) if total_amount > 0 else 0
        methods.append({
            'method': method,
            'amount': amount,
            'percentage': round(percentage, 1),
        })
    
    # Sort by amount descending
    methods.sort(key=lambda x: x['amount'], reverse=True)
    
    return {
        'data': methods,
        'total': total_amount
    }
    
@router.get("/fees/{school_id}")
async def get_fees_summary(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    school_id: Optional[int] = Depends(get_current_school_id)
):
    if user.role not in ['admin', 'school']:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    target_school_id = school_id if school_id else user.school_id
    
    # Subquery for students in this school
    student_ids = db.query(Student.user_id).filter(
        Student.school_id == target_school_id
    ).subquery()
    
    # Single aggregated query for all fee stats
    stats = db.query(
        func.sum(case((Fee.status != "upcoming", Fee.amount), else_=0)).label('total_fees'),
        func.sum(case((Fee.status == "paid", 1), else_=0)).label('paid_count'),
        func.sum(case((Fee.status == "partial", 1), else_=0)).label('partial_count'),
        func.sum(case((Fee.status == "pending", 1), else_=0)).label('pending_count'),
        func.sum(case((Fee.status == "overdue", 1), else_=0)).label('overdue_count'),
    ).filter(
        Fee.student_id.in_(student_ids)
    ).first()
    
    # Single query for total collected
    total_collected = db.query(
        func.sum(FeeTransaction.amount)
    ).filter(
        FeeTransaction.student_id.in_(student_ids),
        FeeTransaction.amount > 0
    ).scalar() or 0
    
    total_fees = stats[0] or 0
    paid_count = stats[1] or 0
    partial_count = stats[2] or 0
    pending_count = stats[3] or 0
    overdue_count = stats[4] or 0
    
    balance = max(0, total_fees - total_collected)
    overpaid = max(0, total_collected - total_fees)
    percentage = round((total_collected / total_fees * 100) if total_fees > 0 else 0, 1)
    
    return {
        "total": total_fees,
        "collected": total_collected,
        "balance": balance,
        "overpaid": overpaid,
        "percentage": f"{percentage}%",
        "pending": pending_count,
        "partial": partial_count,
        "paid": paid_count,
        "overdue": overdue_count,
        "reminders": [
            f"{pending_count} students have pending fees",
            f"{partial_count} students have partial payments",
            f"{overdue_count} students have overdue fees",
            f"Total outstanding: KES {balance:,}"
        ]
    }

# ========== EVENTS ==========
@router.get("/events")
def get_events(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    school_id: Optional[int] = Depends(get_current_school_id)
):
    if user.school_id != school_id:
      raise HTTPException(status_code=403, detail="Not Authorized")
    now = datetime.now(timezone.utc)
    query = db.query(Event).filter(Event.event_date >= now)
    
    if school_id:
        query = query.filter(Event.school_id == school_id)
    
    events = query.order_by(Event.event_date).limit(5).all()
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    result = []
    for event in events:
        if event.event_date < now:
            continue
            
        result.append({
            "id": event.id, "title": event.title, "description": event.description,
            "date": f"{months[event.event_date.month - 1]} {event.event_date.day}",
            "time": event.time or "All Day", "full_date": str(event.event_date)
        })
    return {"data": result}

# ========== ANNOUNCEMENTS ==========
@router.get("/announcements/{school_id}")
def get_announcements(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    school_id: Optional[int] = Depends(get_current_school_id)
):
    if user.school_id != school_id:
      raise HTTPException(status_code=403, detail="Not Authorized")
    query = db.query(Announcement)
    
    if school_id:
        query = query.filter(Announcement.school_id == school_id)
    
    announcements = query.order_by(desc(Announcement.created_at)).limit(10).all()
    result = []
    
    for ann in announcements:
        diff = datetime.utcnow().replace(tzinfo=None) - ann.created_at.replace(tzinfo=None)
        if diff.days > 0: time_ago = f"{diff.days}d ago"
        elif diff.seconds > 3600: time_ago = f"{diff.seconds // 3600}h ago"
        elif diff.seconds > 60: time_ago = f"{diff.seconds // 60}m ago"
        else: time_ago = "Just now"
        
        result.append({
            "id": ann.id,
            "title": ann.title,
            "description": ann.description,
            "time": time_ago,
            "created_at": str(ann.created_at)
        })
    return {"data": result}

# ========== ATTENDANCE ==========
@router.get("/attendance/{school_id}")
def get_attendance(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    school_id: Optional[int] = Depends(get_current_school_id)
):
    if user.school_id != school_id:
      raise HTTPException(status_code=403, detail="Not Authorised")
    today = datetime.utcnow().date()
    
    # Students
    student_query = db.query(StudentAttendance).filter(
        func.date(StudentAttendance.date) == today
    )
    
    if school_id:
        student_query = student_query.join(Student, StudentAttendance.student_id == Student.user_id)\
                                     .filter(Student.school_id == school_id)
    
    total_s = student_query.count()
    present_s = student_query.filter(StudentAttendance.status == 'present').count()
    
    return {
        "students": {
            "present": present_s,
            "absent": total_s - present_s,
            "total": total_s,
            "percentage": f"{round(present_s/total_s*100,1)}%" if total_s > 0 else "0%"
        }
    }

@router.get("/student-stats/{student_id}")
def get_student_stats(student_id: int, db: Session = Depends(get_db)):
    """Get performance stats for a specific student"""
    student = db.query(Student).filter(Student.user_id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    user = db.query(User).filter(User.id == student.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Performance
    performances = db.query(StudentPerformance).filter(
        StudentPerformance.student_id == student_id
    ).all()
    
    total_score = sum(p.score for p in performances)
    count = len(performances)
    average = round(total_score / count, 1) if count > 0 else 0
    
    # Real attendance from timetable (total lessons for student's class)
    total_lessons = db.query(Timetable).filter(
        Timetable.class_name == student.class_name,
        Timetable.school_id == student.school_id,
        Timetable.is_break == False
    ).count()
    
    # For now, assume 90% attendance (you'd need a student_attendance table for real data)
    present = int(total_lessons * 0.9) if total_lessons > 0 else 42
    absent = total_lessons - present if total_lessons > 0 else 3
    attendance_pct = round(present / total_lessons * 100, 1) if total_lessons > 0 else 92
    dp = user.profile_picture
    
    return {
        "average": average,
        "attendance": attendance_pct,
        "present": present,
        "total": total_lessons,
        "absent": absent,
        "dp": dp,
        "subjects": [{"subject": p.subject, "score": p.score, "term": p.term} for p in performances]
    }

# ========== ACTIVITIES ==========
@router.get("/student-activities/{student_id}")
def get_student_activities(student_id: int, db: Session = Depends(get_db)):
    activities = db.query(Activity).filter(Activity.user_id == student_id).all()
    result = []
    icon_map = {"book": "book", "sports_soccer": "sports_soccer", "music_note": "music_note", "science": "science", "school": "school", "construction": "construction", "security": "security", "event": "event"}
    for a in activities:
        result.append({"id": a.id, "name": a.name, "role": a.role, "icon": a.icon_name})
    return {"data": result}

# ========== TEACHER PROFILE DATA ==========
@router.get("/teacher-profile/{teacher_id}")
async def get_teacher_profile(
    teacher_id: int, 
    current_user: User = Depends(get_current_user), 
    db: Session = Depends(get_db)
):
    # Single query for teacher + user + school using JOIN
    teacher_data = db.query(
        Teacher, User, School
    ).join(
        User, User.id == Teacher.user_id
    ).outerjoin(
        School, School.id == Teacher.school_id
    ).filter(
        Teacher.user_id == teacher_id
    ).first()
    
    if not teacher_data:
        raise HTTPException(status_code=404, detail="Teacher not found")
    
    teacher, user, school = teacher_data
    
    # Access control
    if current_user.role not in ["admin", "school"] and current_user.id != teacher.user_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    # Get all CSTs with class info and student counts in ONE query
    cst_data = db.query(
        ClassSubjectTeacher,
        Class.name,
        func.count(Student.id).label('student_count')
    ).outerjoin(
        Class, Class.id == ClassSubjectTeacher.class_id
    ).outerjoin(
        Student, Student.class_id == ClassSubjectTeacher.class_id
    ).filter(
        ClassSubjectTeacher.teacher_id == teacher.id
    ).group_by(
        ClassSubjectTeacher.id, Class.name
    ).all()
    
    # Build subjects list
    subjects = []
    subjects_list = []
    for cst, class_name, student_count in cst_data:
        if cst.subject not in subjects_list:
            subjects_list.append(cst.subject)
        subjects.append({
            "name": cst.subject,
            "class": class_name or "N/A",
            "students": student_count or 0
        })
    
    if not subjects:
        subjects = [{"name": teacher.subject or "N/A", "class": "N/A", "students": 0}]
    
    # Performance with optimized query using CASE for pass rate calculation
    performance_data = []
    if subjects_list:
        perf_query = db.query(
            StudentPerformance.class_id,
            StudentPerformance.subject,
            func.avg(StudentPerformance.score).label('avg_score'),
            func.sum(
                case(
                    (StudentPerformance.score >= 50, 1),
                    else_=0
                )
            ).label('pass_count'),
            func.count(StudentPerformance.id).label('total_count')
        ).filter(
            StudentPerformance.subject.in_(subjects_list)
        ).group_by(
            StudentPerformance.class_id,
            StudentPerformance.subject
        ).all()
        
        performance_data = [
            {
                "class": class_id,
                "subject": subject,
                "average": round(float(avg_score), 1) if avg_score is not None else 0,
                "passRate": round(float(pass_count) / float(total_count) * 100, 1) if total_count else 0
            }
            for class_id, subject, avg_score, pass_count, total_count in perf_query
        ]
    
    # Get activities
    activities = db.query(Activity).filter(
        Activity.user_id == teacher_id
    ).all()
    
    activity_data = [
        {
            "name": a.name, 
            "role": a.role, 
            "icon": a.icon_name
        } 
        for a in activities
    ]
    
    # Get today's timetable
    today = datetime.utcnow().strftime("%A")
    lessons = db.query(Timetable).filter(
        Timetable.teacher_id == teacher.user_id,
        Timetable.day_of_week == today
    ).order_by(
        Timetable.start_time
    ).limit(5).all()
    
    timetable_data = [
        {
            "time": f"{l.start_time}-{l.end_time}", 
            "subject": l.subject, 
            "class": l.class_name
        }
        for l in lessons
    ]
    
    # Get recent assignments
    assignments = db.query(Assignment).filter(
        Assignment.teacher_id == teacher.user_id
    ).order_by(
        desc(Assignment.created_at)
    ).limit(5).all()
    
    assignment_data = [
        {
            "title": a.title, 
            "subject": a.subject, 
            "class": a.class_name,
            "dueDate": a.due_date, 
            "submitted": a.submitted_count,
            "total": a.total_students, 
            "status": a.status
        }
        for a in assignments
    ]
    
    # Build final response
    response_data = {
        "teacher": {
            "id": user.id, 
            "name": user.full_name, 
            "email": user.email,
            "phone": user.phone, 
            "subject": teacher.subject,
            "qualification": teacher.qualification, 
            "experience": teacher.years_of_experience,
            "school": school.school_name if school else None, 
            "status": "Active" if user.is_active else "Inactive"
        },
        "subjects": subjects,
        "performance": performance_data,
        "activities": activity_data,
        "timetable": timetable_data,
        "assignments": assignment_data,
        "photo": user.profile_picture
    }
    
    return response_data

# ========== TIMETABLE ==========
@router.get("/timetable/teacher/{teacher_id}")
async def get_teacher_timetable(
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")

    # 1. Get this teacher's (class_id, subject) pairs from class_subject_teachers
    cst_rows = (
        db.query(ClassSubjectTeacher.class_id, ClassSubjectTeacher.subject)
        .filter(
            ClassSubjectTeacher.teacher_id == teacher.id,
            ClassSubjectTeacher.is_active == True,
        )
        .all()
    )
    if not cst_rows:
        return {"data": [], "total": 0}

    class_ids = list({r[0] for r in cst_rows if r[0]})
    subjects  = list({r[1] for r in cst_rows if r[1]})

    # 2. Translate class IDs → class names (Timetable stores class_name)
    class_rows = db.query(Class).filter(Class.id.in_(class_ids)).all()
    class_names = [c.name for c in class_rows if c.name]

    if not class_names or not subjects:
        return {"data": [], "total": 0}

    # 3. Query the Timetable for (class_name, subject) pairs the teacher teaches
    conditions = []
    for cst in cst_rows:
        # each cst row = one (class_id, subject) assignment
        cls = next((c for c in class_rows if c.id == cst.class_id), None)
        if not cls or not cls.name or not cst.subject:
            continue
        conditions.append(
            and_(
                Timetable.class_name == cls.name,
                Timetable.subject == cst.subject,
            )
        )

    if not conditions:
        return {"data": [], "total": 0}

    lessons = (
        db.query(Timetable)
        .filter(
            Timetable.school_id == teacher.school_id,
            or_(*conditions),
        )
        .order_by(Timetable.day_of_week, Timetable.start_time)
        .all()
    )

    return {
        "data": [
            {
                "id": l.id,
                "time": f"{l.start_time}-{l.end_time}",
                "subject": l.subject,
                "class_id": next((c.id for c in class_rows if c.name == l.class_name), None),
                "class": l.class_name,
                "teacher_id": l.teacher_id,
                "day": l.day_of_week,
                "room": l.room,
                "is_break": l.is_break,
            }
            for l in lessons
        ],
        "total": len(lessons),
    }
    
@router.delete("/assignments/{assignment_id}")
def delete_assignment(
    assignment_id: int,
    teacher_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Delete an assignment"""
    if user.role != 'teacher':
        raise HTTPException(status_code=403, detail="Unauthorized activity")
    
    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
    
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not Found")
    
    assignment = db.query(Assignment).filter(
        Assignment.id == assignment_id,
        Assignment.teacher_id == teacher.id
    ).first()
    
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    db.delete(assignment)
    db.commit()
    
    return {"success": True, "message": "Assignment deleted successfully"}

@router.get("/timetable/school/{school_id}")
async def get_school_timetable(
    day: Optional[str] = Query(None),
    class_name: Optional[str] = Query(None),
    compact: bool = Query(True),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    school_id = user.school_id
    query = db.query(Timetable).filter(Timetable.school_id == school_id)
    
    if class_name:
        query = query.filter(Timetable.class_name == class_name)
    if day:
        query = query.filter(Timetable.day_of_week == day)
        
    lessons = query.order_by(Timetable.day_of_week, Timetable.start_time).all()
    
    # 🔥 Batch load ALL teachers in ONE query
    teacher_ids = list(set(l.teacher_id for l in lessons if l.teacher_id))
    teachers_map = {}
    if teacher_ids:
        teacher_users = db.query(User.id, User.full_name)\
                          .filter(User.id.in_(teacher_ids)).all()
        teachers_map = {t[0]: t[1] for t in teacher_users}
    
    if compact:
        # 🚀 Compact format: group by class, use arrays
        result = {}
        for l in lessons:
            cls = l.class_name
            if cls not in result:
                result[cls] = []
            # Array order: [id, day, time, subject, teacher_name, room, is_break]
            result[cls].append([
                l.id,
                l.day_of_week,
                f"{l.start_time}-{l.end_time}",
                l.subject,
                teachers_map.get(l.teacher_id, "N/A"),
                l.room or "",
                l.is_break
            ])
        
        return {
            "data": result,
            "total_lessons": len(lessons),
            "format": "compact",
            "columns": ["id", "day", "time", "subject", "teacher", "room", "is_break"]
        }
    
    # Legacy full format
    result = []
    for l in lessons:
        result.append({
            "id": l.id,
            "class": l.class_name,
            "day": l.day_of_week,
            "time": f"{l.start_time}-{l.end_time}",
            "start_time": l.start_time,
            "end_time": l.end_time,
            "subject": l.subject,
            "teacher_name": teachers_map.get(l.teacher_id, "N/A"),
            "teacher_id": l.teacher_id,
            "room": l.room,
            "is_break": l.is_break
        })
    
    classes = {}
    for r in result:
        cls = r['class']
        if cls not in classes:
            classes[cls] = []
        classes[cls].append(r)
    
    return {"data": classes, "total_lessons": len(result)}
@router.get("/timetable/classes/{school_id}")
def get_school_classes(school_id: int, db: Session = Depends(get_db)):
    """Get all unique classes in a school's timetable"""
    classes = db.query(Timetable.class_name).filter(
        Timetable.school_id == school_id
    ).distinct().order_by(Timetable.class_name).all()
    return {"classes": [c[0] for c in classes]}

@router.post("/timetable")
def create_timetable_entry(
    school_id: int = Body(...),
    class_name: str = Body(...),
    day_of_week: str = Body(...),
    start_time: str = Body(...),
    end_time: str = Body(...),
    subject: str = Body(...),
    teacher_id: Optional[int] = Body(None),
    room: Optional[str] = Body(None),
    db: Session = Depends(get_db)
):
    """Add a lesson to the timetable"""
    entry = Timetable(
        school_id=school_id,
        class_name=class_name,
        day_of_week=day_of_week,
        start_time=start_time,
        end_time=end_time,
        subject=subject,
        teacher_id=teacher_id,
        room=room
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return {"message": "Lesson added", "id": entry.id}

@router.put("/timetable/{lesson_id}")
def update_timetable_entry(
    lesson_id: int,
    subject: Optional[str] = Body(None),
    teacher_name: Optional[str] = Body(None),  # ✅ Accept teacher name
    teacher_id: Optional[int] = Body(None),
    room: Optional[str] = Body(None),
    start_time: Optional[str] = Body(None),
    end_time: Optional[str] = Body(None),
    db: Session = Depends(get_db)
):
    """Edit a timetable entry"""
    entry = db.query(Timetable).filter(Timetable.id == lesson_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Lesson not found")
    
    if subject is not None: entry.subject = subject
    
    # ✅ Find teacher by name if provided
    if teacher_name and teacher_name.strip():
        teacher_user = db.query(User).filter(
            User.full_name.ilike(f"%{teacher_name.strip()}%"),
            User.role == 'teacher'
        ).first()
        if teacher_user:
            entry.teacher_id = teacher_user.id
    elif teacher_id is not None:
        entry.teacher_id = teacher_id
    
    if room is not None: entry.room = room
    if start_time is not None: entry.start_time = start_time
    if end_time is not None: entry.end_time = end_time
    entry.updated_at = datetime.utcnow()
    
    db.commit()
    return {"message": "Lesson updated", "id": lesson_id}

@router.delete("/timetable/{lesson_id}")
def delete_timetable_entry(lesson_id: int, db: Session = Depends(get_db)):
    """Delete a timetable entry"""
    entry = db.query(Timetable).filter(Timetable.id == lesson_id).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Lesson not found")
    db.delete(entry)
    db.commit()
    return {"message": "Lesson deleted"}


# ========== WORKERS ==========
@router.get("/workers")
def get_workers_by_school(
    school_id: int,
    search: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    if school_id != user.school_id:
        raise HTTPException(status_code=403, detail="Not Authorized")
        
    # Get the school
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    
    # Base query filtered by school
    query = db.query(Worker, User).join(
        User, Worker.user_id == User.id
    ).filter(Worker.school_name == school.school_name)
    
    # Search filter
    if search:
        query = query.filter(
            (User.full_name.ilike(f"%{search}%")) |
            (Worker.department.ilike(f"%{search}%"))
        )
    
    # Status filter
    if status == "active":
        query = query.filter(User.is_active == True)
    elif status == "inactive":
        query = query.filter(User.is_active == False)
    
    # Get total count before pagination
    total = query.count()
    
    # Apply pagination
    results = query.offset((page - 1) * limit).limit(limit).all()
    
    # Format the response
    workers = []
    for worker, user in results:
        workers.append({
            "id": user.id,
            "name": user.full_name,
            "email": user.email,
            "phone": user.phone,
            "role": worker.role_title or "Staff",
            "department": worker.department or "General",
            "supervisor": worker.supervisor or "Admin",
            "employmentType": worker.employment_type or "Full-time",
            "location": worker.location or "Main Campus",
            "joined": str(worker.joined_date) if worker.joined_date else "N/A",
            "school": worker.school_name,
            "status": "Active" if user.is_active else "Inactive",
            "created_at": str(user.created_at) if user.created_at else "",
            "profile_picture": worker.profile_picture
        })
    
    return {
        "data": workers,
        "total": total,
        "page": page,
        "pages": (total + limit - 1) // limit
    }
@router.get("/worker-profile/{worker_id}")
def get_worker_profile(worker_id: int, db: Session = Depends(get_db)):
    worker = db.query(Worker).filter(Worker.user_id == worker_id).first()
    user = db.query(User).filter(User.id == worker_id).first()
    
    if not worker or not user:
        raise HTTPException(status_code=404, detail="Worker not found")
    
    activities = db.query(Activity).filter(Activity.user_id == worker_id).all()
    activity_data = [{"name": a.name, "role": a.role, "icon": a.icon_name} for a in activities]
    
    return {
        "worker": {
            "id": user.id, "name": user.full_name, "email": user.email, "phone": user.phone,
            "role": worker.role_title or "Staff", "department": worker.department or "General",
            "supervisor": worker.supervisor or "Admin", "employmentType": worker.employment_type or "Full-time",
            "location": worker.location or "Main Campus", "joined": worker.joined_date or "N/A",
            "school": worker.school_name, "status": "Active" if user.is_active else "Inactive"
        },
        "activities": activity_data,
        "attendance": {"present": 28, "absent": 2, "leave": 1, "rate": 90},
        "performance": [
            {"label": "Work Quality", "value": 4.5},
            {"label": "Punctuality", "value": 4.8},
            {"label": "Communication", "value": 4.2},
            {"label": "Teamwork", "value": 4.6},
        ]
    }

# ========== WORKER ATTENDANCE ==========
@router.get("/worker-attendance/{worker_id}")
def get_worker_attendance(worker_id: int, db: Session = Depends(get_db)):
    records = db.query(WorkerAttendance).filter(
        WorkerAttendance.worker_id == worker_id
    ).order_by(desc(WorkerAttendance.date)).limit(31).all()
    
    present = sum(1 for r in records if r.status == 'present')
    absent = sum(1 for r in records if r.status == 'absent')
    leave = sum(1 for r in records if r.status == 'leave')
    total = len(records)
    rate = round(present / total * 100, 1) if total > 0 else 100
    
    return {
        "present": present, "absent": absent, "leave": leave, "rate": rate, "total": total,
        "records": [{"date": str(r.date), "status": r.status} for r in records[:10]]
    }

# ========== WORKER PERFORMANCE ==========
@router.get("/worker-performance/{worker_id}")
def get_worker_performance(worker_id: int, db: Session = Depends(get_db)):
    records = db.query(WorkerPerformance).filter(
        WorkerPerformance.worker_id == worker_id
    ).order_by(desc(WorkerPerformance.review_date)).all()
    
    categories = {}
    for r in records:
        if r.category not in categories:
            categories[r.category] = []
        categories[r.category].append(r.rating)
    
    result = []
    for cat, ratings in categories.items():
        avg = round(sum(ratings) / len(ratings), 1)
        result.append({"label": cat, "value": avg})
    
    if not result:
        result = [
            {"label": "Work Quality", "value": 0},
            {"label": "Punctuality", "value": 0},
            {"label": "Communication", "value": 0},
            {"label": "Teamwork", "value": 0},
        ]
    
    return {"data": result}

# ========== ASSIGNMENTS ==========
@router.get("/assignment-stats")
async def assignment_stats(
    user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role != "teacher":
        raise HTTPException(status_code=403, detail="Not Teacher")
    
    # Get all assignments for the teacher
    assignments = db.query(Assignment).filter(Assignment.teacher_id == user.id).all()
    
    # Calculate counts by status
    total = len(assignments)
    active = 0
    completed = 0
    graded = 0
    pending = 0
    
    for assignment in assignments:
        status = assignment.status.lower() if assignment.status else 'pending'
        
        if status == 'active':
            active += 1
        elif status == 'completed':
            completed += 1
        elif status == 'graded':
            graded += 1
        else:
            pending += 1
    
    return {
        "total": total,
        "active": active,
        "completed": completed,
        "graded": graded,
        "pending": pending
    }

@router.get("/assignments/{teacher_id}")
def get_teacher_assignments(
    teacher_id: int,
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    # Get teacher
    teacher = db.query(Teacher).filter(Teacher.user_id == teacher_id).first()
    if not teacher:
        return {"data": []}
    
    # Get active class names for this teacher
    active_class_names = [
        c.name for c in db.query(Class).join(
            ClassSubjectTeacher, Class.id == ClassSubjectTeacher.class_id
        ).filter(
            ClassSubjectTeacher.teacher_id == teacher.id,
            ClassSubjectTeacher.is_active == True
        ).all()
    ]
    if not active_class_names:
        return {"message": "no active classes"}
    
    query = db.query(Assignment).filter(
        Assignment.teacher_id == teacher.id,
        Assignment.class_name.in_(active_class_names) if active_class_names else True
    )
    
    if status:
        query = query.filter(Assignment.status == status)
    
    assignments = query.order_by(desc(Assignment.created_at)).all()
    result = []
    for a in assignments:
        result.append({
            "id": a.id,
            "title": a.title,
            "description": a.description,
            "subject": a.subject,
            "class": a.class_name,
            "dueDate": a.due_date,
            "dueTime": a.due_time,
            "submitted": a.submitted_count,
            "total": a.total_students,
            "status": a.status,
            "created_at": str(a.created_at),
            "file_path": a.file_path,
            "assignment_type": a.assignment_type,
            "typing_questions": a.typing_questions,
            "mcq_questions": a.mcq_questions,
        })
    return {"data": result}


def convert_to_pdf(input_path: str, output_dir: str) -> Optional[str]:
    """Convert a document to PDF using LibreOffice headless. Returns the output path or None on failure."""
    try:
        result = subprocess.run(
            [
                "soffice",
                "--headless",
                "--convert-to", "pdf",
                "--outdir", output_dir,
                input_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode != 0:
            print(f"❌ LibreOffice conversion failed: {result.stderr}")
            return None

        base_name = os.path.splitext(os.path.basename(input_path))[0]
        expected_output = os.path.join(output_dir, f"{base_name}.pdf")

        return expected_output if os.path.exists(expected_output) else None

    except subprocess.TimeoutExpired:
        print("❌ LibreOffice conversion timed out")
        return None
    except Exception as e:
        print(f"❌ Conversion error: {e}")
        return None


def convert_assignment_file_background(assignment_id: int, original_path: str, output_dir: str, db_session_factory):
    """Runs after the response is sent. Converts the file and updates the DB row with the PDF path."""
    converted_path = convert_to_pdf(original_path, output_dir)

    if not converted_path:
        print(f"⚠️ Background conversion failed for assignment {assignment_id}, keeping original file")
        return

    db = db_session_factory()
    try:
        assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
        if assignment:
            assignment.file_path = converted_path
            db.commit()
            print(f"✅ Assignment {assignment_id} converted to PDF: {converted_path}")
    except Exception as e:
        db.rollback()
        print(f"❌ Failed to update assignment {assignment_id} after conversion: {e}")
    finally:
        db.close()

@router.put("/assignments/{assignment_id}")
async def update_assignment(
    assignment_id: int,
    typing_questions: Optional[str] = Form(None),
    mcq_questions: Optional[str] = Form(None),
    user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Update assignment questions"""
    
    assignment = db.query(Assignment).filter(Assignment.id == assignment_id).first()
    
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Update typing questions
    if typing_questions:
        assignment.typing_questions = typing_questions
        print(f"📝 Updated typing questions for assignment {assignment_id}")
    
    # Update MCQ questions
    if mcq_questions:
        assignment.mcq_questions = mcq_questions
        print(f"📝 Updated MCQ questions for assignment {assignment_id}")
    
    try:
        db.commit()
        db.refresh(assignment)
        return {
            "message": "success",
            "id": assignment.id,
            "assignment_type": assignment.assignment_type,
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Database failed: {str(e)}")
        
@router.post("/assignments")
async def post_assignment(
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    description: str = Form(...),
    subject: str = Form(...),
    class_name: str = Form(...),
    due_date: str = Form(...),
    due_time: str = Form(...),
    assignment_type: str = Form("PDF Upload"),
    file: Optional[UploadFile] = File(None),
    typing_questions: Optional[str] = Form(None),
    mcq_questions: Optional[str] = Form(None),
    user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create assignment with file upload support"""

    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()

    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher Not Found")

    file_path = None
    file_name = None
    file_size = 0
    needs_conversion = False
    original_path_for_conversion = None

    if file and assignment_type == "PDF Upload":
        os.makedirs("uploads/assignments", exist_ok=True)

        file_extension = os.path.splitext(file.filename)[1].lower()
        unique_id = f"{teacher.id}_{datetime.utcnow().timestamp()}"
        original_filename = f"assignment_{unique_id}{file_extension}"
        original_path = f"uploads/assignments/{original_filename}"
        file_name = file.filename
        file_size = file.size

        with open(original_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        file_path = original_path 

        if file_extension != ".pdf":
            needs_conversion = True
            original_path_for_conversion = original_path

    typing_questions_data = typing_questions
    mcq_questions_data = mcq_questions

    assignment = Assignment(
        teacher_id=teacher.id,
        title=title,
        description=description,
        subject=subject,
        class_name=class_name,
        due_date=due_date,
        due_time=due_time,
        assignment_type=assignment_type,
        total_students=0,
        submitted_count=0,
        file_path=file_path,
        typing_questions=typing_questions_data,
        mcq_questions=mcq_questions_data,
        created_at=datetime.utcnow()
    )

    try:
        db.add(assignment)
        db.commit()
        db.refresh(assignment)

        if needs_conversion:
            background_tasks.add_task(
                convert_assignment_file_background,
                assignment.id,
                original_path_for_conversion,
                "uploads/assignments",
                SessionLocal,
            )

        return {
            "message": "success",
            "id": assignment.id,
            "title": assignment.title,
            "assignment_type": assignment_type,
            "file_name": file_name,
            "file_size": file_size
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=503, detail=f"Database failed: {str(e)}")    
    

@router.get("/assignment-stats/{teacher_id}")
def get_assignment_stats(teacher_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    # Get teacher first
    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
    
    active_class_names = []
    if teacher:
        active_class_names = [
            c.name for c in db.query(Class).join(
                ClassSubjectTeacher, Class.id == ClassSubjectTeacher.class_id
            ).filter(
                ClassSubjectTeacher.teacher_id == teacher.id,
                ClassSubjectTeacher.is_active == True
            ).all()
        ]
    
    query = db.query(Assignment).filter(
        Assignment.teacher_id == teacher.id,
        Assignment.class_name.in_(active_class_names) if active_class_names else True
    )
    assignments = query.all()
    total = len(assignments)
    active = sum(1 for a in assignments if a.status == "Active")
    completed = sum(1 for a in assignments if a.status == "Completed")
    graded = sum(1 for a in assignments if a.status == "Graded")
    return {"total": total, "active": active, "completed": completed, "graded": graded}
# ========== JOBS & GIGS ==========

@router.get("/jobs")
async def get_jobs(
    request: Request,
    is_gig: Optional[bool] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    user = Depends(get_current_user),  # ✅ Add user dependency
):
    query = db.query(Job)
    if is_gig is not None:
        query = query.filter(Job.is_gig == is_gig)
    if status:
        query = query.filter(Job.status == status)
    if search:
        query = query.filter(Job.title.ilike(f"%{search}%") | Job.school_name.ilike(f"%{search}%"))
    
    total = query.count()
    offset = (page - 1) * limit
    jobs = query.order_by(desc(Job.created_at)).offset(offset).limit(limit).all()
    
    result = []
    for j in jobs:
        reqs = j.requirements.split(",") if j.requirements else []
        
        # ✅ Check if current user has applied
        has_applied = db.query(Applicant).filter(
            Applicant.user_id == user.id,
            Applicant.job_id == j.id
        ).first() is not None
        
        result.append({
            "id": j.id,
            "title": j.title,
            "description": j.description,
            "location": j.location,
            "type": j.type,
            "salary": j.salary,
            "requirements": [r.strip() for r in reqs if r.strip()],
            "applicants": j.applicants,
            "deadline": j.deadline,
            "is_gig": j.is_gig,
            "posted": _time_ago(j.created_at),
            "created_at": str(j.created_at),
            "has_applied": has_applied,  # ✅ Add this field
            "working_hours": j.working_hours,  # ✅ Add if exists
            "duration": j.duration,  # ✅ Add if exists
            "amount_type": j.amount_type,  # ✅ Add if exists
        })
    total_pages = (total + limit - 1) // limit
    return {
        "data": result,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": total_pages,
        "has_more": page < total_pages
    }

def _time_ago(dt):
    diff = datetime.utcnow().replace(tzinfo=None) - dt.replace(tzinfo=None)
    if diff.days > 30: return f"{diff.days // 30}mo ago"
    if diff.days > 0: return f"{diff.days}d ago"
    if diff.seconds > 3600: return f"{diff.seconds // 3600}h ago"
    if diff.seconds > 60: return f"{diff.seconds // 60}m ago"
    return "Just now"
   
@router.post("/post-jobs")
async def post_jobs(
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
    title: str = Form(...),
    description: str = Form(...),
    salary: str = Form(...),
    location: str = Form(...),
    is_gig: bool = Form(False),  # Default to False
    type: str = Form(...),
    requirements: str = Form(None),  # Optional
    duration: str = Form(None),  # Optional
    working_hours: str = Form(None),  # Optional
    amount_type: str = Form(None),  # Optional
    deadline: str = Form(None)  # Optional
):
    if user.role == "student":
        raise HTTPException(status_code=403, detail="Unauthorized Action")
    
    jobs = Job(
        title=title,
        description=description,
        location=location,
        type=type,
        salary=salary,
        requirements=requirements,
        posted_by=user.id,
        deadline=deadline,
        is_gig=is_gig,
        working_hours=working_hours,
        duration=duration,
        amount_type=amount_type
    )
    
    db.add(jobs)
    db.commit()
    db.refresh(jobs)
    
    return {"message": "success", "job_id": jobs.id}

@router.get("/job-stats")
def get_job_stats(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    jobs = db.query(Job).filter(Job.is_gig == False).all()
    gigs = db.query(Job).filter(Job.is_gig == True).all()
    my_jobs = db.query(Job).filter(Job.posted_by == user.id).all()
    return {
        "jobs_total": len(jobs),
        "gigs_total": len(gigs),
        "my_jobs": len(my_jobs)
    }


# ========== BOOKS ==========
@router.get("/books")
def get_books(
    search: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    free_only: Optional[bool] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(12, ge=1, le=100),
    db: Session = Depends(get_db)
):
    query = db.query(Book)
    if search:
        query = query.filter(Book.title.ilike(f"%{search}%") | Book.author.ilike(f"%{search}%"))
    if category and category != "All":
        query = query.filter(Book.category == category)
    if free_only:
        query = query.filter(Book.is_free == True)
    
    total = query.count()
    books = query.order_by(desc(Book.created_at))\
        .offset((page - 1) * limit)\
        .limit(limit).all()
    
    result = []
    for b in books:
        result.append({
            "id": b.id, "title": b.title, "author": b.author,
            "description": b.description, "category": b.category,
            "price": b.price, "isFree": b.is_free,
            "rating": b.rating, "views": b.views, "downloads": b.downloads,
            "likes": b.likes, "publishedDate": b.published_date,
            "image_url": b.image_url,
            "file_url": b.file_path,
            "created_at": str(b.created_at)
        })
    return {"data": result, "total": total, "page": page, "pages": (total + limit - 1) // limit}

@router.get("/book-stats")
def get_book_stats(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    books = db.query(Book).all()
    return {
        "total": len(books),
        "free": sum(1 for b in books if b.is_free),
        "paid": sum(1 for b in books if not b.is_free),
        "myBooks": len(books),
    }

@router.get("/book-categories")
def get_book_categories(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    cats = db.query(Book.category).distinct().all()
    return {"categories": ["All"] + [c[0] for c in cats]}

@router.get("/student-dashboard/{student_id}")
def get_student_dashboard(
    student_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get student dashboard - only self or admin access"""
    
    # Validate access
    if user.role != "admin" and user.id != student_id:
        raise HTTPException(
            status_code=403,
            detail="You can only access your own student dashboard"
        )
    
    student = db.query(Student).filter(Student.user_id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    student_user = db.query(User).filter(User.id == student_id).first()
    if not student_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get performance
    performances = db.query(StudentPerformance).filter(
        StudentPerformance.student_id == student_id
    ).all()
    
    avg_score = 0
    if performances:
        avg_score = round(sum(p.score for p in performances) / len(performances), 1)
    
    # Get assignments - Use the student's class
    assignments = db.query(Assignment).filter(
        Assignment.class_name == student.class_name
    ).all()
    
    total_assignments = len(assignments)
    pending_assignments = sum(1 for a in assignments if a.status == "Active")
    completed_assignments = sum(1 for a in assignments if a.status == "Completed")
    
    # Get fees
    fees = db.query(Fee).filter(Fee.student_id == student_id).all()
    total_fees = sum(f.amount for f in fees) if fees else 0
    total_paid = sum(f.paid for f in fees) if fees else 0
    
    # Get attendance (from timetable count)
    attendance_count = db.query(Timetable).filter(
        Timetable.class_name == student.class_name
    ).count()
    attendance = 92  # Default if no data
    
    return {
        "student": {
            "id": student_user.id,
            "name": student_user.full_name,
            "class": student.class_name,
            "admission_number": student.admission_number
        },
        "stats": {
            "average_score": avg_score,
            "total_assignments": total_assignments,
            "pending_assignments": pending_assignments,
            "completed_assignments": completed_assignments,
            "fees_total": total_fees,
            "fees_paid": total_paid,
            "fees_balance": total_fees - total_paid,
            "attendance": attendance
        },
        "recent_activities": [
            {
                "type": "assignment",
                "title": f"Assignment: {a.title}",
                "description": f"Due: {a.due_date}",
                "date": str(a.created_at)
            }
            for a in assignments[:3]
        ] if assignments else []
    }
    
@router.get("/parent-dashboard/{parent_id}")
def get_parent_dashboard(
    parent_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    parent = db.query(Parent).filter(Parent.user_id == current_user.id).first()
    user = db.query(User).filter(User.id == current_user.id).first()
    if not parent or not user:
        raise HTTPException(status_code=404, detail="Parent not found")

    current_year = datetime.now().year

    # ── Get linked children ──
    links = db.query(ParentStudent).filter(
        ParentStudent.parent_id == parent.id
    ).all()

    student_ids = [link.student_id for link in links]
    children = []

    if student_ids:
        # ════════════════════════════════════════════════════════════
        # 1) Students + user info
        # ════════════════════════════════════════════════════════════
        students_data = db.query(
            Student.id,
            Student.user_id,
            Student.admission_number,
            Student.class_name,
            Student.profile_picture,
            Student.school_id,
            User.full_name,
        ).join(
            User, Student.user_id == User.id
        ).filter(
            Student.id.in_(student_ids)
        ).all()

        if not students_data:
            return {
                "parent": {
                    "id": user.id,
                    "name": user.full_name,
                    "phone": user.phone,
                },
                "children": [],
                "total_children": 0,
            }

        student_user_ids = [s.user_id for s in students_data]
        student_pk_ids = [s.id for s in students_data]

        # ════════════════════════════════════════════════════════════
        # 2) CURRENT YEAR Fee rows for all children
        # ════════════════════════════════════════════════════════════
        all_fees = db.query(Fee).filter(
            Fee.student_id.in_(student_user_ids),
            Fee.academic_year == current_year,
        ).all()

        fees_by_student: dict[int, int] = {}
        fee_ids_by_student: dict[int, list[int]] = {}
        for f in all_fees:
            fees_by_student[f.student_id] = (
                fees_by_student.get(f.student_id, 0) + (f.amount or 0)
            )
            fee_ids_by_student.setdefault(f.student_id, []).append(f.id)

        all_fee_ids = [fid for ids in fee_ids_by_student.values() for fid in ids]

        # ════════════════════════════════════════════════════════════
        # 3) Transactions tied to those Fee rows only
        # ════════════════════════════════════════════════════════════
        all_transactions = []
        if all_fee_ids:
            all_transactions = db.query(FeeTransaction).filter(
                FeeTransaction.fee_id.in_(all_fee_ids),
                FeeTransaction.amount > 0,
            ).all()

        transactions_by_student: dict[int, list] = {}
        for tx in all_transactions:
            transactions_by_student.setdefault(tx.student_id, []).append(tx)

        # ════════════════════════════════════════════════════════════
        # 4) Performances
        # ════════════════════════════════════════════════════════════
        all_performances = db.query(StudentPerformance).filter(
            StudentPerformance.student_id.in_(student_pk_ids)
        ).all()

        performances_by_student: dict[int, list] = {}
        for p in all_performances:
            performances_by_student.setdefault(p.student_id, []).append(p)

        # ════════════════════════════════════════════════════════════
        # 5) Class teachers
        # ════════════════════════════════════════════════════════════
        class_teachers = {}
        school_classes: dict[int, set] = {}
        for s in students_data:
            if s.school_id and s.class_name:
                school_classes.setdefault(s.school_id, set()).add(s.class_name)

        for school_id, class_names in school_classes.items():
            if not class_names:
                continue

            classes_data = db.query(Class).filter(
                Class.name.in_(list(class_names)),
                Class.school_id == school_id,
            ).all()

            teacher_ids = [c.class_teacher_id for c in classes_data if c.class_teacher_id]
            if not teacher_ids:
                continue

            teachers_data = db.query(
                Teacher.id, Teacher.user_id
            ).filter(
                Teacher.id.in_(teacher_ids)
            ).all()

            teacher_user_ids = [t.user_id for t in teachers_data]
            teacher_users = db.query(User).filter(
                User.id.in_(teacher_user_ids)
            ).all()

            teacher_user_map = {u.id: u for u in teacher_users}
            teacher_map = {t.id: t for t in teachers_data}

            for cls in classes_data:
                if cls.class_teacher_id and cls.class_teacher_id in teacher_map:
                    teacher = teacher_map[cls.class_teacher_id]
                    teacher_user = teacher_user_map.get(teacher.user_id)
                    if teacher_user:
                        class_teachers[(school_id, cls.name)] = {
                            "name": teacher_user.full_name,
                            "phone": teacher_user.phone,
                            "email": teacher_user.email,
                            "photo": teacher_user.profile_picture,
                            "user_id": teacher_user.id,
                            "id": teacher.id,
                        }
                        

        # ════════════════════════════════════════════════════════════
        # 6) Process each student
        # ════════════════════════════════════════════════════════════
        for student in students_data:
            student_user_id = student.user_id
            student_pk_id = student.id
            student_school_id = student.school_id

            # ── Fees (current year) ──
            student_fees = fees_by_student.get(student_user_id, 0)
            student_txs = transactions_by_student.get(student_user_id, [])
            total_received = sum((tx.amount or 0) for tx in student_txs)

            total_fees = student_fees
            # Applied = what the current-year fees absorbed (capped)
            total_paid = min(total_received, total_fees)
            balance = max(0, total_fees - total_received)
            overpaid = max(0, total_received - total_fees)
            percent = round((total_paid / total_fees) * 100) if total_fees > 0 else 0

            # ── Performance ──
            student_perfs = performances_by_student.get(student_pk_id, [])
            subject_scores: dict[str, list] = {}
            for p in student_perfs:
                subject_scores.setdefault(p.subject, []).append(p.score)

            subject_averages = {
                subject: round(sum(scores) / len(scores), 1)
                for subject, scores in subject_scores.items()
            }
            sorted_asc = sorted(subject_averages.items(), key=lambda x: x[1])
            sorted_desc = sorted(
                subject_averages.items(), key=lambda x: x[1], reverse=True
            )

            weak_subjects = [
                {"name": s, "subject": s, "score": sc, "percent": sc}
                for s, sc in sorted_asc[:3]
            ]
            strong_subjects = [
                {"name": s, "subject": s, "score": sc, "percent": sc}
                for s, sc in sorted_desc[:3]
            ]
            avg = (
                round(sum(subject_averages.values()) / len(subject_averages), 1)
                if subject_averages else 0
            )

            teacher_info = class_teachers.get(
                (student_school_id, student.class_name),
                {"name": "Not Assigned", "phone": None, "email": None, "photo": None},
            )

            children.append({
                "id": student.user_id,
                "student_id": student.id,
                "name": student.full_name,
                "full_name": student.full_name,
                "class": student.class_name,
                "class_name": student.class_name,
                "admission": student.admission_number,
                "admission_number": student.admission_number,
                "average": avg,

                # ── Fees ──
                "fees_total": total_fees,
                "fees_received": total_received,
                "fees_paid": total_paid,
                "fees_balance": balance,
                "fees_overpaid": overpaid,
                "fees_percent": percent,

                "profile_picture": student.profile_picture,
                "weak_subjects": weak_subjects,
                "strong_subjects": strong_subjects,
                "class_teacher": teacher_info,
            })

    return {
        "parent": {
            "id": user.id,
            "name": user.full_name,
            "phone": user.phone,
        },
        "children": children,
        "total_children": len(children),
    }
"""@router.get("/parent-dashboard/{parent_id}")
def get_parent_dashboard(parent_id: int, db: Session = Depends(get_db), current_user = Depends(get_current_user)):
    parent = db.query(Parent).filter(Parent.user_id == current_user.id).first()
    
    user = db.query(User).filter(User.id == current_user.id).first()
    if not parent or not user:
        raise HTTPException(status_code=404, detail="Parent not found")
    
    current_year = datetime.now().year
    school_id = user.school_id
    
    # Get linked children
    links = db.query(ParentStudent).filter(
        ParentStudent.parent_id == parent.id
    ).all()
    
    student_ids = [link.student_id for link in links]
    
    children = []
    
    if student_ids:
        # ========== SINGLE QUERY: Students with User info ==========
        students_data = db.query(
            Student.id,
            Student.user_id,
            Student.admission_number,
            Student.class_name,
            Student.profile_picture,
            Student.school_id,
            User.full_name,
        ).join(
            User, Student.user_id == User.id
        ).filter(
            Student.id.in_(student_ids)
        ).all()
        
        # ========== SINGLE QUERY: Fee Structures ==========
        fee_structures = db.query(FeeStructure).filter(
            FeeStructure.school_id == school_id,
            FeeStructure.academic_year == current_year
        ).all()
        
        total_fees_from_structure = sum(s.amount for s in fee_structures) if fee_structures else 0
        
        # ========== SINGLE QUERY: All transactions ==========
        user_ids = [s.user_id for s in students_data]
        all_transactions = db.query(FeeTransaction).filter(
            FeeTransaction.student_id.in_(user_ids),
            FeeTransaction.amount > 0
        ).all()
        
        # ========== SINGLE QUERY: All performances ==========
        all_performances = db.query(StudentPerformance).filter(
            StudentPerformance.student_id.in_(user_ids)
        ).all()
        
        # ========== SINGLE QUERY: All class teachers ==========
        class_teachers = {}
        if students_data:
            class_names = list(set(s.class_name for s in students_data if s.class_name))
            if class_names:
                classes_data = db.query(Class).filter(
                    Class.name.in_(class_names),
                    Class.school_id == school_id
                ).all()
                
                teacher_ids = [c.class_teacher_id for c in classes_data if c.class_teacher_id]
                if teacher_ids:
                    teachers_data = db.query(
                        Teacher.id,
                        Teacher.user_id,
                    ).filter(
                        Teacher.id.in_(teacher_ids)
                    ).all()
                    
                    teacher_user_ids = [t.user_id for t in teachers_data]
                    teacher_users = db.query(User).filter(
                        User.id.in_(teacher_user_ids)
                    ).all()
                    
                    teacher_user_map = {u.id: u for u in teacher_users}
                    teacher_map = {t.id: t for t in teachers_data}
                    
                    for cls in classes_data:
                        if cls.class_teacher_id and cls.class_teacher_id in teacher_map:
                            teacher = teacher_map[cls.class_teacher_id]
                            teacher_user = teacher_user_map.get(teacher.user_id)
                            if teacher_user:
                                class_teachers[cls.name] = {
                                    "name": teacher_user.full_name,
                                    "phone": teacher_user.phone,
                                    "email": teacher_user.email,
                                    "photo": teacher_user.profile_picture,
                                    "user_id": teacher_user.id,
                                    "id": teacher.id,
                                }
        
        # Group transactions by student
        transactions_by_student = {}
        for tx in all_transactions:
            if tx.student_id not in transactions_by_student:
                transactions_by_student[tx.student_id] = []
            transactions_by_student[tx.student_id].append(tx)
        
        # Group performances by student
        performances_by_student = {}
        for p in all_performances:
            if p.student_id not in performances_by_student:
                performances_by_student[p.student_id] = []
            performances_by_student[p.student_id].append(p)
        
        for student in students_data:
            student_user_id = student.user_id
            student_transactions = transactions_by_student.get(student_user_id, [])
            total_paid = sum(tx.amount for tx in student_transactions)
            
            balance = max(0, total_fees_from_structure - total_paid)
            overpaid = max(0, total_paid - total_fees_from_structure)
            
            # ========== Calculate subject averages ==========
            student_perfs = performances_by_student.get(student_user_id, [])
            subject_scores = {}
            for p in student_perfs:
                if p.subject not in subject_scores:
                    subject_scores[p.subject] = []
                subject_scores[p.subject].append(p.score)
            
            subject_averages = {
                subject: round(sum(scores) / len(scores), 1)
                for subject, scores in subject_scores.items()
            }
            
            # Sort ascending (weakest first) and descending (strongest first)
            sorted_asc = sorted(subject_averages.items(), key=lambda x: x[1])
            sorted_desc = sorted(subject_averages.items(), key=lambda x: x[1], reverse=True)
            
            # Take 3 weakest and 3 strongest
            weak_subjects = [
                {"name": subj, "subject": subj, "score": score, "percent": score}
                for subj, score in sorted_asc[:3]
            ]
            
            strong_subjects = [
                {"name": subj, "subject": subj, "score": score, "percent": score}
                for subj, score in sorted_desc[:3]
            ]
            
            avg = round(sum(subject_averages.values()) / len(subject_averages), 1) if subject_averages else 0
            
            # Get class teacher from pre-fetched data
            teacher_info = class_teachers.get(student.class_name, {
                "name": "Not Assigned",
                "phone": None,
                "email": None,
                "photo": None,
            })
            
            children.append({
                "id": student.user_id,
                "student_id": student.id,
                "name": student.full_name,
                "full_name": student.full_name,
                "class": student.class_name,
                "class_name": student.class_name,
                "admission": student.admission_number,
                "admission_number": student.admission_number,
                "average": avg,
                "fees_paid": total_paid,
                "fees_total": total_fees_from_structure,
                "fees_balance": balance,
                "fees_overpaid": overpaid,
                "fees_percent": round(total_paid / total_fees_from_structure * 100) if total_fees_from_structure > 0 else 0,
                "profile_picture": student.profile_picture,
                "weak_subjects": weak_subjects,
                "strong_subjects": strong_subjects,
                "class_teacher": teacher_info,
            })
    
    return {
        "parent": {
            "id": user.id,
            "name": user.full_name,
            "phone": user.phone,
        },
        "children": children,
        "total_children": len(children),
    }"""
    
def get_user_school_id(user: User, db: Session) -> Optional[int]:
    """Get school_id based on user role"""
    if user.role == "school":
        school = db.query(School).filter(School.user_id == user.id).first()
        return school.id if school else None
    elif user.role == "admin":
        return user.school_id
    elif user.role == "teacher":
        teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
        return teacher.school_id if teacher else None
    elif user.role == "student":
        student = db.query(Student).filter(Student.user_id == user.id).first()
        return student.school_id if student else None
    elif user.role == "parent":
        parent = db.query(Parent).filter(Parent.user_id == user.id).first()
        return parent.student_id if parent else None
    return None

@router.get("/parent-school/{parent_id}")
def get_parent_school(
    parent_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """
    Parent school dashboard - supports multiple schools per parent
    """
    if user.role not in ['parent', 'admin', 'school']:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    current_year = datetime.now().year
    
    # ========== Get parent ==========
    parent_data = db.query(Parent).filter(Parent.user_id == user.id).first()
    
    if not parent_data:
        raise HTTPException(status_code=404, detail="Parent not found")
    
    # ========== Get all student IDs for this parent ==========
    student_ids = db.query(ParentStudent.student_id).filter(
        ParentStudent.parent_id == parent_data.id
    ).all()
    student_ids = [s[0] for s in student_ids]
    
    if not student_ids:
        return {
            "parent": {"id": parent_data.user_id, "name": user.full_name},
            "school": None,
            "schools": [],
            "children": [],
            "teachers": [],
            "total_students": 0,
            "total_teachers": 0,
            "fees": {},
            "announcements": [],
        }
    
    # ========== Get students with user info ==========
    students_data = db.query(
        Student.id, Student.user_id, Student.admission_number,
        Student.class_name, Student.profile_picture, Student.school_id,
        User.full_name
    ).join(User, Student.user_id == User.id).filter(
        Student.id.in_(student_ids)
    ).all()
    
    if not students_data:
        raise HTTPException(status_code=404, detail="No students found")
    
    # ✅ Get ALL unique school IDs from ALL children
    school_ids = list(set(s.school_id for s in students_data if s.school_id))
    student_user_ids = [s.user_id for s in students_data]
    
    # ========== Get ALL schools ==========
    schools = db.query(School).filter(School.id.in_(school_ids)).all()
    schools_map = {s.id: s for s in schools}
    
    # ========== Get fee structures for ALL schools ==========
    fee_structures = db.query(FeeStructure).filter(
        FeeStructure.school_id.in_(school_ids),
        FeeStructure.academic_year == current_year
    ).all()
    
    # Group total fees by school
    total_fees_by_school = {}
    for fs in fee_structures:
        if fs.school_id not in total_fees_by_school:
            total_fees_by_school[fs.school_id] = 0
        total_fees_by_school[fs.school_id] += fs.amount
    
    # ========== Get ALL transactions in ONE query ==========
    all_transactions = db.query(FeeTransaction).filter(
        FeeTransaction.student_id.in_(student_user_ids),
        FeeTransaction.amount > 0
    ).order_by(FeeTransaction.payment_date.desc()).all()
    
    # Group transactions by student
    transactions_by_student = {}
    for tx in all_transactions:
        if tx.student_id not in transactions_by_student:
            transactions_by_student[tx.student_id] = []
        transactions_by_student[tx.student_id].append(tx)
    
    # ========== Get teachers from ALL schools ==========
    teachers_data = db.query(
        Teacher.id, Teacher.subject, Teacher.qualification,
        Teacher.years_of_experience, Teacher.profile_picture,
        Teacher.school_id,
        User.full_name, User.phone, User.email, User.profile_picture
    ).join(User, Teacher.user_id == User.id).filter(
        Teacher.school_id.in_(school_ids)
    ).all()
    
    # ========== Get total students from ALL schools ==========
    total_students = db.query(func.count(Student.id)).filter(
        Student.school_id.in_(school_ids)
    ).scalar() or 0
    
    # ========== Get announcements from ALL schools ==========
    announcements = db.query(
        Announcement.id, Announcement.title, Announcement.description, 
        Announcement.created_at, Announcement.school_id
    ).filter(
        Announcement.school_id.in_(school_ids)
    ).order_by(desc(Announcement.created_at)).limit(5).all()
    
    # ========== Build children data with per-school fees ==========
    children = []
    for student in students_data:
        student_school_id = student.school_id
        total_fees = total_fees_by_school.get(student_school_id, 0)
        
        student_transactions = transactions_by_student.get(student.user_id, [])
        total_paid = sum(tx.amount for tx in student_transactions)
        balance = max(0, total_fees - total_paid)
        overpaid = max(0, total_paid - total_fees)
        percent = round(total_paid / total_fees * 100) if total_fees > 0 else 0
        
        student_school = schools_map.get(student_school_id)
        
        children.append({
            "id": student.user_id,
            "student_id": student.id,
            "name": student.full_name,
            "admission": student.admission_number,
            "class": student.class_name,
            "profile_picture": student.profile_picture,
            "school_id": student_school_id,
            "school_name": student_school.school_name if student_school else None,
            "fees_total": total_fees,
            "fees_paid": total_paid,
            "fees_balance": balance,
            "fees_overpaid": overpaid,
            "fees_percent": percent,
            "fees": {
                "total": total_fees,
                "paid": total_paid,
                "balance": balance,
                "overpaid": overpaid,
                "percent": percent,
                "transactions": [
                    {
                        "id": tx.id,
                        "amount": tx.amount,
                        "payment_provider": tx.payment_provider,
                        "transaction_reference": tx.transaction_reference,
                        "payment_date": tx.payment_date.isoformat() if tx.payment_date else None,
                    }
                    for tx in student_transactions
                ]
            }
        })
    
    # ========== Build teachers data ==========
    teachers = [
        {
            "id": t.id,
            "name": t.full_name,
            "subject": t.subject,
            "qualification": t.qualification,
            "experience": t.years_of_experience,
            "phone": t.phone,
            "email": t.email,
            "photo": t.profile_picture,
            "school_id": t.school_id,
        }
        for t in teachers_data
    ]
    
    # ========== Build announcements ==========
    announcement_data = []
    now = datetime.utcnow()
    for a in announcements:
        created = a.created_at.replace(tzinfo=None) if a.created_at else now
        diff = now - created
        
        if diff.days > 30:
            time_str = f"{diff.days // 30}mo ago"
        elif diff.days > 0:
            time_str = f"{diff.days}d ago"
        elif diff.seconds >= 3600:
            time_str = f"{diff.seconds // 3600}h ago"
        elif diff.seconds >= 60:
            time_str = f"{diff.seconds // 60}m ago"
        else:
            time_str = "Just now"
        
        announcement_data.append({
            "id": a.id,
            "title": a.title,
            "description": a.description,
            "time": time_str,
            "school_id": a.school_id,
        })
    
    # ========== Primary school (first child's school) ==========
    primary_school = schools_map.get(students_data[0].school_id) if students_data else None
    
    return {
        "parent": {"id": parent_data.user_id, "name": user.full_name},
        "school": {
            "id": primary_school.id,
            "name": primary_school.school_name,
            "address": primary_school.address,
            "type": primary_school.school_type,
            "phone": primary_school.phone,
            "email": primary_school.email,
        } if primary_school else None,
        "schools": [
            {"id": s.id, "name": s.school_name}
            for s in schools
        ],
        "children": children,
        "teachers": teachers,
        "total_students": total_students,
        "total_teachers": len(teachers),
        "fees": children[0]["fees"] if children else {},
        "announcements": announcement_data,
    }
   
@router.get("/pending-approvals/{school_id}")
def get_pending_approvals(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    school_id: Optional[int] = Depends(get_current_school_id)
):
    if user.role not in ['admin', 'school'] and user.school_id != school_id:
      raise HTTPException(status_code=403, detail="Not Autorized")
    query = db.query(User).filter(User.approval_status == 'pending')
    
    if school_id:
        query = query.filter(User.school_id == school_id)
    
    pending_users = query.all()
    
    return {
        "data": [
            {
                "id": u.id,
                "username": u.username,
                "full_name": u.full_name,
                "role": u.role,
                "email": u.email,
                "phone": u.phone,
                "created_at": str(u.created_at),
            }
            for u in pending_users
        ]
    }

@router.post("/approve-user/{user_id}")
def approve_user(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.approval_status = 'approved'
        user.is_active = True
        db.commit()
        return {"message": f"User {user.username} approved"}
    raise HTTPException(status_code=404, detail="User not found")

@router.get("/schools-list")
def get_schools_list(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=1000),
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Public endpoint to get list of schools for registration with pagination and search"""
    query = db.query(School)
    
    if search:
        query = query.filter(
            or_(
                School.school_name.ilike(f"%{search}%"),
                School.address.ilike(f"%{search}%"),
            )
        )
    
    total = query.count()
    offset = (page - 1) * limit
    
    schools = query.order_by(School.id).offset(offset).limit(limit).all()
    
    return {
        "data": [
            {
                "id": s.id,
                "name": s.school_name,
                "school_type": s.school_type,
                "logo": s.logo,
                "address": s.address,
                "phone": s.phone,
                "email": s.email,
            }
            for s in schools
        ],
        "total": total,
        "page": page,
        "limit": limit,
        "has_more": page * limit < total,
        "total_pages": (total + limit - 1) // limit if total > 0 else 0,
    }

@router.get("/school-subject-averages/{school_id}")
def get_school_subject_averages(
    school_id: int,
    term: str = Query(None),
    db: Session = Depends(get_db)
):
    """Get average scores per subject for a school"""
    
    # Get all students in this school
    students = db.query(Student).filter(Student.school_id == school_id).all()
    student_user_ids = [s.user_id for s in students]
    
    if not student_user_ids:
        return {"school_id": school_id, "overall_average": 0, "subjects": []}
    
    query = db.query(StudentPerformance).filter(
        StudentPerformance.student_id.in_(student_user_ids)
    )
    if term:
        query = query.filter(StudentPerformance.term == term)
    
    performances = query.all()
    
    if not performances:
        return {"school_id": school_id, "overall_average": 0, "subjects": []}
    
    # Group by subject
    subjects_dict = {}
    for p in performances:
        if p.subject not in subjects_dict:
            subjects_dict[p.subject] = []
        subjects_dict[p.subject].append(p.score)
    
    subject_averages = []
    total_sum = 0
    total_count = 0
    
    for subject, scores in subjects_dict.items():
        avg = round(sum(scores) / len(scores), 1)
        subject_averages.append({"subject": subject, "average": avg})
        total_sum += sum(scores)
        total_count += len(scores)
    
    subject_averages.sort(key=lambda x: x['subject'])
    overall_avg = round(total_sum / total_count, 1) if total_count > 0 else 0
    
    return {
        "school_id": school_id,
        "student_count": len(student_user_ids),
        "overall_average": overall_avg,
        "subjects": subject_averages
    }

@router.post("/reject-user/{user_id}")
def reject_user(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        user.approval_status = 'rejected'
        db.commit()
        return {"message": f"User {user.username} rejected"}
    raise HTTPException(status_code=404, detail="User not found")  
 
# ========== PROFILE PICTURE UPLOAD (Generic) ==========
@router.post("/upload-picture/{entity_type}/{entity_id}")
async def upload_profile_picture(
    entity_type: str,
    entity_id: int,
    file: UploadFile = File(...),
    remove_bg: bool = False,
    db: Session = Depends(get_db)
):
    """Upload profile picture for any entity"""
    
    # Map entity type to model and lookup
    entity_map = {
        "students": (Student, "user_id"),
        "teachers": (Teacher, "user_id"),
        "parents": (Parent, "user_id"),
        "workers": (Worker, "user_id"),
        "schools": (School, "id"),
        "users": (User, "id"),
    }
    
    if entity_type not in entity_map:
        raise HTTPException(status_code=400, detail=f"Invalid entity type: {entity_type}")
    
    model, lookup_field = entity_map[entity_type]
    
    # Find the record
    if lookup_field == "user_id":
        record = db.query(model).filter(getattr(model, lookup_field) == entity_id).first()
    else:
        record = db.query(model).filter(getattr(model, "id") == entity_id).first()
    
    if not record:
        raise HTTPException(status_code=404, detail=f"{entity_type[:-1]} not found")
    
    # Create directory
    upload_dir = f"uploads/{entity_type}"
    os.makedirs(upload_dir, exist_ok=True)
    
    # Read and process image
    contents = await file.read()
    
    if remove_bg:
        contents = await remove_background(contents)
    
    # Save file
    field_name = "logo" if entity_type == "schools" else "profile_picture"
    file_name = f"{entity_type[:-1]}_{entity_id}.png"
    file_path = os.path.join(upload_dir, file_name)
    
    with open(file_path, "wb") as f:
        f.write(contents)
    
    # Update database
    setattr(record, field_name, f"/{file_path}")
    db.commit()
    
    return {
        "message": f"Picture uploaded for {entity_type[:-1]}",
        "url": f"http://127.0.0.1:8000/{file_path}",
        "bg_removed": remove_bg
    }

async def remove_background(image_bytes: bytes) -> bytes:
    """Remove background using rembg"""
    try:
        from rembg import remove
        return remove(image_bytes)
    except ImportError:
        return simple_bg_remove(image_bytes)

def simple_bg_remove(image_bytes: bytes) -> bytes:
    """Simple background removal fallback"""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    data = img.getdata()
    
    new_data = []
    for item in data:
        if item[0] > 200 and item[1] > 200 and item[2] > 200:
            new_data.append((255, 255, 255, 0))
        else:
            new_data.append(item)
    
    img.putdata(new_data)
    output = io.BytesIO()
    img.save(output, format='PNG')
    return output.getvalue()
    
@router.get("/student-parent/{student_id}")
def get_student_parent(student_id: int, db: Session = Depends(get_db)):
    """Get parent linked to a student"""
    
    # Find student
    student = db.query(Student).filter(Student.user_id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # ✅ Find parent through ParentStudent relationship
    parent_link = db.query(ParentStudent).filter(
        ParentStudent.student_id == student.id
    ).first()
    
    if not parent_link:
        return {"name": None, "phone": None, "email": None, "relationship": None}
    
    # ✅ Get parent from Parent table using parent_link.parent_id
    parent = db.query(Parent).filter(Parent.id == parent_link.parent_id).first()
    if not parent:
        return {"name": None, "phone": None, "email": None, "relationship": None}
    
    # ✅ Get user from Parent.user_id
    parent_user = db.query(User).filter(User.id == parent.user_id).first()
    if not parent_user:
        return {"name": None, "phone": None, "email": None, "relationship": None}
    
    return {
        "name": parent_user.full_name,
        "phone": parent_user.phone,
        "email": parent_user.email,
        "relationship": parent_link.relation_type or "Parent"
    }
    
# ========== STUDENT FEE RECEIPT ==========
@router.get("/student-fee-receipt/{student_id}/{transact_id}", response_class=HTMLResponse)
def get_fee_receipt(
    student_id: int,
    transact_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not user:
        raise HTTPException(status_code=403, detail="Not Authenticated")

    student = db.query(Student).filter(Student.id == student_id).first()
    transact = db.query(FeeTransaction).filter(FeeTransaction.id==transact_id).first()
    
    if not student and not transact:
        raise HTTPException(status_code=404, detail="Student or Transaction not found")
    
    user_obj = db.query(User).filter(User.id == student.user_id).first()
    school = db.query(School).filter(School.id == student.school_id).first()
    
    fees = db.query(Fee).filter(Fee.student_id == student.user_id).all()
    total_fees = sum(f.amount for f in fees)
    total_paid = transact.amount
    balance = fees[0].balance
    
    
    if balance < 0:
        status_color = "green"
        status_text = "CREDIT+"
    elif balance == 0:
        status_color = "green"
        status_text = "FULLY PAID"
    else:
        status_color = "red"
        status_text = "BALANCE DUE"
    
    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <link href="https://fonts.googleapis.com/css2?family=Monsieur+La+Doulaise&display=swap" rel="stylesheet">
    <title>Receipt</title>
    <style>
        body {{
            margin: 0;
            background: #ddd;
            font-family: Arial, sans-serif;
        }}
        .page {{
            width: 100%;
            margin: 0;
            background: #f8f5ec;
            padding: 0 42px 40px;
            box-sizing: border-box;
            position: relative;
        }}
        .topbar {{
            height: 18px;
            background: #163765;
            margin: 0 -42px 18px;
        }}
        .header {{
            display: flex;
            align-items: center;
            gap: 16px;
        }}
        .logo {{
            width: 65px;
            height: 65px;
            border: 2px solid #163765;
            border-radius: 4px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 34px;
            color: #163765;
        }}
        .school h1 {{
            margin: 0;
            font: 700 36px Georgia, serif;
            text-transform: uppercase;
        }}
        .school .sub {{
            font-size: 14px;
            text-align: center;
        }}
        .school .addr {{
            font-size: 12px;
        }}
        h2 {{
            text-align: center;
            margin: 22px 0;
            font-size: 34px;
        }}
        .meta {{
            width: 42%;
            margin-left: auto;
            font-size: 14px;
            line-height: 1.8;
            text-align: right;
        }}
        .info {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px 20px;
            margin: 18px 0;
        }}
        .row {{
            border-bottom: 1px solid #222;
            padding: 2px 0;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 10px;
        }}
        th, td {{
            border: 1px solid #222;
            padding: 6px 8px;
        }}
        th {{
            background: #e6e6e6;
        }}
        td:last-child, th:last-child {{
            text-align: right;
        }}
        .total td {{
            font-weight: bold;
        }}
        .sig {{
            display: flex;
            justify-content: space-between;
            margin-top: 45px;
        }}
        .sign {{
            width: 45%;
            text-align: center;
        }}
        .line {{
            border-top: 1px solid #000;
            margin-top: 35px;
        }}
        .script {{
            font-family: "Brush Script MT", cursive;
            font-size: 42px;
            color: #123d7a;
        }}
        .stamp {{
            width: 200px;
            height: 200px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #1b4d9b;
            font-weight: bold;
            margin: 0 auto;
        }}
        .watermark {{
            position: absolute;
            left: 50%;
            top: 50%;
            transform: translate(-50%, -50%);
            width: 50%;
            opacity: 0.06;
            height: 50%;
            border: 10px solid rgba(50, 80, 150, .05);
            border-radius: 50%;
            z-index: 0;
        }}
        table, .info, .meta, .sig, .header, h2 {{
            position: relative;
            z-index: 2;
        }}
    </style>
</head>
<body>
    <div class="page">
        <!-- Watermark -->
        <div class="watermark"><img src="http://127.0.0.1:8000/uploads/bg.png" /></div>

        <!-- Top Bar -->
        <div class="topbar"></div>

        <!-- Header -->
        <div class="header">
            <div class="logo"><img src="http://127.0.0.1:8000{school.logo}" class="logo" /></div>
            <div class="school">
                <h1 style="align: center";>{school.school_name}</h1>
                <div class="sub">{school.address}</div>
                <div class="addr" style="text-align: center;">Email: {school.email} - tel: {school.phone}</div>
            </div>
        </div>

        <!-- Title -->
        <h2>PAYMENT RECEIPT</h2>

        <!-- Meta Info (Right Aligned) -->
        <div class="meta">
            <div><b>RECEIPT NUMBER:</b> <b style="font-size:20px; color:red;">00{transact.id}</b></div>
            <div><b>RECEIPT DATE:</b> {datetime.now().strftime("%b %d, %Y")}</div>
            <div><b>PAYMENT DATE:</b> {(transact.payment_date).strftime("%b %d, %Y")}</div>
        </div>

        <!-- Student Info Grid -->
        <div class="info" style="font-size: 12px">
            <div class="row"><b>STUDENT NAME:</b> <span class="script" style="font-family: cookie; font-size:12px;">{user_obj.full_name}</span></div>
            <div class="row"><b>STUDENT ADM:</b> {student.admission_number}</div>
            <div class="row"><b>CLASS:</b> {student.class_name}</div>
        </div>

        <!-- Payment Table -->
        <table>
            <tr>
                <th>DESCRIPTION</th>
                <th>AMOUNT</th>
            </tr>
            <tr class="total">
                <td>MODE</td>
                <td>{transact.payment_provider}</td>
            </tr>
            <tr class="total" style="color:green;">
                <td>AMOUNT PAID</td>
                <td>{transact.amount}</td>
            </tr>
            <tr class="total" style="color:{status_color};">
    <td>CURRENT BALANCE</td>
    <td>{abs(balance):,}</td>
</tr>
<tr>
    <td colspan="2" style="text-align:center;color:{status_color};font-weight:bold;">
        {status_text}
    </td>
</tr>
        </table>

        <!-- Payment Method -->
        <div style="margin-top:12px; text-align:right;">
            <b>Reference:</b> <span class="script" style="font-size:18px; ">{transact.transaction_reference}</span>
        </div>

        <!-- Signature Section -->
        <div class="sig">
            <div class="sign">
                <div style="font-size: 30px; font-family: 'Monsieur La Doulaise', cursive;">{user.full_name}</div>
                <div class="line"></div>
                <div>AUTHORIZED ADMINISTRATOR SIGNATURE<br>Administrative Officer</div>
            </div>
            <div class="sign">
                <div><img src="http://127.0.0.1:8000{school.stamp}" class="stamp" /></div>
            </div>
        </div>
    </div>
</body>
</html>"""
    pdf_bytes = HTML(string=html).write_pdf()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="receipt_{student_id}.pdf"'
        },
    )
    
@router.post("/schools/{school_id}/upload-stamp")
async def upload_school_stamp(
    school_id: int,
    file: UploadFile = File(...),
    remove_bg: bool = True,
    db: Session = Depends(get_db)
):
    """Upload school stamp with background removal"""
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    
    upload_dir = "uploads/stamps"
    os.makedirs(upload_dir, exist_ok=True)
    
    contents = await file.read()
    
    # Remove background
    if remove_bg:
        contents = await remove_background(contents)
    
    file_path = f"{upload_dir}/stamp_{school_id}.png"
    with open(file_path, "wb") as f:
        f.write(contents)
    
    school.stamp = f"/{file_path}"
    db.commit()
    
    return {"message": "Stamp uploaded", "url": f"http://127.0.0.1:8000/{file_path}"}
    
def determine_current_term() -> Tuple[int, str, int]:
    """Determine current term based on month"""
    month = datetime.now().month
    year = datetime.now().year
    
    if 1 <= month <= 4:  # January to April
        return 1, f"Term 1 - {year}", year
    elif 5 <= month <= 8:  # May to August
        return 2, f"Term 2 - {year}", year
    else:  # September to December
        return 3, f"Term 3 - {year}", year

def get_term_sort_key(fee: Fee) -> Tuple[int, int]:
    """
    Sort key for terms - oldest first
    Returns (year, term_number) for chronological sorting
    """
    term_year = fee.academic_year if hasattr(fee, 'academic_year') else datetime.now().year
    return (term_year, fee.term_number)

def allocate_payment_to_terms(total_paid: int, fees: List[Fee]) -> Tuple[List[Fee], int]:
    """
    Allocate payment to terms sequentially, oldest first.
    Only allocate to terms that are due or overdue.
    Future terms should not receive payment allocation.
    Returns (updated_fees, overpayment_amount)
    """
    remaining = total_paid
    current_term_num, current_term_name, current_year = determine_current_term()
    
    # Sort fees chronologically (oldest first)
    sorted_fees = sorted(fees, key=lambda x: (x.academic_year or 0, x.term_number or 0))
    
    for fee in sorted_fees:
        # Determine if this term is in the past, current, or future
        term_year = fee.academic_year if fee.academic_year else current_year
        
        is_future_term = term_year > current_year or (term_year == current_year and fee.term_number > current_term_num)
        
        if is_future_term:
            # Future term - no payment allocated
            fee.paid = 0
            fee.balance = fee.amount
            fee.status = "upcoming"
            continue
        
        if remaining <= 0:
            # No more payment to allocate
            fee.paid = fee.paid or 0
            fee.balance = fee.amount - (fee.paid or 0)
            
            # Set status
            if fee.balance <= 0:
                fee.status = "paid"
            elif term_year < current_year or fee.term_number < current_term_num:
                fee.status = "overdue"
            elif fee.term_number == current_term_num:
                fee.status = "pending" if (fee.paid or 0) == 0 else "partial"
            continue
        
        # Calculate balance for this term
        term_balance = fee.amount - (fee.paid or 0)
        
        if term_balance <= 0:
            # Already paid
            fee.balance = 0
            fee.status = "paid"
            continue
        
        if remaining >= term_balance:
            # Fully pay this term
            fee.paid = fee.amount
            fee.balance = 0
            fee.status = "paid"
            remaining -= term_balance
        else:
            # Partially pay this term
            fee.paid = (fee.paid or 0) + remaining
            fee.balance = fee.amount - fee.paid
            fee.status = "partial"
            remaining = 0
    
    # Remaining amount is overpayment
    # This is the amount left after paying all due terms
    return sorted_fees, remaining

def get_or_create_fee_terms(student_id: int, db: Session, school_id: int) -> List[Fee]:
    """
    Return the student's existing Fee rows.
    If the student has NO rows at all, create one for the current term.
    Never invents historical terms.
    """
    fees = (
        db.query(Fee)
        .filter(Fee.student_id == student_id)
        .order_by(Fee.academic_year.asc(), Fee.term_number.asc())
        .all()
    )
    if fees:
        return fees

    # No rows — create only the current term
    current_term_num, _, current_year = determine_current_term()

    structure = (
        db.query(FeeStructure)
        .filter(
            FeeStructure.school_id == school_id,
            FeeStructure.academic_year == current_year,
            FeeStructure.term_number == current_term_num,
        )
        .first()
    )
    if not structure:
        return []

    fee = Fee(
        student_id=student_id,
        amount=structure.amount,
        paid=0,
        balance=structure.amount,
        term_number=structure.term_number,
        term_name=structure.term_name,
        academic_year=structure.academic_year,
        status="pending",
        due_date=datetime.now(),
    )
    db.add(fee)
    db.flush()
    return [fee]

@router.get("/student-fee-details/{student_id}")
async def get_student_fee_details(
    student_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    from collections import defaultdict

    # ── Load student ──
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    # ── Authorization ──
    if user.role in ['admin', 'school'] and user.school_id == student.school_id:
        pass
    elif user.role == 'parent':
        parent = db.query(Parent).filter(Parent.user_id == user.id).first()
        if not parent:
            raise HTTPException(status_code=403, detail="Parent record not found")
        parent_student = (
            db.query(ParentStudent)
            .filter(
                ParentStudent.parent_id == parent.id,
                ParentStudent.student_id == student.id,
            )
            .first()
        )
        if not parent_student:
            raise HTTPException(status_code=403, detail="Not Authorized for parent")
    else:
        raise HTTPException(
            status_code=403,
            detail=f"Not Authorized for role: {user.role} for student {student.id}",
        )

    # ── School ──
    school = db.query(School).filter(School.id == student.school_id).first()

    # ── Current term ──
    current_term_num, current_term_name, current_year = determine_current_term()

    # ════════════════════════════════════════════════════════════
    # 1) CARRY-OVER from PRIOR YEARS
    # ════════════════════════════════════════════════════════════
    prior_fee_rows = (
        db.query(Fee)
        .filter(
            Fee.student_id == student.user_id,
            Fee.academic_year < current_year,
        )
        .order_by(Fee.academic_year, Fee.term_number)
        .all()
    )

    carried_over = 0
    prior_breakdown = []
    for pf in prior_fee_rows:
        unpaid = max(0, (pf.amount or 0) - (pf.paid or 0))
        if unpaid > 0:
            carried_over += unpaid
            prior_breakdown.append({
                "year": pf.academic_year,
                "term": pf.term_name or f"Term {pf.term_number}",
                "term_number": pf.term_number,
                "total": pf.amount,
                "paid": int(pf.paid or 0),
                "balance": unpaid,
            })

    # ════════════════════════════════════════════════════════════
    # 2) CURRENT YEAR Fee rows
    # ════════════════════════════════════════════════════════════
    fee_rows = (
        db.query(Fee)
        .filter(
            Fee.student_id == student.user_id,
            Fee.academic_year == current_year,
        )
        .order_by(Fee.term_number)
        .all()
    )

    print(f"📊 Fee rows for {current_year}: {len(fee_rows)}")
    for f in fee_rows:
        print(f"   Term {f.term_number}: amount={f.amount}, paid={f.paid}")

    # ════════════════════════════════════════════════════════════
    # 3) Transactions tied to CURRENT YEAR fee rows
    # ════════════════════════════════════════════════════════════
    fee_ids = [f.id for f in fee_rows]
    transactions = []
    if fee_ids:
        transactions = (
            db.query(FeeTransaction)
            .filter(
                FeeTransaction.fee_id.in_(fee_ids),
                FeeTransaction.student_id == student.user_id,
            )
            .order_by(FeeTransaction.payment_date.desc())
            .all()
        )

    # ════════════════════════════════════════════════════════════
    # 4) FIFO split of txs across terms
    # ════════════════════════════════════════════════════════════
    txs_asc = sorted(
        [t for t in transactions if (t.amount or 0) > 0],
        key=lambda t: t.payment_date or t.created_at,
    )

    allocations = defaultdict(list)
    remaining_capacity = {f.id: max(0, f.amount) for f in fee_rows}

    for tx in txs_asc:
        left = tx.amount or 0
        for f in fee_rows:
            if left <= 0:
                break
            cap = remaining_capacity[f.id]
            if cap <= 0:
                continue
            portion = min(cap, left)
            allocations[f.id].append({
                "id": tx.id,
                "amount": int(portion),
                "payment_provider": tx.payment_provider,
                "transaction_reference": tx.transaction_reference,
                "payment_date": tx.payment_date.isoformat() if tx.payment_date else None,
            })
            remaining_capacity[f.id] -= portion
            left -= portion

    # ════════════════════════════════════════════════════════════
    # 5) Per-term breakdown
    # ════════════════════════════════════════════════════════════
    total_fees_this_year = 0
    total_paid_this_year = 0
    balance_this_year = 0
    terms = []

    for f in fee_rows:
        term_paid = f.paid or 0
        term_balance = max(0, f.amount - term_paid)
        status = f.status or (
            "paid" if term_balance == 0 and f.amount > 0
            else "partial" if term_paid > 0
            else "overdue" if f.term_number < current_term_num
            else "pending" if f.term_number == current_term_num
            else "upcoming"
        )

        total_fees_this_year += f.amount
        total_paid_this_year += term_paid
        if status != "upcoming":
            balance_this_year += term_balance

        terms.append({
            "term": f.term_name or f"Term {f.term_number} - {f.academic_year}",
            "term_number": f.term_number,
            "academic_year": f.academic_year,
            "total": f.amount,
            "paid": int(term_paid),
            "balance": int(term_balance),
            "status": status,
            "payments": allocations.get(f.id, []),
        })

    # ════════════════════════════════════════════════════════════
    # 6) Overpayment — from RAW transactions, not capped Fee.paid
    # ════════════════════════════════════════════════════════════
    total_received = sum(
        (t.amount or 0) for t in transactions if (t.amount or 0) > 0
    )
    overpaid = max(0, total_received - total_fees_this_year)
    balance_this_year = max(0, balance_this_year)

    # ════════════════════════════════════════════════════════════
    # 7) Grand totals (with carry-over)
    # ════════════════════════════════════════════════════════════
    grand_total_owed = total_fees_this_year + carried_over
    grand_balance = carried_over + balance_this_year
    overpaid_remaining = overpaid

    # Apply overpayment against grand_balance (carryover first, then this year)
    if overpaid_remaining > 0 and grand_balance > 0:
        applied = min(overpaid_remaining, grand_balance)
        grand_balance -= applied
        overpaid_remaining -= applied

    # ════════════════════════════════════════════════════════════
    # 8) Response
    # ════════════════════════════════════════════════════════════
    return {
        "student_id": student.id,
        "student_name": student.user.full_name if student.user else None,
        "full_name": student.user.full_name if student.user else None,
        "admission_number": student.admission_number,
        "class_name": student.class_name,
        "class": student.class_name,

        "school": {
            "id": school.id if school else None,
            "name": school.school_name if school else None,
            "address": school.address if school else None,
            "type": school.school_type if school else None,
            "phone": school.phone if school else None,
            "email": school.email if school else None,
            "logo": school.logo if school else None,
        },
        "school_name": school.school_name if school else None,
        "school_logo": school.logo if school else None,

        # ── Current year ──
        "academic_year": current_year,
        "current_term": current_term_name,
        "current_term_number": current_term_num,
        "total_fees": int(total_fees_this_year),
        "total_received": int(total_received),
        "total_paid": int(total_paid_this_year),
        "balance": int(balance_this_year),
        "overpaid": int(overpaid_remaining),

        # ── Carry-over from prior years ──
        "carried_over": int(carried_over),
        "prior_breakdown": prior_breakdown,

        # ── Grand totals ──
        "grand_total_owed": int(grand_total_owed),
        "grand_total_paid": int(total_paid_this_year),
        "grand_balance": int(grand_balance),

        # ── Detail ──
        "terms": terms,
        "transactions": [
            {
                "id": tx.id,
                "amount": tx.amount,
                "payment_provider": tx.payment_provider,
                "transaction_reference": tx.transaction_reference,
                "payment_date": tx.payment_date.isoformat() if tx.payment_date else None,
                "created_at": tx.created_at.isoformat() if tx.created_at else None,
                "fee_id": tx.fee_id,
            }
            for tx in transactions
        ],
    }
             
@router.post("/refresh")
async def refresh_token(
    refresh_token: str = Form(...),
    db: Session = Depends(get_db)
):
    from ....core.session_auth import create_session
    session = db.query(Refresh).filter(
        Refresh.session_key == refresh_token,
        Refresh.is_active == True,
        Refresh.expires_at > datetime.utcnow()
    ).first()
    
    if not session:
        raise HTTPException(status_code=401, detail="Refresh token expired")
    
    new_session_key = create_session(
        user_id=session.user_id,
        db=db,
        request=None,
        school_id=session.school_id
    )
    
    session.is_active = False
    db.commit()
    
    return {
        "session_key": new_session_key,
        "token_type": "bearer"
    }
    
@router.put("/update-profile")
def update_profile(
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(None),
    username: str = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    user.full_name = full_name
    user.email = email
    user.phone = phone
    if username: user.username = username
    db.commit()
    return {"message": "Profile updated"}

@router.put("/change-password")
def change_password(
    current_password: str = Form(...),
    new_password: str = Form(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not verify_password(current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    user.hashed_password = get_password_hash(new_password)
    db.commit()
    return {"message": "Password changed"}

@router.put("/update-school")
def update_school(
    school_name: str = Form(None),
    address: str = Form(None),
    motto: str = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    school_id = get_user_school_id(user, db)  # ← Make sure this line exists!
    school = db.query(School).filter(School.id == school_id).first()
    
    if school:
        if school_name: school.school_name = school_name
        if address: school.address = address
        if motto: school.motto = motto
        db.commit()
    return {"message": "School updated"}
    
@router.get("/my-sessions")
def get_my_sessions(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):

    current_key = request.headers.get("Authorization", "").replace("Bearer ", "")
    
    sessions = db.query(Refresh).filter(
        Refresh.user_id == user.id,
        Refresh.is_active == True
    ).order_by(Refresh.created_at.desc()).all()
    
    result = []
    for s in sessions:
        result.append({
            "id": s.id,
            "device": s.user_agent or "Unknown device",
            "ip": s.ip_address or "Unknown",
            "created_at": str(s.created_at),
            "is_current": s.session_key == current_key
        })
    return {"data": result}
    
@router.post("/deactivate-session/{session_id}")
def deactivate_session(
    session_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    session = db.query(Refresh).filter(
        Refresh.id == session_id,
        Refresh.user_id == user.id
    ).first()
    
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    session.is_active = False
    db.commit()
    return {"message": "Session deactivated"}
    
@router.put("/update-school")
def update_school(
    school_name: str = Form(None),
    address: str = Form(None),
    motto: str = Form(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    school_id = get_user_school_id(user, db)
    school = db.query(School).filter(School.id == school_id).first()
    if school:
        if school_name: school.school_name = school_name
        if address: school.address = address
        if motto: school.motto = motto
        db.commit()
    return {"message": "School updated"}
    
# ========== SCHOOL LOGO & STAMP UPLOAD ==========

@router.post("/schools/{school_id}/upload-logo")
async def upload_school_logo(
    school_id: int,
    file: UploadFile = File(...),
    remove_bg: bool = False,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Upload school logo"""
    
    # Check authorization
    if user.role not in ["admin", "school"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Get school
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    
    # Check if user owns this school (if not admin)
    if user.role != "admin" and school.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Create directory
    upload_dir = "uploads/schools"
    os.makedirs(upload_dir, exist_ok=True)
    
    # Read and process image
    contents = await file.read()
    
    # Remove background if requested
    if remove_bg:
        contents = await remove_background(contents)
    
    # Resize image
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(contents))
        img.thumbnail((512, 512), Image.LANCZOS)
        output = io.BytesIO()
        img.save(output, format='PNG')
        contents = output.getvalue()
    except Exception as e:
        print(f"Image processing error: {e}")
    
    # Save file
    file_name = f"logo_{school_id}.png"
    file_path = os.path.join(upload_dir, file_name)
    
    with open(file_path, "wb") as f:
        f.write(contents)
    
    # Update database
    school.logo = f"/{file_path}"
    db.commit()
    
    return {
        "message": "School logo uploaded successfully",
        "url": f"/{file_path}",
        "school_id": school_id
    }


@router.post("/schools/{school_id}/upload-stamp")
async def upload_school_stamp(
    school_id: int,
    file: UploadFile = File(...),
    remove_bg: bool = True,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Upload school stamp with background removal"""
    
    # Check authorization
    if user.role not in ["admin", "school"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Get school
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    
    # Check if user owns this school (if not admin)
    if user.role != "admin" and school.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Create directory
    upload_dir = "uploads/stamps"
    os.makedirs(upload_dir, exist_ok=True)
    
    # Read and process image
    contents = await file.read()
    
    # Remove background if requested
    if remove_bg:
        contents = await remove_background(contents)
    
    # Resize image
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(contents))
        img.thumbnail((512, 512), Image.LANCZOS)
        output = io.BytesIO()
        img.save(output, format='PNG')
        contents = output.getvalue()
    except Exception as e:
        print(f"Image processing error: {e}")
    
    # Save file
    file_path = f"uploads/stamps/stamp_{school_id}.png"
    with open(file_path, "wb") as f:
        f.write(contents)
    
    # Update database
    school.stamp = f"/{file_path}"
    db.commit()
    
    return {
        "message": "School stamp uploaded successfully",
        "url": f"/{file_path}",
        "school_id": school_id
    }


@router.delete("/schools/{school_id}/logo")
async def remove_school_logo(
    school_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Remove school logo"""
    
    if user.role not in ["admin", "school"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    
    if user.role != "admin" and school.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    school.logo = None
    db.commit()
    
    return {"message": "School logo removed"}


@router.delete("/schools/{school_id}/stamp")
async def remove_school_stamp(
    school_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Remove school stamp"""
    
    if user.role not in ["admin", "school"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    
    if user.role != "admin" and school.user_id != user.id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    school.stamp = None
    db.commit()
    
    return {"message": "School stamp removed"}
    
def validate_user_access(
    target_user_id: int,
    current_user: User,
    db: Session,
    role: str = None
) -> bool:
    """
    Validate that the current user can access the target user's data.
    
    Args:
        target_user_id: The ID of the user being accessed
        current_user: The user making the request
        db: Database session
        role: Optional role to validate (teacher, student, parent)
    
    Returns:
        True if authorized, raises HTTPException otherwise
    """
    
    # 1. ADMIN - Full access
    if current_user.role == "admin":
        return True
    
    # 2. SELF - Users can access their own data
    if current_user.id == target_user_id:
        return True
    
    # 3. PARENT - Can access their children's data
    if current_user.role == "parent" and role == "student":
        # Check if this student is their child
        parent = db.query(Parent).filter(Parent.user_id == current_user.id).first()
        if parent:
            child = db.query(Student).filter(Student.user_id == target_user_id).first()
            if child and child.school_id == parent.school_id:
                return True
    
    # 4. TEACHER - Can access their students' data
    if current_user.role == "teacher" and role == "student":
        # Check if this student is in their class
        teacher = db.query(Teacher).filter(Teacher.user_id == current_user.id).first()
        if teacher:
            student = db.query(Student).filter(Student.user_id == target_user_id).first()
            if student and student.class_name in teacher.classes:
                return True
    
    # 5. STUDENT - Can only access their own data (already handled in step 2)
    
    # If none of the above, deny access
    raise HTTPException(
        status_code=403,
        detail=f"You are not authorized to access this {role or 'user'}'s data"
    )


def get_target_user(
    target_user_id: int,
    current_user: User,
    db: Session
) -> User:
    """Get target user with access validation"""
    
    target_user = db.query(User).filter(User.id == target_user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Validate access
    validate_user_access(target_user_id, current_user, db)
    
    return target_user
    
# ========== STUDENT ENDPOINTS WITH SINGLE SESSION ACCESS ==========

@router.get("/student-profile/{student_id}")
def get_student_profile(
    student_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get student profile - only self or admin access"""
    
    # Validate access
    if user.role != "admin" and user.id != student_id:
        raise HTTPException(
            status_code=403,
            detail="You can only access your own student profile"
        )
    
    student = db.query(Student).filter(Student.user_id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    student_user = db.query(User).filter(User.id == student_id).first()
    if not student_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get performance data
    performances = db.query(StudentPerformance).filter(
        StudentPerformance.student_id == student_id
    ).all()
    
    avg_score = 0
    subject_scores = []
    if performances:
        avg_score = round(sum(p.score for p in performances) / len(performances), 1)
        subject_scores = [
            {
                "subject": p.subject,
                "score": p.score,
                "term": p.term,
                "class": p.class_name
            }
            for p in performances
        ]
    
    # Get fees
    fees = db.query(Fee).filter(Fee.student_id == student_id).all()
    total_fees = sum(f.amount for f in fees)
    total_paid = sum(f.paid for f in fees)
    
    # Get parent
    parent = db.query(Parent).filter(Parent.school_id == student.school_id).first()
    parent_user = db.query(User).filter(User.id == parent.user_id).first() if parent else None
    
    # Get school
    school = db.query(School).filter(School.id == student.school_id).first() if student.school_id else None
    
    return {
        "student": {
            "id": student_user.id,
            "name": student_user.full_name,
            "email": student_user.email,
            "phone": student_user.phone,
            "class": student.class_name,
            "admission_number": student.admission_number,
            "gender": student.gender,
            "school_name": student.school_name,
            "profile_picture": student.profile_picture,
            "created_at": str(student_user.created_at)
        },
        "performance": {
            "average": avg_score,
            "subjects": subject_scores,
            "total": len(performances)
        },
        "fees": {
            "total": total_fees,
            "paid": total_paid,
            "balance": total_fees - total_paid
        },
        "parent": {
            "id": parent_user.id if parent_user else None,
            "name": parent_user.full_name if parent_user else None,
            "email": parent_user.email if parent_user else None,
            "phone": parent_user.phone if parent_user else None,
            "relationship": parent.relationship if parent else None
        } if parent_user else None,
        "school": {
            "id": school.id if school else None,
            "name": school.school_name if school else None,
            "logo": school.logo if school else None,
            "motto": school.motto if school else None
        } if school else None
    }


@router.get("/student-dashboard/{student_id}")
def get_student_dashboard(
    student_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get student dashboard - only self or admin access"""
    
    # Validate access
    if user.role != "admin" and user.id != student_id:
        raise HTTPException(
            status_code=403,
            detail="You can only access your own student dashboard"
        )
    
    student = db.query(Student).filter(Student.user_id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    student_user = db.query(User).filter(User.id == student_id).first()
    if not student_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get performance
    performances = db.query(StudentPerformance).filter(
        StudentPerformance.student_id == student_id
    ).all()
    
    avg_score = 0
    if performances:
        avg_score = round(sum(p.score for p in performances) / len(performances), 1)
    
    # Get assignments
    assignments = db.query(Assignment).filter(
        Assignment.class_name == student.class_name
    ).all()
    
    total_assignments = len(assignments)
    pending_assignments = sum(1 for a in assignments if a.status == "Active")
    
    # Get attendance (from timetable)
    attendance_count = db.query(Timetable).filter(
        Timetable.class_name == student.class_name
    ).count()
    
    # Get fees
    fees = db.query(Fee).filter(Fee.student_id == student_id).all()
    total_fees = sum(f.amount for f in fees)
    total_paid = sum(f.paid for f in fees)
    
    # Get recent activities
    recent_activities = []
    
    # Get events
    events = db.query(Event).filter(
        Event.school_id == student.school_id
    ).order_by(desc(Event.created_at)).limit(3).all()
    
    for event in events:
        recent_activities.append({
            "type": "event",
            "title": event.title,
            "description": event.description,
            "date": str(event.event_date),
            "time": event.time
        })
    
    # Get announcements
    announcements = db.query(Announcement).filter(
        Announcement.school_id == student.school_id
    ).order_by(desc(Announcement.created_at)).limit(3).all()
    
    for ann in announcements:
        recent_activities.append({
            "type": "announcement",
            "title": ann.title,
            "description": ann.description,
            "date": str(ann.created_at)
        })
    
    return {
        "student": {
            "id": student_user.id,
            "name": student_user.full_name,
            "class": student.class_name,
            "admission_number": student.admission_number
        },
        "stats": {
            "average_score": avg_score,
            "total_assignments": total_assignments,
            "pending_assignments": pending_assignments,
            "fees_total": total_fees,
            "fees_paid": total_paid,
            "fees_balance": total_fees - total_paid
        },
        "recent_activities": recent_activities[:5]
    }


@router.get("/student-performance/{student_id}")
def get_student_performance(
    term: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Student performance."""

    # ----------------------------
    # Find student
    # ----------------------------
    student_id = user.id
    student = db.query(Student).filter(
        Student.user_id == student_id
    ).first()

    if not student:
        raise HTTPException(
            status_code=404,
            detail="Student not found"
        )

    # ----------------------------
    # Access Control
    # ----------------------------

    # Student
    if user.role == "student":
        if user.id != student_id:
            raise HTTPException(
                status_code=403,
                detail="You can only view your own performance."
            )

    # Parent
    elif user.role == "parent":

        parent = db.query(Parent).filter(
            Parent.user_id == user.id
        ).first()

        if not parent:
            raise HTTPException(
                status_code=403,
                detail="Parent profile not found."
            )

        relation = db.query(ParentStudent).filter(
            ParentStudent.parent_id == parent.id,
            ParentStudent.student_id == student.id
        ).first()

        if not relation:
            raise HTTPException(
                status_code=403,
                detail="You can only access your own children's performance."
            )

    # Admin
    elif user.role == "admin":

        if student.school_id != user.school_id:
            raise HTTPException(
                status_code=403,
                detail="You can only access students in your school."
            )

    else:
        raise HTTPException(
            status_code=403,
            detail="Access denied."
        )

    # ----------------------------
    # Student user
    # ----------------------------
    student_user = db.query(User).filter(
        User.id == student.user_id
    ).first()

    if not student_user:
        raise HTTPException(
            status_code=404,
            detail="Student user not found."
        )

    # ----------------------------
    # Performance
    # ----------------------------
    query = db.query(StudentPerformance).filter(
        StudentPerformance.student_id == student.user_id
    )
    
    all_terms_result = db.query(StudentPerformance.term).filter(
        StudentPerformance.student_id == student.user_id
    ).order_by(StudentPerformance.term.desc()).all()
    
    all_terms = [t[0] for t in all_terms_result] if all_terms_result else []
    unique_terms = list(dict.fromkeys(all_terms))
    if term is None:
        term = unique_terms[0] if unique_terms else None

    if term:
        query = query.filter(
            StudentPerformance.term == term
        )

    performances = query.order_by(
        StudentPerformance.created_at.desc()
    ).all()

    terms_dict = {}

    for p in performances:
        terms_dict.setdefault(p.term, []).append({
            "subject": p.subject,
            "score": p.score,
            "class": p.class_id,
            "assessment": p.assessment
        })

    term_averages = {}

    for term_name, subjects in terms_dict.items():
        term_averages[term_name] = round(
            sum(s["score"] for s in subjects) / len(subjects),
            1
        )

    overall_average = (
        round(sum(term_averages.values()) / len(term_averages), 1)
        if term_averages else 0
    )

    return {
        "student": {
            "id": student.user_id,
            "name": student_user.full_name,
            "class": student.class_id,
        },
        "all_terms": unique_terms,
        "term": terms_dict,
        "term_averages": term_averages,
        "overall_average": overall_average,
    }

@router.get("/student-assignments/{student_id}")
def get_student_assignments(
    student_id: int,
    status: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get student assignments - only self or admin access"""
    
    if user.role != "admin" and user.id != student_id:
        raise HTTPException(
            status_code=403,
            detail="You can only access your own assignments"
        )
    
    student = db.query(Student).filter(Student.user_id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    query = db.query(Assignment).filter(
        Assignment.class_name == student.class_name
    )
    
    if status:
        query = query.filter(Assignment.status == status)
    
    assignments = query.order_by(desc(Assignment.created_at)).all()
    
    # Get submissions
    submissions = db.query(AssignmentSubmission).filter(
        AssignmentSubmission.student_id == student_id
    ).all()
    submission_map = {sub.assignment_id: sub for sub in submissions}
    
    from datetime import datetime
    today = datetime.utcnow().date()
    
    data = []
    for a in assignments:
        # ✅ SKIP if expired and not submitted
        if a.due_date:
            try:
                # Parse the date string
                due_date_str = a.due_date.strip()
                
                # Handle different formats
                if "-" in due_date_str:
                    # Format: YYYY-MM-DD or DD-MM-YYYY
                    parts = due_date_str.split("-")
                    if len(parts) == 3:
                        if len(parts[0]) == 4:
                            # YYYY-MM-DD
                            due_date = datetime(int(parts[0]), int(parts[1]), int(parts[2])).date()
                        else:
                            # DD-MM-YYYY
                            due_date = datetime(int(parts[2]), int(parts[1]), int(parts[0])).date()
                    else:
                        continue
                elif "/" in due_date_str:
                    # Format: D/M/YYYY or M/D/YYYY
                    parts = due_date_str.split("/")
                    if len(parts) == 3:
                        # Assume D/M/YYYY (e.g., 2/9/2026 = September 2, 2026)
                        due_date = datetime(int(parts[2]), int(parts[1]), int(parts[0])).date()
                    else:
                        continue
                else:
                    continue
                
                # ✅ Check if expired AND not submitted
                if due_date < today and a.id not in submission_map:
                    print(f"⏭️ Skipping expired: {a.id} due {due_date}")
                    continue
                    
            except Exception as e:
                print(f"⚠️ Date parse error for {a.id}: {e}")
                # If can't parse, still show the assignment
        
        assignment_data = {
            "id": a.id,
            "title": a.title,
            "description": a.description,
            "subject": a.subject,
            "due_date": a.due_date,
            "due_time": a.due_time,
            "status": a.status,
            "submitted": a.submitted_count,
            "total": a.total_students,
            "created_at": str(a.created_at),
            "assignment_type": getattr(a, 'assignment_type', ''),
            "typing_questions": getattr(a, 'typing_questions', ''),
            "mcq_questions": getattr(a, 'mcq_questions', ''),
            "file_path": getattr(a, 'file_path', ''),
            "student_grade": submission_map[a.id].grade if a.id in submission_map else None,
            "student_score": submission_map[a.id].score if a.id in submission_map else None,
            "student_submitted": a.id in submission_map,
            "student_submission_status": submission_map[a.id].status if a.id in submission_map else None,
        }
        
        data.append(assignment_data)
    
    print(f"📊 Returning {len(data)} from {len(assignments)}")
    
    return {
        "data": data,
        "total": len(data)
    }


@router.get("/student-timetable")
def get_student_timetable(user = Depends(get_current_user), db: Session = Depends(get_db), day: Optional[str] = Query(None)):
    student = db.query(Student).filter(Student.user_id == user.id).first()
    
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    student_user = db.query(User).filter(User.id == user.id).first()
    if not student_user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Get school
    school = db.query(School).filter(School.id == student.school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    
    # Get timetable for student's class
    query = db.query(Timetable).filter(
        Timetable.school_id == student.school_id,
        Timetable.class_name == student.class_name
    )
    
    if day:
        query = query.filter(Timetable.day_of_week == day)
    else:
        today = datetime.utcnow().strftime("%A")
        query = query.filter(Timetable.day_of_week == today)
    
    lessons = query.order_by(Timetable.start_time).all()
    
    # Build lesson list with teacher names
    lesson_list = []
    for l in lessons:
        teacher_name = "N/A"
        if l.teacher_id:
            teacher = db.query(User).filter(User.id == l.teacher_id).first()
            if teacher:
                teacher_name = teacher.full_name
        
        lesson_list.append({
            "id": l.id,
            "subject": l.subject,
            "teacher_id": l.teacher_id,
            "teacher_name": teacher_name,
            "start_time": l.start_time,
            "end_time": l.end_time,
            "time": f"{l.start_time} - {l.end_time}",
            "room": l.room,
            "is_break": l.is_break
        })
    
    return {
        "student": {
            "id": student.user_id,
            "name": student_user.full_name,
            "class": student.class_name
        },
        "day": day or datetime.utcnow().strftime("%A"),
        "lessons": lesson_list,
        "total": len(lesson_list)
    }

@router.get("/teacher-dashboard/{teacher_id}")
def get_teacher_dashboard(
    teacher_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get teacher dashboard with stats"""
    teacher_id = user.id
    # Validate access - only self, admin, or school admin
    if user.role not in ["teacher", "admin", "school"] and user.id != teacher_id:
        raise HTTPException(status_code=403, detail="Access denied")

    # Single query to get teacher and user info
    teacher_data = (
        db.query(Teacher, User)
        .join(User, User.id == Teacher.user_id)
        .filter(Teacher.user_id == teacher_id)
        .first()
    )
    if not teacher_data:
        raise HTTPException(status_code=404, detail="Teacher not found")

    teacher, teacher_user = teacher_data

    # ── Classes the teacher teaches (subjects)
    class_ids = (
        db.query(ClassSubjectTeacher.class_id)
        .filter(
            ClassSubjectTeacher.teacher_id == teacher.id,
            ClassSubjectTeacher.is_active == True,
        )
        .distinct()
        .all()
    )
    class_ids = [cid[0] for cid in class_ids]

    class_names = []
    if class_ids:
        classes = db.query(Class).filter(Class.id.in_(class_ids)).all()
        class_names = [cls.name for cls in classes]

    total_students = (
        db.query(func.count(Student.id))
        .filter(Student.class_id.in_(class_ids))
        .scalar()
        if class_ids else 0
    )

    # ── Class-teacher info
    cts = db.query(Class).filter(Class.class_teacher_id == teacher.id).first()
    is_class_teacher = bool(cts)

    ct_id = cts.id if cts else None
    ct_name = cts.name if cts else None
    ct_student_count = None
    ct_mean_grade = None
    ct_mean_score = None

    if cts:
        ct_student_count = (
            db.query(func.count(Student.id))
            .filter(Student.class_id == cts.id)
            .scalar()
        ) or 0

        # ── Latest non-zero assessment per (student, subject)
        _, term_num = current_term()
        term_str = f"Term {term_num}"

        assessment_rank = case(
            (StudentPerformance.assessment == "Opener",   1),
            (StudentPerformance.assessment == "Midterm",  2),
            (StudentPerformance.assessment == "End Term", 3),
            else_=0,
        )

        ranked = (
            db.query(
                StudentPerformance.student_id.label("sid"),
                StudentPerformance.subject.label("subject"),
                StudentPerformance.score.label("score"),
                func.row_number().over(
                    partition_by=(
                        StudentPerformance.student_id,
                        StudentPerformance.subject,
                    ),
                    order_by=assessment_rank.desc(),
                ).label("rn"),
            )
            .filter(
                StudentPerformance.class_id == cts.id,
                StudentPerformance.term == term_str,
                StudentPerformance.score > 0,
            )
            .subquery()
        )

        # Average of student averages (matches class-results exactly)
        student_avg = (
            db.query(
                ranked.c.sid.label("student_user_id"),
                func.avg(ranked.c.score).label("avg_score"),
            )
            .filter(ranked.c.rn == 1)
            .group_by(ranked.c.sid)
            .subquery()
        )

        term_mean = db.query(func.avg(student_avg.c.avg_score)).scalar()

        if term_mean is not None:
            ct_mean_score = round(float(term_mean), 2)
            ct_mean_grade = _grade_from_percent(ct_mean_score)

    # ── Assignment stats
    assignment_stats = db.query(
        func.count(Assignment.id).label("total"),
        func.sum(case((Assignment.status == "Active", 1), else_=0)).label("active"),
        func.sum(case((Assignment.status == "Completed", 1), else_=0)).label("completed"),
        func.sum(case((Assignment.status == "Graded", 1), else_=0)).label("graded"),
    ).filter(Assignment.teacher_id == teacher.id).first()

    # ── Today's timetable
    today = datetime.utcnow().strftime("%A")
    today_lessons = (
        db.query(Timetable)
        .filter(
            Timetable.teacher_id == teacher.id,
            Timetable.day_of_week == today,
        )
        .order_by(Timetable.start_time)
        .all()
    )

    # ── Recent assignments
    recent_assignments = (
        db.query(Assignment)
        .filter(Assignment.teacher_id == teacher.id)
        .order_by(desc(Assignment.created_at))
        .limit(5)
        .all()
    )

    # ── School
    school = None
    if teacher.school_id:
        school = db.query(School).filter(School.id == teacher.school_id).first()

    return {
        "teacher": {
            "id": teacher_user.id,
            "name": teacher_user.full_name,
            "email": teacher_user.email,
            "phone": teacher_user.phone,
            "subject": teacher.subject,
            "qualification": teacher.qualification,
            "experience": teacher.years_of_experience,
            "school_name": school.school_name if school else None,
        },
        "is_class_teacher": is_class_teacher,
        "ct_id": ct_id,
        "ct_name": ct_name,
        "ct_student_count": ct_student_count,
        "ct_mean_grade": ct_mean_grade,
        "ct_mean_score": ct_mean_score,
        "stats": {
            "total_students": total_students,
            "total_classes": len(class_names),
            "total_assignments": assignment_stats.total if assignment_stats else 0,
            "active_assignments": (assignment_stats.active or 0) if assignment_stats else 0,
            "completed_assignments": (assignment_stats.completed or 0) if assignment_stats else 0,
            "graded_assignments": (assignment_stats.graded or 0) if assignment_stats else 0,
        },
        "classes": class_names,
        "today_lessons": [
            {
                "id": l.id,
                "teacher_id": l.teacher_id,
                "day": today,
                "subject": l.subject,
                "class": l.class_name,
                "time": f"{l.start_time} - {l.end_time}",
                "start_time": l.start_time,
                "end_time": l.end_time,
                "room": l.room,
            }
            for l in today_lessons
        ],
        "recent_assignments": [
            {
                "id": a.id,
                "title": a.title,
                "subject": a.subject,
                "class": a.class_name,
                "status": a.status,
                "due_date": a.due_date,
                "submitted": a.submitted_count,
                "total": a.total_students,
                "created_at": str(a.created_at),
            }
            for a in recent_assignments
        ],
        "school": {
            "id": school.id if school else None,
            "name": school.school_name if school else None,
            "logo": school.logo if school else None,
            "motto": school.motto if school else None,
        } if school else None,
    }
    
@router.get("/teacher-classes/{teacher_id}")
def get_teacher_classes(
    teacher_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get classes taught by a teacher"""
    
    if user.role not in ["admin", "school"] and user.id != teacher_id:
        raise HTTPException(status_code=403, detail="Access denied")
    
    teacher = db.query(Teacher).filter(Teacher.user_id == teacher_id).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    
    # Get classes from ClassSubjectTeacher
    csts = db.query(ClassSubjectTeacher).filter(
        ClassSubjectTeacher.teacher_id == teacher.id,
        ClassSubjectTeacher.is_active == True
    ).all()
    
    result = []
    for cst in csts:
        cls = db.query(Class).filter(Class.id == cst.class_id).first()
        if cls:
            student_count = db.query(Student).filter(
                Student.class_id == cls.id
            ).count()
            result.append({
                "id": cst.id,           # ADD THIS - needed for delete
                "name": cls.name,
                "subject": cst.subject,
                "students": student_count,
            })
    
    return {"data": result}
    
# ========== CLASS MANAGEMENT ==========

@router.post("/classes")
def create_class(
    name: str = Body(...),
    school_id: int = Body(...),
    class_teacher_id: Optional[int] = Body(None),
    room: Optional[str] = Body(None),
    capacity: int = Body(45),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Create a new class with optional class teacher"""
    
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Only admin can create classes")
    
    # Check if school exists
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")
    
    # Check if class teacher exists (if provided)
    if class_teacher_id:
        teacher = db.query(Teacher).filter(Teacher.id == class_teacher_id).first()
        if not teacher:
            raise HTTPException(status_code=404, detail="Teacher not found")
    
    new_class = Class(
        name=name,
        school_id=school_id,
        class_teacher_id=class_teacher_id,
        room=room,
        capacity=capacity,
        academic_year=str(datetime.now().year)
    )
    db.add(new_class)
    db.commit()
    db.refresh(new_class)
    
    return {
        "message": "Class created successfully",
        "class_id": new_class.id,
        "name": new_class.name
    }
    
@router.put("/class-subject-teacher/{assignment_id}/deactivate")
def deactivate_subject_teacher(
    assignment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    cst = db.query(ClassSubjectTeacher).filter(ClassSubjectTeacher.id == assignment_id).first()
    if not cst:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Verify the teacher owns this assignment or is admin
    teacher = db.query(Teacher).filter(Teacher.id == cst.teacher_id).first()
    if user.role not in ["admin", "school"] and (not teacher or user.id != teacher.user_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    
    cst.is_active = False
    db.commit()
    return {"message": "Assignment deactivated"}

@router.put("/class-subject-teacher/{assignment_id}/deactivate")
def deactivate_subject_teacher(
    assignment_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Mark a class-subject assignment as inactive"""
    cst = db.query(ClassSubjectTeacher).filter(ClassSubjectTeacher.id == assignment_id).first()
    if not cst:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    # Verify authorization
    teacher = db.query(Teacher).filter(Teacher.id == cst.teacher_id).first()
    if user.role not in ["admin", "school"] and (not teacher or user.id != teacher.user_id):
        raise HTTPException(status_code=403, detail="Not authorized")
    
    cst.is_active = False
    db.commit()
    return {"message": "Assignment deactivated", "id": assignment_id}

@router.post("/class-subject-teacher")
def assign_subject_teacher(
    class_id: int = Body(...),
    teacher_id: int = Body(...),
    subject: str = Body(...),
    is_primary: bool = Body(False),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Assign a teacher to a subject in a class, and sync the timetable."""
    try:
        # ── 1. Auth ──
        if user.role not in ("admin", "school") and user.id != teacher_id:
            raise HTTPException(status_code=403, detail="Not authorized")

        # ── 2. Resolve teacher ──
        teacher = db.query(Teacher).filter(Teacher.user_id == teacher_id).first()
        if not teacher:
            raise HTTPException(status_code=404, detail="Teacher not found")

        teacher_user = db.query(User).filter(User.id == teacher.user_id).first()

        # ── 3. Resolve class ──
        class_obj = db.query(Class).filter(Class.id == class_id).first()
        if not class_obj:
            raise HTTPException(status_code=404, detail="Class not found")

        # ── 4. Upsert the ClassSubjectTeacher row ──
        existing = (
            db.query(ClassSubjectTeacher)
            .filter(
                ClassSubjectTeacher.class_id == class_id,
                ClassSubjectTeacher.subject == subject,
            )
            .first()
        )

        if existing:
            existing.teacher_id = teacher.id
            existing.is_primary = is_primary
            existing.is_active = True
            action = "Updated"
        else:
            db.add(ClassSubjectTeacher(
                class_id=class_id,
                teacher_id=teacher.id,
                subject=subject,
                is_primary=is_primary,
                is_active=True,
            ))
            action = "Assigned"

        updated_lessons = (
            db.query(Timetable)
            .filter(
                Timetable.school_id == class_obj.school_id,
                Timetable.class_name == class_obj.name,
                Timetable.subject == subject,
                Timetable.is_break == False,
            )
            .update(
                {Timetable.teacher_id: teacher.user_id},
                synchronize_session=False,
            )
        )

        db.commit()

        return {
            "message": (
                f"{action} {subject} teacher for {class_obj.name}"
                f" — updated {updated_lessons} timetable lesson(s)"
            ),
            "class_id": class_id,
            "class_name": class_obj.name,
            "teacher_id": teacher.id,
            "teacher_user_id": teacher.user_id,
            "teacher_name": teacher_user.full_name if teacher_user else "Unknown",
            "subject": subject,
            "is_primary": is_primary,
            "timetable_lessons_updated": updated_lessons,
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"❌ Error in assign_subject_teacher: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/class-teachers/{student_id}")
def get_student_teachers(
    student_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all teachers for a student (class teacher + subject teachers)"""
    
    try:
        # Validate access
        if user.role != "admin" and user.id != student_id:
            parent = db.query(Parent).filter(Parent.user_id == user.id).first()
            if not parent or parent.student_id != student_id:
                raise HTTPException(
                    status_code=403,
                    detail="You don't have access to this student's teachers"
                )
        
        # Get student's class
        student = db.query(Student).filter(Student.user_id == student_id).first()
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")
        
        student_user = db.query(User).filter(User.id == student_id).first()
        
        # Get student's class
        class_obj = None
        if student.class_id:
            class_obj = db.query(Class).filter(Class.id == student.class_id).first()
        
        if not class_obj:
            return {
                "student": {
                    "id": student_id,
                    "name": student_user.full_name if student_user else "Unknown"
                },
                "class": None,
                "class_teacher": None,
                "subject_teachers": []
            }
        
        # Get class teacher
        class_teacher = None
        if class_obj.class_teacher_id:
            teacher = db.query(Teacher).filter(Teacher.id == class_obj.class_teacher_id).first()
            if teacher:
                teacher_user_obj = db.query(User).filter(User.id == teacher.user_id).first()
                class_teacher = {
                    "id": teacher.id,
                    "name": teacher_user_obj.full_name if teacher_user_obj else "Unknown",
                    "role": "Class Teacher",
                    "subject": "Class Teacher",
                    "email": teacher_user_obj.email if teacher_user_obj else None,
                    "phone": teacher_user_obj.phone if teacher_user_obj else None
                }
        
        # Get subject teachers
        subject_teachers = []
        assignments = db.query(ClassSubjectTeacher).filter(
            ClassSubjectTeacher.class_id == class_obj.id
        ).all()
        
        for assignment in assignments:
            teacher = db.query(Teacher).filter(Teacher.id == assignment.teacher_id).first()
            if teacher:
                teacher_user_obj = db.query(User).filter(User.id == teacher.user_id).first()
                subject_teachers.append({
                    "id": teacher.id,
                    "name": teacher_user_obj.full_name if teacher_user_obj else "Unknown",
                    "role": "Subject Teacher",
                    "subject": assignment.subject,
                    "is_primary": assignment.is_primary,
                    "email": teacher_user_obj.email if teacher_user_obj else None,
                    "phone": teacher_user_obj.phone if teacher_user_obj else None
                })
        
        return {
            "student": {
                "id": student_id,
                "name": student_user.full_name if student_user else "Unknown"
            },
            "class": {
                "id": class_obj.id,
                "name": class_obj.name,
                "room": class_obj.room,
                "academic_year": class_obj.academic_year
            },
            "class_teacher": class_teacher,
            "subject_teachers": subject_teachers
        }
    except Exception as e:
        print(f"❌ Error in get_student_teachers: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/classes")
def get_all_classes(
    school_id: Optional[int] = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all classes (optionally filtered by school)"""
    
    if user.role not in ["admin", "school", "teacher"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    query = db.query(Class)
    
    if school_id:
        query = query.filter(Class.school_id == school_id)
    elif user.role == "school":
        school = db.query(School).filter(School.user_id == user.id).first()
        if school:
            query = query.filter(Class.school_id == school.id)
    
    classes = query.all()
    
    result = []
    for cls in classes:
        # Count students in this class
        student_count = db.query(Student).filter(Student.class_id == cls.id).count()
        
        # Get class teacher name
        teacher_name = None
        if cls.class_teacher_id:
            teacher = db.query(Teacher).filter(Teacher.id == cls.class_teacher_id).first()
            if teacher:
                teacher_user = db.query(User).filter(User.id == teacher.user_id).first()
                teacher_name = teacher_user.full_name if teacher_user else None
        
        result.append({
            "id": cls.id,
            "name": cls.name,
            "room": cls.room,
            "capacity": cls.capacity,
            "student_count": student_count,
            "class_teacher_id": cls.class_teacher_id,
            "class_teacher_name": teacher_name,
            "academic_year": cls.academic_year,
            "created_at": str(cls.created_at)
        })
    
    return {"data": result}
    
@router.get("/teacher-students/{teacher_id}")
def get_teacher_students(
    teacher_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get all students for a teacher"""
    
    if user.role != "admin" and user.id != teacher_id:
        raise HTTPException(
            status_code=403,
            detail="You can only access your own students"
        )
    
    teacher = db.query(Teacher).filter(Teacher.user_id == teacher_id).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found") 
        
    students = db.query(Student, User).join(User, Student.user_id == User.id)
    
    class_ids = db.query(Class.id).filter(Class.class_teacher_id == teacher.id).subquery()
    students_from_class = students.filter(Student.class_id.in_(class_ids))
    
    subject_class_ids = db.query(ClassSubjectTeacher.class_id).filter(
        ClassSubjectTeacher.teacher_id == teacher.id,
        ClassSubjectTeacher.is_active == True
    ).subquery()
    students_from_subject = students.filter(Student.class_id.in_(subject_class_ids))
    
    all_students = students_from_class.union(students_from_subject).all()
    
    result = []
    for student, student_user in all_students:
        performances = db.query(StudentPerformance).filter(
            StudentPerformance.student_id == student.user_id
        ).all()
        avg_score = round(sum(p.score for p in performances) / len(performances), 1) if performances else 0
        
        result.append({
            "id": student_user.id,
            "name": student_user.full_name,
            "email": student_user.email,
            "phone": student_user.phone,
            "class": student.class_name,
            "admission_number": student.admission_number,
            "avg_score": avg_score,
            "profile_picture": student.profile_picture,
            "gender": student.gender
        })
    
    return {"data": result, "total": len(result)}
    
# ========== TEACHER ATTENDANCE (for marking) ==========
@router.get("/teacher-attendance/history")
def attendance_history(
    class_id: Optional[int] = Query(None),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Day-by-day attendance summary for a class within a date range."""
    if user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers")

    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")

    # Auth: teacher must be assigned to the class (subject or class-teacher)
    my_class_ids = set(
        row[0] for row in db.query(ClassSubjectTeacher.class_id).filter(
            ClassSubjectTeacher.teacher_id == teacher.id,
            ClassSubjectTeacher.is_active == True,
        ).all()
    )
    managed = db.query(Class).filter(Class.class_teacher_id == teacher.id).first()
    if managed:
        my_class_ids.add(managed.id)

    if class_id and class_id not in my_class_ids:
        raise HTTPException(status_code=403, detail="Not your class")
    if not class_id:
        raise HTTPException(status_code=400, detail="class_id required")

    # Date range
    today = datetime.utcnow().date()
    to_d = datetime.strptime(to_date, "%Y-%m-%d").date() if to_date else today
    from_d = (
        datetime.strptime(from_date, "%Y-%m-%d").date()
        if from_date else to_d - timedelta(days=7)
    )

    # Total students in the class
    total_students = (
        db.query(func.count(Student.id))
        .filter(Student.class_id == class_id)
        .scalar()
    ) or 0

    # Attendance rows in range, grouped by date + status
    rows = (
        db.query(
            func.date(StudentAttendance.date).label("d"),
            StudentAttendance.status,
            func.count(StudentAttendance.id).label("c"),
        )
        .join(Student, Student.user_id == StudentAttendance.student_id)
        .filter(
            Student.class_id == class_id,
            func.date(StudentAttendance.date) >= from_d,
            func.date(StudentAttendance.date) <= to_d,
        )
        .group_by(func.date(StudentAttendance.date), StudentAttendance.status)
        .all()
    )

    by_date: dict[str, dict[str, int]] = {}
    for d, status, c in rows:
        d_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
        by_date.setdefault(d_str, {"present": 0, "absent": 0, "late": 0})
        if status in by_date[d_str]:
            by_date[d_str][status] = int(c)

    # Build day-by-day list, one entry per calendar day in range
    history = []
    cur = from_d
    while cur <= to_d:
        d_str = cur.isoformat()
        counts = by_date.get(d_str, {"present": 0, "absent": 0, "late": 0})
        present = counts["present"]
        absent = counts["absent"]
        late = counts["late"]
        marked = present + absent + late
        rate = round((present + late) / total_students * 100) if total_students else 0

        # only include days that had any marking
        if marked > 0:
            history.append({
                "date": d_str,
                "total": total_students,
                "present": present,
                "absent": absent,
                "late": late,
                "marked": marked,
                "attendanceRate": rate,
            })
        cur += timedelta(days=1)

    history.sort(key=lambda x: x["date"], reverse=True)
    return {"data": history, "total": len(history), "class_id": class_id}
    
@router.get("/teacher-attendance/reports")
def attendance_reports(
    class_id: int = Query(...),
    mode: str = Query("weekly"),         # 'weekly' | 'monthly' | 'students'
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Attendance reports for a class.
    mode:
      - weekly   → last 7 days, day-by-day breakdown
      - monthly  → last 30 days summary
      - students → per-student attendance rates
    """
    if user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers")

    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")

    # ── Auth: teacher must own the class (subject teacher or class teacher)
    my_class_ids = set(
        row[0] for row in db.query(ClassSubjectTeacher.class_id).filter(
            ClassSubjectTeacher.teacher_id == teacher.id,
            ClassSubjectTeacher.is_active == True,
        ).all()
    )
    managed = db.query(Class).filter(Class.class_teacher_id == teacher.id).first()
    if managed:
        my_class_ids.add(managed.id)

    if class_id not in my_class_ids:
        raise HTTPException(status_code=403, detail="Not your class")

    total_students = (
        db.query(func.count(Student.id))
        .filter(Student.class_id == class_id)
        .scalar()
    ) or 0

    # ════════════════════════════════════════════════════════════
    # WEEKLY — last 7 days, day by day
    # ════════════════════════════════════════════════════════════
    if mode == "weekly":
        today = datetime.utcnow().date()
        from_d = today - timedelta(days=6)

        rows = (
            db.query(
                func.date(StudentAttendance.date).label("d"),
                StudentAttendance.status,
                func.count(StudentAttendance.id).label("c"),
            )
            .join(Student, Student.user_id == StudentAttendance.student_id)
            .filter(
                Student.class_id == class_id,
                func.date(StudentAttendance.date) >= from_d,
                func.date(StudentAttendance.date) <= today,
            )
            .group_by(func.date(StudentAttendance.date), StudentAttendance.status)
            .all()
        )

        by_date: dict[str, dict[str, int]] = {}
        for d, status, c in rows:
            d_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
            by_date.setdefault(d_str, {"present": 0, "absent": 0, "late": 0})
            if status in by_date[d_str]:
                by_date[d_str][status] = int(c)

        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        daily = []
        for i in range(7):
            day = from_d + timedelta(days=i)
            d_str = day.isoformat()
            c = by_date.get(d_str, {"present": 0, "absent": 0, "late": 0})
            present = c["present"]
            late = c["late"]
            rate = round((present + late) / total_students * 100) if total_students else 0
            daily.append({
                "day": day_names[day.weekday()],
                "date": d_str,
                "rate": rate,
                "present": present,
                "absent": c["absent"],
                "late": late,
                "total": total_students,
            })

        marked = [d for d in daily if d["present"] + d["absent"] + d["late"] > 0]
        avg = round(sum(d["rate"] for d in marked) / len(marked)) if marked else 0
        best = max(marked, key=lambda d: d["rate"]) if marked else None
        worst = min(marked, key=lambda d: d["rate"]) if marked else None

        return {
            "data": {
                "mode": "weekly",
                "weekStart": from_d.isoformat(),
                "weekEnd": today.isoformat(),
                "totalDays": len(marked),
                "avgAttendance": avg,
                "bestDay": {"day": best["day"], "rate": best["rate"]} if best else None,
                "worstDay": {"day": worst["day"], "rate": worst["rate"]} if worst else None,
                "trend": "up" if avg >= 70 else "down",
                "improvement": f"{avg - 65:+d}%",
                "dailyStats": daily,
            }
        }

    # ════════════════════════════════════════════════════════════
    # MONTHLY — last 30 days summary
    # ════════════════════════════════════════════════════════════
    if mode == "monthly":
        today = datetime.utcnow().date()
        from_d = today - timedelta(days=29)

        rows = (
            db.query(
                func.date(StudentAttendance.date).label("d"),
                StudentAttendance.status,
                func.count(StudentAttendance.id).label("c"),
            )
            .join(Student, Student.user_id == StudentAttendance.student_id)
            .filter(
                Student.class_id == class_id,
                func.date(StudentAttendance.date) >= from_d,
                func.date(StudentAttendance.date) <= today,
            )
            .group_by(func.date(StudentAttendance.date), StudentAttendance.status)
            .all()
        )

        by_date: dict[str, dict[str, int]] = {}
        for d, status, c in rows:
            d_str = d.isoformat() if hasattr(d, "isoformat") else str(d)
            by_date.setdefault(d_str, {"present": 0, "absent": 0, "late": 0})
            if status in by_date[d_str]:
                by_date[d_str][status] = int(c)

        daily_rates = []
        for d_str, c in by_date.items():
            present = c["present"] + c["late"]
            if total_students:
                daily_rates.append((d_str, round(present / total_students * 100)))

        avg = round(sum(r for _, r in daily_rates) / len(daily_rates)) if daily_rates else 0
        highest = max(daily_rates, key=lambda x: x[1]) if daily_rates else (None, 0)
        lowest = min(daily_rates, key=lambda x: x[1]) if daily_rates else (None, 0)

        return {
            "data": {
                "mode": "monthly",
                "monthStart": from_d.isoformat(),
                "monthEnd": today.isoformat(),
                "totalDays": len(daily_rates),
                "avgAttendance": avg,
                "highest": {"day": highest[0], "rate": highest[1]},
                "lowest": {"day": lowest[0], "rate": lowest[1]},
                "improvement": f"{avg - 65:+d}%",
            }
        }

    # ════════════════════════════════════════════════════════════
    # STUDENTS — per-student attendance rate
    # ════════════════════════════════════════════════════════════
    if mode == "students":
        today = datetime.utcnow().date()
        from_d = today - timedelta(days=30)

        rows = (
            db.query(
                Student.user_id.label("sid"),
                User.full_name.label("name"),
                StudentAttendance.status,
                func.count(StudentAttendance.id).label("c"),
            )
            .join(User, User.id == Student.user_id)
            .outerjoin(
                StudentAttendance,
                (StudentAttendance.student_id == Student.user_id) &
                (func.date(StudentAttendance.date) >= from_d),
            )
            .filter(Student.class_id == class_id)
            .group_by(Student.user_id, User.full_name, StudentAttendance.status)
            .all()
        )

        totals: dict[int, dict] = {}
        for sid, name, status, c in rows:
            t = totals.setdefault(sid, {
                "id": sid, "name": name,
                "present": 0, "absent": 0, "late": 0,
            })
            if status in ("present", "absent", "late"):
                t[status] = int(c)

        for t in totals.values():
            denom = t["present"] + t["absent"] + t["late"]
            t["rate"] = round((t["present"] + t["late"]) / denom * 100) if denom else 0

        ordered = sorted(totals.values(), key=lambda x: x["rate"], reverse=True)

        top = [
            {"name": s["name"], "attendance": s["rate"]}
            for s in ordered if s["rate"] >= 80
        ][:5]
        weak = [
            {"name": s["name"], "attendance": s["rate"]}
            for s in ordered if s["rate"] < 70
        ][:5]

        return {
            "data": {
                "mode": "students",
                "topPerformers": top,
                "needsAttention": weak,
                "all": [{"name": s["name"], "attendance": s["rate"]} for s in ordered],
            }
        }

    # ════════════════════════════════════════════════════════════
    # Unknown mode
    # ════════════════════════════════════════════════════════════
    raise HTTPException(
        status_code=400,
        detail=f"Unknown mode '{mode}'. Use one of: weekly, monthly, students",
    )
    
@router.get("/teacher-attendance/students")
def get_teacher_attendance_students(
    class_id: Optional[int] = Query(None),
    date: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get students for attendance marking - OPTIMIZED SINGLE QUERY"""
    if user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can access")
    
    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    
    # Get teacher's class IDs - single query
    cst_class_ids = [
        row[0] for row in db.query(ClassSubjectTeacher.class_id).filter(
            ClassSubjectTeacher.teacher_id == teacher.id,
            ClassSubjectTeacher.is_active == True
        ).all()
    ]
    
    if not cst_class_ids:
        raise HTTPException(status_code=404, detail="No subject/classes assigned")
    
    attendance_date = date or datetime.utcnow().strftime("%Y-%m-%d")
    
    # SINGLE QUERY with LEFT JOIN - Fastest approach
    query = db.query(
        Student.user_id.label('student_id'),
        User.full_name.label('name'),
        Student.class_name.label('class_name'),
        Student.class_id.label('class_id'),
        func.coalesce(StudentAttendance.status, 'unmarked').label('status'),
        func.coalesce(
            func.to_char(StudentAttendance.created_at, 'HH24:MI'),
            None
        ).label('check_in')
    ).join(
        User, Student.user_id == User.id
    ).outerjoin(
        StudentAttendance,
        (StudentAttendance.student_id == Student.user_id) &
        (func.date(StudentAttendance.date) == attendance_date)
    ).filter(
        Student.class_id.in_(cst_class_ids)
    )
    
    # Optional class filter
    if class_id:
        query = query.filter(Student.class_id == class_id)
    
    # Execute single query
    results = query.all()
    
    # Convert to list of dicts
    students = [
        {
            "id": row.student_id,
            "name": row.name,
            "class": row.class_name,
            "class_id": row.class_id,
            "status": row.status,
            "check_in": row.check_in if row.status in ["present", "late"] else None
        }
        for row in results
    ]
    
    return {"data": students, "total": len(students)}


@router.post("/teacher-attendance/mark")
def mark_attendance(
    records: List[dict] = Body(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not records:
        raise HTTPException(status_code=404, detail="No class given")
    """Save attendance records for multiple students"""
    if user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers can mark attendance")
    
    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    
    # Get teacher's assigned class IDs
    my_class_ids = set(
        cst.class_id for cst in db.query(ClassSubjectTeacher.class_id).filter(
            ClassSubjectTeacher.teacher_id == teacher.id
        ).all()
    )
    
    if not my_class_ids:
        raise HTTPException(status_code=403, detail="You have no classes assigned")
    
    marked = 0
    for record in records:
        student_id = record.get("student_id")
        status = record.get("status", "present")
        date_str = record.get("date", datetime.utcnow().strftime("%Y-%m-%d"))
        
        # Verify this student is in teacher's class
        student = db.query(Student).filter(
            Student.user_id == student_id,
            Student.class_id.in_(my_class_ids)  # Must be in teacher's class
        ).first()
        
        if not student:
            continue  # Skip students not in this teacher's classes
        
        attendance_date = datetime.strptime(date_str, "%Y-%m-%d") if isinstance(date_str, str) else date_str
        
        # Upsert
        existing = db.query(StudentAttendance).filter(
            StudentAttendance.student_id == student_id,
            func.date(StudentAttendance.date) == func.date(attendance_date)
        ).first()
        
        if existing:
            existing.status = status
        else:
            db.add(StudentAttendance(
                student_id=student_id,
                date=attendance_date,
                status=status
            ))
        marked += 1
    
    db.commit()
    return {"message": f"Attendance marked for {marked} students", "count": marked}


@router.get("/teacher-attendance/classes")
def get_teacher_classes_for_attendance(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Get classes this teacher can take attendance for"""
    if user.role != "teacher":
        raise HTTPException(status_code=403, detail="Only teachers")
    
    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
    
    csts = db.query(ClassSubjectTeacher).filter(
        ClassSubjectTeacher.teacher_id == teacher.id,
        ClassSubjectTeacher.is_active == True
    ).all()
    
    classes = []
    seen = set()
    for cst in csts:
        if cst.class_id not in seen:
            seen.add(cst.class_id)
            cls = db.query(Class).filter(Class.id == cst.class_id).first()
            if cls:
                classes.append({"id": cls.id, "name": cls.name})
    
    return {"data": classes}

@router.get("/teacher/results")
async def teacher_results(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
    class_id: int = Query(..., description="Class ID"),
    term: str = Query(..., description="Term"),
    subject: Optional[str] = Query(None, description="Subject (optional)"),
    include_total: bool = Query(True, description="Include total score"),
):
    if user.role != "teacher":
        raise HTTPException(status_code=403, detail="You must be a teacher")

    # ── Teacher profile ──
    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
    if not teacher:
        return {
            "status": "success",
            "class_id": class_id,
            "term": term,
            "subject": None,
            "student_count": 0,
            "data": [],
            "message": "Teacher profile not found",
        }

    # ── Subject resolution ──
    # If the client sent a subject, verify the teacher teaches it in this class.
    # Otherwise pick the teacher's first active assignment for this class.
    if subject:
        assignment = (
            db.query(ClassSubjectTeacher)
            .filter(
                ClassSubjectTeacher.class_id == class_id,
                ClassSubjectTeacher.teacher_id == teacher.id,
                ClassSubjectTeacher.subject == subject,
                ClassSubjectTeacher.is_active == True,
            )
            .first()
        )
        if not assignment:
            raise HTTPException(
                status_code=403,
                detail=f"You do not teach {subject} in this class",
            )
        subject_name = assignment.subject
    else:
        assignments = (
            db.query(ClassSubjectTeacher)
            .filter(
                ClassSubjectTeacher.class_id == class_id,
                ClassSubjectTeacher.teacher_id == teacher.id,
                ClassSubjectTeacher.is_active == True,
            )
            .all()
        )
        if not assignments:
            raise HTTPException(
                status_code=403,
                detail="You are not assigned to this class",
            )
        subject_name = assignments[0].subject

    # ── Fetch scores ──
    scores = (
        db.query(StudentPerformance)
        .filter(
            StudentPerformance.class_id == class_id,
            StudentPerformance.subject == subject_name,
            StudentPerformance.term == term,
        )
        .order_by(StudentPerformance.student_id)
        .all()
    )

    # Empty result is a valid state, not an error.
    if not scores:
        return {
            "status": "success",
            "class_id": class_id,
            "term": term,
            "subject": subject_name,
            "student_count": 0,
            "data": [],
        }

    # ── Student info ──
    student_ids = list({s.student_id for s in scores})

    students = (
        db.query(Student, User)
        .join(User, User.id == Student.user_id)
        .filter(Student.user_id.in_(student_ids))
        .all()
    )

    student_info = {}
    for student, user_row in students:
        student_info[student.user_id] = {
            "student_name": user_row.full_name,
            "admission_number": getattr(student, "admission_number", None),
        }

    # ── Group by student ──
    student_data: dict[int, dict] = {}
    for score in scores:
        if score.student_id not in student_data:
            info = student_info.get(score.student_id, {})
            student_data[score.student_id] = {
                "student_id": score.student_id,
                "student_name": info.get("student_name", "Unknown"),
                "admission_number": info.get("admission_number"),
                "scores": {
                    "Opener": 0,
                    "Midterm": 0,
                    "End Term": 0,
                },
            }
        # Map assessment name; keep unknown assessment labels out of the grid.
        key = score.assessment
        if key in student_data[score.student_id]["scores"]:
            student_data[score.student_id]["scores"][key] = score.score

    # ── Build results ──
    results = []
    for student_id, data in student_data.items():
        cat1 = data["scores"]["Opener"]
        cat2 = data["scores"]["Midterm"]
        end_term = data["scores"]["End Term"]
        total = cat1 + cat2 + end_term
        total_exams = 0
        if cat1 > 0:
            total_exams += 1
        if cat2 > 0:
            total_exams += 1
        if end_term > 0:
            total_exams += 1
        average = round(total / total_exams, 1) if total_exams > 0 else 0.0

        result = {
            "student_id": student_id,
            "student_name": data["student_name"],
            "admission_number": data["admission_number"],
            "adm_no": data["admission_number"],   # alias for Flutter
            "Opener": cat1,
            "Midterm": cat2,
            "End Term": end_term,
        }

        if include_total:
            result["total"] = total
            result["average"] = average

            if average >= 80:
                result["grade"] = "A"
            elif average >= 75:
                result["grade"] = "A-"
            elif average >= 70:
                result["grade"] = "B+"
            elif average >= 65:
                result["grade"] = "B"
            elif average >= 60:
                result["grade"] = "B-"
            elif average >= 55:
                result["grade"] = "C+"
            elif average >= 50:
                result["grade"] = "C"
            elif average >= 45:
                result["grade"] = "C-"
            elif average >= 40:
                result["grade"] = "D+"
            elif average >= 35:
                result["grade"] = "D"
            elif average >= 30:
                result["grade"] = "D-"
            else:
                result["grade"] = "E"

        results.append(result)

    if include_total:
        results.sort(key=lambda x: x.get("total", 0), reverse=True)

    return {
        "status": "success",
        "class_id": class_id,
        "term": term,
        "subject": subject_name,
        "student_count": len(results),
        "data": results,
    }
    
@router.put("/class-subject-teacher/{assignment_id}/deactivate")
def deactivate_subject_teacher(assignment_id: int, db: Session = Depends(get_db)):
    cst = db.query(ClassSubjectTeacher).filter(ClassSubjectTeacher.id == assignment_id).first()
    if cst:
        cst.is_active = False
        db.commit()
        return {"message": "Assignment deactivated"}
    raise HTTPException(status_code=404, detail="Not found")
    
@router.get("/books/recommended/{student_id}")
def get_recommended_books(
    student_id: int,
    db: Session = Depends(get_db),
    limit: int = Query(5, ge=1, le=20)
):
    """Get books recommended based on student's weak subjects"""
    
    # Find student's weak subjects (scores below 60)
    performances = db.query(StudentPerformance).filter(
        StudentPerformance.student_id == student_id
    ).all()
    
    weak_subjects = set()
    subject_scores = {}
    for p in performances:
        if p.subject not in subject_scores:
            subject_scores[p.subject] = []
        subject_scores[p.subject].append(p.score)
    
    for subject, scores in subject_scores.items():
        avg = sum(scores) / len(scores)
        if avg < 60:
            weak_subjects.add(subject.lower())
    
    if not weak_subjects:
        # Return top rated books if no weak subjects
        books = db.query(Book).order_by(desc(Book.rating)).limit(limit).all()
    else:
        # Score books by relevance to weak subjects
        all_books = db.query(Book).all()
        scored = []
        for b in all_books:
            score = 0
            title = (b.title or '').lower()
            cat = (b.category or '').lower()
            desc = (b.description or '').lower()
            for ws in weak_subjects:
                if ws in title: score += 3
                if ws in cat: score += 2
                if ws in desc: score += 1
            if score > 0:
                scored.append((b, score))
        
        scored.sort(key=lambda x: x[1], reverse=True)
        books = [b for b, _ in scored[:limit]]
    
    result = []
    for b in books:
        result.append({
            "id": b.id, "title": b.title, "author": b.author,
            "description": b.description, "category": b.category,
            "price": b.price, "isFree": b.is_free,
            "rating": b.rating, "views": b.views, "downloads": b.downloads,
            "likes": b.likes, "publishedDate": b.published_date,
        })
    
    return {
        "data": result,
        "weak_subjects": list(weak_subjects),
        "total": len(result)
    }
    
@router.post("/upload-picture/books/{book_id}")
async def upload_book_cover(
    book_id: int,
    file: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book not found")
    
    upload_dir = "uploads/books"
    os.makedirs(upload_dir, exist_ok=True)
    
    contents = await file.read()
    ext = file.filename.split('.')[-1] if file.filename else 'png'
    file_path = f"{upload_dir}/book_{book_id}.{ext}"
    
    with open(file_path, "wb") as f:
        f.write(contents)
    
    book.image_url = f"/{file_path}"
    db.commit()
    
    return {"message": "Cover uploaded", "url": f"/{file_path}"}

@router.post("/create-revision-materials")
async def create_revision_material(
    subject: str = Form(...),
    title: str = Form(...),
    description: Optional[str] = Form(None),
    type: str = Form(...),
    school_id: Optional[int] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """Create a new revision material"""
    
    # Handle file upload
    file_url = None
    if file:
        # Create upload directory
        upload_dir = "uploads/revision_materials"
        os.makedirs(upload_dir, exist_ok=True)
        
        # Generate unique filename
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{user.id}_{timestamp}_{file.filename}"
        file_path = os.path.join(upload_dir, filename)
        
        # Save file
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        
        file_url = f"/{file_path}"
    
    # Create material
    new_material = RevisionMaterial(
        subject=subject,
        title=title,
        description=description,
        type=type,
        file_url=file_url,
        school_id=school_id,
        created_by=user.id,
        created_at=datetime.now()
    )
    
    db.add(new_material)
    db.commit()
    db.refresh(new_material)
    
    return {
        "message": "Revision material created successfully",
        "data": {
            "id": new_material.id,
            "subject": new_material.subject,
            "title": new_material.title,
            "description": new_material.description,
            "type": new_material.type,
            "file_url": new_material.file_url,
            "school_id": new_material.school_id,
            "created_by": new_material.created_by,
            "created_at": new_material.created_at.isoformat() if new_material.created_at else None,
        }
    }
    
@router.put("/update-revision-materials/{material_id}")
async def update_revision_material(
    material_id: int,
    subject: str = Form(...),
    title: str = Form(...),
    description: Optional[str] = Form(None),
    type: str = Form(...),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """Update a revision material"""
    material = db.query(RevisionMaterial).filter(
        RevisionMaterial.id == material_id,
        RevisionMaterial.created_by == user.id
    ).first()
    
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")
    
    material.subject = subject
    material.title = title
    material.description = description
    material.type = type
    
    if file:
        upload_dir = "uploads/revision_materials"
        os.makedirs(upload_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{user.id}_{timestamp}_{file.filename}"
        file_path = os.path.join(upload_dir, filename)
        
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        
        if material.file_url:
            old_file = material.file_url.lstrip("/")
            if os.path.exists(old_file):
                os.remove(old_file)
        
        material.file_url = f"/{file_path}"
    
    db.commit()
    db.refresh(material)
    
    return {"message": "Revision material updated successfully"}
    
@router.delete("/delete-revision-materials/{material_id}")
async def delete_revision_material(
    material_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    """Delete a revision material"""
    material = db.query(RevisionMaterial).filter(
        RevisionMaterial.id == material_id,
        RevisionMaterial.created_by == user.id
    ).first()
    
    if not material:
        raise HTTPException(status_code=404, detail="Material not found")
    
    if material.file_url:
        file_path = material.file_url.lstrip("/")
        if os.path.exists(file_path):
            os.remove(file_path)
    
    db.delete(material)
    db.commit()
    
    return {"message": "Revision material deleted successfully"}


@router.get("/revision-materials")
def get_revision_materials(
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Get all revision materials"""
    materials = db.query(RevisionMaterial).order_by(desc(RevisionMaterial.created_at)).all()
    return {
        "data": [{
            "id": m.id,
            "subject": m.subject,
            "title": m.title,
            "description": m.description,
            "type": m.type,
            "file_url": m.file_url,
            "created_by": m.created_by,
        } for m in materials]
    }

@router.post("/revision-materials")
async def create_revision_material(
    subject: str = Form(...),
    title: str = Form(...),
    description: Optional[str] = Form(None),
    type: str = Form(...),
    duration: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Create revision material"""
    file_url = None
    if file:
        upload_dir = "uploads/revision_materials"
        os.makedirs(upload_dir, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{user.id}_{timestamp}_{file.filename}"
        file_path = os.path.join(upload_dir, filename)
        with open(file_path, "wb") as f:
            content = await file.read()
            f.write(content)
        file_url = f"/{file_path}"
    
    new_material = RevisionMaterial(
        subject=subject,
        title=title,
        description=description,
        type=type,
        file_url=file_url,
        created_by=user.id,
    )
    
    db.add(new_material)
    db.commit()
    db.refresh(new_material)
    
    return {"message": "Created successfully", "data": {"id": new_material.id}}

@router.post("/parents/{parent_id}/link-child")
async def link_child_to_parent(
    parent_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    try:
        body = await request.json()
        student_id = body.get('student_id')
        school_id = body.get('school_id')
        class_id = body.get('class_id')
        
        if student_id is None or school_id is None or class_id is None:
            missing = []
            if student_id is None: missing.append("student_id")
            if school_id is None: missing.append("school_id")
            if class_id is None: missing.append("class_id")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Missing required fields: {', '.join(missing)}"
            )
        
        try:
            student_id = int(student_id)
            school_id = int(school_id)
            class_id = int(class_id)
        except (ValueError, TypeError) as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid ID format: {str(e)}"
            )
        
        # Get parent by user_id (from URL)
        parent = db.query(Parent).filter(Parent.user_id == parent_id).first()
        if not parent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Parent not found with user_id={parent_id}"
            )
        
        # ✅ FIX: student_id from body is Student.id (database primary key)
        # Use Student.id, not Student.user_id
        student = db.query(Student).filter(
            Student.id == student_id,
            Student.school_id == school_id
        ).first()
        
        if not student:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Student not found with id={student_id} and school_id={school_id}"
            )
        
        # Check for existing link using Student.id
        existing_link = db.query(ParentStudent).filter(
            and_(
                ParentStudent.parent_id == parent.id,
                ParentStudent.student_id == student.id
            )
        ).first()
        
        if existing_link:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This child is already linked to this parent"
            )
        
        # Create the link using Student.id
        parent_student = ParentStudent(
            parent_id=parent.id,
            student_id=student.id,
            relation_type="parent"
        )
        
        db.add(parent_student)
        db.commit()
        db.refresh(parent_student)
        
        # Get student user for name
        student_user = db.query(User).filter(User.id == student.user_id).first()
        student_name = student_user.full_name if student_user else f"Student #{student.id}"
        
        print(f"✅ Linked parent {parent.id} (user {parent_id}) to student {student.id} ({student_name})")
        
        return {
            "success": True,
            "message": f"Successfully linked {student_name}",
            "data": {
                "id": parent_student.id,
                "parent_id": parent_student.parent_id,
                "student_id": parent_student.student_id,
                "student_name": student_name
            }
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Error linking child: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to link child: {str(e)}"
        )
        
@router.delete("/parents/{parent_id}/unlink-child/{child_id}")
async def unlink_child_from_parent(
    parent_id: int,
    child_id: int,  # This might be user_id or student.id
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Unlink a student from a parent.
    URL: DELETE /api/v1/admin/parents/{parent_id}/unlink-child/{child_id}
    """
    print(f"📝 Unlink child request: parent_id={parent_id}, child_id={child_id}")
    
    # Find parent by user_id
    parent = db.query(Parent).filter(Parent.user_id == parent_id).first()
    if not parent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Parent not found"
        )
    
    # Try to find the student by id first, then by user_id
    student = db.query(Student).filter(Student.id == child_id).first()
    
    if not student:
        # Try finding by user_id
        student = db.query(Student).filter(Student.user_id == child_id).first()
    
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Student not found with id or user_id: {child_id}"
        )
    
    print(f"✅ Found student: id={student.id}, user_id={student.user_id}")
    
    # Now search for the link using student.id
    link = db.query(ParentStudent).filter(
        and_(
            ParentStudent.parent_id == parent.id,
            ParentStudent.student_id == student.id
        )
    ).first()
    
    if not link:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No link found between parent {parent.id} and student {student.id}"
        )
    
    try:
        db.delete(link)
        db.commit()
        print(f"✅ Successfully unlinked parent {parent.id} from student {student.id}")
        return {
            "success": True,
            "message": "Child unlinked successfully"
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to unlink child: {str(e)}"
        )

@router.get("/parents/{parent_id}/children")
def get_parent_children(
    parent_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Get all children linked to a parent"""
    parent_id = current_user.id
    
    parent = db.query(Parent).filter(Parent.user_id == parent_id).first()
    if not parent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Parent not found"
        )
    
    links = db.query(ParentStudent).filter(
        ParentStudent.parent_id == parent.id
    ).all()
    
    if not links:
        return {"children": [], "total": 0}
    
    student_ids = [link.student_id for link in links]
    
    students = db.query(Student, User).join(
        User, Student.user_id == User.id
    ).filter(Student.id.in_(student_ids)).all()
    
    children = []
    for student, user in students:
        school = db.query(School).filter(School.id == student.school_id).first()
        class_obj = db.query(Class).filter(Class.id == student.class_id).first()
        
        children.append({
            "id": user.id,              # User ID
            "student_id": student.id,   # Student DB ID - for unlinking
            "user_id": user.id,
            "name": user.full_name,
            "full_name": user.full_name,
            "student_name": user.full_name,
            "admission_number": student.admission_number,
            "class": student.class_name,
            "class_name": class_obj.name if class_obj else student.class_name,
            "school": student.school_name,
            "school_name": school.school_name if school else student.school_name,
            "email": user.email,
            "phone": user.phone,
            "profile_picture": student.profile_picture,
        })
    
    return {"children": children, "total": len(children)}
    

@router.get("/student-results/{student_id}", response_class=HTMLResponse)
def get_student_performance_report(
    student_id: int,
    term: str = Query(...),
    year: str = Query(""),
    db: Session = Depends(get_db)
):
    """Generate HTML performance report for a student"""
    if user.student_id != student_id:
      raise HTTPException(status_code=403, detail="Not Authorized")
    
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    student_user = db.query(User).filter(User.id == student.user_id).first()
    school = db.query(School).filter(School.id == student.school_id).first()
    
    # The term in DB is stored as "Term 1 2026", "Term 2 2026", etc.
    # Build the search term
    search_term = f"{term} {year}" if year and year.strip() else term
    
    query = db.query(StudentPerformance).filter(
        StudentPerformance.student_id == student.user_id,
        StudentPerformance.term == search_term
    )
    
    # If no results with combined term, try with just the term prefix
    performances = query.order_by(StudentPerformance.created_at.desc()).all()
    
    if not performances:
        # Try LIKE search
        query = db.query(StudentPerformance).filter(
            StudentPerformance.student_id == student.user_id,
            StudentPerformance.term.like(f"{term}%")
        )
        if year and year.strip():
            query = query.filter(StudentPerformance.term.like(f"%{year}%"))
        performances = query.order_by(StudentPerformance.created_at.desc()).all()
    
    total_score = sum(p.score for p in performances)
    count = len(performances)
    average = round(total_score / count, 1) if count > 0 else 0
    
    display_term = search_term
    
    # Build subjects HTML
    subjects_html = ""
    if performances:
        for p in performances:
            grade = "A" if p.score >= 80 else "B" if p.score >= 65 else "C" if p.score >= 50 else "D" if p.score >= 40 else "E"
            color = "green" if p.score >= 65 else "orange" if p.score >= 50 else "red"
            subjects_html += f"""
            <tr>
                <td>{p.subject}</td>
                <td>{p.score}%</td>
                <td style="color:{color}; font-weight:bold;">{grade}</td>
            </tr>
            """
    else:
        subjects_html = f'<tr><td colspan="3" style="text-align:center;padding:30px;">No results for {display_term}</td></tr>'
    
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Performance Report - {student_user.full_name}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Segoe UI', Arial, sans-serif; margin: 20px; color: #333; background: #fff; }}
        .header {{ text-align: center; border-bottom: 3px solid #1a237e; padding-bottom: 20px; margin-bottom: 30px; }}
        .school-name {{ font-size: 24px; font-weight: bold; color: #1a237e; }}
        .report-title {{ font-size: 20px; color: #555; margin: 10px 0; }}
        .info-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 30px; }}
        .info-item {{ padding: 12px; background: #f5f5f5; border-radius: 8px; border-left: 3px solid #1a237e; }}
        .info-label {{ font-weight: bold; font-size: 12px; color: #666; text-transform: uppercase; }}
        .info-value {{ font-size: 15px; margin-top: 4px; font-weight: 500; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th {{ background: #1a237e; color: white; padding: 12px; text-align: left; }}
        td {{ padding: 12px; border-bottom: 1px solid #ddd; }}
        tr:nth-child(even) {{ background: #f9f9f9; }}
        .summary {{ background: linear-gradient(135deg, #e8eaf6, #c5cae9); padding: 24px; border-radius: 12px; margin-top: 20px; text-align: center; }}
        .average {{ font-size: 48px; font-weight: bold; color: #1a237e; }}
    </style>
</head>
<body>
    <div class="header">
        <div class="school-name">{school.school_name if school else 'School Name'}</div>
        <div class="report-title">Student Results Form</div>
        <div>{display_term}</div>
    </div>
    <div class="info-grid">
        <div class="info-item"><div class="info-label">Student Name</div><div class="info-value">{student_user.full_name}</div></div>
        <div class="info-item"><div class="info-label">Admission No</div><div class="info-value">{student.admission_number or 'N/A'}</div></div>
        <div class="info-item"><div class="info-label">Class</div><div class="info-value">{student.class_name or 'N/A'}</div></div>
        <div class="info-item"><div class="info-label">Term</div><div class="info-value">{display_term}</div></div>
    </div>
    <table>
        <thead><tr><th>Subject</th><th>Score (%)</th><th>Grade</th></tr></thead>
        <tbody>{subjects_html}</tbody>
    </table>
    <div class="summary">
        <div style="font-size:14px;color:#666;">OVERALL AVERAGE</div>
        <div class="average">{average}%</div>
        <div style="margin-top:8px;">Total Subjects: {count}</div>
    </div>
</body>
</html>
    """
    return HTMLResponse(content=html)
    
@router.get("/student-performance-years/{student_id}")
def get_student_performance_years(
    student_id: int,
    db: Session = Depends(get_db)
):
    """Get available years from term field (e.g., 'Term 1 2026')"""
    
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    # Extract year from term field (e.g., "Term 1 2026" -> "2026")
    terms = db.query(StudentPerformance.term).filter(
        StudentPerformance.student_id == student.user_id
    ).distinct().all()
    
    years = set()
    for t in terms:
        term_str = t[0]
        # Extract year - last 4 characters
        parts = term_str.split()
        for part in parts:
            if part.isdigit() and len(part) == 4:
                years.add(part)
    
    year_list = sorted(list(years), reverse=True)
    if not year_list:
        year_list = [str(datetime.now().year)]
    
    return {"years": year_list, "student_id": student_id}
    


#========================================
#   Timetable reading
#========================================
@router.post("/timetable/analyze-upload-stream")
async def analyze_timetable_upload_stream(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    if current_user.role not in ['admin','school'] and current_user.school_id != school_id:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    GEMINI_MODELS = [
        "gemini-flash-latest",
        "gemini-2.5-flash-preview",
        "gemini-2.5-pro-preview",
        "gemini-2.0-flash-lite",
        "gemini-2.0-flash",
    ]
    
    async def event_generator():
        try:
            # Step 1: Receive file
            yield f"data: {json.dumps({'step': 1, 'message': '📤 Receiving file...', 'progress': 5, 'status': 'uploading'})}\n\n"
            await asyncio.sleep(0.2)
            
            contents = await file.read()
            file_ext = file.filename.split('.')[-1].lower()
            
            yield f"data: {json.dumps({'step': 1, 'message': f'🔍 Detected format: {file_ext.upper()}', 'progress': 10, 'status': 'processing'})}\n\n"
            await asyncio.sleep(0.2)
            
            # Route based on file type - FIX: Use async for instead of yield from
            if file_ext in ['jpg', 'jpeg', 'png', 'gif', 'bmp']:
                async for msg in process_image_timetable(contents, GEMINI_MODELS):
                    yield msg
            elif file_ext == 'xlsx':
                async for msg in process_xlsx_timetable(contents):
                    yield msg
            elif file_ext == 'docx':
                async for msg in process_docx_timetable(contents):
                    yield msg
            else:
                yield f"data: {json.dumps({'error': f'Unsupported file format: {file_ext}', 'done': True})}\n\n"
                
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"}
    )


# Helper: Process Image
async def process_image_timetable(contents: bytes, GEMINI_MODELS: list):
    """Extract timetable from image using Gemini"""
    try:
        image = Image.open(io.BytesIO(contents))
        yield f"data: {json.dumps({'step': 1, 'message': f'🖼️ Image loaded ({image.size[0]}x{image.size[1]}px)', 'progress': 15, 'status': 'processing'})}\n\n"
        
        client = genai.Client(api_key=os.getenv("API_KEY"))
        
        prompt = """
You are an expert document parser.

Extract this school timetable into valid JSON.

Rules:
- Detect all days (Monday through Friday).
- Detect all time slots (e.g., 8:00-8:40).
- Detect all classes (e.g., Form 1A, Form 2B).
- Detect all subjects (e.g., Mathematics, English).
- If subjects are in short form like maths/mao/math/geo/eng/kiswa return in full like mathematics, geography
- Detect teacher names if present.
- Capitalize first letter only others to be small
- Return ONLY valid JSON. No markdown. No explanation.

Output format:
{
  "school": "",
  "is_timetable": true,
  "confidence": 95,
  "entries": [
    {
      "day": "Monday",
      "time": "08:00-08:40",
      "subject": "Mathematics",
      "teacher": "Mr. Smith",
      "class": "Form 1A",
      "room": "Room 101"
    }
  ],
  "detected_subjects": ["Mathematics"],
  "detected_teachers": ["Mr. Smith"],
  "detected_classes": ["Form 1A"],
  "total_entries": 1
}

If NOT a timetable, return: {"is_timetable": false, "confidence": 0, "message": "Not a timetable image"}
"""
        
        last_error = None
        progress = 15
        total_models = len(GEMINI_MODELS)
        
        for idx, model in enumerate(GEMINI_MODELS):
            progress = 15 + (idx * (70 // total_models))
            
            yield f"data: {json.dumps({'step': 2, 'message': f'🔄 Model {idx+1}/{total_models}: {model}', 'progress': progress, 'status': 'trying_model', 'model': model})}\n\n"
            await asyncio.sleep(0.3)
            
            success = False
            for attempt in range(2):
                try:
                    yield f"data: {json.dumps({'step': 2, 'message': f'🤖 Calling {model}... (attempt {attempt+1}/2)', 'progress': progress + 2, 'status': 'calling_api'})}\n\n"
                    await asyncio.sleep(0.2)
                    
                    response = client.models.generate_content(
                        model=model,
                        contents=[image, prompt],
                    )
                    
                    yield f"data: {json.dumps({'step': 2, 'message': f'✅ {model} responded!', 'progress': progress + 5, 'status': 'model_success'})}\n\n"
                    await asyncio.sleep(0.2)
                    
                    text = response.text.strip().replace("```json", "").replace("```", "").strip()
                    data = json.loads(text)
                    
                    yield f"data: {json.dumps({'step': 4, 'message': f'✨ Complete! {data.get("total_entries", 0)} entries found', 'progress': 100, 'status': 'complete', 'done': True, 'data': data, 'model_used': model, 'source': 'image'})}\n\n"
                    success = True
                    return
                    
                except json.JSONDecodeError:
                    yield f"data: {json.dumps({'step': 2, 'message': f'❌ {model} returned bad JSON', 'progress': progress + 3, 'status': 'parse_error'})}\n\n"
                    break
                except Exception as e:
                    last_error = e
                    error_str = str(e)
                    
                    if "503" in error_str or "UNAVAILABLE" in error_str:
                        yield f"data: {json.dumps({'step': 2, 'message': f'⏳ {model} overloaded, retrying...', 'progress': progress + 1, 'status': 'overloaded'})}\n\n"
                        await asyncio.sleep(2)
                    elif "429" in error_str:
                        yield f"data: {json.dumps({'step': 2, 'message': f'⏳ Rate limited, retrying...', 'progress': progress + 1, 'status': 'rate_limited'})}\n\n"
                        await asyncio.sleep(3)
                    else:
                        break
            
            if success:
                return
            
            yield f"data: {json.dumps({'step': 2, 'message': f'⏭️ Switching to next model...', 'progress': progress + 6, 'status': 'switching'})}\n\n"
        
        yield f"data: {json.dumps({'error': f'All models failed: {str(last_error)}', 'done': True})}\n\n"
        
    except Exception as e:
        yield f"data: {json.dumps({'error': str(e), 'done': True})}\n\n"


# Helper: Process XLSX
async def process_xlsx_timetable(contents: bytes):
    """Extract timetable from Excel file"""
    try:
        import pandas as pd
        from io import BytesIO
        
        yield f"data: {json.dumps({'step': 2, 'message': '📊 Parsing Excel file...', 'progress': 20, 'status': 'parsing'})}\n\n"
        await asyncio.sleep(0.5)
        
        # Read Excel
        excel_file = BytesIO(contents)
        xl_file = pd.ExcelFile(excel_file)
        
        yield f"data: {json.dumps({'step': 2, 'message': f'📄 Found {len(xl_file.sheet_names)} sheets', 'progress': 30, 'status': 'parsing'})}\n\n"
        
        entries = []
        all_subjects = set()
        all_teachers = set()
        all_classes = set()
        
        # Parse each sheet (day)
        for sheet_name in xl_file.sheet_names:
            df = pd.read_excel(excel_file, sheet_name=sheet_name)
            
            yield f"data: {json.dumps({'step': 2, 'message': f'⏰ Processing {sheet_name}...', 'progress': 40, 'status': 'parsing'})}\n\n"
            
            day = sheet_name if sheet_name.lower() in ['monday', 'tuesday', 'wednesday', 'thursday', 'friday'] else None
            
            for idx, row in df.iterrows():
                time_slot = row.iloc[0] if pd.notna(row.iloc[0]) else None
                
                for class_idx, class_col in enumerate(df.columns[1:], 1):
                    subject = row.iloc[class_idx]
                    
                    if pd.notna(subject) and str(subject).strip() and subject.lower() not in ['break', 'lunch']:
                        entry = {
                            "day": day or sheet_name,
                            "time": str(time_slot),
                            "subject": str(subject).strip(),
                            "teacher": "",
                            "class": class_col,
                            "room": ""
                        }
                        entries.append(entry)
                        all_subjects.add(str(subject).strip())
                        all_classes.add(class_col)
        
        data = {
            "school": "",
            "is_timetable": True,
            "confidence": 85,
            "entries": entries,
            "detected_subjects": list(all_subjects),
            "detected_teachers": list(all_teachers),
            "detected_classes": list(all_classes),
            "total_entries": len(entries)
        }
        
        yield f"data: {json.dumps({'step': 4, 'message': f'✨ Complete! {len(entries)} entries extracted', 'progress': 100, 'status': 'complete', 'done': True, 'data': data, 'source': 'xlsx'})}\n\n"
        
    except Exception as e:
        yield f"data: {json.dumps({'error': f'Excel parsing error: {str(e)}', 'done': True})}\n\n"


# Helper: Process DOCX
_DAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}

def _is_day_name(text: str) -> bool:
    return text.strip().lower().rstrip(":") in _DAYS


def _is_break_row(text: str) -> bool:
    t = text.strip().lower()
    return any(k in t for k in ("break", "lunch", "assembly", "registration",
                                "games", "clubs", "remedial", "prep"))


def _clean_cell(text: str) -> str:
    # Normalise weird whitespace and strip the leading/trailing noise
    if not text:
        return ""
    # Replace non-breaking spaces, collapse whitespace within lines
    text = text.replace("\xa0", " ").replace("\u200b", "")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines).strip()


_SUBJECT_ABBR = {
    "math": "Mathematics", "maths": "Mathematics", "mao": "Mathematics",
    "eng": "English",
    "kis": "Kiswahili", "kisw": "Kiswahili", "kiswa": "Kiswahili",
    "bio": "Biology",
    "chem": "Chemistry",
    "phy": "Physics", "phys": "Physics",
    "geo": "Geography",
    "hist": "History",
    "cre": "C.R.E", "c.r.e": "C.R.E", "c r e": "C.R.E",
    "ire": "I.R.E",
    "bst": "Business Studies", "bus": "Business Studies",
    "agr": "Agriculture", "agri": "Agriculture",
    "comp": "Computer Studies",
}


def _split_subject_teacher(cell_text: str) -> tuple[str, str]:
    """
    Given a cell that may look like:
        "Mathematics\nBrian"
        "Mathematics\nBrian\n..."
        "Business Studies\nJohn"
        "Class Meeting"
    Return (subject, teacher).
    """
    lines = [ln.strip() for ln in cell_text.splitlines() if ln.strip()]
    if not lines:
        return "", ""

    subject_raw = lines[0]
    teacher = lines[1] if len(lines) > 1 else ""

    # Expand common subject short forms
    key = subject_raw.strip().lower()
    subject = _SUBJECT_ABBR.get(key, subject_raw)

    # Title-case teacher / subject nicely
    subject = _title(subject)
    teacher = _title(teacher)
    return subject, teacher


def _title(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    # Keep common acronyms upper (C.R.E, I.R.E)
    if s.upper() in {"C.R.E", "I.R.E", "CRE", "IRE"}:
        return s.upper().replace("CRE", "C.R.E").replace("IRE", "I.R.E")
    return s[0].upper() + s[1:]


_TIME_RE = re.compile(
    r"(\d{1,2})\s*[:.]\s*(\d{2})\s*[-–]\s*(\d{1,2})\s*[:.]\s*(\d{2})"
)


def _to_24h(hour: int) -> int:
    """
    School timetables use a 12-hour clock without AM/PM.
    - 1..6  → PM (add 12)   : 1:20 → 13:20, 2:40 → 14:40
    - 7..11 → AM (as-is)    : 8:10 → 08:10, 11:50 → 11:50
    - 12    → 12            : 12:30 → 12:30
    """
    if hour == 12:
        return 12
    if 1 <= hour <= 6:
        return hour + 12
    return hour


def _norm_time(raw: str) -> str:
    m = _TIME_RE.search(raw or "")
    if not m:
        return (raw or "").strip()

    h1, m1, h2, m2 = (int(x) for x in m.groups())

    start_h = _to_24h(h1)
    end_h = _to_24h(h2)

    # Safety: if end hour ended up before start hour, treat it as next session
    if end_h < start_h:
        end_h += 12

    return f"{start_h:02d}:{m1:02d} - {end_h:02d}:{m2:02d}"
    
async def process_docx_timetable(contents: bytes):
    """
    Extract timetable from a Word document.
    Handles the layout where each day is:
        Paragraph: "Monday"
        Table:     Time | Form 1A | Form 1B | ... | Form 4A
                   row per time slot, each cell = "Subject\nTeacher"
    """
    from docx import Document
    from io import BytesIO
    import re

    yield f"data: {json.dumps({'step': 2, 'message': '📝 Parsing Word document...', 'progress': 20, 'status': 'parsing'})}\n\n"
    await asyncio.sleep(0.3)

    doc = Document(BytesIO(contents))

    entries: list[dict] = []
    all_subjects: set[str] = set()
    all_teachers: set[str] = set()
    all_classes: set[str] = set()

    # ── 1. Walk the document body in order: paragraph → table → paragraph → table ──
    body = doc.element.body
    current_day: str | None = None

    # We need to iterate paragraphs and tables together, in document order.
    # python-docx doesn't expose that directly, so do it via the XML body.
    from docx.text.paragraph import Paragraph
    from docx.table import Table

    blocks = []
    for child in body.iterchildren():
        if child.tag.endswith('}p'):
            blocks.append(Paragraph(child, doc))
        elif child.tag.endswith('}tbl'):
            blocks.append(Table(child, doc))

    yield f"data: {json.dumps({'step': 2, 'message': f'📊 Found {len([b for b in blocks if isinstance(b, Table)])} tables...', 'progress': 40, 'status': 'parsing'})}\n\n"
    await asyncio.sleep(0.2)

    for block in blocks:
        # --- Paragraph: maybe it's a day name ---
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if not text:
                continue
            if _is_day_name(text):
                current_day = text
            continue

        # --- Table: parse timetable rows ---
        if isinstance(block, Table):
            if not block.rows:
                continue

            # Header row → class names (columns 1..N)
            header_cells = block.rows[0].cells
            class_names = [
                _clean_cell(c.text) for c in header_cells[1:]
            ]

            # If header row is empty/garbage, fall back to "Class N"
            if not any(class_names):
                class_names = [f"Class {i+1}" for i in range(len(header_cells) - 1)]

            for r_idx, row in enumerate(block.rows):
                if r_idx == 0:
                    continue  # skip header

                cells = row.cells
                if len(cells) < 2:
                    continue

                time_slot = _clean_cell(cells[0].text)
                if not time_slot:
                    continue
                # Skip break / lunch / assembly rows
                if _is_break_row(time_slot):
                    continue

                for c_idx in range(1, len(cells)):
                    cell_text = _clean_cell(cells[c_idx].text)
                    if not cell_text:
                        continue
                    if _is_break_row(cell_text):
                        continue

                    subject, teacher = _split_subject_teacher(cell_text)
                    if not subject:
                        continue

                    class_name = class_names[c_idx - 1] if c_idx - 1 < len(class_names) else f"Class {c_idx}"

                    entry = {
                        "day": current_day or "Unknown",
                        "time": _norm_time(time_slot),
                        "subject": subject,
                        "teacher": teacher,
                        "class": class_name,
                        "room": "",
                    }
                    entries.append(entry)
                    all_subjects.add(subject)
                    if teacher:
                        all_teachers.add(teacher)
                    all_classes.add(class_name)

    data = {
        "school": "",
        "is_timetable": True,
        "confidence": 90,
        "entries": entries,
        "detected_subjects": sorted(all_subjects),
        "detected_teachers": sorted(all_teachers),
        "detected_classes": sorted(all_classes),
        "total_entries": len(entries),
    }

    yield f"data: {json.dumps({'step': 3, 'message': f'✅ Extracted {len(entries)} entries', 'progress': 90, 'status': 'parsed', 'data': data})}\n\n"
    await asyncio.sleep(0.2)

    yield f"data: {json.dumps({'step': 4, 'message': '✨ Done', 'progress': 100, 'status': 'complete', 'done': True, 'data': data})}\n\n"
    
@router.get("/school-admins/{school_id}")
def get_school_admins(
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    if current_user.role not in ['admin', 'school'] and current_user.school_id != school_id:
      raise HTTPExeption(status_code=403, detail="Not Authorized")
    """Get all admins for the current user's school"""
    school_id = get_user_school_id(current_user, db)
    
    if not school_id:
        raise HTTPException(status_code=400, detail="No school associated")
    
    admins = db.query(User).filter(
        User.school_id == school_id,
        User.role.in_(['school', 'admin'])
    ).all()
    
    return {
        "data": [
            {
                "id": u.id,
                "full_name": u.full_name,
                "username": u.username,
                "email": u.email,
                "phone": u.phone,
                "role": u.role,
                "is_active": u.is_active,
                "profile_picture": u.profile_picture,
            }
            for u in admins
        ]
    }

@router.delete("/users/{user_id}")
def remove_user_from_school(
    user_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    if user.role == 'school' and current_user.id != user.id:
        raise HTTPException(status_code=403, detail="School owners can only delete themselves")
    
    current_school_id = get_user_school_id(current_user, db)
    target_school_id = get_user_school_id(user, db)
    
    if current_school_id != target_school_id:
        raise HTTPException(status_code=403, detail="User not of same school as you... Forbidden Action")
    
    school = db.query(School).filter(School.id == current_school_id).first()
    school_name = school.school_name if school else "Unknown"
    today = datetime.utcnow().date()
    
    # ========== TEACHER CV ==========
    if user.role == "teacher":
        teacher = db.query(Teacher).filter(Teacher.user_id == user_id).first()
        if teacher:
            # Get subjects taught
            csts = db.query(ClassSubjectTeacher).filter(
                ClassSubjectTeacher.teacher_id == teacher.id
            ).all()
            subjects = list(set(c.subject for c in csts))
            
            # Get classes taught
            class_ids = list(set(c.class_id for c in csts))
            classes = db.query(Class).filter(Class.id.in_(class_ids)).all() if class_ids else []
            class_names = [c.name for c in classes]
            
            # Get student performance
            student_ids = db.query(Student.user_id).filter(
                Student.class_id.in_(class_ids)
            ).all() if class_ids else []
            student_id_list = [s[0] for s in student_ids]
            
            avg_performance = 0.0
            pass_rate = 0.0
            total_students = len(student_id_list)
            
            if student_id_list:
                performances = db.query(StudentPerformance).filter(
                    StudentPerformance.student_id.in_(student_id_list)
                ).all()
                if performances:
                    scores = [p.score for p in performances]
                    avg_performance = round(sum(scores) / len(scores), 1)
                    pass_rate = round(len([s for s in scores if s >= 50]) / len(scores) * 100, 1)
            
            # Get activities
            activities = db.query(Activity).filter(Activity.user_id == user_id).all()
            activity_names = [a.name for a in activities]
            
            # Student rating (1-5 scale based on performance)
            student_rating = round(min(avg_performance / 20, 5.0), 1)
            
            # Save CV
            db.add(TeacherEmploymentHistory(
                teacher_user_id=user_id,
                school_id=current_school_id,
                school_name=school_name,
                subject_taught=", ".join(subjects) if subjects else (teacher.subject or ""),
                classes_taught=", ".join(class_names),
                start_date=datetime.strptime(teacher.joined_date, "%Y-%m-%d").date() if teacher.joined_date else today,
                end_date=today,
                average_student_performance=avg_performance,
                student_pass_rate=pass_rate,
                activities=", ".join(activity_names) if activity_names else "",
                student_rating=student_rating,
                total_students_taught=total_students,
                notes=f"Taught at {school_name}"
            ))
        
        db.query(Teacher).filter(Teacher.user_id == user_id).update({"school_id": None})
    
    # ========== ADMIN CV ==========
    elif user.role in ["admin", "school"]:
        total_students = db.query(Student).filter(Student.school_id == current_school_id).count()
        total_teachers = db.query(Teacher).filter(Teacher.school_id == current_school_id).count()
        total_workers = db.query(Worker).filter(Worker.school_id == current_school_id).count()
        
        # School average performance
        performances = db.query(StudentPerformance).join(
            Student, StudentPerformance.student_id == Student.user_id
        ).filter(Student.school_id == current_school_id).all()
        
        school_avg = round(sum(p.score for p in performances) / len(performances), 1) if performances else 0.0
        
        db.add(AdminEmploymentHistory(
            admin_user_id=user_id,
            school_id=current_school_id,
            school_name=school_name,
            role=user.role,
            responsibilities=f"Managed {total_students} students, {total_teachers} teachers, {total_workers} workers",
            start_date=user.created_at.date() if user.created_at else today,
            end_date=today,
            total_students_managed=total_students,
            total_teachers_managed=total_teachers,
            total_workers_managed=total_workers,
            school_performance_avg=school_avg,
            notes=f"Administrator at {school_name}"
        ))
    
    # ========== WORKER CV ==========
    elif user.role == "worker":
        worker = db.query(Worker).filter(Worker.user_id == user_id).first()
        if worker:
            # Performance rating
            perf_records = db.query(WorkerPerformance).filter(
                WorkerPerformance.worker_id == user_id
            ).all()
            avg_rating = round(sum(p.rating for p in perf_records) / len(perf_records), 1) if perf_records else 0.0
            
            # Attendance rate
            attend_count = db.query(WorkerAttendance).filter(
                WorkerAttendance.worker_id == user_id,
                WorkerAttendance.status == 'present'
            ).count()
            total_attend = db.query(WorkerAttendance).filter(
                WorkerAttendance.worker_id == user_id
            ).count()
            attend_rate = round(attend_count / total_attend * 100, 1) if total_attend > 0 else 0.0
            
            db.add(WorkerEmploymentHistory(
                worker_user_id=user_id,
                school_id=current_school_id,
                school_name=school_name,
                department=worker.department or "",
                role_title=worker.role_title or "",
                start_date=datetime.strptime(worker.joined_date, "%Y-%m-%d").date() if worker.joined_date else today,
                end_date=today,
                performance_rating=avg_rating,
                attendance_rate=attend_rate,
                supervisor_name=worker.supervisor or "",
                notes=f"Worked at {school_name}"
            ))
        
        db.query(Worker).filter(Worker.user_id == user_id).update({"school_id": None})
    
    # ========== STUDENT ==========
    elif user.role == "student":
        db.query(Student).filter(Student.user_id == user_id).update({"school_id": None})
    
    # ========== PARENT ==========
    elif user.role == "parent":
        db.query(Parent).filter(Parent.user_id == user_id).update({"school_id": None})
    
    # Deactivate user
    user.is_active = False
    user.school_id = None
    user.approval_status = 'inactive'
    
    db.commit()
    
    return {
        "message": f"User {user.username} removed from {school_name}. Employment history saved for CV.",
        "school": school_name,
        "role": user.role
    }
    
# Teachers cv
@router.get("/cv/teacher/{user_id}")
def get_teacher_cv(user_id: int, db: Session = Depends(get_db)):
    history = db.query(TeacherEmploymentHistory).filter(
        TeacherEmploymentHistory.teacher_user_id == user_id
    ).order_by(TeacherEmploymentHistory.start_date.desc()).all()
    
    user = db.query(User).filter(User.id == user_id).first()
    teacher = db.query(Teacher).filter(Teacher.user_id == user_id).first()
    
    return {
        "name": user.full_name if user else "",
        "email": user.email if user else "",
        "qualification": teacher.qualification if teacher else "",
        "experience_years": teacher.years_of_experience if teacher else "",
        "employment_history": [
            {
                "school": h.school_name,
                "subjects": h.subject_taught,
                "classes": h.classes_taught,
                "period": f"{h.start_date} to {h.end_date}",
                "average_performance": f"{h.average_student_performance}%",
                "pass_rate": f"{h.student_pass_rate}%",
                "students_taught": h.total_students_taught,
                "rating": f"{h.student_rating}/5",
                "activities": h.activities,
            }
            for h in history
        ]
    }

@router.post("/timetable/save-extracted")
def save_extracted_timetable(
    entries: List[dict] = Body(...),
    school_id: int = Body(...),
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Save timetable entries extracted from image analysis - REPLACES existing timetable"""
    
    # Verify authorization
    if current_user.role not in ['admin', 'school']:
        raise HTTPException(status_code=403, detail="Not authorized")
    school_id = current_user.school_id
    
    try:
        # ========== DELETE existing timetable for this school ==========
        deleted_count = db.query(Timetable).filter(
            Timetable.school_id == school_id
        ).delete()
        print(f"Deleted {deleted_count} existing timetable entries for school {school_id}")
        
        # ========== INSERT new entries ==========
        saved_count = 0
        errors = []
        
        for entry in entries:
            try:
                day = entry.get('day', '')
                time_str = entry.get('time', '')
                class_name = entry.get('class', '')
                subject = entry.get('subject', '')
                room = entry.get('room', '')
                
                # Parse time
                start_time = ''
                end_time = ''
                if '-' in time_str:
                    parts = time_str.split('-')
                    start_time = parts[0].strip()
                    end_time = parts[1].strip()
                
                # Create new entry
                timetable_entry = Timetable(
                    school_id=school_id,
                    class_name=class_name,
                    day_of_week=day,
                    start_time=start_time,
                    end_time=end_time,
                    subject=subject,
                    room=room if room else None,
                    is_break=False
                )
                db.add(timetable_entry)
                saved_count += 1
                    
            except Exception as e:
                errors.append(f"Error: {str(e)}")
        
        db.commit()
        
        return {
            "message": f"Replaced timetable: deleted {deleted_count} old entries, saved {saved_count} new entries",
            "saved": saved_count,
            "deleted": deleted_count,
            "total": len(entries),
            "errors": errors if errors else None
        }
        
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save: {str(e)}")
        
@router.get("/sms")
def get_sms(user = Depends(get_current_user), db: Session = Depends(get_db)):
    if not user:
         raise HTTPException(status_code=403, detail="Login and try again")
    
    school = db.query(School).filter(School.id==user.school_id).first()

    return {"balance": school.sms_bal}

@router.post("/send-message")
def send_message(
    background_tasks: BackgroundTasks,
    message: str = Body(...),
    recipient_type: str = Body(...),
    send_method: Optional[str] = Body(None),
    student_ids: Optional[List[int]] = Body(None),
    metadata: Optional[dict] = Body(None),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    school_id = get_user_school_id(user, db)
    phones = []
    emails = []

    if recipient_type == 'all':
        # Send to ALL parents
        query = db.query(User.phone, User.email)\
            .join(ParentStudent, ParentStudent.parent_id == User.id)\
            .join(Student, ParentStudent.student_id == Student.id)\
            .filter(User.role == 'parent')
        if school_id:
            query = query.filter(Student.school_id == school_id)
        recipients = query.all()
        phones = list(set(r.phone for r in recipients if r.phone))
        emails = list(set(r.email for r in recipients if r.email))

    elif recipient_type == 'debtors':
        # Send to parents of students with pending/partial fees
        debtor_ids = db.query(Fee.student_id).filter(
            Fee.status.in_(['pending', 'partial'])
        ).distinct().all()
        debtor_id_list = [d[0] for d in debtor_ids]
        query = db.query(User.phone, User.email)\
            .join(ParentStudent, ParentStudent.parent_id == User.id)\
            .join(Student, ParentStudent.student_id == Student.id)\
            .filter(User.role == 'parent')\
            .filter(Student.id.in_(debtor_id_list))
        if school_id:
            query = query.filter(Student.school_id == school_id)
        recipients = query.all()
        phones = list(set(r.phone for r in recipients if r.phone))
        emails = list(set(r.email for r in recipients if r.email))

    elif recipient_type == 'specific' and student_ids:
        # Send to parents of specific selected students
        query = db.query(User.phone, User.email)\
            .join(ParentStudent, ParentStudent.parent_id == User.id)\
            .join(Student, ParentStudent.student_id == Student.id)\
            .filter(User.role == 'parent')\
            .filter(Student.id.in_(student_ids))
        recipients = query.all()
        phones = list(set(r.phone for r in recipients if r.phone))
        emails = list(set(r.email for r in recipients if r.email))

    elif recipient_type == 'importParents' and metadata:
        # Send directly to imported parents list
        parents = metadata.get('parents', [])
        phones = list(set(p['phone'] for p in parents if p.get('phone')))
        emails = list(set(p['email'] for p in parents if p.get('email')))
    
    if not phones and not emails:
        raise HTTPException(
            status_code=400,
            detail="No valid phone numbers or email addresses found."
        )
    # Send
    if send_method in ['sms', 'both'] and phones:
        background_tasks.add_task(send_sms_batch, phones, message, school_id)

    if send_method in ['email', 'both'] and emails:
        background_tasks.add_task(send_email_batch, emails, message, school_id)

    return {
        "message": f"Message queued for {len(phones) + len(emails)} recipients",
        "unique_phones": len(phones),
        "unique_emails": len(emails),
        "method": send_method,
        "recipient_type": recipient_type,
        "status": "queued"
    }
    
@router.post("/extract-parents")
async def extract_parents(
    file: UploadFile = File(...),
    current_user = Depends(get_current_user)
):
    # Fixed the authorization logic condition
    if current_user.role not in ['admin', 'school'] and current_user != "":
        raise HTTPException(status_code=403, detail="Not Authorized")
        
    GEMINI_MODELS = [
        "gemini-flash-latest",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
        "gemini-1.5-pro",
    ]
    
    API_KEY = os.getenv("API_KEY")
    if not API_KEY:
        raise HTTPException(status_code=500, detail="API_KEY not configured")

    async def event_generator():
        try:
            # Step 1: Receiving image
            yield f"data: {json.dumps({'step': 1, 'message': '📤 Receiving image...', 'progress': 5, 'status': 'uploading'})}\n\n"
            await asyncio.sleep(0.2)
            
            contents = await file.read()
            image = Image.open(io.BytesIO(contents))
            
            yield f"data: {json.dumps({'step': 1, 'message': f'🖼️ Image loaded ({image.size[0]}x{image.size[1]}px)', 'progress': 10, 'status': 'processing'})}\n\n"
            
            # Setup GenAI client
            client = genai.Client(api_key=API_KEY)
            
            prompt = """
You are an expert document parser.

Extract this parents data document.

Rules:
- Detect all mobile numbers(eg 0745000000)
- Detect all emails
- Detect all classes (e.g., Form 1A, Form 2B).
- Detect all admission number(eg ADM001 or 5753)  or child/student name (eg Tony Mumenyei)
- Detect all parent names if present (eg Jane Mwangi)
- Return ONLY valid JSON. No markdown. No explanation.

Output format:
{
  "student_name": "",    can also be admission_number if admission was provided
  "confidence": 95,
  "entries": [
    {
      "parent_name": "Nick Tyson",   if available
      "email": "nicktyson@gmail.com",
      "phone": "0745000000"
    }
  ]
}

If NOT a parent document, return: {"is_parent": false, "confidence": 0, "message": "Not a parent list image"}
"""
            
            # Step 2: Try models with fallback
            last_error = None
            progress = 15
            total_models = len(GEMINI_MODELS)
            successful_response = None
            
            for idx, model in enumerate(GEMINI_MODELS):
                progress = 15 + (idx * (70 // total_models))
                
                # Show current model
                yield f"data: {json.dumps({'step': 2, 'message': f'🔄 Model {idx+1}/{total_models}: {model}', 'progress': progress, 'status': 'trying_model', 'model': model, 'model_index': idx+1, 'total_models': total_models})}\n\n"
                await asyncio.sleep(0.3)
                
                model_success = False
                for attempt in range(2):
                    try:
                        yield f"data: {json.dumps({'step': 2, 'message': f'🤖 Calling {model}... (attempt {attempt+1}/2)', 'progress': progress + 2, 'status': 'calling_api', 'model': model})}\n\n"
                        await asyncio.sleep(0.2)
                        
                        response = client.models.generate_content(
                            model=model,
                            contents=[image, prompt],
                        )
                        
                        if response and response.text:
                            successful_response = response.text
                            model_success = True
                            break
                    except Exception as e:
                        last_error = str(e)
                        await asyncio.sleep(0.5)
                
                if model_success:
                    break
            
            if not successful_response:
                yield f"data: {json.dumps({'step': 3, 'message': f'❌ All models failed. Last error: {last_error}', 'progress': 100, 'status': 'error'})}\n\n"
                return

            # Step 3: Success and parsing result
            yield f"data: {json.dumps({'step': 3, 'message': '✨ Parsing complete successfully!', 'progress': 90, 'status': 'parsing'})}\n\n"
            await asyncio.sleep(0.2)
            
            # Clean text just in case model included markdown code blocks
            cleaned_text = successful_response.strip()
            if cleaned_text.startswith("```json"):
                cleaned_text = cleaned_text[7:]
            if cleaned_text.startswith("```"):
                cleaned_text = cleaned_text[3:]
            if cleaned_text.endswith("```"):
                cleaned_text = cleaned_text[:-3]
            cleaned_text = cleaned_text.strip()
            
            parsed_data = json.loads(cleaned_text)
            
            yield f"data: {json.dumps({'step': 3, 'message': '✅ Data extracted successfully', 'progress': 100, 'status': 'completed', 'data': parsed_data})}\n\n"
            
        except Exception as e:
            yield f"data: {json.dumps({'step': 3, 'message': f'⚠️ Error: {str(e)}', 'progress': 100, 'status': 'error'})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )
    
@router.post("/save-parents")
async def save_parents(
    request: ImportParentRequest,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """import Extracted parents"""
    try:
        count = 0
        existing_parents = db.query(ParentUpload).filter(
            ParentUpload.school_id == request.school_id
        ).all()
        
        existing_emails = {p.email for p in existing_parents if p.email}
        existing_phones = {p.phone for p in existing_parents if p.phone}
        
        for parent_data in request.parents:        
            if parent_data.email and parent_data.email in existing_emails:
                continue
            if parent_data.phone and parent_data.phone in existing_phones:
                continue
            student_id = None
            
            if hasattr(request, 'admission_number') and request.admission_number:
               student = db.query(Student).filter(Student.school_id == request.school_id, Student.admission_number == parent_data.admission_number).first()
               if student:
                   student_id = student.id
            
            parent_upload = ParentUpload(
                school_id=request.school_id,
                student_id=student_id,
                name=parent_data.parent_name,
                email=parent_data.email,
                phone=parent_data.phone
            )
            db.add(parent_upload)
            if parent_data.email:
                existing_emails.add(parent_data.email)
            if parent_data.phone:
                existing_phones.add(parent_data.phone)
            count += 1
        db.commit()
        return {
            "success": True,
            "message": f"Successfully imported {count} parents"
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
        
@router.get("/parent-imports")
def parent_imports(
    current_user = Depends(get_current_user),
    db: Session = Depends(get_db)
):

    parents = []
    if current_user.role not in ['admin', 'school']:
        raise HTTPException(status_code=403, detail='Not Authorized')
    
    parent_data = db.query(ParentUpload).filter(ParentUpload.school_id == current_user.school_id).all()
    
    if not parent_data:
        raise HTTPException(status_code=404, detail="No upload found for your school")
    for data in parent_data:
        parents.append({
            "school_id": data.school_id,
            "student_id": data.student_id,
            "email": data.email,
            "phone": data.phone,
            "name": data.name
        })
    
    return {
        "data": parents
    }
    
    
UPLOAD_DIR = "uploads/books"
COVER_DIR = "uploads/covers"

# Ensure upload directories exist
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(COVER_DIR, exist_ok=True)

@router.post("/books/upload")
async def book_upload(
    title: str = Form(...),
    author: str = Form(...),
    category: str = Form(...),
    description: str = Form(...),
    is_free: str = Form(...),
    price: str = Form(...),
    file: UploadFile = File(...),
    cover_image: Optional[UploadFile] = File(None),
    user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not user:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    # Validate file type
    allowed_extensions = ('.pdf', '.doc', '.docx', '.txt')
    if not file.filename or not file.filename.lower().endswith(allowed_extensions):
        raise HTTPException(status_code=400, detail="Invalid file type. Allowed: PDF, DOC, DOCX, TXT")
    
    # Convert types
    is_free_bool = is_free.lower() == "true"
    price_float = float(price) if price else 0.0
    
    # Read PDF file
    pdf_bytes = await file.read()
    
    # Generate unique filename for PDF
    file_ext = os.path.splitext(file.filename)[1]
    unique_pdf_name = f"{uuid.uuid4()}{file_ext}"
    pdf_path = os.path.join(UPLOAD_DIR, unique_pdf_name)
    
    # Save PDF to disk
    with open(pdf_path, "wb") as f:
        f.write(pdf_bytes)
    
    # Handle cover image
    cover_path = None
    if cover_image and cover_image.filename:
        # Validate image type
        if not cover_image.filename.lower().endswith(('.jpg', '.jpeg', '.png', '.gif', '.webp')):
            raise HTTPException(status_code=400, detail="Invalid image type. Allowed: JPG, PNG, GIF, WEBP")
        
        cover_ext = os.path.splitext(cover_image.filename)[1]
        unique_cover_name = f"{uuid.uuid4()}{cover_ext}"
        cover_path = os.path.join(COVER_DIR, unique_cover_name)
        
        cover_bytes = await cover_image.read()
        with open(cover_path, "wb") as f:
            f.write(cover_bytes)
    
    # Create book record in database
    book = Book(
        title=title,
        user_id=user.id,
        author=author,
        description=description,
        category=category,
        price=int(price_float),
        is_free=is_free_bool,
        rating=0,
        views=0,
        downloads=0,
        likes=0,
        published_by=user.id if hasattr(user, 'id') else None,
        published_date=datetime.now().strftime("%Y-%m-%d"),
        image_url=cover_path,
        file_path=pdf_path,
    )
    
    db.add(book)
    db.commit()
    db.refresh(book)
    
    return {
        "success": True,
        "message": "Book uploaded successfully",
        "data": {
            "id": book.id,
            "title": book.title,
            "author": book.author,
            "category": book.category,
            "filename": file.filename,
            "saved_as": unique_pdf_name,
            "size_kb": round(len(pdf_bytes) / 1024, 2),
            "has_cover": cover_path is not None,
            "is_free": book.is_free,
            "price": book.price,
        }
    }
    
@router.post("/view")
async def views(
    user = Depends(get_current_user),
    db: Session = Depends(get_db),
    book_id: int = Form(...)
):
    if not user:
        raise HTTPException(status_code=403, detail="Not Authorized")
        
    books = db.query(Book).filter(Book.id==book_id).first()
    if not books:
        raise HTTPException(status_code=404, detail="Book Not Found")
    views = db.query(View).filter(View.book_id==book_id, View.user_id==user.id).first()
    if not views:
        views = View(
            book_id=book_id,
            user_id=user.id,
        )
        db.add(views)
        books.views += 1
        db.commit()
        db.refresh(views)
    
    return {"message": f"View recorded now at {books.views} views"}
    
@router.api_route("/books/last-page", methods=["GET", "POST"])
async def get_save_lastpage(
    request: Request,
    user = Depends(get_current_user),
    db: Session = Depends(get_db),
    last_page: int = Form(None)
):   
    if request.method == "GET":
        book_id = int(request.query_params["book_id"])
        book = db.query(Book).filter(Book.id == book_id).first()
        
        if not book:
            raise HTTPException(status_code=404, detail=f"{book_id} Book Not Found")
        # Find existing last page record
        lastpage = db.query(Lastpage).filter(Lastpage.book_id == book_id, Lastpage.user_id == user.id).first()
        if lastpage:
            return {
                "success": True,
                "data": {
                    "last_page": lastpage.lastpage,
                    "book_id": lastpage.book_id,
                    "user_id": lastpage.user_id
                }
            }
        else:
            return {
                "success": True,
                "data": None,
                "message": "No reading progress found"
            }
    
    elif request.method == "POST":
        form = await request.form()
        book_id = int(form["book_id"])
        book = db.query(Book).filter(Book.id == book_id).first()
        if not book:
            raise HTTPException(status_code=404, detail=f"{book_id} Book Not Found")
        
        # Find existing last page record
        lastpage = db.query(Lastpage).filter(Lastpage.book_id == book_id, Lastpage.user_id == user.id).first()
        if lastpage:
            # Update existing record
            lastpage.lastpage = last_page
        else:
            # Create new record
            lastpage = Lastpage(
                book_id=book_id,
                user_id=user.id,
                lastpage=last_page
            )
            db.add(lastpage)
        
        db.commit()
        db.refresh(lastpage)
        
        return {
            "success": True,
            "message": "Reading progress saved",
            "data": {
                "last_page": lastpage.lastpage,
                "book_id": lastpage.book_id,
                "user_id": lastpage.user_id
            }
        }


@router.api_route("/books/rate", methods=["POST"])
async def rate_book(
    user = Depends(get_current_user),
    db: Session = Depends(get_db),
    book_id: int = Form(...),
    rating: int = Form(...),
):
    # Validate rating
    if rating < 1 or rating > 5:
        raise HTTPException(status_code=400, detail="Rating must be between 1 and 5")
    
    # Check if book exists
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(status_code=404, detail="Book Not Found")
    
    # Check if already rated
    existing_rating = db.query(Rating).filter(
        Rating.book_id == book_id,
        Rating.user_id == user.id
    ).first()
    
    if existing_rating:
        # Update existing rating
        existing_rating.rating = rating
        db.commit()
        
        # Update book average rating
        _update_book_avg_rating(db, book_id)
        
        return {
            "success": True,
            "message": "Rating updated",
            "data": {
                "rating": rating,
                "book_id": book_id,
                "user_id": user.id
            }
        }
    else:
        # Create new rating
        new_rating = Rating(
            book_id=book_id,
            user_id=user.id,
            rating=rating
        )
        db.add(new_rating)
        db.commit()
        db.refresh(new_rating)
        
        # Update book average rating
        _update_book_avg_rating(db, book_id)
        
        return {
            "success": True,
            "message": "Rating submitted",
            "data": {
                "rating": rating,
                "book_id": book_id,
                "user_id": user.id
            }
        }


def _update_book_avg_rating(db: Session, book_id: int):
    """Calculate and update the average rating for a book"""
    avg_rating = db.query(func.avg(Rating.rating)).filter(
        Rating.book_id == book_id
    ).scalar()
    
    total_ratings = db.query(func.count(Rating.id)).filter(
        Rating.book_id == book_id
    ).scalar()
    
    book = db.query(Book).filter(Book.id == book_id).first()
    if book:
        book.rating = round(float(avg_rating or 0), 1)  # ✅ Assignment (=), not comparison (==)
        book.total_ratings = total_ratings or 0
        db.commit()
        
@router.post("/jobs/{job_id}/apply")
async def apply_job(
    job_id: int,
    documents: List[UploadFile] = File(None),
    cover_letter: Optional[str] = Form(None),
    user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if user.role == "student":
        raise HTTPException(status_code=403, detail="Unauthorized Action")
    
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.posted_by == user.id:
        raise HTTPException(status_code=400, detail="You can not apply your own job")

    applicants = db.query(Applicant).filter(Applicant.user_id == user.id, Applicant.job_id==job_id).first()
    
    if applicants:
        return {"message": "Job Applied"}
    
    application = Applicant(
        job_id=job_id,
        user_id=user.id,
        status="pending",
    )
    
    job.applicants += 1
    
    # Create alert
    alert = Alert(
        user_id=job.posted_by,
        applicant_id=user.id,
        job_id=job_id,
        type="application",
        message=f"{user.full_name} applied for {job.title}",
        title=f"New application for {job.title}"
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    
    # ✅ Save documents if provided
    saved_docs_count = 0
    if documents:
        os.makedirs("uploads/applications", exist_ok=True)
        
        for file in documents:
            if file.filename:
                # Read file content
                content = await file.read()
                
                # Get file extension
                file_extension = os.path.splitext(file.filename)[1].lower()
                original_filename = file.filename
                
                # ✅ Check if it's doc, docx, or ppt that needs conversion
                needs_conversion = file_extension in ['.doc', '.docx', '.ppt', '.pptx']
                final_file_path = None
                final_file_name = original_filename
                final_file_type = "document"
                
                if needs_conversion:
                    # Convert to PDF
                    try:
                        # Save original file temporarily
                        temp_dir = "uploads/temp"
                        os.makedirs(temp_dir, exist_ok=True)
                        temp_file_path = f"{temp_dir}/{user.id}_{datetime.utcnow().timestamp()}{file_extension}"
                        
                        with open(temp_file_path, "wb") as buffer:
                            buffer.write(content)
                        
                        # Convert to PDF using LibreOffice (headless)
                        pdf_dir = "uploads/applications"
                        os.makedirs(pdf_dir, exist_ok=True)
                        
                        conversion_result = subprocess.run(
                            [
                                'libreoffice',
                                '--headless',
                                '--convert-to', 'pdf',
                                '--outdir', pdf_dir,
                                temp_file_path
                            ],
                            capture_output=True,
                            text=True,
                            timeout=60
                        )
                        
                        if conversion_result.returncode == 0:
                            # Get the converted PDF filename
                            base_name = os.path.splitext(os.path.basename(temp_file_path))[0]
                            converted_pdf_path = f"{pdf_dir}/{base_name}.pdf"
                            
                            if os.path.exists(converted_pdf_path):
                                # Rename to use alert ID
                                unique_id = f"{alert.id}_{datetime.utcnow().timestamp()}"
                                final_pdf_path = f"{pdf_dir}/application_{unique_id}.pdf"
                                os.rename(converted_pdf_path, final_pdf_path)
                                
                                final_file_path = final_pdf_path
                                final_file_name = f"{os.path.splitext(original_filename)[0]}.pdf"
                                final_file_type = "pdf"
                                
                                # Remove temp file
                                os.remove(temp_file_path)
                            else:
                                # Conversion failed, save original
                                final_file_path = f"uploads/applications/application_{alert.id}_{datetime.utcnow().timestamp()}{file_extension}"
                                with open(final_file_path, "wb") as buffer:
                                    buffer.write(content)
                                final_file_type = "document"
                        else:
                            # LibreOffice not available, save original
                            final_file_path = f"uploads/applications/application_{alert.id}_{datetime.utcnow().timestamp()}{file_extension}"
                            with open(final_file_path, "wb") as buffer:
                                buffer.write(content)
                            final_file_type = "document"
                            
                    except Exception as e:
                        print(f"❌ Conversion failed: {e}")
                        # Save original if conversion fails
                        final_file_path = f"uploads/applications/application_{alert.id}_{datetime.utcnow().timestamp()}{file_extension}"
                        with open(final_file_path, "wb") as buffer:
                            buffer.write(content)
                        final_file_type = "document"
                else:
                    # No conversion needed (PDF, images, etc.)
                    unique_id = f"{alert.id}_{datetime.utcnow().timestamp()}"
                    saved_filename = f"application_{unique_id}{file_extension}"
                    final_file_path = f"uploads/applications/{saved_filename}"
                    
                    with open(final_file_path, "wb") as buffer:
                        buffer.write(content)
                    
                    # Determine file type
                    lower_name = original_filename.lower()
                    if "resume" in lower_name or "cv" in lower_name:
                        final_file_type = "resume"
                    elif "cover" in lower_name:
                        final_file_type = "cover_letter"
                    elif "cert" in lower_name:
                        final_file_type = "certificate"
                    elif file_extension in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                        final_file_type = "image"
                    elif file_extension == '.pdf':
                        final_file_type = "pdf"
                    else:
                        final_file_type = "document"
                
                # Save document record
                if final_file_path:
                    doc = ApplicationDocument(
                        alert_id=alert.id,
                        user_id=user.id,
                        file_name=final_file_name,
                        file_path=final_file_path,
                        file_type=final_file_type,
                        file_size=os.path.getsize(final_file_path) if os.path.exists(final_file_path) else None,
                    )
                    db.add(doc)
                    saved_docs_count += 1
        
        db.commit()
    
    db.add(application)
    db.commit()
    
    return {
        "message": "Success",
        "alert_id": alert.id,
        "documents_saved": saved_docs_count
    }
    
async def create_alert(applicant_id: int, job_id: int, boss_id: int, db, name: str, title: str):
    alert = Alert(
        user_id=boss_id,
        applicant_id=applicant_id,
        job_id=job_id,
        type="application",
        message=f"{name} applied for {title}"
    )
    db.add(alert)
    db.commit()
    return {"message": "Alert Created"}

# ==================== CHATS / MESSAGES ====================

@router.get("/messages")
async def get_messages(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    chat_with: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Get messages for current user"""

    if chat_with is None:
        updated = db.query(Chat).filter(
            Chat.to_id == user.id,
            Chat.status == 'sent',
            Chat.is_read == False
        ).update({
            "status": "delivered",
            "delivered_at": datetime.now()
        })
        db.commit()
        
        unread_count = db.query(Chat).filter(
            Chat.to_id == user.id,
            Chat.is_read == False,
            Chat.status == 'delivered'
        ).count()
        
        return {
            "data": [],
            "total": 0,
            "page": page,
            "limit": limit,
            "unread_count": unread_count,
            "has_more": False
        }
    
    query = db.query(Chat).filter(
        or_(
            and_(Chat.user_id == user.id, Chat.to_id == chat_with),
            and_(Chat.user_id == chat_with, Chat.to_id == user.id),
        )
    )

    total = query.count()
    chats = query.order_by(Chat.created_at.asc())\
        .offset((page - 1) * limit)\
        .limit(limit)\
        .all()

    unread_count = db.query(Chat).filter(
        Chat.to_id == user.id,
        Chat.is_read == False
    ).count()

    data = []
    for chat in chats:
        sender = db.query(User).filter(User.id == chat.user_id).first()
        
        # ✅ ADD: Get reply message if exists
        reply_to = None
        if chat.reply_to_id:
            reply_msg = db.query(Chat).filter(Chat.id == chat.reply_to_id).first()
            if reply_msg:
                reply_sender = db.query(User).filter(User.id == reply_msg.user_id).first()
                reply_to = {
                    "id": reply_msg.id,
                    "message": reply_msg.message,
                    "sender_name": reply_sender.full_name if reply_sender else "Unknown",
                }
        
        data.append({
            "id": chat.id,
            "sender_id": chat.user_id,
            "sender_name": sender.full_name if sender else "Unknown",
            "message": chat.message,
            "status": chat.status if hasattr(chat, 'status') else 'sent',
            "is_mine": chat.user_id == user.id,
            "is_read": chat.is_read if hasattr(chat, 'is_read') else False,
            "reply_to_id": chat.reply_to_id,  # ✅ ADD
            "reply_to": reply_to,  # ✅ ADD
            "created_at": chat.created_at.isoformat() if chat.created_at else None,
        })

    return {
        "data": data,
        "total": total,
        "page": page,
        "limit": limit,
        "unread_count": unread_count,
        "has_more": (page * limit) < total
    }
    
@router.post("/messages/send")
async def send_chat_message(
    to_id: int = Form(...),
    message: str = Form(...),
    reply_to_id: Optional[int] = Form(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Send a chat message to another user"""
    
    # Validate recipient exists
    recipient = db.query(User).filter(User.id == to_id).first()
    if not recipient:
        raise HTTPException(status_code=404, detail="Recipient not found")
    
    # Validate message
    if not message or not message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    
    # Prevent sending to self
    if to_id == user.id:
        raise HTTPException(status_code=400, detail="Cannot send message to yourself")
    
    reply_to = None
    if reply_to_id:
        reply_to = db.query(Chat).filter(Chat.id == reply_to_id).first()
        if not reply_to:
            raise HTTPException(status_code=404, detail="Reply message not found")
    
    # Create new chat message
    new_chat = Chat(
        user_id=user.id,  # Sender
        to_id=to_id,      # Recipient
        reply_to_id=reply_to_id,
        message=message.strip(),
        status="sent",
        is_read=False,
        created_at=datetime.now()
    )
    
    db.add(new_chat)
    db.commit()
    db.refresh(new_chat)
    
    reply_data = None
    if reply_to:
        reply_sender = db.query(User).filter(User.id == reply_to.user_id).first()
        reply_data = {
            "id": reply_to.id,
            "message": reply_to.message,
            "sender_name": reply_sender.full_name if reply_sender else "Unknown",
        }
    
    # Format response
    response_data = {
        "id": new_chat.id,
        "sender_id": new_chat.user_id,
        "sender_name": user.full_name if user else "Unknown",
        "message": new_chat.message,
        "status": new_chat.status,
        "reply_to_id": new_chat.reply_to_id,  # ADD THIS
        "reply_to": reply_data,
        "is_mine": True,
        "is_read": new_chat.is_read,
        "created_at": new_chat.created_at.isoformat() if new_chat.created_at else None,
    }
    
    return {
        "message": "Message sent successfully",
        "data": response_data
    }

@router.get("/messages/unread-count")
async def get_unread_message_count(
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Get only unread message count"""
    
    count = db.query(Chat).filter(
        Chat.to_id == user.id,
        Chat.is_read == False
    ).count()
    
    return {"unread_count": count}


@router.get("/messages/conversations")
async def get_conversations(
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Get list of unique conversations"""
    
    # Get unique users that the current user has chatted with
    user_ids = set()
    
    # Users who sent messages to current user
    senders = db.query(Chat.user_id).filter(Chat.to_id == user.id).distinct().all()
    for (sender_id,) in senders:
        user_ids.add(sender_id)
    
    # Users who received messages from current user
    receivers = db.query(Chat.to_id).filter(Chat.user_id == user.id).distinct().all()
    for (receiver_id,) in receivers:
        user_ids.add(receiver_id)
    
    conversations = []
    for other_user_id in user_ids:
        other_user = db.query(User).filter(User.id == other_user_id).first()
        if not other_user:
            continue
        
        # Get last message between the two users
        last_msg = db.query(Chat).filter(
            ((Chat.user_id == user.id) & (Chat.to_id == other_user_id)) |
            ((Chat.user_id == other_user_id) & (Chat.to_id == user.id))
        ).order_by(desc(Chat.created_at)).first()
        
        # Count unread messages from this user
        unread = db.query(Chat).filter(
            Chat.to_id == user.id,
            Chat.user_id == other_user_id,
            Chat.is_read == False
        ).count()
        
        conversations.append({
            "user_id": other_user_id,
            "name": other_user.full_name if other_user else "Unknown",
            "dp_url": other_user.profile_picture,
            "avatar": other_user.profile_picture,
            "last_message": last_msg.message if last_msg else "",
            "last_message_at": last_msg.created_at.isoformat() if last_msg and last_msg.created_at else None,
            "last_time": last_msg.created_at.isoformat() if last_msg and last_msg.created_at else None,
            "unread_count": unread,
        })
    
    # Sort by last message time (most recent first)
    conversations.sort(key=lambda x: x['last_time'] or '', reverse=True)
    return {"data": conversations}


@router.post("/messages/{message_id}/read")
async def mark_message_read(
    message_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Mark a single message as read"""
    
    chat = db.query(Chat).filter(
        Chat.id == message_id,
        Chat.to_id == user.id
    ).first()
    
    if not chat:
        raise HTTPException(status_code=404, detail="Message not found")
    
    chat.is_read = True
    chat.status = "read"
    chat.read_at = datetime.now()
    db.commit()
    
    return {"message": "Marked as read"}

@router.post("/messages/{message_id}/delivered")
def mark_delivered(message_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    message = db.query(Chat).filter(Chat.id == message_id).first()
    if not message:
        raise HTTPException(404, "Message not found")
    
    if message.to_id == user.id:  # Only recipient can mark as delivered
        message.status = "delivered"
        message.delivered_at = datetime.now()
        db.commit()
    
    return {"success": True}

@router.post("/messages/read-all/{sender_id}")
async def mark_all_messages_read(
    sender_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Mark all messages from a sender as read"""
    
    updated = db.query(Chat).filter(
        Chat.to_id == user.id,
        Chat.user_id == sender_id,
        Chat.is_read == False
    ).update({"is_read": True})
    
    db.commit()
    
    return {"message": f"{updated} messages marked as read"}


@router.get("/alerts")
async def get_alerts(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Get alerts for current user"""
    
    query = db.query(Alert).filter(
        Alert.user_id == user.id
    )
    
    if type:
        query = query.filter(Alert.type == type)
    
    total = query.count()
    
    # ✅ Unread first, then most recent
    alerts = query.order_by(
        Alert.is_read.asc(),
        desc(Alert.created_at)
    ).offset((page - 1) * limit).limit(limit).all()
    
    unread_count = db.query(Alert).filter(
        Alert.user_id == user.id,
        Alert.is_read == False
    ).count()
    
    data = []
    for alert in alerts:
        applicant = db.query(User).filter(User.id == alert.applicant_id).first()
        job = db.query(Job).filter(Job.id == alert.job_id).first() if alert.job_id else None
        
        data.append({
            "id": alert.id,
            "type": alert.type,
            "message": alert.message,
            "applicant_id": alert.applicant_id,
            "applicant_name": applicant.full_name if applicant else None,
            "applicant_avatar": applicant.profile_picture if applicant else None,
            "job_id": alert.job_id,
            "job_title": job.title if job else None,
            "is_read": alert.is_read,
            "read_at": alert.read_at.isoformat() if alert.read_at else None,
            "created_at": alert.created_at.isoformat() if alert.created_at else None,
        })
    
    return {
        "data": data,
        "total": total,
        "page": page,
        "limit": limit,
        "unread_count": unread_count,
        "has_more": (page * limit) < total
    }

# ==================== ALERTS / NOTIFICATIONS ====================

@router.get("/alerts")
async def get_alerts(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    type: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Get alerts for current user"""
    
    query = db.query(Alert).filter(
        Alert.user_id == user.id
    )
    
    if type:
        query = query.filter(Alert.type == type)
    
    total = query.count()
    alerts = query.order_by(desc(Alert.created_at))\
        .offset((page - 1) * limit)\
        .limit(limit)\
        .all()
    
    unread_count = db.query(Alert).filter(
        Alert.user_id == user.id,
        Alert.is_read == False
    ).count()
    
    data = []
    for alert in alerts:
        applicant = db.query(User).filter(User.id == alert.applicant_id).first()
        job = db.query(Job).filter(Job.id == alert.job_id).first() if alert.job_id else None
        
        data.append({
            "id": alert.id,
            "type": alert.type,
            "message": alert.message,
            "applicant_id": alert.applicant_id,
            "applicant_name": applicant.full_name if applicant else None,
            "applicant_avatar": applicant.profile_picture if applicant else None,
            "job_id": alert.job_id,
            "job_title": job.title if job else None,
            "is_read": alert.is_read,
            "read_at": alert.read_at.isoformat() if alert.read_at else None,
            "created_at": alert.created_at.isoformat() if alert.created_at else None,
        })
    
    return {
        "data": data,
        "total": total,
        "page": page,
        "limit": limit,
        "unread_count": unread_count,
        "has_more": (page * limit) < total
    }


@router.get("/alerts/unread-count")
async def get_unread_alert_count(
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Get only unread alert count"""
    
    count = db.query(Alert).filter(
        Alert.user_id == user.id,
        Alert.is_read == False
    ).count()
    
    return {"unread_count": count}


@router.post("/alerts/{alert_id}/read")
async def mark_alert_read(
    alert_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Mark an alert as read"""
    
    alert = db.query(Alert).filter(
        Alert.id == alert_id,
        Alert.user_id == user.id
    ).first()
    
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    
    alert.is_read = True
    alert.read_at = datetime.now()  # ✅ Add read timestamp
    db.commit()
    
    return {"message": "Alert marked as read"}


@router.post("/alerts/read-all")
async def mark_all_alerts_read(
    type: Optional[str] = None,
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Mark all alerts as read"""
    
    query = db.query(Alert).filter(
        Alert.user_id == user.id,
        Alert.is_read == False
    )
    
    if type:
        query = query.filter(Alert.type == type)
    
    count = query.update({
        "is_read": True,
        "read_at": datetime.now()  # ✅ Add read timestamp
    })
    db.commit()
    
    return {"message": f"{count} alerts marked as read"}


@router.delete("/alerts/{alert_id}")
async def delete_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Delete an alert"""
    
    alert = db.query(Alert).filter(
        Alert.id == alert_id,
        Alert.user_id == user.id
    ).first()
    
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    
    db.delete(alert)
    db.commit()
    
    return {"message": "Alert deleted"}
    
#============================== Online ======================
@router.get("/users/online")
async def get_online_users(
    search: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Get online users from the same school (with auto-cleanup)"""
    
    # ✅ Auto-cleanup: Set offline if last_seen > 1 hour ago
    from datetime import datetime, timedelta
    timeout_threshold = datetime.utcnow() - timedelta(hours=1)
    
    stale_users = db.query(User).filter(
        User.is_online == True,
        User.last_seen < timeout_threshold
    ).all()
    
    for stale in stale_users:
        stale.is_online = False
    
    if stale_users:
        db.commit()
        print(f"🕐 Auto-set {len(stale_users)} stale users offline")
    
    # Now get online users
    query = db.query(User).filter(
        User.id != user.id,
        User.is_online == True,
        User.school_id == user.school_id
    )
    
    if search:
        query = query.filter(
            User.full_name.ilike(f"%{search}%") | 
            User.username.ilike(f"%{search}%")
        )
    
    users = query.limit(50).all()
    
    data = []
    for u in users:
        data.append({
            "id": u.id,
            "full_name": u.full_name,
            "username": u.username,
            "dp_url": u.profile_picture,
            "is_online": u.is_online,
            "last_seen": u.last_seen.isoformat() if u.last_seen else None,
        })
    
    return {"data": data}


@router.post("/users/online/ping")
async def ping_online(
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Update user's online status"""
    
    db.query(User).filter(User.id == user.id).update({
        "is_online": True,
        "last_seen": func.now()
    })
    db.commit()
    
    return {"status": "online"}


@router.post("/users/offline")
async def set_offline(
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """Set user as offline"""
    
    db.query(User).filter(User.id == user.id).update({
        "is_online": False,
        "last_seen": func.now()
    })
    db.commit()
    
    return {"status": "offline"}
    
@router.get("/school-users")
async def get_school_users(
    search: Optional[str] = Query(None, description="Search query for users"),
    school_id: Optional[int] = Query(None, description="School ID filter"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get users from the same school as the current user.
    Supports search by name or email.
    """

    target_school_id = school_id or current_user.school_id
    
    if not target_school_id:
        return {"data": []}

    query = db.query(User).filter(
        User.school_id == target_school_id,
        User.id != current_user.id,
        User.is_active == True,
        User.approval_status == 'approved'
    )

    if search and search.strip():
        search_term = f"%{search.strip()}%"
        query = query.filter(
            (User.full_name.ilike(search_term)) |
            (User.username.ilike(search_term)) |
            (User.email.ilike(search_term))
        )
    
    # Get users with their role-specific profiles
    users = query.limit(20).all()
    
    # Get school info
    school = db.query(School).filter(School.id == target_school_id).first()
    
    # Format response with online status from the User model
    data = []
    for user in users:
        user_data = {
            "id": user.id,
            "full_name": user.full_name,
            "name": user.full_name,
            "username": user.username,
            "email": user.email,
            "dp_url": user.profile_picture,
            "avatar": user.profile_picture,
            "role": user.role,
            "is_online": user.is_online if user.is_online is not None else False,
            "last_seen": user.last_seen.isoformat() if user.last_seen else None,
            "school_id": user.school_id,
            "school_name": school.school_name if school else None,
        }
        
        # Add role-specific info
        if user.role == "student":
            student = db.query(Student).filter(Student.user_id == user.id).first()
            if student:
                user_data["admission_number"] = student.admission_number
                user_data["class_name"] = student.class_name
                user_data["gender"] = student.gender
                user_data["class_id"] = student.class_id
                
        elif user.role == "teacher":
            teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
            if teacher:
                user_data["subject"] = teacher.subject
                user_data["qualification"] = teacher.qualification
                user_data["years_of_experience"] = teacher.years_of_experience
                
        elif user.role == "parent":
            parent = db.query(Parent).filter(Parent.user_id == user.id).first()
            if parent:
                user_data["relation_type"] = parent.relation_type
                # Get children info
                parent_students = db.query(ParentStudent).filter(
                    ParentStudent.parent_id == parent.id
                ).all()
                children = []
                for ps in parent_students:
                    student = db.query(Student).filter(Student.id == ps.student_id).first()
                    if student:
                        child_user = db.query(User).filter(User.id == student.user_id).first()
                        children.append({
                            "id": student.user_id,
                            "name": child_user.full_name if child_user else None,
                            "class": student.class_name,
                            "admission_number": student.admission_number
                        })
                user_data["children"] = children
                
        elif user.role == "worker":
            worker = db.query(Worker).filter(Worker.user_id == user.id).first()
            if worker:
                user_data["department"] = worker.department
                user_data["role_title"] = worker.role_title
                user_data["employment_type"] = worker.employment_type
                
        data.append(user_data)
    
    return {"data": data}
    
@router.post("/update-result")
async def update_results(
    user = Depends(get_current_user),
    db: Session = Depends(get_db),
    student_id: int = Query(..., description="Student Id"),
    cat1: str = Query(..., description="Cat 1"),
    cat2: str = Query(..., description="Cat 2"),
    exam: str = Query(..., description="Exam"),
    term: str = Query(..., description="Term"),
    subject: str = Query(..., description="Subject"),
):
    if user.role != "teacher":
        raise HTTPException(status_code=403, detail="Not Authorized")
        
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        return {"message": f"Student with id {student_id} not found"}
    student_id = student.user_id
    
    performance = db.query(StudentPerformance).filter(
        StudentPerformance.student_id==student_id,
        StudentPerformance.subject==subject,
        StudentPerformance.term==term
    ).first()
    
    if not performance:
        return {"message": "No results found"}
        
    performance.cat1 = cat1
    performance.cat2 = cat2
    performance.exan = exam
    
    db.commit()
    db.refresh(performance)
    
    return {"message": "Success"}
    
@router.get("/applicants")
async def get_applicants(
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
    role: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    job_id: Optional[int] = Query(None),
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    try:
        query = db.query(Alert).filter(
            Alert.user_id == user.id,
            Alert.type == "application"
        )
        
        if job_id:
            query = query.filter(Alert.job_id == job_id)
        
        all_alerts = query.order_by(Alert.created_at.desc()).all()
        
        if not all_alerts:
            return {
                "success": True,
                "data": [],
                "meta": {
                    "current_page": page,
                    "last_page": 1,
                    "per_page": per_page,
                    "total": 0
                }
            }
        
        all_applicants = []
        for alert in all_alerts:
            applicant_user = db.query(User).filter(User.id == alert.applicant_id).first()
            if not applicant_user:
                continue
            
            applicant_record = db.query(Applicant).filter(
                Applicant.user_id == alert.applicant_id,
                Applicant.job_id == alert.job_id
            ).first()
            
            application_status = "pending"
            if applicant_record and hasattr(applicant_record, 'status') and applicant_record.status:
                application_status = applicant_record.status
            
            # Apply filters
            if role and role != "all" and applicant_user.role != role:
                continue
            
            if status and status != "all":
                if status != application_status:
                    continue
            
            if search:
                search_lower = search.lower()
                if (search_lower not in (applicant_user.full_name or "").lower() and
                    search_lower not in (applicant_user.email or "").lower() and
                    search_lower not in (applicant_user.phone or "").lower() and
                    search_lower not in (applicant_user.username or "").lower()):
                    continue
            
            job = db.query(Job).filter(Job.id == alert.job_id).first()
            
            documents = db.query(ApplicationDocument).filter(
                ApplicationDocument.alert_id == alert.id
            ).all()
            
            docs_list = [
                {
                    "id": doc.id,
                    "file_name": doc.file_name,
                    "file_path": doc.file_path,
                    "file_type": doc.file_type,
                    "file_size": doc.file_size,
                    "created_at": doc.created_at.isoformat() if doc.created_at else None
                }
                for doc in documents
            ]
            
            applicant_data = {
                "id": alert.id,
                "applicant_id": alert.applicant_id,
                "name": applicant_user.full_name,
                "username": applicant_user.username,
                "email": applicant_user.email,
                "phone": applicant_user.phone,
                "role": applicant_user.role,
                "status": application_status,
                "profile_picture": applicant_user.profile_picture,
                "avatar": applicant_user.profile_picture,
                "is_active": applicant_user.is_active,
                "created_at": alert.created_at.isoformat() if alert.created_at else None,
                "applied_date": alert.created_at.strftime("%Y-%m-%d") if alert.created_at else None,
                "job_id": alert.job_id,
                "job_title": job.title if job else None,
                "job": {
                    "id": job.id,
                    "title": job.title,
                    "description": job.description,
                    "location": job.location,
                    "type": job.type,
                    "salary": job.salary,
                    "requirements": job.requirements,
                } if job else None,
                "title": alert.title,
                "message": alert.message,
                "is_read": alert.is_read,
                "documents": docs_list,
            }
            
            if applicant_user.role == "teacher":
                teacher = db.query(Teacher).filter(Teacher.user_id == applicant_user.id).first()
                if teacher:
                    applicant_data["subject"] = teacher.subject
                    applicant_data["qualification"] = teacher.qualification
                    applicant_data["experience"] = teacher.years_of_experience
            elif applicant_user.role == "parent":
                parent = db.query(Parent).filter(Parent.user_id == applicant_user.id).first()
                if parent and parent.student_id:
                    student = db.query(Student).filter(Student.id == parent.student_id).first()
                    if student:
                        student_user = db.query(User).filter(User.id == student.user_id).first()
                        applicant_data["child_name"] = student_user.full_name if student_user else None
                        applicant_data["child_grade"] = student.class_name
            
            all_applicants.append(applicant_data)
        
        total = len(all_applicants)
        last_page = max(1, (total + per_page - 1) // per_page)
        current_page = min(page, last_page)
        start_idx = (current_page - 1) * per_page
        end_idx = start_idx + per_page
        
        paginated_data = all_applicants[start_idx:end_idx]
        
        return {
            "success": True,
            "data": paginated_data,
            "meta": {
                "current_page": current_page,
                "last_page": last_page,
                "per_page": per_page,
                "total": total
            }
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load applicants: {str(e)}"
        )

@router.get("/jobs/posted")
async def get_posted_jobs(
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """
    Get all jobs with applicant counts for the current user
    """
    try:
        # Get all application alerts for this user
        alerts = db.query(Alert).filter(
            Alert.user_id == user.id,
            Alert.type == "application"
        ).all()
        
        # Extract unique job IDs
        job_ids = set()
        for alert in alerts:
            if alert.job_id:
                job_ids.add(alert.job_id)
        
        # Get job details
        jobs_data = []
        for job_id in job_ids:
            job = db.query(Job).filter(Job.id == job_id).first()
            if not job:
                continue
            
            # Count applicants for this job
            applicant_count = db.query(Alert).filter(
                Alert.user_id == user.id,
                Alert.type == "application",
                Alert.job_id == job_id
            ).count()
            
            jobs_data.append({
                "id": job.id,
                "title": job.title,
                "applicants_count": applicant_count,
                "description": job.description,
                "location": job.location,
                "type": job.type,
                "salary": job.salary,
                "requirements": job.requirements,
                "deadline": job.deadline,
                "is_gig": job.is_gig,
                "working_hours": job.working_hours,
                "duration": job.duration,
                "amount_type": job.amount_type,
                "created_at": job.created_at.isoformat() if job.created_at else None
            })
        
        # Sort by created_at descending
        jobs_data.sort(key=lambda x: x['created_at'] or '', reverse=True)
        
        return {
            "success": True,
            "data": jobs_data
        }
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load jobs: {str(e)}"
        )


@router.post("/applicants/{alert_id}/approve")
async def approve_applicant(
    alert_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    try:
        alert = db.query(Alert).filter(
            Alert.id == alert_id,
            Alert.user_id == user.id,
            Alert.type == "application"
        ).first()
        
        if not alert:
            raise HTTPException(status_code=404, detail="Application not found")
        
        # Update Applicant record
        applicant_record = db.query(Applicant).filter(
            Applicant.user_id == alert.applicant_id,
            Applicant.job_id == alert.job_id
        ).first()
        
        if applicant_record:
            applicant_record.status = "approved"
        
        # ✅ DON'T change alert.type - keep as "application"
        alert.is_read = True
        alert.read_at = datetime.utcnow()
        
        # ✅ Send notification to applicant
        job = db.query(Job).filter(Job.id == alert.job_id).first()
        applicant_notification = Alert(
            user_id=alert.applicant_id,
            applicant_id=user.id,
            job_id=alert.job_id,
            type="message",
            title="Application Accepted",
            message=f"Congratulations! Your application for '{job.title if job else 'the position'}' has been accepted.",
            icon="check_circle",
            is_read=False,
            created_at=datetime.utcnow()
        )
        db.add(applicant_notification)
        
        db.commit()
        return {"success": True, "message": "Applicant approved successfully"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed: {str(e)}")


@router.post("/applicants/{alert_id}/reject")
async def reject_applicant(
    alert_id: int,
    reason: Optional[str] = None,
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    try:
        alert = db.query(Alert).filter(
            Alert.id == alert_id,
            Alert.user_id == user.id,
            Alert.type == "application"
        ).first()
        
        if not alert:
            raise HTTPException(status_code=404, detail="Application not found")
        
        # Update Applicant record
        applicant_record = db.query(Applicant).filter(
            Applicant.user_id == alert.applicant_id,
            Applicant.job_id == alert.job_id
        ).first()
        
        if applicant_record:
            applicant_record.status = "rejected"
        
        # ✅ DON'T change alert.type - keep as "application"
        alert.is_read = True
        alert.read_at = datetime.utcnow()
        
        # ✅ Send notification to applicant
        job = db.query(Job).filter(Job.id == alert.job_id).first()
        rejection_message = f"Your application for '{job.title if job else 'the position'}' has been rejected."
        if reason:
            rejection_message += f" Reason: {reason}"
        
        applicant_notification = Alert(
            user_id=alert.applicant_id,
            applicant_id=user.id,
            job_id=alert.job_id,
            type="message",
            title="Application Rejected",
            message=rejection_message,
            icon="cancel",
            is_read=False,
            created_at=datetime.utcnow()
        )
        db.add(applicant_notification)
        
        db.commit()
        return {"success": True, "message": "Applicant rejected successfully"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed: {str(e)}")


@router.post("/applicants/{alert_id}/reject")
async def reject_applicant(
    alert_id: int,
    reason: Optional[str] = None,
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    """
    Reject an applicant using the alert ID
    """
    try:
        # Get the application alert
        alert = db.query(Alert).filter(
            Alert.id == alert_id,
            Alert.user_id == user.id,
            Alert.type == "application"
        ).first()
        
        if not alert:
            raise HTTPException(
                status_code=404,
                detail="Application not found"
            )
        
        # Get job info
        job = db.query(Job).filter(Job.id == alert.job_id).first()
        
        # Update the Applicant record status
        applicant_record = db.query(Applicant).filter(
            Applicant.user_id == alert.applicant_id,
            Applicant.job_id == alert.job_id
        ).first()
        
        if applicant_record:
            applicant_record.status = "rejected"
        else:
            applicant_record = Applicant(
                user_id=alert.applicant_id,
                job_id=alert.job_id,
                status="rejected"
            )
            db.add(applicant_record)
        
        # Update the alert
        alert.is_read = True
        alert.read_at = datetime.utcnow()
        
        rejection_message = f"Your application for '{job.title if job else 'the position'}' has been rejected."
        if reason:
            rejection_message += f" Reason: {reason}"
        
        applicant_notification = Alert(
            user_id=alert.applicant_id,
            applicant_id=user.id,
            job_id=alert.job_id,
            type="message",
            title="Application Rejected",
            message=rejection_message,
            icon="cancel",
            is_read=False,
            created_at=datetime.utcnow()
        )
        db.add(applicant_notification)
        
        db.commit()
        
        return {
            "success": True,
            "message": "Applicant rejected successfully"
        }
        
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to reject applicant: {str(e)}"
        )
        
@router.post("/applicants/{applicant_id}/message")
async def message_applicant(
    applicant_id: int,
    message: str,
    db: Session = Depends(get_db),
    user = Depends(get_current_user),
):
    try:
        applicant_user = db.query(User).filter(User.id == applicant_id).first()
        if not applicant_user:
            raise HTTPException(status_code=404, detail="Applicant not found")
        
        new_alert = Alert(
            user_id=applicant_id,
            applicant_id=user.id,
            type="message",
            title="Message from Employer",
            message=message,
            icon="message",
            is_read=False,
            created_at=datetime.utcnow()
        )
        db.add(new_alert)
        db.commit()
        
        return {"success": True, "message": "Message sent successfully"}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to send message: {str(e)}")
        
@router.post("/mpesa/confirmation")
async def mpesa_confirmation(data: dict):
    print("🔥 M-PESA PAYMENT RECEIVED")
    print(data)

    # process payment...
    
    return {"ResultCode": 0, "ResultDesc": "Accepted"}
    
@router.post("/fees/update")
async def Update_fees(
    admission_number: str = Form(...),
    amount: int = Form(...),
    payment_method: str = Form(...),
    payment_date: str = Form(...),
    receipt_number: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if user.role not in ("admin", "school"):
        raise HTTPException(status_code=403, detail="Not Authorized")

    # ── 1. Find student ──
    student = db.query(Student).filter(
        Student.admission_number == admission_number,
        Student.school_id == user.school_id,
    ).first()
    if not student:
        raise HTTPException(404, f"Student {admission_number} not found")

    try:
        payment_date_obj = datetime.strptime(payment_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD")

    # ── 2. Ensure every term this student should be billed for exists ──
    fees = get_or_create_fee_terms(student.user_id, db, user.school_id)
    if not fees:
        raise HTTPException(400, "No fee structure set for this school yet")

    # ── 3. Sort terms oldest → newest ──
    fees_sorted = sorted(fees, key=get_term_sort_key)

    # ── 4. Create the FeeTransaction ──
    #    fee_id is temporarily the oldest unpaid term (or the oldest term).
    #    We'll re-point it after the waterfall to the first term the money
    #    actually touched.
    fallback_fee = next(
        (f for f in fees_sorted if (f.balance or 0) > 0),
        fees_sorted[0],
    )

    transaction = FeeTransaction(
        fee_id=fallback_fee.id,
        student_id=student.user_id,
        amount=amount,
        payment_provider=payment_method,
        transaction_reference=receipt_number
            or f"TXN-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        payment_date=payment_date_obj,
        created_at=datetime.now(),
    )
    db.add(transaction)

    # ── 5. Waterfall: apply the payment oldest → newest ──
    remaining = amount
    applied_to_fee_id = None

    for fee in fees_sorted:
        if remaining <= 0:
            break

        balance = (fee.amount or 0) - (fee.paid or 0)
        if balance <= 0:
            # This term is already fully paid or has credit; skip
            continue

        apply = min(remaining, balance)
        fee.paid = (fee.paid or 0) + apply
        fee.balance = (fee.amount or 0) - fee.paid

        if fee.paid <= 0:
            fee.status = "pending"
        elif fee.paid < fee.amount:
            fee.status = "partial"
        else:
            fee.status = "paid"

        if applied_to_fee_id is None:
            applied_to_fee_id = fee.id

        remaining -= apply

    # ── 6. Leftover becomes an overpayment on the newest term ──
    #    Stored as `paid > amount`, so `balance` goes negative.
    #    That negative balance IS the credit for the student.
    if remaining > 0 and fees_sorted:
        newest = fees_sorted[-1]
        newest.paid = (newest.paid or 0) + remaining
        newest.balance = (newest.amount or 0) - newest.paid
        # Keep status "paid" — balance carries the credit info
        newest.status = "paid"
        if applied_to_fee_id is None:
            applied_to_fee_id = newest.id
        remaining = 0

    # ── 7. Point the transaction at the first term the money landed on ──
    if applied_to_fee_id:
        transaction.fee_id = applied_to_fee_id

    db.commit()

    # ── 8. Compute total overpayment across all terms ──
    #    Any term whose balance < 0 contributes to the student's credit.
    fees_all = (
        db.query(Fee)
        .filter(Fee.student_id == student.user_id)
        .order_by(Fee.academic_year, Fee.term_number)
        .all()
    )

    overpayment = sum(
        -(f.balance or 0) for f in fees_all if (f.balance or 0) < 0
    )

    terms_response = [
        {
            "term": f.term_name,
            "term_number": f.term_number,
            "academic_year": f.academic_year,
            "total": f.amount,
            "paid": f.paid or 0,
            "balance": f.balance if f.balance is not None else (f.amount - (f.paid or 0)),
            "status": f.status,
        }
        for f in fees_all
    ]

    return {
        "success": True,
        "message": f"Payment of KES {amount} recorded for {admission_number}",
        "data": {
            "transaction_id": transaction.id,
            "student_id": student.user_id,
            "amount": amount,
            "payment_method": payment_method,
            "payment_date": payment_date,
            "receipt_number": transaction.transaction_reference,
            "overpayment": overpayment,
            "terms": terms_response,
        },
    }

@router.get("/transactions")
async def get_transactions(
    school_id: int | None = None,
    limit: int = 20,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role not in ("admin", "school"):
        raise HTTPException(status_code=403, detail="Not Authorized")

    # Use the school from the JWT; allow override only for super-admin
    effective_school_id = user.school_id
    if school_id is not None and school_id != user.school_id:
        raise HTTPException(status_code=403, detail="Not your school")

    limit = max(1, min(limit, 500))

    rows = (
        db.query(FeeTransaction, User)
        .join(User, User.id == FeeTransaction.student_id)
        .filter(User.school_id == effective_school_id)
        .order_by(FeeTransaction.created_at.desc())
        .limit(limit)
        .all()
    )

    data = []
    for txn, student in rows:
        data.append({
            "id": txn.id,
            "fee_id": txn.fee_id,
            "student_id": txn.student_id,
            "student_name": getattr(student, "full_name", None)
                             or getattr(student, "name", None),
            "amount": txn.amount,
            "method": txn.payment_provider,
            "payment_provider": txn.payment_provider,
            "reference": txn.transaction_reference,
            "transaction_reference": txn.transaction_reference,
            "date": (txn.payment_date or txn.created_at).isoformat()
                     if (txn.payment_date or txn.created_at) else None,
            "payment_date": txn.payment_date.isoformat() if txn.payment_date else None,
            "created_at": txn.created_at.isoformat() if txn.created_at else None,
            "type": "credit",
            "status": "Completed",
        })

    return {"data": data, "total": len(data), "limit": limit}
    

@router.get("/students/{student_id}/transactions")
async def get_student_transactions(
    student_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Get all fee transactions for a specific student
    """
    # Verify student exists
    student = db.query(User).filter(User.id == student_id).first()
    if not student:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Student not found"
        )
    
    # Get transactions
    transactions = db.query(FeeTransaction).filter(
        FeeTransaction.student_id == student_id
    ).order_by(FeeTransaction.payment_date.desc()).all()
    
    # Format response
    return {
        "data": [
            {
                "id": tx.id,
                "fee_id": tx.fee_id,
                "student_id": tx.student_id,
                "amount": tx.amount,
                "payment_provider": tx.payment_provider,
                "transaction_reference": tx.transaction_reference,
                "payment_date": tx.payment_date.isoformat() if tx.payment_date else None,
                "created_at": tx.created_at.isoformat() if tx.created_at else None,
                "status": "Completed"  # You might want to add this field to your model
            }
            for tx in transactions
        ]
    }

def propagate_structure_change(db, structure: FeeStructure):
    """
    Called after a fee_structure is created or edited.
    Updates every existing Fee row for that school+year+term so
    Fee.amount matches FeeStructure.amount, then recalculates balance/status.

    Does NOT touch Fee.paid.
    """
    affected = (
        db.query(Fee)
        .join(Student, Student.user_id == Fee.student_id)
        .filter(
            Student.school_id == structure.school_id,
            Fee.academic_year == structure.academic_year,
            Fee.term_number == structure.term_number,
        )
        .all()
    )

    for fee in affected:
        fee.amount = structure.amount
        fee.term_name = structure.term_name
        fee.balance = structure.amount - (fee.paid or 0)
        if (fee.paid or 0) <= 0:
            fee.status = "pending"
        elif fee.paid < structure.amount:
            fee.status = "partial"
        else:
            fee.status = "paid"
    return len(affected)
    
@router.post("/fee-structure")
async def create_fee_structure(
    data: FeeStructureCreate,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Create or update fee structure for a term"""
    if user.role not in ['admin', 'school']:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    # Check if structure already exists
    existing = db.query(FeeStructure).filter(
        FeeStructure.school_id == user.school_id,
        FeeStructure.academic_year == data.academic_year,
        FeeStructure.term_number == data.term_number
    ).first()
    
    if existing:
        # Update existing
        existing.amount = data.amount
        existing.term_name = f"Term {data.term_number} - {data.academic_year}"
        db.add(existing)
        updated = propagate_structure_change(db, existing)
    else:
        # Create new
        structure = FeeStructure(
            school_id=user.school_id,
            academic_year=data.academic_year,
            term_number=data.term_number,
            term_name=f"Term {data.term_number} - {data.academic_year}",
            amount=data.amount
        )
        db.add(structure)
        updated = propagate_structure_change(db, structure)
    
    db.commit()
    
    return {
        "success": True,
        "message": "Fee structure updated successfully"
    }

@router.get("/fee-structure")
async def get_fee_structure(
    academic_year: Optional[int] = None,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Get fee structure for a school"""
    if user.role not in ['admin', 'school']:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    query = db.query(FeeStructure).filter(
        FeeStructure.school_id == user.school_id
    )
    
    if academic_year:
        query = query.filter(FeeStructure.academic_year == academic_year)
    else:
        # Default to current year
        query = query.filter(FeeStructure.academic_year == datetime.now().year)
    
    structures = query.order_by(FeeStructure.term_number.asc()).all()
    
    return {
        "data": [
            {
                "id": s.id,
                "academic_year": s.academic_year,
                "term_number": s.term_number,
                "term_name": s.term_name,
                "amount": s.amount
            }
            for s in structures
        ]
    }

@router.put("/fee-structure/{structure_id}")
async def update_fee_structure(
    structure_id: int,
    data: FeeStructureUpdate,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Update a specific fee structure"""
    if user.role not in ['admin', 'school']:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    structure = db.query(FeeStructure).filter(
        FeeStructure.id == structure_id,
        FeeStructure.school_id == user.school_id
    ).first()
    
    if not structure:
        raise HTTPException(status_code=404, detail="Fee structure not found")
    
    structure.amount = data.amount
    db.add(structure)
    db.commit()
    
    return {
        "success": True,
        "message": "Fee structure updated"
    }

@router.delete("/fee-structure/{structure_id}")
async def delete_fee_structure(
    structure_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Delete a fee structure"""
    if user.role not in ['admin', 'school']:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    structure = db.query(FeeStructure).filter(
        FeeStructure.id == structure_id,
        FeeStructure.school_id == user.school_id
    ).first()
    
    if not structure:
        raise HTTPException(status_code=404, detail="Fee structure not found")
    
    db.delete(structure)
    db.commit()
    
    return {
        "success": True,
        "message": "Fee structure deleted"
    }
    
@router.get("/debtors")
async def get_debtors(
    school_id: Optional[int] = Query(None),
    limit: int = Query(10),
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Get list of students with outstanding fee balances"""
    if user.role not in ['admin', 'school']:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    # Get school_id from user if not provided
    school_id = user.school_id
    
    # Single query to get all debtors with balances
    debtors_query = db.query(
        Student.user_id.label('student_id'),
        Student.admission_number,
        Student.class_name,
        User.full_name.label('student_name'),
        func.coalesce(func.sum(case((Fee.status != "upcoming", Fee.amount), else_=0)), 0).label('total_fees'),
        func.coalesce(func.sum(case((Fee.status != "upcoming", Fee.paid), else_=0)), 0).label('total_paid_from_fees'),
    ).join(
        User, Student.user_id == User.id
    ).outerjoin(
        Fee, Fee.student_id == Student.user_id
    ).filter(
        Student.school_id == school_id
    ).group_by(
        Student.user_id,
        Student.admission_number,
        Student.class_name,
        User.full_name
    ).having(
        func.coalesce(func.sum(case((Fee.status != "upcoming", Fee.amount), else_=0)), 0) >
        func.coalesce(func.sum(case((Fee.status != "upcoming", Fee.paid), else_=0)), 0)
    ).order_by(
        (func.coalesce(func.sum(case((Fee.status != "upcoming", Fee.amount), else_=0)), 0) -
         func.coalesce(func.sum(case((Fee.status != "upcoming", Fee.paid), else_=0)), 0)).desc()
    ).limit(limit)
    
    debtors = debtors_query.all()
    
    # Build response
    result = []
    for debtor in debtors:
        balance = debtor.total_fees - debtor.total_paid_from_fees
        result.append({
            'student_id': debtor.student_id,
            'student_name': debtor.student_name or 'Unknown',
            'admission_number': debtor.admission_number,
            'class_name': debtor.class_name,
            'balance': balance,
        })
    
    return {
        'data': result,
        'total': len(result)
    }
    
@router.get("/parent/student-fees/{student_id}")
async def get_student_fees_for_parent(
    student_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Get fee details for a student (parent access)"""
    # Verify parent has access to this student
    parent = db.query(Parent).filter(Parent.user_id == user.id).first()
    if not parent:
        raise HTTPException(status_code=403, detail="Not authorized")
        
    student = db.query(Student).filter(Student.id==student_id).first()
    student_id = student.user_id
    
    # Check if this student is linked to this parent
    link = db.query(ParentStudent).filter(
        ParentStudent.parent_id == parent.id,
        ParentStudent.student_id == student_id
    ).first()
    
    if not link:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Get fee details
    fees = db.query(Fee).filter(Fee.student_id == student_id).all()
    transactions = db.query(FeeTransaction).filter(
        FeeTransaction.student_id == student_id,
        FeeTransaction.amount > 0
    ).all()
    
    total_fees = sum(f.amount for f in fees)
    total_paid = sum(tx.amount for tx in transactions)
    
    return {
        'total': total_fees,
        'paid': total_paid,
        'balance': max(0, total_fees - total_paid),
        'overpaid': max(0, total_paid - total_fees),
    }
    
@router.get("/fees/statement/{student_id}")
async def get_fee_statement(
    student_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Get fee statement for a student"""
    if user.role not in ['admin', 'school', 'parent']:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    user_id = student.user_id
    current_year = datetime.now().year
    current_term_num, current_term_name, _ = determine_current_term()
    
    # Get all transactions
    transactions = db.query(FeeTransaction).filter(
        FeeTransaction.student_id == user_id
    ).order_by(FeeTransaction.payment_date.asc()).all()
    
    # Calculate total paid
    total_paid = sum(tx.amount for tx in transactions if tx.amount > 0)
    
    # Get fee structure
    fee_structures = db.query(FeeStructure).filter(
        FeeStructure.school_id == student.school_id,
        FeeStructure.academic_year == current_year
    ).order_by(FeeStructure.term_number.asc()).all()
    
    # Calculate total fees
    total_fees = sum(s.amount for s in fee_structures) if fee_structures else 0
    
    # Allocate payments to terms sequentially
    remaining_payment = total_paid
    terms = []
    
    for structure in fee_structures:
        term_amount = structure.amount
        term_paid = 0
        
        if remaining_payment >= term_amount:
            # Fully paid
            term_paid = term_amount
            remaining_payment -= term_amount
            term_status = "paid"
        elif remaining_payment > 0:
            # Partially paid
            term_paid = remaining_payment
            remaining_payment = 0
            term_status = "partial"
        else:
            # No payment for this term
            term_paid = 0
            if structure.term_number < current_term_num:
                term_status = "overdue"
            elif structure.term_number == current_term_num:
                term_status = "pending"
            else:
                term_status = "upcoming"
        
        term_balance = max(0, term_amount - term_paid)
        
        terms.append({
            'term_number': structure.term_number,
            'term_name': structure.term_name,
            'amount': term_amount,
            'paid': term_paid,
            'balance': term_balance,
            'status': term_status,
        })
    
    overpaid = max(0, remaining_payment)
    balance = max(0, total_fees - total_paid)
    
    return {
        'student': {
            'id': student.id,
            'user_id': user_id,
            'name': student.user.full_name if student.user else 'Unknown',
            'admission_number': student.admission_number,
            'class_name': student.class_name,
        },
        'total': total_fees,
        'paid': total_paid,
        'balance': balance,
        'overpaid': overpaid,
        'transactions': [
            {
                'id': tx.id,
                'fee_id': tx.fee_id,
                'amount': tx.amount,
                'payment_provider': tx.payment_provider,
                'transaction_reference': tx.transaction_reference,
                'payment_date': tx.payment_date.isoformat() if tx.payment_date else None,
                'created_at': tx.created_at.isoformat() if tx.created_at else None,
            }
            for tx in transactions
        ],
        'terms': terms,
    }
    
@router.get("/fees/receipt/{student_id}")
async def get_fee_receipt(
    student_id: int,
    db: Session = Depends(get_db),
    user = Depends(get_current_user)
):
    """Get fee receipt for a student"""
    if user.role not in ['admin', 'school', 'parent']:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    # Find the student first
    student = db.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")
    
    user_id = student.user_id
    
    # Get all transactions
    transactions = db.query(FeeTransaction).filter(
        FeeTransaction.student_id == user_id,
        FeeTransaction.amount > 0
    ).order_by(FeeTransaction.payment_date.desc()).all()
    
    return {
        'student': {
            'id': student.id,
            'name': student.user.full_name if student.user else 'Unknown',
            'admission_number': student.admission_number,
            'class_name': student.class_name,
        },
        'transactions': [
            {
                'id': tx.id,
                'amount': tx.amount,
                'payment_provider': tx.payment_provider,
                'transaction_reference': tx.transaction_reference,
                'payment_date': tx.payment_date.isoformat() if tx.payment_date else None,
                'created_at': tx.created_at.isoformat() if tx.created_at else None,
            }
            for tx in transactions
        ],
        'total_paid': sum(tx.amount for tx in transactions),
    }
    
@router.get("/{schoolId}/classes")
def get_classes(schoolId: int, db = Depends(get_db)):
    classes = db.query(Class).filter(Class.school_id == schoolId).all()
    
    class_data = []
    for cls in classes:
        class_data.append({
            "id": cls.id,
            "class_name": cls.name
        })
    
    return {"classes": class_data}
    
@router.get("/groups", response_model=dict)
async def get_groups(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        # Get groups where user is creator or member
        groups = db.query(Group).filter(
            (Group.created_by == current_user.id) | 
            (Group.id.in_(
                db.query(GroupMember.group_id).filter(GroupMember.user_id == current_user.id)
            ))
        ).all()
        
        result = []
        for group in groups:
            member_count = db.query(GroupMember).filter(GroupMember.group_id == group.id).count()
            result.append({
                'id': group.id,
                'name': group.name,
                'description': group.description,
                'group_type': group.group_type,
                'class_name': group.class_name,
                'created_by': group.created_by,
                'creator_role': group.creator_role,
                'avatar': group.avatar,
                'created_at': group.created_at.isoformat() if group.created_at else None,
                'member_count': member_count,
            })
        
        return {'data': result, 'total': len(result)}
    except Exception as e:
        print(f"❌ Error getting groups: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Create a new group
@router.post("/groups", response_model=dict)
async def create_group(
    group_data: GroupCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        # Create the group
        new_group = Group(
            name=group_data.name,
            description=group_data.description,
            group_type=group_data.type,
            class_name=group_data.class_name,
            created_by=group_data.created_by or current_user.id,
            creator_role=group_data.creator_role or current_user.role,
            school_id=current_user.school_id,
        )
        db.add(new_group)
        db.flush()  # Get the group ID
        
        # Add creator as admin member
        creator_member = GroupMember(
            group_id=new_group.id,
            user_id=new_group.created_by,
            role='admin',
        )
        db.add(creator_member)
        
        # Add members based on type
        if group_data.type == 'individual':
            # Add individual members
            for user_id in group_data.members:
                if user_id != new_group.created_by:  # Skip creator
                    member = GroupMember(
                        group_id=new_group.id,
                        user_id=user_id,
                        role='member',
                    )
                    db.add(member)
        elif group_data.type == 'class':
            # Get all students in the class
            students = db.query(Student).filter(
                Student.class_name == group_data.class_name,
                Student.school_id == current_user.school_id,
            ).all()
            
            for student in students:
                if student.user_id != new_group.created_by:
                    member = GroupMember(
                        group_id=new_group.id,
                        user_id=student.user_id,
                        role='member',
                    )
                    db.add(member)
        
        db.commit()
        db.refresh(new_group)
        
        # Get member count
        member_count = db.query(GroupMember).filter(GroupMember.group_id == new_group.id).count()
        
        return {
            'success': True,
            'message': 'Group created successfully',
            'data': {
                'id': new_group.id,
                'name': new_group.name,
                'description': new_group.description,
                'group_type': new_group.group_type,
                'class_name': new_group.class_name,
                'created_by': new_group.created_by,
                'creator_role': new_group.creator_role,
                'member_count': member_count,
            }
        }
    except Exception as e:
        db.rollback()
        print(f"❌ Error creating group: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Get group members
@router.get("/groups/{group_id}/members", response_model=dict)
async def get_group_members(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        members = db.query(GroupMember, User).join(
            User, GroupMember.user_id == User.id
        ).filter(GroupMember.group_id == group_id).all()
        
        result = []
        for member, user in members:
            result.append({
                'id': member.id,
                'user_id': user.id,
                'name': user.full_name,
                'email': user.email,
                'role': member.role,
                'joined_at': member.joined_at.isoformat() if member.joined_at else None,
            })
        
        return {'data': result, 'total': len(result)}
    except Exception as e:
        print(f"❌ Error getting group members: {e}")
        raise HTTPException(status_code=500, detail=str(e))

# Exit group
@router.delete("/groups/{group_id}/members/{user_id}")
async def exit_group(
    group_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    group = db.query(Group).filter(Group.id==group_id).first()
    try:
        # Only allow user to exit themselves or admin to remove others
        if current_user.id != group.created_by:
            raise HTTPException(status_code=403, detail="Not authorized")
        
        member = db.query(GroupMember).filter(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        ).first()
        
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")
        
        db.delete(member)
        db.commit()
        
        return {'success': True, 'message': 'Exited group successfully'}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Error exiting group: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/groups/{group_id}/messages", response_model=dict)
async def send_group_message(
    group_id: int,
    message_data: GroupMessageCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        is_member = db.query(GroupMember).filter(
            GroupMember.group_id == group_id,
            GroupMember.user_id == current_user.id,
        ).first()
        
        if not is_member:
            raise HTTPException(status_code=403, detail="Not a group member")
        
        new_message = GroupMessage(
            group_id=group_id,
            sender_id=current_user.id,
            message=message_data.message,
            reply_to_id=message_data.reply_to_id,
        )
        db.add(new_message)
        db.commit()
        db.refresh(new_message)
        
        # ✅ Fetch reply_to data if it exists
        reply_to = None
        if new_message.reply_to_id:
            replied_msg = db.query(GroupMessage).filter(
                GroupMessage.id == new_message.reply_to_id
            ).first()
            if replied_msg:
                reply_sender = db.query(User).filter(
                    User.id == replied_msg.sender_id
                ).first()
                reply_to = {
                    'id': replied_msg.id,
                    'message': replied_msg.message,
                    'sender_id': replied_msg.sender_id,
                    'sender_name': reply_sender.full_name if reply_sender else 'Unknown',
                }
        
        return {
            'success': True,
            'message': 'Message sent',
            'data': {
                'id': new_message.id,
                'group_id': new_message.group_id,
                'sender_id': new_message.sender_id,
                'sender_name': current_user.full_name,
                'message': new_message.message,
                'created_at': new_message.created_at.isoformat() if new_message.created_at else None,
                'reply_to_id': new_message.reply_to_id,  # ✅ ADD THIS
                'reply_to': reply_to,  # ✅ ADD THIS
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Error sending group message: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/groups/{group_id}/messages", response_model=dict)
async def get_group_messages(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        messages = db.query(GroupMessage, User).join(
            User, GroupMessage.sender_id == User.id
        ).filter(GroupMessage.group_id == group_id).order_by(GroupMessage.created_at).all()
        
        result = []
        for message, sender in messages:
            # ✅ Fetch reply_to data if it exists
            reply_to = None
            if message.reply_to_id:
                replied_msg = db.query(GroupMessage).filter(
                    GroupMessage.id == message.reply_to_id
                ).first()
                if replied_msg:
                    reply_sender = db.query(User).filter(
                        User.id == replied_msg.sender_id
                    ).first()
                    reply_to = {
                        'id': replied_msg.id,
                        'message': replied_msg.message,
                        'sender_id': replied_msg.sender_id,
                        'sender_name': reply_sender.full_name if reply_sender else 'Unknown',
                    }
            
            result.append({
                'id': message.id,
                'group_id': message.group_id,
                'sender_id': message.sender_id,
                'sender_name': sender.full_name,
                'message': message.message,
                'is_read': message.is_read,
                'created_at': message.created_at.isoformat() if message.created_at else None,
                'reply_to_id': message.reply_to_id,  # ✅ Return the ID too
                'reply_to': reply_to,  # ✅ Return full reply data
            })
        
        return {'data': result, 'total': len(result)}
    except Exception as e:
        print(f"❌ Error getting group messages: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        
# ============ MISSING GROUP MANAGEMENT ENDPOINTS ============

# Update group
@router.put("/groups/{group_id}", response_model=dict)
async def update_group(
    group_id: int,
    group_data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        group = db.query(Group).filter(Group.id == group_id).first()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        
        if group.created_by != current_user.id and current_user.role not in ['admin', 'school']:
            raise HTTPException(status_code=403, detail="Not authorized")
        
        if 'name' in group_data and group_data['name']:
            group.name = group_data['name']
        if 'description' in group_data:
            group.description = group_data['description']
        if 'avatar' in group_data and group_data['avatar']:
            group.avatar = group_data['avatar']
        
        db.commit()
        return {'success': True, 'message': 'Group updated'}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Error updating group: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Add member to group
@router.post("/groups/{group_id}/members", response_model=dict)
async def add_group_member(
    group_id: int,
    member_data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        group = db.query(Group).filter(Group.id == group_id).first()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        
        if group.created_by != current_user.id and current_user.role not in ['admin', 'school']:
            raise HTTPException(status_code=403, detail="Not authorized")
        
        identifier = member_data.get('identifier', '')
        
        user = db.query(User).filter(
            (User.email == identifier) | (User.username == identifier) | (User.full_name == identifier)
        ).first()
        
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        existing = db.query(GroupMember).filter(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user.id,
        ).first()
        
        if existing:
            raise HTTPException(status_code=400, detail="Already a member")
        
        new_member = GroupMember(
            group_id=group_id,
            user_id=user.id,
            role='member',
        )
        db.add(new_member)
        db.commit()
        
        return {'success': True, 'message': 'Member added'}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Error adding member: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Update member role
@router.put("/groups/{group_id}/members/{user_id}/role", response_model=dict)
async def update_member_role(
    group_id: int,
    user_id: int,
    role_data: dict,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        group = db.query(Group).filter(Group.id == group_id).first()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        
        if group.created_by != current_user.id and current_user.role not in ['admin', 'school']:
            raise HTTPException(status_code=403, detail="Not authorized")
        
        member = db.query(GroupMember).filter(
            GroupMember.group_id == group_id,
            GroupMember.user_id == user_id,
        ).first()
        
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")
        
        member.role = role_data.get('role', 'member')
        db.commit()
        
        return {'success': True, 'message': 'Role updated'}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Error updating role: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Delete group
@router.delete("/groups/{group_id}", response_model=dict)
async def delete_group(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        group = db.query(Group).filter(Group.id == group_id).first()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        
        if group.created_by != current_user.id and current_user.role not in ['admin', 'school']:
            raise HTTPException(status_code=403, detail="Not authorized")
        
        db.delete(group)
        db.commit()
        
        return {'success': True, 'message': 'Group deleted'}
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        print(f"❌ Error deleting group: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Exit group (for non-admin)
@router.post("/groups/{group_id}/exit", response_model=dict)
async def exit_group_endpoint(
    group_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        group = db.query(Group).filter(Group.id == group_id).first()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        
        # If admin exits, delete the group
        if group.created_by == current_user.id:
            db.delete(group)
            db.commit()
            return {'success': True, 'message': 'Group deleted (admin exited)'}
        
        # Otherwise remove member
        member = db.query(GroupMember).filter(
            GroupMember.group_id == group_id,
            GroupMember.user_id == current_user.id,
        ).first()
        
        if member:
            db.delete(member)
            db.commit()
        
        return {'success': True, 'message': 'Exited group'}
    except Exception as e:
        db.rollback()
        print(f"❌ Error exiting group: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        
# Upload group image
@router.put("/groups/{group_id}/image")
async def upload_group_image(
    group_id: int,
    image: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        group = db.query(Group).filter(Group.id == group_id).first()
        if not group:
            raise HTTPException(status_code=404, detail="Group not found")
        
        if group.created_by != current_user.id and current_user.role not in ['admin', 'school']:
            raise HTTPException(status_code=403, detail="Not authorized")
        
        # Save image
        import os
        import shutil
        
        upload_dir = "uploads/groups"
        os.makedirs(upload_dir, exist_ok=True)
        
        file_extension = os.path.splitext(image.filename)[1]
        file_name = f"group_{group_id}_{int(time.time())}{file_extension}"
        file_path = os.path.join(upload_dir, file_name)
        
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(image.file, buffer)
        
        # Update group avatar
        group.avatar = f"/{file_path}"
        db.commit()
        
        return {
            'success': True,
            'message': 'Image uploaded',
            'data': {'avatar': group.avatar}
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(e))
        
@router.get("/search-users")
async def search_all_users(
    search: Optional[str] = Query(None, description="Search query for users"),
    role: Optional[str] = Query(None, description="Filter by role"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    """
    Search ALL users regardless of school.
    Use with caution - this is for admin features like adding members to groups.
    """
    
    # Base query - all active and approved users
    query = db.query(User).filter(
        User.id != current_user.id,
        User.is_active == True,
        User.approval_status == 'approved'
    )

    # Filter by role if provided
    if role and role.strip():
        query = query.filter(User.role == role.strip())

    # Apply search
    if search and search.strip():
        search_term = f"%{search.strip()}%"
        query = query.filter(
            (User.full_name.ilike(search_term)) |
            (User.username.ilike(search_term)) |
            (User.email.ilike(search_term))
        )
    
    # Get total count
    total = query.count()
    
    # Apply pagination
    users = query.order_by(User.full_name).offset(offset).limit(limit).all()
    
    # Format response
    data = []
    for user in users:
        user_data = {
            "id": user.id,
            "full_name": user.full_name,
            "name": user.full_name,
            "username": user.username,
            "email": user.email,
            "dp_url": user.profile_picture,
            "avatar": user.profile_picture,
            "profile_picture": user.profile_picture,
            "role": user.role,
            "is_online": user.is_online if user.is_online is not None else False,
            "last_seen": user.last_seen.isoformat() if user.last_seen else None,
            "school_id": user.school_id,
        }
        
        # Add role-specific info
        if user.role == "student":
            student = db.query(Student).filter(Student.user_id == user.id).first()
            if student:
                user_data["admission_number"] = student.admission_number
                user_data["class_name"] = student.class_name
                user_data["class_id"] = student.class_id
                # Get school name
                school = db.query(School).filter(School.id == student.school_id).first()
                user_data["school_name"] = school.school_name if school else None
                
        elif user.role == "teacher":
            teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
            if teacher:
                user_data["subject"] = teacher.subject
                user_data["qualification"] = teacher.qualification
                school = db.query(School).filter(School.id == teacher.school_id).first()
                user_data["school_name"] = school.school_name if school else None
                
        elif user.role == "parent":
            parent = db.query(Parent).filter(Parent.user_id == user.id).first()
            if parent:
                user_data["relation_type"] = parent.relation_type
                parent_students = db.query(ParentStudent).filter(
                    ParentStudent.parent_id == parent.id
                ).all()
                children = []
                for ps in parent_students:
                    student = db.query(Student).filter(Student.id == ps.student_id).first()
                    if student:
                        child_user = db.query(User).filter(User.id == student.user_id).first()
                        school = db.query(School).filter(School.id == student.school_id).first()
                        children.append({
                            "id": student.user_id,
                            "name": child_user.full_name if child_user else None,
                            "class": student.class_name,
                            "admission_number": student.admission_number,
                            "school_name": school.school_name if school else None,
                        })
                user_data["children"] = children
                
        elif user.role == "worker":
            worker = db.query(Worker).filter(Worker.user_id == user.id).first()
            if worker:
                user_data["department"] = worker.department
                user_data["role_title"] = worker.role_title
                school = db.query(School).filter(School.id == worker.school_id).first()
                user_data["school_name"] = school.school_name if school else None
        
        data.append(user_data)
    
    return {
        "data": data,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < total
    }

# ==========================================
# SMS BUYING / M-PESA
# ==========================================

# ── Lazy env access so load_dotenv() always runs first ─────────
def _env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)

def _mpesa_consumer_key() -> str | None:
    return _env("MPESA_CONSUMER_KEY")

def _mpesa_consumer_secret() -> str | None:
    return _env("MPESA_CONSUMER_SECRET")

def _mpesa_passkey() -> str | None:
    return _env("MPESA_PASSKEY")

def _mpesa_callback_url() -> str:
    return _env(
        "MPESA_CALLBACK_URL",
        "https://candidate-nearby-tradition-pair.trycloudflare.com/api/v1/admin/mpesa/confirm",
    )

def _mpesa_env() -> str:
    return _env("MPESA_ENV", "sandbox")

def _mpesa_account_type() -> str:
    return _env("MPESA_ACCOUNT_TYPE", "till")

def _mpesa_shortcode() -> str:
    return _env("MPESA_SHORTCODE", "8315966")

def _mpesa_base_url() -> str:
    return (
        "https://api.safaricom.co.ke"
        if _mpesa_env() == "production"
        else "https://sandbox.safaricom.co.ke"
    )

def _mpesa_transaction_type() -> str:
    return (
        "CustomerBuyGoodsOnline"
        if _mpesa_account_type() == "till"
        else "CustomerPayBillOnline"
    )

# ==========================================
# M-PESA CALLBACK
# ==========================================

@router.post("/mpesa/confirm")
async def mpesa_callback(request: Request, db: Session = Depends(get_db)):
    try:
        data = await request.json()
    except Exception:
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    cb = data.get("Body", {}).get("stkCallback", {})
    checkout_id = cb.get("CheckoutRequestID")
    result_code = cb.get("ResultCode")
    result_desc = cb.get("ResultDesc")

    if not checkout_id:
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    items = cb.get("CallbackMetadata", {}).get("Item", [])
    meta = {i.get("Name"): i.get("Value") for i in items}
    receipt = meta.get("MpesaReceiptNumber")

    # ── SMS top-up? ──────────────────────────────────────
    topup = db.query(SmsTopup).filter(
        SmsTopup.checkout_request_id == checkout_id
    ).first()

    if topup:
        if topup.status == "success":
            return {"ResultCode": 0, "ResultDesc": "Accepted"}

        topup.result_code = result_code
        topup.result_desc = result_desc

        if result_code != 0:
            topup.status = "cancelled" if result_code == 1032 else "failed"
            db.commit()
            return {"ResultCode": 0, "ResultDesc": "Accepted"}

        topup.mpesa_receipt = receipt
        school = db.query(School).filter(School.id == topup.school_id).first()
        if school:
            school.sms_bal = (school.sms_bal or 0) + topup.sms_count
            topup.status = "success"
            db.commit()
            print(f"✅ SMS topup #{topup.id} credited")
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    # ── Book purchase? ───────────────────────────────────
    purchase = db.query(BookPurchase).filter(
        BookPurchase.checkout_request_id == checkout_id
    ).first()

    if purchase:
        if purchase.status == "success":
            return {"ResultCode": 0, "ResultDesc": "Accepted"}

        purchase.result_desc = result_desc

        if result_code != 0:
            purchase.status = "cancelled" if result_code == 1032 else "failed"
            db.commit()
            print(f"❌ Book purchase #{purchase.id} {purchase.status}")
            return {"ResultCode": 0, "ResultDesc": "Accepted"}

        purchase.mpesa_receipt = receipt
        purchase.status = "success"
        purchase.completed_at = datetime.utcnow()
        db.commit()
        print(f"✅ Book purchase #{purchase.id} — book {purchase.book_id} unlocked for user {purchase.user_id}")
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    print(f"⚠️ No matching purchase for checkout {checkout_id}")
    return {"ResultCode": 0, "ResultDesc": "Accepted"}

# ==========================================
# GET M-PESA ACCESS TOKEN
# ==========================================

async def get_mpesa_token() -> str:
    consumer_key = _mpesa_consumer_key()
    consumer_secret = _mpesa_consumer_secret()

    if not consumer_key:
        raise HTTPException(500, "MPESA_CONSUMER_KEY is not set")
    if not consumer_secret:
        raise HTTPException(500, "MPESA_CONSUMER_SECRET is not set")

    credentials = f"{consumer_key}:{consumer_secret}"
    encoded = base64.b64encode(credentials.encode("utf-8")).decode("utf-8")

    url = f"{_mpesa_base_url()}/oauth/v1/generate?grant_type=client_credentials"
    headers = {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
    except httpx.TimeoutException:
        raise HTTPException(504, "M-Pesa authentication request timed out")
    except httpx.RequestError:
        raise HTTPException(502, "Could not connect to M-Pesa authentication service")

    if response.status_code != 200:
        try:
            err = response.json()
        except Exception:
            err = response.text
        raise HTTPException(502, {
            "message": "M-Pesa authentication failed",
            "status_code": response.status_code,
            "response": err,
        })

    data = response.json()
    token = data.get("access_token")

    if not token:
        raise HTTPException(502, "M-Pesa auth response missing access_token")

    return token


# ==========================================
# TOP UP SMS
# ==========================================

@router.post("/topup-sms")
async def topup_sms(
    phone_number: str = Body(...),
    sms_count: int = Body(...),
    amount: float = Body(...),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user:
        raise HTTPException(401, "Not authenticated")
    if sms_count <= 0:
        raise HTTPException(400, "SMS count must be > 0")
    if amount <= 0:
        raise HTTPException(400, "Amount must be > 0")

    passkey = _mpesa_passkey()
    callback_url = _mpesa_callback_url()
    shortcode = _mpesa_shortcode()
    base_url = _mpesa_base_url()
    transaction_type = _mpesa_transaction_type()
    
    if not passkey:
        raise HTTPException(500, "M-Pesa Passkey not configured")
    if not callback_url:
        raise HTTPException(500, "M-Pesa callback URL not configured")

    # Normalize phone
    phone_number = phone_number.strip()
    if phone_number.startswith("07"):
        phone_number = "254" + phone_number[1:]
    elif phone_number.startswith("+254"):
        phone_number = phone_number[1:]
    elif phone_number.startswith("7"):
        phone_number = "254" + phone_number

    if (not phone_number.startswith("254")
            or len(phone_number) != 12
            or not phone_number.isdigit()):
        raise HTTPException(400, "Invalid Kenyan phone number")

    # Save pending row FIRST
    topup = SmsTopup(
        school_id=user.school_id,
        user_id=user.id,
        phone_number=phone_number,
        amount=int(amount),
        sms_count=sms_count,
        status="pending",
    )
    db.add(topup)
    db.commit()
    db.refresh(topup)

    # STK push
    access_token = await get_mpesa_token()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password_string = shortcode + passkey + timestamp
    stk_password = base64.b64encode(password_string.encode("utf-8")).decode("utf-8")

    payload = {
        "BusinessShortCode": shortcode,
        "Password": stk_password,
        "Timestamp": timestamp,
        "TransactionType": transaction_type,
        "Amount": int(amount),
        "PartyA": phone_number,
        "PartyB": shortcode,
        "PhoneNumber": phone_number,
        "CallBackURL": callback_url,
        "AccountReference": f"SMS-{topup.id}",
        "TransactionDesc": "SMS TOPUP",
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/mpesa/stkpush/v1/processrequest",
                json=payload,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )
    except Exception as e:
        topup.status = "failed"
        topup.result_desc = str(e)[:500]
        db.commit()
        raise HTTPException(502, f"M-Pesa STK request failed: {e}")

    result = response.json()

    if response.status_code != 200 or result.get("ResponseCode") != "0":
        topup.status = "failed"
        topup.result_desc = str(result.get("ResponseDescription") or result)[:500]
        db.commit()
        raise HTTPException(502, {
            "message": "M-Pesa STK request failed",
            "response": result,
        })

    topup.checkout_request_id = result.get("CheckoutRequestID")
    db.commit()

    return {
        "success": True,
        "message": "STK push sent. Enter your PIN.",
        "topup_id": topup.id,
        "sms_count": sms_count,
        "amount": int(amount),
        "phone_number": phone_number,
        "checkout_request_id": topup.checkout_request_id,
    }
    
@router.post("/add-events")
async def create_event(
    title: str = Body(...),
    description: Optional[str] = Body(None),
    event_date: datetime = Body(...),
    time: Optional[str] = Body(None),
    user = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    new_event = Event(
        title=title,
        description=description,
        event_date=event_date,
        time=time,
        school_id=user.school_id,
        created_by=user.id,
    )
    
    db.add(new_event)
    db.commit()
    db.refresh(new_event)
    return {"message": "Success", "id": new_event.id}
    
@router.post("/add-announcements")
async def create_announcement(
    title: str = Body(...),
    description: Optional[str] = Body(None),
    priority: Optional[str] = Body("Medium"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    new_ann = Announcement(
        title=title,
        description=description,
        school_id=user.school_id,
        created_by=user.id,
    )
    db.add(new_ann)
    db.commit()
    db.refresh(new_ann)
    return {"message": "Success", "id": new_ann.id}
    
async def _fire_stk_push(
    phone_number: str,
    amount: int,
    account_ref: str,
    description: str,
) -> dict:
    """Fire STK push. Returns Daraja response JSON. Raises HTTPException on failure."""
    passkey = _mpesa_passkey()
    callback_url = _mpesa_callback_url()
    shortcode = _mpesa_shortcode()
    base_url = _mpesa_base_url()
    transaction_type = _mpesa_transaction_type()

    if not passkey:
        raise HTTPException(500, "M-Pesa Passkey not configured")
    if not callback_url:
        raise HTTPException(500, "M-Pesa callback URL not configured")

    access_token = await get_mpesa_token()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    password_string = shortcode + passkey + timestamp
    stk_password = base64.b64encode(password_string.encode("utf-8")).decode("utf-8")

    payload = {
        "BusinessShortCode": shortcode,
        "Password": stk_password,
        "Timestamp": timestamp,
        "TransactionType": transaction_type,
        "Amount": amount,
        "PartyA": phone_number,
        "PartyB": shortcode,
        "PhoneNumber": phone_number,
        "CallBackURL": callback_url,
        "AccountReference": account_ref,
        "TransactionDesc": description,
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{base_url}/mpesa/stkpush/v1/processrequest",
                json=payload,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
            )
    except httpx.TimeoutException:
        raise HTTPException(504, "M-Pesa STK request timed out")
    except httpx.RequestError:
        raise HTTPException(502, "Could not connect to M-Pesa STK service")

    return response.json()
    
def _normalize_phone(phone: str) -> str:
    phone = phone.strip()
    if phone.startswith("07"):
        phone = "254" + phone[1:]
    elif phone.startswith("+254"):
        phone = phone[1:]
    elif phone.startswith("7"):
        phone = "254" + phone
    if (not phone.startswith("254")
            or len(phone) != 12
            or not phone.isdigit()):
        raise HTTPException(400, "Invalid Kenyan phone number")
    return phone
    
@router.post("/books/{book_id}/purchase")
async def purchase_book(
    book_id: int,
    phone_number: str = Body(..., embed=True),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user:
        raise HTTPException(401, "Not authenticated")

    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(404, "Book not found")

    if book.is_free:
        raise HTTPException(400, "This book is free — no payment required")

    # Use the DB price — never trust a client-sent amount
    amount = int(book.price or 0)
    if amount <= 0:
        raise HTTPException(400, "Book price is not set")

    # Has the user already bought it?
    existing = (
        db.query(BookPurchase)
        .filter(
            BookPurchase.user_id == user.id,
            BookPurchase.book_id == book_id,
            BookPurchase.status == "success",
        )
        .first()
    )
    if existing:
        raise HTTPException(400, "You already own this book")

    phone = _normalize_phone(phone_number)

    purchase = BookPurchase(
        user_id=user.id,
        book_id=book_id,
        phone_number=phone,
        amount=amount,
        status="pending",
    )
    db.add(purchase)
    db.commit()
    db.refresh(purchase)

    # Fire STK — pass the purchase id as the reference
    result = await _fire_stk_push(
        phone_number=phone,
        amount=amount,
        account_ref=f"BOOK-{purchase.id}",
        description=f"Book purchase #{purchase.id}",
    )

    if result.get("ResponseCode") != "0":
        purchase.status = "failed"
        purchase.result_desc = str(result.get("ResponseDescription") or result)[:500]
        db.commit()
        raise HTTPException(502, {
            "message": "M-Pesa STK request failed",
            "response": result,
        })

    purchase.checkout_request_id = result.get("CheckoutRequestID")
    db.commit()

    return {
        "success": True,
        "message": "STK push sent. Enter your PIN.",
        "purchase_id": purchase.id,
        "book_id": book_id,
        "book_title": book.title,
        "amount": amount,
        "phone_number": phone,
        "checkout_request_id": purchase.checkout_request_id,
    }
    
@router.get("/books/{book_id}/access")
async def check_book_access(
    book_id: int,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    book = db.query(Book).filter(Book.id == book_id).first()
    if not book:
        raise HTTPException(404, "Book not found")

    if book.is_free:
        return {"has_access": True, "reason": "free"}

    purchase = (
        db.query(BookPurchase)
        .filter(
            BookPurchase.user_id == user.id,
            BookPurchase.book_id == book_id,
            BookPurchase.status == "success",
        )
        .first()
    )

    return {
        "has_access": purchase is not None,
        "reason": "purchased" if purchase else "not_purchased",
        "purchase_id": purchase.id if purchase else None,
    }
    
@router.get("/books/purchases/{purchase_id}/status")
async def book_purchase_status(
    purchase_id: int,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    purchase = (
        db.query(BookPurchase)
        .filter(BookPurchase.id == purchase_id, BookPurchase.user_id == user.id)
        .first()
    )
    if not purchase:
        raise HTTPException(404, "Purchase not found")

    return {
        "purchase_id": purchase.id,
        "status": purchase.status,
        "book_id": purchase.book_id,
        "amount": purchase.amount,
        "mpesa_receipt": purchase.mpesa_receipt,
        "result_desc": purchase.result_desc,
    }
    

@router.get("/class-teachers")
async def class_teachers(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user or user.role not in ['admin', 'school']:
        raise HTTPException(status_code=403, detail="Not Authorized")

    classes = (
        db.query(Class)
        .filter(Class.school_id == user.school_id)
        .order_by(Class.name)
        .all()
    )

    result = []
    for c in classes:
        teacher_name = None
        teacher_subject = None
        teacher_user_id = None

        if c.class_teacher_id:
            teacher = (
                db.query(Teacher)
                .filter(Teacher.id == c.class_teacher_id)
                .first()
            )
            if teacher:
                teacher_subject = teacher.subject
                teacher_user_id = teacher.user_id
                t_user = (
                    db.query(User)
                    .filter(User.id == teacher.user_id)
                    .first()
                )
                if t_user:
                    teacher_name = t_user.full_name

        result.append({
            "class_id": c.id,
            "class_name": c.name,
            "teacher_id": c.class_teacher_id,
            "teacher_user_id": teacher_user_id,
            "teacher_name": teacher_name,
            "teacher_subject": teacher_subject,
        })

    return {"class_teachers": result}
    
@router.post("/classes/{class_id}/assign-teacher")
async def assign_teacher(
    class_id: int,
    teacher_id: int = Body(..., embed=True),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user or user.role not in ['admin', 'school']:
        raise HTTPException(status_code=403, detail="Not Authorized")

    # Verify class
    clas = (
        db.query(Class)
        .filter(Class.id == class_id, Class.school_id == user.school_id)
        .first()
    )
    if not clas:
        raise HTTPException(status_code=404, detail="Class not found")

    # 🔥 Accept EITHER teachers.id OR users.id
    teacher = db.query(Teacher).filter(Teacher.id == teacher_id).first()
    if not teacher:
        teacher = db.query(Teacher).filter(Teacher.user_id == teacher_id).first()

    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")

    if teacher.school_id and teacher.school_id != user.school_id:
        raise HTTPException(
            status_code=403,
            detail="Teacher belongs to a different school",
        )

    # Store the REAL teachers.id
    clas.class_teacher_id = teacher.id
    db.commit()
    db.refresh(clas)

    return {
        "message": "Success",
        "class_id": clas.id,
        "teacher_id": teacher.id,          # teachers.id
        "teacher_user_id": teacher.user_id, # users.id (echo back for client)
    }
    
@router.post("/add-classes")
async def create_class(
    name: str = Body(...),
    stream: Optional[str] = Body(None),
    level: Optional[str] = Body(None),
    class_teacher_id: Optional[int] = Body(None),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user or user.role not in ['admin', 'school']:
        raise HTTPException(403, "Not Authorized")

    name = name.strip()
    if not name:
        raise HTTPException(400, "Class name is required")

    # Duplicate check for the same school
    existing = (
        db.query(Class)
        .filter(Class.school_id == user.school_id, Class.name == name)
        .first()
    )
    if existing:
        raise HTTPException(409, f"Class '{name}' already exists")

    # Resolve teacher id (accept either teachers.id or users.id)
    resolved_teacher_id = None
    if class_teacher_id is not None:
        teacher = (
            db.query(Teacher)
            .filter(Teacher.id == class_teacher_id)
            .first()
        )
        if not teacher:
            teacher = (
                db.query(Teacher)
                .filter(Teacher.user_id == class_teacher_id)
                .first()
            )
        if not teacher:
            raise HTTPException(404, "Teacher not found")

        if teacher.school_id and teacher.school_id != user.school_id:
            raise HTTPException(403, "Teacher belongs to a different school")

        resolved_teacher_id = teacher.id   # store teachers.id

    new_class = Class(
        name=name,
        school_id=user.school_id,
        class_teacher_id=resolved_teacher_id,
        # If your Class model has these columns, uncomment:
        # stream=stream,
        # level=level,
    )
    db.add(new_class)
    db.commit()
    db.refresh(new_class)

    return {
        "message": "Success",
        "class": {
            "id": new_class.id,
            "class_name": new_class.name,
            "class_teacher_id": new_class.class_teacher_id,
        },
    }
    
@router.delete("/delete-classes/{class_id}")
async def delete_class(
    class_id: int,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user or user.role not in ['admin', 'school']:
        raise HTTPException(403, "Not Authorized")

    clas = (
        db.query(Class)
        .filter(Class.id == class_id, Class.school_id == user.school_id)
        .first()
    )
    if not clas:
        raise HTTPException(404, "Class not found")

    # Optional safety: block delete if students are assigned
    student_count = (
        db.query(Student)
        .filter(Student.class_id == class_id)
        .count()
    )
    if student_count > 0:
        raise HTTPException(
            400,
            f"Cannot delete: {student_count} student(s) still assigned to this class",
        )

    db.delete(clas)
    db.commit()

    return {"message": "Success", "deleted_id": class_id}
    
@router.get("/studentid/{user_id}")
async def get_studentid(
    user_id: int,
    user = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user:
        raise HTTPException(status_code=403, detail="Not Authorized")
    
    student = db.query(Student).filter(Student.user_id == user_id).first()
    
    return {"student_id": student.id}


HEADER_ALIASES = {
    "admission_number": [
        "adm-no", "adm no", "admno", "adm_no",
        "admission", "admission number", "admission_number",
        "admission no", "admissionno",
        "student id", "student_id", "studentid",
        "reg no", "reg_no", "regno",
    ],
    "name": [
        "student name", "student_name", "studentname",
        "name", "full name", "full_name", "fullname",
        "student", "pupil", "pupil name",
    ],
    "email": [
        "email", "student email", "student_email",
        "e-mail", "mail", "email address",
    ],
    "phone": [
        "phone", "student phone", "student_phone",
        "phone number", "phone_number", "phonenumber",
        "mobile", "tel", "telephone",
    ],
    "class_name": [
        "class", "class name", "class_name", "classname",
        "form", "form name", "grade", "grade name",
    ],
}


def _normalize_header(h) -> str:
    if h is None:
        return ""
    s = str(h).strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\s-]", "", s)
    return s.strip()


def _build_header_map(header_row: list) -> dict:
    header_map = {}
    normalized = [_normalize_header(h) for h in header_row]
    for key, aliases in HEADER_ALIASES.items():
        for i, h in enumerate(normalized):
            if not h:
                continue
            if h in aliases:
                header_map[key] = i
                break
    return header_map


def _row_from_headers(cells: list, header_map: dict, index: int) -> dict:
    def get(key: str) -> str:
        i = header_map.get(key)
        if i is None:
            return ""
        if i < len(cells) and cells[i] is not None:
            v = str(cells[i]).strip()
            return "" if v == "-" else v
        return ""

    return {
        "row_number": index,
        "admission_number": get("admission_number"),
        "name": get("name"),
        "class_name": get("class_name"),
        "email": get("email"),
        "phone": get("phone"),
    }


def _parse_rows_from_table(all_rows: list) -> list:
    if not all_rows:
        return []

    header_idx = 0
    for i, r in enumerate(all_rows):
        if any(c is not None and str(c).strip() for c in r):
            header_idx = i
            break

    header_map = _build_header_map(all_rows[header_idx])
    print(f"📥 header_map: {header_map}")

    rows: list[dict] = []
    data_rows = all_rows[header_idx + 1:]

    if header_map:
        for i, r in enumerate(data_rows, start=1):
            if not any(c is not None and str(c).strip() for c in r):
                continue
            rows.append(_row_from_headers(r, header_map, i))
    else:
        print("⚠️  No recognizable header — falling back to positional")
        for i, r in enumerate(data_rows, start=1):
            if not any(c is not None and str(c).strip() for c in r):
                continue
            rows.append(_row_from_cells(r, i))   # ← uses LOCKED function

    return rows

# ════════════════════════════════════════════════════════════════
# MAIN ENDPOINT
# ════════════════════════════════════════════════════════════════
@router.post("/students/parse")
async def parse_students_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        print(f"📥 PARSE: file={file.filename}, user={user.id} ({user.role})")

        if user.role not in ("admin", "school"):
            raise HTTPException(403, "Only admin allowed")

        content = await file.read()
        filename = (file.filename or "").lower()
        print(f"📥 PARSE: {len(content)} bytes, ext={filename.split('.')[-1]}")

        rows: list[dict] = []
        method = "unknown"

        if filename.endswith(".csv"):
            method = "csv"
            text = content.decode("utf-8-sig", errors="replace")
            reader = csv.reader(io.StringIO(text))
            all_rows = list(reader)
            rows = _parse_rows_from_table(all_rows)

        # ═══════════════════════════════════════════════════
        # XLSX
        # ═══════════════════════════════════════════════════
        elif filename.endswith(".xlsx"):
            method = "xlsx"
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
            ws = wb.active
            all_rows = [list(r) for r in ws.iter_rows(values_only=True)]
            rows = _parse_rows_from_table(all_rows)

        # ═══════════════════════════════════════════════════
        # XLS (legacy Excel)
        # ═══════════════════════════════════════════════════
        elif filename.endswith(".xls"):
            method = "xls"
            parsed = False
            try:
                import xlrd
                wb = xlrd.open_workbook(file_contents=content)
                ws = wb.sheet_by_index(0)
                all_rows = [
                    [ws.cell_value(i, j) for j in range(ws.ncols)]
                    for i in range(ws.nrows)
                ]
                rows = _parse_rows_from_table(all_rows)
                parsed = True
            except ImportError:
                print("⚠️  xlrd not installed")
            except Exception as e:
                print(f"⚠️  xlrd failed: {e}")

            if not parsed:
                method = "xls-gemini"
                rows = await _extract_with_gemini(content, filename)

        # ═══════════════════════════════════════════════════
        # DOCX (modern Word)
        # ═══════════════════════════════════════════════════
        elif filename.endswith(".docx"):
            method = "docx"
            from docx import Document
            doc = Document(io.BytesIO(content))

            if doc.tables:
                for table in doc.tables:
                    all_rows = [[c.text for c in row.cells] for row in table.rows]
                    rows.extend(_parse_rows_from_table(all_rows))
            else:
                all_rows = []
                for p in doc.paragraphs:
                    line = p.text.strip()
                    if not line:
                        continue
                    all_rows.append(re.split(r"[\t,]+", line))
                rows = _parse_rows_from_table(all_rows)

        # ═══════════════════════════════════════════════════
        # DOC (legacy Word)
        # ═══════════════════════════════════════════════════
        elif filename.endswith(".doc"):
            method = "doc"
            parsed = False
            try:
                import textract
                import tempfile
                with tempfile.NamedTemporaryFile(
                    suffix=".doc", delete=False
                ) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name
                text = textract.process(tmp_path).decode(
                    "utf-8", errors="replace"
                )
                os.unlink(tmp_path)

                all_rows = []
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    cells = re.split(r"[\t,]+", line)
                    if len(cells) >= 3:
                        all_rows.append(cells)
                rows = _parse_rows_from_table(all_rows)
                parsed = True
            except ImportError:
                print("⚠️  textract not installed")
            except Exception as e:
                print(f"⚠️  textract failed: {e}")

            if not parsed:
                method = "doc-gemini"
                rows = await _extract_with_gemini(content, filename)

        # ═══════════════════════════════════════════════════
        # PDF
        # ═══════════════════════════════════════════════════
        elif filename.endswith(".pdf"):
            method = "pdf"
            parsed = False
            try:
                import pdfplumber
                all_rows = []
                with pdfplumber.open(io.BytesIO(content)) as pdf:
                    for page in pdf.pages:
                        tables = page.extract_tables()
                        if tables:
                            for table in tables:
                                for trow in table:
                                    all_rows.append([str(c or "") for c in trow])
                        else:
                            text = page.extract_text() or ""
                            for line in text.splitlines():
                                line = line.strip()
                                if not line:
                                    continue
                                cells = re.split(r"\s{2,}|\t", line)
                                if len(cells) >= 3:
                                    all_rows.append(cells)
                rows = _parse_rows_from_table(all_rows)
                if rows:
                    parsed = True
            except ImportError:
                print("⚠️  pdfplumber not installed")
            except Exception as e:
                print(f"⚠️  pdfplumber failed: {e}")

            if not parsed:
                return {"message": "Extraction failed"}

        # ═══════════════════════════════════════════════════
        # Images — Gemini only
        # ═══════════════════════════════════════════════════
        elif filename.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp")):
            method = "image-gemini"
            rows = await _extract_with_gemini(content, filename)

        else:
            raise HTTPException(
                400,
                f"Unsupported file type: {filename}. Use CSV, XLSX, XLS, "
                "DOCX, DOC, PDF, JPG, or PNG.",
            )

        print(f"📥 PARSE: method={method}, {len(rows)} rows")

        # ── Mark duplicates ──
        for r in rows:
            adm = r.get("admission_number") or ""
            if adm:
                exists = (
                    db.query(Student)
                    .filter(
                        Student.admission_number == adm,
                        Student.school_id == user.school_id,
                    )
                    .first()
                )
                r["_duplicate"] = bool(exists)
            else:
                r["_duplicate"] = False

        return {
            "rows": rows,
            "total": len(rows),
            "file_name": file.filename,
            "method": method,
        }

    except HTTPException:
        raise
    except Exception as e:
        print("❌ PARSE EXCEPTION:")
        traceback.print_exc()
        raise HTTPException(500, f"{type(e).__name__}: {e}")


# ════════════════════════════════════════════════════════════════
# Gemini extraction
# ════════════════════════════════════════════════════════════════
async def _extract_with_gemini(content: bytes, filename: str) -> list[dict]:
    try:
        import google.generativeai as genai
        from PIL import Image
    except ImportError as e:
        raise HTTPException(500, f"Missing dependency: {e}")

    api_key = os.getenv("API_KEY") or os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise HTTPException(500, "API_KEY (Gemini) not configured")

    genai.configure(api_key=api_key)

    # Build media part
    if filename.endswith(".pdf"):
        file_part = {"mime_type": "application/pdf", "data": content}
    elif filename.endswith((".doc", ".docx")):
        raise HTTPException(
            400,
            f"Cannot parse {filename} as an image — install textract or "
            "python-docx to enable direct DOC/DOCX parsing.",
        )
    else:
        # Image
        file_part = Image.open(io.BytesIO(content))

    prompt = """
You are an expert document parser. Extract all student records from this document.

Rules:
- Detect all admission numbers (e.g. ADM001, 5753, SCH01-2026-1001)
- Detect student names
- Detect class names (e.g. Form 1A, Grade 3, Form 4West)
- Detect emails
- Detect phone numbers (mobile: 07xxxxxxxx or +2547xxxxxxxx)
- Detect parent/guardian names and their phones if present

Return ONLY valid JSON, no markdown, no explanation.

Format:
{
  "students": [
    {
      "admission_number": "ADM001",
      "name": "John Kamau",
      "email": "john@example.com",
      "phone": "0712345678"
    }
  ]
}

If a field is missing, use "". Do NOT invent values.
If this is not a student list, return {"students": []}.
"""

    # Only try 2 models, 1 attempt each — keeps response time under 20s
    models = ["gemini-flash-latest", "gemini-2.0-flash"]

    last_error = None
    text_out = None
    for model_name in models:
        try:
            print(f"🤖 Gemini: trying {model_name}")
            model = genai.GenerativeModel(model_name)
            response = model.generate_content([file_part, prompt])
            if response and response.text:
                text_out = response.text
                print(f"✅ Gemini: {model_name} succeeded")
                break
        except Exception as e:
            last_error = str(e)
            print(f"⚠️  Gemini {model_name} failed: {e}")
            continue

    if not text_out:
        raise HTTPException(502, f"All Gemini models failed: {last_error}")

    # Strip code fences
    cleaned = text_out.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        parsed = json.loads(cleaned)
    except Exception as e:
        raise HTTPException(502, f"Invalid JSON from Gemini: {e}\n{cleaned[:400]}")

    students = parsed.get("students") or []
    rows: list[dict] = []
    for i, s in enumerate(students, start=1):
        rows.append({
            "row_number": i,
            "admission_number": str(s.get("admission_number") or "").strip(),
            "name": str(s.get("name") or "").strip(),
            "class_name": "",
            "email": str(s.get("email") or "").strip(),
            "phone": str(s.get("phone") or "").strip(),
        })

    return rows
    
class StudentRow(BaseModel):
    admission_number: str
    name: str
    class_name: Optional[str] = ""
    email: Optional[str] = ""
    phone: Optional[str] = ""

class BulkCreateRequest(BaseModel):
    students: List[StudentRow]
    
def current_term():
    from datetime import date
    today = date.today()
    month = today.month
    if 1 <= month <= 4:
        term = 1
    elif 5 <= month <= 8:
        term = 2
    else:
        term = 3
    return today.year, term
    
def add_fees(db: Session, student_id: int, school_id: int):
    """Create this student's Fee row for the current term."""
    year, term = current_term()

    fee_struct = (
        db.query(FeeStructure)
        .filter(
            FeeStructure.school_id == school_id,
            FeeStructure.academic_year == year,
            FeeStructure.term_number == term,
        )
        .first()
    )

    if not fee_struct:
        return None

    # Safety: don't create a second row for the same student+year+term
    existing = (
        db.query(Fee)
        .filter(
            Fee.student_id == student_id,
            Fee.academic_year == year,
            Fee.term_number == term,
        )
        .first()
    )
    if existing:
        return existing

    fee = Fee(
        student_id=student_id,
        amount=fee_struct.amount,
        paid=0,
        balance=fee_struct.amount,
        academic_year=year,
        term_number=term,
        term_name=fee_struct.term_name,
        status="pending",
    )
    db.add(fee)
    return fee

@router.post("/students/bulk-create")
async def create_bulk_student(
    data: BulkCreateRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role not in ('admin', 'school'):
        raise HTTPException(status_code=403, detail="Not Authorized")

    school = db.query(School).filter(School.id == user.school_id).first()
    if not school:
        raise HTTPException(status_code=400, detail="School not found")

    created = []
    failed = []
    
    for student in data.students:
        try:
            name = re.sub(r"\s+", "", student.name).lower()
            username = name + student.admission_number
            password = name + student.admission_number

            # Skip duplicates
            if db.query(User).filter(User.username == username).first():
                failed.append({
                    "admission_number": student.admission_number,
                    "reason": f"Username '{username}' already exists",
                })
                continue

            # Create the user
            user_obj = User(
                username=username,
                email=student.email or f"{username}@student.local",
                hashed_password=get_password_hash(password),
                full_name=student.name,
                role="student",
                phone=student.phone or None,
                approval_status="approved",
                school_id=user.school_id,
            )
            db.add(user_obj)
            db.flush()
            
            class_row = (
                db.query(Class)
                .filter(Class.name == student.class_name, Class.school_id == user.school_id)
                .first()
            )

            # Create the student profile
            profile = Student(
                user_id=user_obj.id,
                admission_number=student.admission_number,
                school_name=school.school_name,
                class_name=student.class_name,
                class_id=class_row.id if class_row else None,
                gender="null",
                school_id=user.school_id,
            )
            db.add(profile)
            db.flush()
            
            add_fees(db, user_obj.id, user.school_id)

            db.commit()
            created.append(student.admission_number)

        except Exception as e:
            db.rollback()
            failed.append({
                "admission_number": student.admission_number,
                "reason": str(e),
            })

    return {
        "created": created,
        "failed": failed,
        "created_count": len(created),
        "failed_count": len(failed),
    }

class ResultRow(BaseModel):
    student_id: int
    opener: int | None = 0
    midterm: int | None = 0
    end_term: int | None = 0
    term: str
    subject: str
    class_id: int


class ResultsBulkPayload(BaseModel):
    results: List[ResultRow]


@router.post("/teacher/results/bulk")
def save_results_bulk(
    payload: ResultsBulkPayload,
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role != "teacher":
        raise HTTPException(403, "Not Authorized")

    if not payload.results:
        return {"success": True, "saved": 0, "skipped": []}
    class_id = payload.results[0].class_id

    cls = db.query(Class).filter(Class.id == class_id).first()
    if not cls:
        raise HTTPException(404, "Class not found")

    school_id = user.school_id or cls.school_id
    if cls.school_id != school_id:
        raise HTTPException(403, "Class is not in your school")

    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
    if not teacher:
        raise HTTPException(403, "Teacher profile not found")

    saved = 0
    skipped: list[dict] = []

    for row in payload.results:
        if user.role == "teacher":
            teaches = (
                db.query(ClassSubjectTeacher)
                .filter(
                    ClassSubjectTeacher.teacher_id == teacher.id,
                    ClassSubjectTeacher.class_id == row.class_id,
                    ClassSubjectTeacher.subject == row.subject,
                    ClassSubjectTeacher.is_active == True,
                )
                .first()
            )
            if not teaches:
                skipped.append({
                    "student_id": row.student_id,
                    "reason": f"Not assigned to {row.subject} in this class",
                })
                continue

        student = (
            db.query(Student)
            .filter(Student.user_id == row.student_id)
            .first()
        )
        if not student:
            skipped.append({
                "student_id": row.student_id,
                "reason": "Student not found",
            })
            continue
        if student.class_id != row.class_id:
            skipped.append({
                "student_id": row.student_id,
                "reason": "Student not in this class",
            })
            continue

        pairs: list[tuple[str, int]] = []
        if row.opener is not None:
            pairs.append(("Opener", row.opener))
        if row.midterm is not None:
            pairs.append(("Midterm", row.midterm))
        if row.end_term is not None:
            pairs.append(("End Term", row.end_term))

        if not pairs:
            skipped.append({
                "student_id": row.student_id,
                "reason": "No scores provided",
            })
            continue

        for assessment, score in pairs:
            existing = (
                db.query(StudentPerformance)
                .filter(
                    StudentPerformance.student_id == row.student_id,
                    StudentPerformance.class_id == row.class_id,
                    StudentPerformance.subject == row.subject,
                    StudentPerformance.term == row.term,
                    StudentPerformance.assessment == assessment,
                )
                .first()
            )
            if existing:
                existing.score = score
            else:
                db.add(StudentPerformance(
                    student_id=row.student_id,
                    class_id=row.class_id,
                    subject=row.subject,
                    assessment=assessment,
                    score=score,
                    term=row.term,
                ))
            saved += 1

    db.commit()

    return {
        "success": True,
        "saved": saved,
        "skipped": skipped,
    }
    
@router.get("/teacher/class-subjects")
def get_teacher_class_subjects(
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the (class_id, class_name, subject) pairs this teacher teaches."""
    if user.role != "teacher":
        raise HTTPException(403, "Teacher only")

    teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
    if not teacher:
        raise HTTPException(404, "Teacher profile not found")

    rows = (
        db.query(
            Class.id.label("class_id"),
            Class.name.label("class_name"),
            ClassSubjectTeacher.subject,
        )
        .join(ClassSubjectTeacher, ClassSubjectTeacher.class_id == Class.id)
        .filter(
            ClassSubjectTeacher.teacher_id == teacher.id,
            ClassSubjectTeacher.is_active == True,
        )
        .order_by(Class.name, ClassSubjectTeacher.subject)
        .all()
    )

    data = [
        {
            "class_id": r.class_id,
            "class_name": r.class_name,
            "subject": r.subject,
        }
        for r in rows
        if r.subject
    ]

    return {"data": data}
    

    
@router.get("/class-results", response_class=HTMLResponse)
def get_class_Results(user=Depends(get_current_user), db: Session = Depends(get_db)):
    if not user:
        raise HTTPException(status_code=403, detail="Not Authorized")
    if user.role != "teacher":
        raise HTTPException(status_code=403, detail="Not Authorized")
        
    teacher = db.query(Teacher).filter(Teacher.user_id==user.id).first()
    if not teacher:
        raise HTTPException(status_code=404, detail="Teacher not found")
        
    class_data = db.query(Class).filter(Class.class_teacher_id==teacher.id).first()
    if not class_data:
        raise HTTPException(status_code=404, detail="Class not found")
    class_id = class_data.id
    
    # Same school and class teacher
    if user.school_id != class_data.school_id and class_data.class_teacher_id != teacher.id:
        raise HTTPException(status_code=403, detail='Not Authorized')
        
    school = db.query(School).filter(School.id==user.school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail='School Not Found')
    
    from collections import defaultdict, Counter

    # ── Per (student, subject): mean of all non-zero scores in the current term
    _, term_num = current_term()
    term_str = f"Term {term_num}"

    subject_avg = (
        db.query(
            StudentPerformance.student_id.label("sid"),
            StudentPerformance.subject.label("subject"),
            func.avg(StudentPerformance.score).label("avg_score"),
            func.count(StudentPerformance.id).label("attempts"),
        )
        .filter(
            StudentPerformance.class_id == class_id,
            StudentPerformance.term == term_str,
            StudentPerformance.score > 0,     # skip missed assessments
        )
        .group_by(
            StudentPerformance.student_id,
            StudentPerformance.subject,
        )
        .subquery()
    )

    # ── Join to students — one row per (student, subject)
    subject_rows = (
        db.query(
            Student.id,
            Student.admission_number,
            User.full_name,
            subject_avg.c.subject,
            subject_avg.c.avg_score,
            subject_avg.c.attempts,
        )
        .join(User, User.id == Student.user_id)
        .outerjoin(subject_avg, subject_avg.c.sid == Student.user_id)
        .filter(Student.class_id == class_id)
        .order_by(Student.admission_number.asc(), subject_avg.c.subject.asc())
        .all()
    )

    if not subject_rows:
        raise HTTPException(status_code=404, detail="No Results found")

    # ── Subjects for column headers (alphabetical for now)
    subject_list = sorted({row[3] for row in subject_rows if row[3]})

    # ── Group by student
    by_student = defaultdict(lambda: {
        "id": None,
        "admission_number": None,
        "name": None,
        "subjects": {},
        "scores": [],
    })

    for sid, adm, name, subject, avg_score, _attempts in subject_rows:
        e = by_student[sid]
        e["id"] = sid
        e["admission_number"] = adm
        e["name"] = name
        if subject and avg_score is not None:
            e["subjects"][subject] = round(float(avg_score), 1)
            e["scores"].append(float(avg_score))

    # ── Per-student average across subjects
    students = []
    for sid, e in by_student.items():
        avg = sum(e["scores"]) / len(e["scores"]) if e["scores"] else None
        students.append({
            "id": e["id"],
            "admission_number": e["admission_number"],
            "name": e["name"],
            "subjects": e["subjects"],
            "average": round(avg, 2) if avg is not None else None,
            "grade": _grade_from_percent(avg) if avg is not None else "—",
            "subjects_count": len(e["subjects"]),
        })

    # ── Sort by performance (top first, ungraded last)
    students.sort(
        key=lambda s: (
            s["average"] is None,
            -(s["average"] or 0),
            s["admission_number"] or "",
        )
    )
        
    # ── Build the <th> headers
    headers_html = ""
    for subj in subject_list:
        headers_html += f'<th style="width: 8%;">{subj}<br><small>(100)</small></th>'

    # ── Build the <tr> rows
    rows_html = ""
    for i, st in enumerate(students, start=1):
        cells_html = ""
        for subj in subject_list:
            score = st["subjects"].get(subj)
            if score is not None:
                cells_html += f"<td>{int(round(score))}</td>"
            else:
                cells_html += "<td>—</td>"

        avg = f"{st['average']:.1f}" if st["average"] is not None else "—"
        rows_html += f"""
        <tr>
            <td>{i}</td>
            <td>{st['admission_number'] or ''}</td>
            <td class="text-left" style="padding-left:8px;">{st['name']}</td>
            {cells_html}
            <td class="bold">{avg}</td>
            <td class="bold">{st['grade']}</td>
        </tr>
    """
    body_rows = ""
    for i, st in enumerate(students, start=1):
        # subject cells for this student
        cells = ""
        for subj in subject_list:
            score = st["subjects"].get(subj)
            if score is not None:
                cells += f"<td>{int(round(score))}</td>"
            else:
                cells += "<td>—</td>"

        avg = f"{st['average']:.1f}" if st["average"] is not None else "—"
        adm = st["admission_number"] or ""
        name = st["name"] or ""
        grade = st["grade"] or "—"

        body_rows += f"""
                <tr>
                    <td>{i}</td>
                    <td>{adm}</td>
                    <td class="text-left">{name}</td>
                    {cells}
                    <td>{avg}</td>
                    <td class="bold">{grade}</td>
                </tr>
        """
        
    subject_scores = {}

    for st in students:
        for subj, score in st["subjects"].items():
            if score is not None:
                subject_scores.setdefault(subj, []).append(score)

    # Build the rows
    subject_summary_rows = ""
    for subj in subject_list:
        scores = subject_scores.get(subj, [])
        if scores:
            mean = round(sum(scores) / len(scores), 1)
            grade = _grade_from_percent(mean)
        else:
            mean = "—"
            grade = "—"

        subject_summary_rows += f"""
        <tr>
            <td class="text-left">{subj}</td>
            <td>{mean}</td>
            <td class="bold">{grade}</td>
        </tr>
        """
    GRADE_ORDER = ["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "E"]

    # Count grades
    grade_counts = Counter(
        st["grade"] for st in students
        if st["grade"] and st["grade"] != "—"
    )

    total_graded = sum(grade_counts.values())

    # Build the rows
    grade_dist_rows = ""
    for grade in GRADE_ORDER:
        count = grade_counts.get(grade, 0)
        pct = (count / total_graded * 100) if total_graded > 0 else 0.0
        grade_dist_rows += f"""
        <tr>
            <td class="bold">{grade}</td>
            <td>{count}</td>
            <td>{pct:.1f}%</td>
        </tr>
        """
    graded = [s["average"] for s in students if s["average"] is not None]
    class_mean = round(sum(graded) / len(graded), 1) if graded else None
    class_mean_grade = _grade_from_percent(class_mean) if class_mean is not None else "—"
    
    if class_mean_grade == 'A':
        remarks = "Exceptional performance! You have demonstrated a thorough mastery of the concepts. Keep up the brilliant work"
    elif class_mean_grade == 'A-':
        remarks = "Very impressive performance. With just a little more consistency, you can easily secure a straight A"
    elif class_mean_grade == 'B+':
        remarks = "Good work overall. A bit more focus on refining your problem-solving skills will elevate your grade even further."
    elif class_mean_grade == 'B':
        remarks = "Solid performance. You are demonstrating good understanding, though some key areas still need reinforcement."
    elif class_mean_grade == 'B-':
        remarks = "Fair effort with steady progress. Identify your weaker topics and focus your revision on those specific areas."
    elif class_mean_grade == 'C+':
        remarks = "Satisfactory effort. You have achieved a basic grasp of the concepts, but consistent revision is needed to improve."
    elif class_mean_grade == 'C':
        remarks = "Adequate work, but there is substantial room for improvement. Regular practice will help boost your confidence and marks."
    elif class_mean_grade == 'C-':
        remarks = "Below target performance. Immediate attention, structured revision, and extra guidance are required to build your foundational understanding."
    elif class_mean_grade == 'D+':
        remarks = "Stronger effort required. You are struggling with foundational concepts. Reach out for extra guidance and dedicate time to daily practice"
    elif class_mean_grade == 'D':
        remarks = "Needs improvement. While you show occasional understanding, inconsistent preparation is holding you back. Focus on mastering the core syllabus topics."
    elif class_mean_grade == 'D-':
        remarks = "Poor expectations. Step up your revision routine and seek immediate assistance on difficult subjects to prevent falling further behind."
    else:
        remarks = "Critical attention needed. Seek additional help from your teacher, prioritize basic concepts, and establish a daily study routine to turn this around."
            
    html=f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Student Results - Green Valley High School</title>
    <style>
        body {{
            font-family: Arial, sans-serif;
            color: #333;
            background-color: #f9f9f9;
            margin: 0;
            padding: 20px;
        }}

        .container {{
            max-width: 900px;
            margin: 0 auto;
            background-color: #fff;
            padding: 25px;
            border: 1px solid #ccc;
            box-shadow: 0 0 10px rgba(0,0,0,0.05);
        }}

        /* Header Layout */
        .header-table {{
            width: 100%;
            border-collapse: collapse;
            margin-bottom: 20px;
        }}

        .header-table td {{
            vertical-align: middle;
            border: none;
            padding: 0;
        }}

        .logo-section {{
            width: 60px;
        }}

        .logo-placeholder {{
            width: 70px;
            height: 70px;
            border-radius: 50%;
            border: 2px solid #003875;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #003875;
            font-size: 20px;
        }}

        .title-section {{
            padding-left: 15px !important;
        }}

        .school-title {{
            color: #002b5c;
            font-size: 20px;
            font-weight: bold;
            margin: 0;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .school-motto {{
            color: #4a7bb0;
            font-size: 9px;
            font-weight: bold;
            margin-top: 4px;
            letter-spacing: 1px;
        }}

        .report-title-section {{
            text-align: right;
            border-left: 1px solid #ccc !important;
            padding-left: 15px !important;
        }}

        .report-title {{
            color: #002b5c;
            font-size: 18px;
            font-weight: bold;
            margin: 0;
            text-transform: uppercase;
        }}

        .report-term {{
            color: #002b5c;
            font-size: 13px;
            margin-top: 4px;
        }}

        /* Meta Information Cards */
        .meta-container {{
            background-color: #eef4f8;
            border-radius: 4px;
            padding: 10px 15px;
            margin-bottom: 20px;
            font-size: 13px;
        }}

        .meta-table {{
            width: 100%;
            border-collapse: collapse;
        }}

        .meta-table td {{
            padding: 3px 0;
            border: none;
        }}

        .meta-label {{
            font-weight: bold;
            color: #002b5c;
            width: 15%;
        }}

        .meta-value {{
            width: 35%;
            color: #333;
        }}

        /* Main Data Tables */
        table.data-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 12px;
            margin-bottom: 20px;
        }}

        table.data-table th {{
            background-color: #003875;
            color: #fff;
            padding: 8px 5px;
            font-weight: bold;
            text-align: center;
            border: 1px solid #002b5c;
        }}

        table.data-table td {{
            padding: 6px 5px;
            border: 1px solid #dcdcdc;
            text-align: center;
        }}

        table.data-table tr:nth-child(even) td {{
            background-color: #f4f8fb;
        }}

        table.data-table td.text-left {{
            text-align: left;
            padding-left: 8px;
        }}

        table.data-table td.bold {{
            font-weight: bold;
        }}

        /* Bottom Section Grid */
        .bottom-grid {{
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
        }}

        .grid-col-left {{
            flex: 1.2;
        }}

        .grid-col-mid {{
            flex: 1;
        }}

        .grid-col-right {{
            flex: 1.2;
            display: flex;
            flex-direction: column;
            gap: 15px;
        }}

        .card-header {{
            background-color: #003875;
            color: white;
            font-weight: bold;
            font-size: 12px;
            padding: 6px 10px;
            border-radius: 2px 2px 0 0;
        }}

        .card-body {{
            border: 1px solid #003875;
            border-top: none;
            background-color: #fff;
        }}

        .sub-card {{
            border: 1px solid #003875;
            margin-bottom: 10px;
        }}

        .sub-card-header {{
            background-color: #003875;
            color: white;
            font-weight: bold;
            font-size: 12px;
            padding: 5px 10px;
        }}

        .sub-card-body {{
            padding: 8px 10px;
            font-size: 12px;
            background-color: #fdfdfd;
        }}

        .summary-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 6px 0;
        }}

        .summary-label {{
            font-weight: bold;
            color: #002b5c;
        }}

        .summary-value {{
            font-weight: bold;
            font-size: 13px;
        }}

        .remarks-text {{
            color: #002b5c;
            font-size: 11px;
            line-height: 1.4;
        }}

        .remarks-text p {{
            margin: 3px 0;
        }}

        /* Footer */
        .footer {{
            margin-top: 25px;
            border-top: 1px solid #ccc;
            padding-top: 8px;
            display: flex;
            justify-content: space-between;
            font-size: 10px;
            color: #4a7bb0;
            font-style: italic;
        }}

        .footer-left {{
            text-align: left;
        }}

        .footer-right {{
            text-align: right;
            font-weight: bold;
        }}
    </style>
</head>
<body>

<div class="container">

    <!-- Header -->
    <table class="header-table">
        <tr>
            <td class="logo-section">
                <div class="logo-placeholder"><img style="width: 70%" src="http://127.0.0.1:8000{school.logo if school.logo else 'S'}"></div>
            </td>
            <td class="title-section">
                <div class="school-title">{school.school_name}</div>
                <div class="school-motto">{school.motto}</div>
            </td>
            <td class="report-title-section">
                <div class="report-title">Results</div>
                <div class="report-term">Term {current_term()[1]} ({current_term()[0]})</div>
            </td>
        </tr>
    </table>

    <hr style="border: none; border-top: 2px solid #003875; margin-bottom: 15px;">

    <!-- Meta Details -->
    <div class="meta-container">
        <table class="meta-table">
            <tr>
                <td class="meta-label">Class:</td>
                <td class="meta-value">{class_data.name}</td>
            </tr>
            <tr>
                <td class="meta-label">Class Teacher:</td>
                <td class="meta-value">{user.full_name}</td>
            </tr>
            <tr>
                <td class="meta-label">Academic Year:</td>
                <td class="meta-value">{current_term()[0]}</td>
                <td class="meta-label"></td>
                <td class="meta-value"></td>
            </tr>
        </table>
    </div>

    <!-- Main Results Table -->
    <table class="data-table">
        <thead>
            <tr>
                <th style="width:5%;">No.</th>
                <th style="width:12%;">Adm No.</th>
                <th style="width:25%;text-align:left;padding-left:8px;">Student Name</th>
                {headers_html}
                <th style="width:10%;">Mean<br><small>(%)</small></th>
                <th style="width:8%;">Grade</th>
            </tr>
        </thead>
        <tbody>
            <tr>
                {body_rows}
            </tr>
        </tbody>
    </table>

    <!-- Bottom Tables Layout -->
    <div class="bottom-grid">
        
        <!-- Left Column: Subject Means -->
        <div class="grid-col-left">
            <div class="card-header">Total Mean Grade per Subject</div>
            <div class="card-body">
                <table class="data-table" style="margin-bottom: 0;">
                    <thead>
                        <tr>
                            <th style="text-align: left; padding-left: 8px;">Subject</th>
                            <th>Total Mean (%)</th>
                            <th>Grade</th>
                        </tr>
                    </thead>
                    <tbody>
                        {subject_summary_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Middle Column: Grade Distribution -->
        <div class="grid-col-mid">
            <div class="card-header">Grade Distribution</div>
            <div class="card-body">
                <table class="data-table" style="margin-bottom: 0;">
                    <thead>
                        <tr>
                            <th>Grade</th>
                            <th>Count</th>
                            <th>Percentage</th>
                        </tr>
                    </thead>
                    <tbody>
                        {grade_dist_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Right Column: Summaries & Remarks -->
        <div class="grid-col-right">
            
            <div class="sub-card">
                <div class="sub-card-header">Class Summary</div>
                <div class="sub-card-body">
                    <div class="summary-row">
                        <span class="summary-label">Whole Class Mean Grade</span>
                        <span class="summary-value">{class_mean}</span>
                    </div>
                    <div class="summary-row" style="margin-top: 5px;">
                        <span class="summary-label">Overall Grade</span>
                        <span class="summary-value" style="font-size: 15px;">{class_mean_grade}</span>
                    </div>
                </div>
            </div>
            <div class="sub-card">
                <div class="sub-card-header">Remarks</div>
                <div class="sub-card-body remarks-text">
                    <p>{remarks}</p>
                </div>
            </div>

        </div>

    </div>

    <!-- Footer -->
    <div class="footer">
        <div class="footer-left">This is a computer generated report and does not require a signature.</div>
        <div class="footer-right">Thankyou for using Scholin' We LOVE you</div>
    </div>

</div>

</body>
</html>
    """
    pdf_bytes = HTML(string=html).write_pdf()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="results {school.school_name} {class_data.name}.pdf"'
        },
    )
    
@router.get("/stream-results", response_class=HTMLResponse)
def get_stream_results(
    class_ids: str = Query(..., description="Comma-separated class IDs"),
    title: Optional[str] = Query(None),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role not in ("admin", "school"):
        raise HTTPException(status_code=403, detail="Admins only")

    try:
        ids = [int(x.strip()) for x in class_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid class_ids")

    if not ids:
        raise HTTPException(status_code=400, detail="No class IDs given")

    classes = (
        db.query(Class)
        .filter(Class.id.in_(ids), Class.school_id == user.school_id)
        .all()
    )
    if not classes:
        raise HTTPException(status_code=404, detail="No matching classes")

    real_ids = [c.id for c in classes]

    school = db.query(School).filter(School.id == user.school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School Not Found")

    from collections import defaultdict, Counter

    # ── Per (student, subject): mean of all non-zero scores in the current term
    _, term_num = current_term()
    term_str = f"Term {term_num}"

    subject_avg = (
        db.query(
            StudentPerformance.student_id.label("sid"),
            StudentPerformance.subject.label("subject"),
            func.avg(StudentPerformance.score).label("avg_score"),
            func.count(StudentPerformance.id).label("attempts"),
        )
        .filter(
            StudentPerformance.class_id.in_(real_ids),
            StudentPerformance.term == term_str,
            StudentPerformance.score > 0,
        )
        .group_by(
            StudentPerformance.student_id,
            StudentPerformance.subject,
        )
        .subquery()
    )

    subject_rows = (
        db.query(
            Student.id,
            Student.admission_number,
            User.full_name,
            subject_avg.c.subject,
            subject_avg.c.avg_score,
            subject_avg.c.attempts,
        )
        .join(User, User.id == Student.user_id)
        .outerjoin(subject_avg, subject_avg.c.sid == Student.user_id)
        .filter(Student.class_id.in_(real_ids))
        .order_by(Student.admission_number.asc(), subject_avg.c.subject.asc())
        .all()
    )

    if not subject_rows:
        raise HTTPException(status_code=404, detail="No Results found")

    subject_list = sorted({row[3] for row in subject_rows if row[3]})

    by_student = defaultdict(lambda: {
        "id": None,
        "admission_number": None,
        "name": None,
        "subjects": {},
        "scores": [],
    })

    for sid, adm, name, subject, avg_score, _attempts in subject_rows:
        e = by_student[sid]
        e["id"] = sid
        e["admission_number"] = adm
        e["name"] = name
        if subject and avg_score is not None:
            e["subjects"][subject] = round(float(avg_score), 1)
            e["scores"].append(float(avg_score))

    students = []
    for sid, e in by_student.items():
        avg = sum(e["scores"]) / len(e["scores"]) if e["scores"] else None
        students.append({
            "id": e["id"],
            "admission_number": e["admission_number"],
            "name": e["name"],
            "subjects": e["subjects"],
            "average": round(avg, 2) if avg is not None else None,
            "grade": _grade_from_percent(avg) if avg is not None else "—",
            "subjects_count": len(e["subjects"]),
        })

    students.sort(
        key=lambda s: (
            s["average"] is None,
            -(s["average"] or 0),
            s["admission_number"] or "",
        )
    )

    headers_html = ""
    for subj in subject_list:
        headers_html += f'<th style="width: 8%;">{subj}<br><small>(100)</small></th>'

    body_rows = ""
    for i, st in enumerate(students, start=1):
        cells = ""
        for subj in subject_list:
            score = st["subjects"].get(subj)
            if score is not None:
                cells += f"<td>{int(round(score))}</td>"
            else:
                cells += "<td>—</td>"

        avg = f"{st['average']:.1f}" if st["average"] is not None else "—"
        adm = st["admission_number"] or ""
        name = st["name"] or ""
        grade = st["grade"] or "—"

        body_rows += f"""
                <tr>
                    <td>{i}</td>
                    <td>{adm}</td>
                    <td class="text-left">{name}</td>
                    {cells}
                    <td>{avg}</td>
                    <td class="bold">{grade}</td>
                </tr>
        """

    subject_scores = {}
    for st in students:
        for subj, score in st["subjects"].items():
            if score is not None:
                subject_scores.setdefault(subj, []).append(score)

    subject_summary_rows = ""
    for subj in subject_list:
        scores = subject_scores.get(subj, [])
        if scores:
            mean = round(sum(scores) / len(scores), 1)
            grade = _grade_from_percent(mean)
        else:
            mean = "—"
            grade = "—"

        subject_summary_rows += f"""
        <tr>
            <td class="text-left">{subj}</td>
            <td>{mean}</td>
            <td class="bold">{grade}</td>
        </tr>
        """

    GRADE_ORDER = ["A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "E"]
    grade_counts = Counter(
        st["grade"] for st in students
        if st["grade"] and st["grade"] != "—"
    )
    total_graded = sum(grade_counts.values())

    grade_dist_rows = ""
    for grade in GRADE_ORDER:
        count = grade_counts.get(grade, 0)
        pct = (count / total_graded * 100) if total_graded > 0 else 0.0
        grade_dist_rows += f"""
        <tr>
            <td class="bold">{grade}</td>
            <td>{count}</td>
            <td>{pct:.1f}%</td>
        </tr>
        """

    graded = [s["average"] for s in students if s["average"] is not None]
    class_mean = round(sum(graded) / len(graded), 1) if graded else None
    class_mean_grade = _grade_from_percent(class_mean) if class_mean is not None else "—"

    if class_mean_grade == 'A':
        remarks = "Exceptional performance! You have demonstrated a thorough mastery of the concepts. Keep up the brilliant work"
    elif class_mean_grade == 'A-':
        remarks = "Very impressive performance. With just a little more consistency, you can easily secure a straight A"
    elif class_mean_grade == 'B+':
        remarks = "Good work overall. A bit more focus on refining your problem-solving skills will elevate your grade even further."
    elif class_mean_grade == 'B':
        remarks = "Solid performance. You are demonstrating good understanding, though some key areas still need reinforcement."
    elif class_mean_grade == 'B-':
        remarks = "Fair effort with steady progress. Identify your weaker topics and focus your revision on those specific areas."
    elif class_mean_grade == 'C+':
        remarks = "Satisfactory effort. You have achieved a basic grasp of the concepts, but consistent revision is needed to improve."
    elif class_mean_grade == 'C':
        remarks = "Adequate work, but there is substantial room for improvement. Regular practice will help you boost your confidence and marks."
    elif class_mean_grade == 'C-':
        remarks = "Below target performance. Immediate attention, structured revision, and extra guidance are required to build your foundational understanding."
    elif class_mean_grade == 'D+':
        remarks = "Stronger effort required. You are struggling with foundational concepts. Reach out for extra guidance and dedicate time to daily practice"
    elif class_mean_grade == 'D':
        remarks = "Needs improvement. While you show occasional understanding, inconsistent preparation is holding you back. Focus on mastering the core syllabus topics."
    elif class_mean_grade == 'D-':
        remarks = "Poor expectations. Step up your revision routine and seek immediate assistance on difficult subjects to prevent falling further behind."
    else:
        remarks = "Critical attention needed. Seek additional help from your teacher, prioritize basic concepts, and establish a daily study routine to turn this around."

    # ── Title for the header
    page_title = title or (
        classes[0].name if len(classes) == 1
        else f"{len(classes)} Streams — Combined Results"
    )

    # ── School name — check which column exists
    school_name = getattr(school, "school_name", None) or getattr(school, "name", "") or ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{page_title}</title>
    <style>
        body {{ font-family: Arial, sans-serif; color: #333; background: #f9f9f9; margin: 0; padding: 20px; }}
        .container {{ max-width: 900px; margin: 0 auto; background: #fff; padding: 25px; border: 1px solid #ccc; }}
        h1 {{ color: #002b5c; font-size: 20px; margin: 0 0 4px; text-transform: uppercase; }}
        h2 {{ color: #003875; font-size: 14px; margin: 20px 0 8px; }}
        .meta {{ color: #666; font-size: 12px; margin-bottom: 16px; }}
        table.data-table {{ width: 100%; border-collapse: collapse; font-size: 12px; margin-bottom: 20px; }}
        table.data-table th {{ background: #003875; color: #fff; padding: 6px 5px;
                               font-weight: bold; text-align: center; border: 1px solid #002b5c; }}
        table.data-table td {{ padding: 6px 5px; border: 1px solid #dcdcdc; text-align: center; }}
        table.data-table tr:nth-child(even) td {{ background: #f4f8fb; }}
        table.data-table td.text-left {{ text-align: left; padding-left: 8px; }}
        table.data-table td.bold {{ font-weight: bold; }}
        small {{ font-size: 9px; font-weight: normal; }}
        .remarks {{ margin-top: 20px; padding: 12px; background: #eef4f8; border-left: 4px solid #003875; font-size: 12px; color: #002b5c; line-height: 1.4; }}
    </style>
</head>
<body>
<div class="container">

    <h1>{page_title}</h1>
    <div class="meta">{school_name} · {term_str}</div>

    <h2>Class Mean: {class_mean if class_mean is not None else '—'}% ({class_mean_grade})</h2>

    <h2>Student Performance</h2>
    <table class="data-table">
        <thead>
            <tr>
                <th style="width: 5%;">No.</th>
                <th style="width: 12%;">Adm No.</th>
                <th style="width: 25%; text-align: left; padding-left: 8px;">Student Name</th>
                {headers_html}
                <th style="width: 10%;">Mean<br><small>(%)</small></th>
                <th style="width: 8%;">Grade</th>
            </tr>
        </thead>
        <tbody>
            {body_rows}
        </tbody>
    </table>

    <h2>Subject Averages</h2>
    <table class="data-table">
        <thead>
            <tr>
                <th style="text-align: left; padding-left: 8px;">Subject</th>
                <th style="width: 20%;">Mean</th>
                <th style="width: 20%;">Grade</th>
            </tr>
        </thead>
        <tbody>
            {subject_summary_rows}
        </tbody>
    </table>

    <h2>Grade Distribution</h2>
    <table class="data-table">
        <thead>
            <tr>
                <th style="width: 40%;">Grade</th>
                <th style="width: 30%;">Count</th>
                <th style="width: 30%;">Percentage</th>
            </tr>
        </thead>
        <tbody>
            {grade_dist_rows}
        </tbody>
    </table>

    <div class="remarks">
        <strong>Remarks:</strong> {remarks}
    </div>

</div>
</body>
</html>
    """

    pdf_bytes = HTML(string=html).write_pdf()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'inline; filename="stream_results_{"_".join(str(i) for i in real_ids)}.pdf"',
        },
    )
    
@router.get("/fees-report", response_class=HTMLResponse)
def get_fees_report(
    year: Optional[str] = Query(None),
    term: Optional[str] = Query(None),
    user=Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if user.role not in ("admin", "school"):
        raise HTTPException(status_code=403, detail="Admins only")

    school = db.query(School).filter(School.id == user.school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School Not Found")

    from collections import defaultdict

    # ── Term + year
    y = int(year) if year and year.isdigit() else datetime.utcnow().year
    term_num = current_term()[1]
    if term and term.startswith("Term "):
        try:
            term_num = int(term.split(" ")[1])
        except (IndexError, ValueError):
            pass
    term_str = f"Term {term_num}"

    # ── All fees for this school's year
    fee_rows = (
        db.query(Fee, Student, User)
        .join(Student, Student.user_id == Fee.student_id)
        .join(User, User.id == Student.user_id)
        .filter(
            Student.school_id == user.school_id,
            Fee.academic_year == y,
        )
        .all()
    )

    if not fee_rows:
        raise HTTPException(status_code=404, detail="No fee data")

    # ── Group per student
    by_student: dict[int, dict] = {}

    for fee, student, student_user in fee_rows:
        key = student.id
        e = by_student.setdefault(key, {
            "name": student_user.full_name,
            "adm": student.admission_number or "",
            "class": student.class_name or "Unassigned",
            "current_billed": 0,
            "current_paid": 0,
            "prev_billed": 0,
            "prev_paid": 0,
        })

        if fee.term_number == term_num:
            e["current_billed"] += (fee.amount or 0)
            e["current_paid"]   += (fee.paid or 0)
        else:
            e["prev_billed"] += (fee.amount or 0)
            e["prev_paid"]   += (fee.paid or 0)

    # ── Compute final per-student state
    students = []
    for sid, e in by_student.items():
        cur_bal = e["current_billed"] - e["current_paid"]
        prev_bal = e["prev_billed"] - e["prev_paid"]

        overpaid = max(0, cur_bal * -1)          # paid more than billed this term
        cur_owed = max(0, cur_bal)                # owing this term
        prev_owed = max(0, prev_bal)              # owing from earlier terms

        total_owed = cur_owed + prev_owed

        # Payment rate against current-term bill only
        if e["current_billed"] > 0:
            rate = round((e["current_paid"] / e["current_billed"]) * 100)
        else:
            rate = 100 if prev_owed == 0 else 0

        # ── Status colour
        # Priority: red > yellow > gold > green
        if rate < 30 and total_owed > 0:
            status = "critical"      # red
        elif prev_owed > 0:
            status = "debt"          # yellow — has prior balance
        elif overpaid > 0:
            status = "overpaid"      # gold
        elif total_owed == 0:
            status = "clear"         # green
        else:
            status = "partial"       # neutral (paid some, still owing this term)

        students.append({
            "id": sid,
            "name": e["name"],
            "adm": e["adm"],
            "class": e["class"],
            "current_billed": e["current_billed"],
            "current_paid": e["current_paid"],
            "overpaid": overpaid,
            "prev_owed": prev_owed,
            "total_owed": total_owed,
            "rate": rate,
            "status": status,
        })

    # ── Group by class
    by_class: dict[str, list[dict]] = defaultdict(list)
    for s in students:
        by_class[s["class"]].append(s)

    # Sort classes alphabetically (Form 1, Form 2, ...)
    def class_sort_key(name: str) -> tuple:
        import re
        m = re.match(r"Form\s*(\d+)", name, re.IGNORECASE)
        return (int(m.group(1)) if m else 999, name)

    sorted_classes = sorted(by_class.keys(), key=class_sort_key)

    # Sort students within each class by admission number
    for c in sorted_classes:
        by_class[c].sort(key=lambda s: s["adm"] or "")

    # ── Colour palette
    STATUS_COLORS = {
        "clear":    ("#d4edda", "#155724"),   # green
        "overpaid": ("#d4edda", "#155724"),   # green
        "debt":     ("#ffffff", "#333333"),   # yellow (prior balance)
        "partial":  ("#ffffff", "#333333"),   # plain
        "critical": ("#ffffff", "#333333"),   # red
    }

    def kes(n: int) -> str:
        return f"{n:,}"

    # ── Build one page per class
    pages = []
    for idx, cls_name in enumerate(sorted_classes):
        rows = ""
        for i, s in enumerate(by_class[cls_name], start=1):
            bg, fg = STATUS_COLORS.get(s["status"], ("#ffffff", "#333333"))
            rows += f"""
            <tr style="background-color:{bg}; color:{fg};">
                <td>{i}</td>
                <td>{s['adm']}</td>
                <td class="text-left">{s['name']}</td>
                <td>{kes(s['current_billed'])}</td>
                <td>{kes(s['current_paid'])}</td>
                <td>{kes(s['prev_owed']) if s['prev_owed'] > 0 else '—'}</td>
                <td>{kes(s['overpaid']) if s['overpaid'] > 0 else '—'}</td>
                <td class="bold">{kes(s['total_owed']) if s['total_owed'] > 0 else '0'}</td>
                <td class="bold">{s['rate']}%</td>
            </tr>
            """

        # Totals for this class
        c_billed = sum(s["current_billed"] for s in by_class[cls_name])
        c_paid = sum(s["current_paid"] for s in by_class[cls_name])
        c_owed = sum(s["total_owed"] for s in by_class[cls_name])
        c_rate = round((c_paid / c_billed) * 100) if c_billed > 0 else 0

        page_style = "page-break-before: always;" if idx > 0 else ""
        pages.append(f"""
        <div class="class-page" style="{page_style}">
            <h2>{cls_name} · {len(by_class[cls_name])} students</h2>

            <table class="data-table">
                <thead>
                    <tr>
                        <th style="width:4%;">No.</th>
                        <th style="width:11%;">Adm No.</th>
                        <th style="width:24%;text-align:left;padding-left:8px;">Student Name</th>
                        <th style="width:10%;">Billed</th>
                        <th style="width:10%;">Paid</th>
                        <th style="width:10%;">Prev. Debt</th>
                        <th style="width:9%;">Overpaid</th>
                        <th style="width:10%;">Total Owed</th>
                        <th style="width:8%;">Rate</th>
                    </tr>
                </thead>
                <tbody>{rows}</tbody>
                <tfoot>
                    <tr style="background:#e9ecef; font-weight:bold;">
                        <td colspan="3" class="text-left">Class Total</td>
                        <td>{kes(c_billed)}</td>
                        <td>{kes(c_paid)}</td>
                        <td colspan="2"></td>
                        <td>{kes(c_owed)}</td>
                        <td>{c_rate}%</td>
                    </tr>
                </tfoot>
            </table>
        </div>
        """)

    pages_html = "".join(pages)

    # ── School-wide totals (header summary)
    total_billed = sum(s["current_billed"] for s in students)
    total_paid = sum(s["current_paid"] for s in students)
    total_owed = sum(s["total_owed"] for s in students)
    total_over = sum(s["overpaid"] for s in students)
    total_debt = sum(s["prev_owed"] for s in students)
    overall_rate = round((total_paid / total_billed) * 100) if total_billed > 0 else 0

    # Counts
    cnt_clear = sum(1 for s in students if s["status"] == "clear")
    cnt_over = sum(1 for s in students if s["status"] == "overpaid")
    cnt_debt = sum(1 for s in students if s["status"] == "debt")
    cnt_critical = sum(1 for s in students if s["status"] == "critical")

    school_name = school.school_name or ""

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Fees Report · {term_str} {y}</title>
<style>
    @page {{ size: A4; margin: 14mm; }}
    body {{ font-family: Arial, sans-serif; color: #222; font-size: 12px; }}
    h1 {{ color: #5a2d82; font-size: 22px; margin: 0 0 4px; text-transform: uppercase; }}
    h2 {{ color: #5a2d82; font-size: 14px; margin: 16px 0 8px; }}
    .meta {{ color: #555; font-size: 12px; margin-bottom: 14px; }}

    .summary {{ display: flex; gap: 8px; margin-bottom: 16px; }}
    .card {{
        flex: 1; padding: 10px; border-radius: 6px;
        background: #f3ecf7; border-left: 4px solid #5a2d82;
    }}
    .card.green  {{ background:#e8f5e9; border-left-color:#2e7d32; }}
    .card.gold   {{ background:#fff8e1; border-left-color:#b8860b; }}
    .card.yellow {{ background:#fffde7; border-left-color:#c9a200; }}
    .card.red    {{ background:#fdecea; border-left-color:#c62828; }}
    .card .label {{ font-size: 10px; color: #666; text-transform: uppercase; }}
    .card .value {{ font-size: 18px; font-weight: bold; color: #5a2d82; margin-top: 2px; }}

    .legend {{ display:flex; gap:8px; font-size:11px; margin-bottom:14px; }}
    .legend span {{ padding:2px 8px; border-radius:10px; }}
    .lg-clear    {{ background:#d4edda; color:#155724; }}
    .lg-overpaid {{ background:#fff3cd; color:#856404; }}
    .lg-debt     {{ background:#fff8b3; color:#7a5b00; }}
    .lg-critical {{ background:#f8d7da; color:#721c24; }}

    table.data-table {{ width: 100%; border-collapse: collapse; font-size: 11px; }}
    table.data-table th {{
        background: #5a2d82; color: #fff; padding: 6px 4px;
        border: 1px solid #3d1d5e; text-align: center; font-weight: bold;
    }}
    table.data-table td {{
        padding: 5px 4px; border: 1px solid #dcdcdc; text-align: center;
    }}
    table.data-table td.text-left {{ text-align: left; padding-left: 8px; }}
    table.data-table td.bold {{ font-weight: bold; }}
    .class-page {{ page-break-inside: avoid; }}
</style>
</head>
<body>

    <h1>{school_name}</h1>
    <div class="meta">Fees Report · {term_str} · {y}</div>

    <div class="summary">
        <div class="card">
            <div class="label">Billed</div>
            <div class="value">{kes(total_billed)}</div>
        </div>
        <div class="card green">
            <div class="label">Collected</div>
            <div class="value">{kes(total_paid)}</div>
        </div>
        <div class="card red">
            <div class="label">Outstanding</div>
            <div class="value">{kes(total_owed)}</div>
        </div>
        <div class="card gold">
            <div class="label">Overpaid</div>
            <div class="value">{kes(total_over)}</div>
        </div>
        <div class="card">
            <div class="label">Collection Rate</div>
            <div class="value">{overall_rate}%</div>
        </div>
    </div>

    <div class="legend">
        <span class="lg-clear">Fully Paid ({cnt_clear})</span>
        <span class="lg-overpaid">Overpaid ({cnt_over})</span>
        <span class="lg-debt">Prev. Debt ({cnt_debt})</span>
        <span class="lg-critical">Critical &lt;30% ({cnt_critical})</span>
    </div>

    {pages_html}

</body>
</html>
"""

    pdf_bytes = HTML(string=html).write_pdf()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'inline; filename="fees_report_{y}_{term_str.replace(" ", "_")}.pdf"',
        },
    )
