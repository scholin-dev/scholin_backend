import sqlalchemy
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text, Date, Float, UniqueConstraint, Index
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship
from ..core.database import Base

class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        Index('idx_users_role', 'role'),
        Index('idx_users_school_id', 'school_id'),
        Index('idx_users_approval', 'approval_status'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    email = Column(String(100), unique=True, index=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(String(30), nullable=False)
    phone = Column(String(30), nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    profile_picture = Column(String, nullable=True)
    approval_status = Column(String(20), default='pending')
    school_id = Column(Integer, ForeignKey("schools.id", ondelete="SET NULL"), nullable=True)
    is_online = Column(Boolean, default=False)
    last_seen = Column(DateTime(timezone=True), nullable=True)
    
    # Relationships - use different names to avoid conflicts
    student_profile = relationship("Student", back_populates="user", uselist=False)
    teacher_profile = relationship("Teacher", back_populates="user", uselist=False)
    parent_profile = relationship("Parent", back_populates="user_ref", uselist=False)
    worker_profile = relationship("Worker", back_populates="user", uselist=False)

class Student(Base):
    __tablename__ = "students"
    __table_args__ = (
        Index('idx_students_school_id', 'school_id'),
        Index('idx_students_class_id', 'class_id'),
        Index('idx_students_class_name', 'class_name'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    admission_number = Column(String(50), unique=True, nullable=True)
    school_name = Column(String(200), nullable=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=True)
    class_name = Column(String(50), nullable=True)
    gender = Column(String(10), nullable=True)
    profile_picture = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    enrolled_date = Column(String(20), nullable=True)
    
    # Relationships
    user = relationship("User", back_populates="student_profile")
    class_ = relationship("Class", back_populates="students")
    parent_students = relationship("ParentStudent", back_populates="student")

class Teacher(Base):
    __tablename__ = "teachers"
    __table_args__ = (
        Index('idx_teachers_school_id', 'school_id'),
        Index('idx_teachers_user_id', 'user_id'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    subject = Column(String(1000), nullable=True)
    qualification = Column(String(200), nullable=True)
    years_of_experience = Column(String(10), nullable=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True)
    profile_picture = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    joined_date = Column(String(20), nullable=True)
    
    # Relationships
    user = relationship("User", back_populates="teacher_profile")
    class_teacher_of = relationship("Class", foreign_keys="Class.class_teacher_id", back_populates="class_teacher")
    subject_teaching = relationship("ClassSubjectTeacher", back_populates="teacher")

class Parent(Base):
    __tablename__ = "parents"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    relation_type = Column(String(50), nullable=True)
    student_id = Column(Integer, ForeignKey("students.id"), nullable=True)
    profile_picture = Column(String, nullable=True)
    
    # Relationships - FIXED: use 'user' as relationship name
    user_ref = relationship("User", back_populates="parent_profile", foreign_keys=[user_id])
    parent_students = relationship("ParentStudent", back_populates="parent")

class School(Base):
    __tablename__ = "schools"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    school_name = Column(String(200), nullable=True)
    address = Column(String(300), nullable=True)
    school_type = Column(String(50), nullable=True)
    motto = Column(String(300), nullable=True)
    logo = Column(String, nullable=True)
    stamp = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    email = Column(String, nullable=True)
    signatory_name = Column(String(100), nullable=True)
    signatory_title = Column(String(100), nullable=True)
    sms_bal = Column(Integer, nullable=True)
    
    # Relationships
    classes = relationship("Class", back_populates="school")
    user = relationship("User", foreign_keys=[user_id])

class PasswordReset(Base):
    __tablename__ = "password_resets"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String(100), index=True, nullable=False)
    code = Column(String(6), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_used = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class StudentPerformance(Base):
    __tablename__ = "student_performances"
    __table_args__ = (
        Index('idx_performances_student', 'student_id'),
        Index('idx_performances_subject', 'subject'),
        Index('idx_performances_term', 'term'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=False)
    assessment = Column(String(20), nullable=False)
    subject = Column(String(100), nullable=False)
    score = Column(Integer, nullable=False)
    term = Column(String(50), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
class FeeStructure(Base):
    __tablename__ = "fee_structures"
    __table_args__ = (
        Index('idx_fee_structure_school', 'school_id'),
        Index('idx_fee_structure_year', 'academic_year'),
    )

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id", ondelete="CASCADE"), nullable=False)
    academic_year = Column(Integer, nullable=False)  # 2026
    term_number = Column(Integer, nullable=False)  # 1, 2, 3
    term_name = Column(String(50), nullable=False)  # "Term 1 - 2026"
    amount = Column(Integer, nullable=False)  # Fee amount for this term
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationship
    school = relationship("School")

class Fee(Base):
    __tablename__ = "fees"
    __table_args__ = (
        UniqueConstraint("student_id", "academic_year", "term_number",
                         name="uq_fee_student_year_term"),
        Index('idx_fees_student', 'student_id'),
        Index('idx_fees_term', 'term_number'),
        Index('idx_fees_status', 'status'),
        Index('idx_fees_year_term', 'academic_year', 'term_number'),
    )

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Integer, nullable=False)
    paid = Column(Integer, default=0, nullable=False)
    balance = Column(Integer, nullable=True)
    term_number = Column(Integer, nullable=False, default=1)
    term_name = Column(String(50), nullable=True)
    academic_year = Column(Integer, nullable=True)
    status = Column(String(20), default="pending", nullable=False)
    due_date = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
class FeeTransaction(Base):
    __tablename__ = "fee_transactions"
    __table_args__ = (
        Index("idx_fee_transactions_student", "student_id"),
        Index("idx_fee_transactions_fee", "fee_id"),
        Index("idx_fee_transactions_provider", "payment_provider"),
    )

    id = Column(Integer, primary_key=True, index=True)
    fee_id = Column(Integer, ForeignKey("fees.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    amount = Column(Integer, nullable=False)
    payment_provider = Column(String(30), nullable=False)
    transaction_reference = Column(String(100), unique=True, nullable=True)
    payment_date = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index('idx_events_school', 'school_id', 'event_date'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(String(500), nullable=True)
    event_date = Column(DateTime(timezone=True), nullable=False)
    time = Column(String(20), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Announcement(Base):
    __tablename__ = "announcements"
    __table_args__ = (
        Index('idx_announcements_school', 'school_id'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(String(1000), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True) 
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Activity(Base):
    __tablename__ = "activities"
    __table_args__ = (
        Index('idx_activities_user', 'user_id'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(200), nullable=False)
    role = Column(String(100), nullable=False)
    icon_name = Column(String(50), default="book")
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Timetable(Base):
    __tablename__ = "timetables"
    __table_args__ = (
        Index('idx_timetables_school_class', 'school_id', 'class_name'),
        Index('idx_timetables_teacher', 'teacher_id'),
        Index('idx_timetables_day', 'day_of_week'),
        Index('idx_timetables_teacher_day', 'teacher_id', 'day_of_week'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id", ondelete="CASCADE"), nullable=False)
    class_name = Column(String(50), nullable=False)
    day_of_week = Column(String(20), nullable=False)
    start_time = Column(String(10), nullable=False)
    end_time = Column(String(10), nullable=False)
    subject = Column(String(100), nullable=False)
    teacher_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    room = Column(String(50), nullable=True)
    is_break = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class Worker(Base):
    __tablename__ = "workers"
    __table_args__ = (
        Index('idx_workers_school_id', 'school_id'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    school_name = Column(String(200), nullable=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True)
    department = Column(String(100), nullable=True)
    role_title = Column(String(100), nullable=True)
    supervisor = Column(String(100), nullable=True)
    employment_type = Column(String(50), nullable=True)
    joined_date = Column(String(20), nullable=True)
    location = Column(String(200), nullable=True)
    profile_picture = Column(String, nullable=True)
    
    # Relationships
    user = relationship("User", back_populates="worker_profile")

class WorkerAttendance(Base):
    __tablename__ = "worker_attendance"
    __table_args__ = (
        Index('idx_worker_attendance_worker', 'worker_id', 'date'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    worker_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    date = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False)  # present, absent, leave
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class WorkerPerformance(Base):
    __tablename__ = "worker_performance"
    __table_args__ = (
        Index('idx_worker_performance_worker', 'worker_id'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    worker_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    category = Column(String(100), nullable=False)  # Work Quality, Punctuality, etc.
    rating = Column(Integer, nullable=False)  # 1-5
    review_date = Column(DateTime(timezone=True), nullable=False)
    notes = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Assignment(Base):
    __tablename__ = "assignments"
    __table_args__ = (
        Index('idx_assignments_teacher', 'teacher_id'),
        Index('idx_assignments_class', 'class_name'),
        Index('idx_assignments_teacher_status', 'teacher_id', 'status'),
        Index('idx_assignments_teacher_created', 'teacher_id', 'created_at'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    teacher_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(String(500), nullable=True)
    subject = Column(String(100), nullable=False)
    class_name = Column(String(50), nullable=False)
    due_date = Column(String(20), nullable=True)
    due_time = Column(String(10), nullable=True)
    total_students = Column(Integer, default=0)
    submitted_count = Column(Integer, default=0)
    status = Column(String(20), default="Active")
    assignment_type = Column(String(20), nullable=True)
    file_path = Column(String(50), nullable=True)
    typing_questions = Column(Text, nullable=True)
    mcq_questions = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True, index=True)
    posted_by = Column(Integer)
    title = Column(String(200), nullable=False)
    description = Column(String(1000), nullable=True)
    location = Column(String(200), nullable=True)
    type = Column(String(50), nullable=False)  # Full-time, Part-time, Contract, Remote
    salary = Column(String(100), nullable=True)
    requirements = Column(String(500), nullable=True)
    applicants = Column(Integer, default=0)
    deadline = Column(String(20), nullable=True)
    is_gig = Column(Boolean, default=False)
    working_hours = Column(String(50), nullable=True)
    duration = Column(String(50), nullable=True)
    amount_type = Column(String(50), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

class Book(Base):
    __tablename__ = "books"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer)
    title = Column(String(200), nullable=False)
    author = Column(String(100), nullable=False)
    description = Column(String(1000), nullable=True)
    category = Column(String(50), nullable=False)
    price = Column(Integer, default=0)
    is_free = Column(Boolean, default=True)
    rating = Column(Integer, default=0)
    views = Column(Integer, default=0)
    downloads = Column(Integer, default=0)
    likes = Column(Integer, default=0)
    published_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    published_date = Column(String(20), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    image_url = Column(String, nullable=True)
    file_path = Column(String, nullable=True)
    
class View(Base):
    __tablename__ = "views"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer)
    book_id = Column(Integer)

class ParentStudent(Base):
    __tablename__ = "parent_students"
    __table_args__ = (
        Index('idx_parent_student_parent', 'parent_id'),
        Index('idx_parent_student_student', 'student_id'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    parent_id = Column(Integer, ForeignKey("parents.id", ondelete="CASCADE"), nullable=False)
    student_id = Column(Integer, ForeignKey("students.id", ondelete="CASCADE"), nullable=False)
    relation_type = Column(String(50), nullable=True)
    
    # Relationships
    parent = relationship("Parent", back_populates="parent_students")
    student = relationship("Student", back_populates="parent_students")

class Session(Base):
    __tablename__ = "sessions"
    id = Column(Integer, primary_key=True, index=True)
    session_key = Column(String(64), unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(String(500), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
class FeePayment(Base):
    __tablename__ = "fee_payments"
    __table_args__ = (
        Index('idx_fee_payments_student', 'student_id'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    term = Column(String(50), nullable=False)
    amount = Column(Integer, nullable=False)
    payment_date = Column(Date, nullable=False)
    payment_method = Column(String(50), default="Cash")
    reference_number = Column(String(100), nullable=True)
    receipt_number = Column(String(50), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
class Class(Base):
    """Class/Form table with class teacher"""
    __tablename__ = "classes"
    __table_args__ = (
        Index('idx_classes_school', 'school_id'),
        Index('idx_classes_teacher', 'class_teacher_id'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(50), nullable=False)
    school_id = Column(Integer, ForeignKey("schools.id", ondelete="CASCADE"), nullable=False)
    class_teacher_id = Column(Integer, ForeignKey("teachers.id", ondelete="SET NULL"), nullable=True)
    room = Column(String(20), nullable=True)
    capacity = Column(Integer, default=45)
    academic_year = Column(String(20), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    school = relationship("School", back_populates="classes")
    class_teacher = relationship("Teacher", foreign_keys=[class_teacher_id], back_populates="class_teacher_of")
    students = relationship("Student", back_populates="class_")
    subject_teachers = relationship("ClassSubjectTeacher", back_populates="class_")

class ClassSubjectTeacher(Base):
    """Junction table for subject teachers per class"""
    __tablename__ = "class_subject_teachers"
    __table_args__ = (
        UniqueConstraint('class_id', 'subject', name='uq_class_subject'),
        Index('idx_cst_class', 'class_id'),
        Index('idx_cst_teacher', 'teacher_id'),
        Index('idx_cst_teacher_active', 'teacher_id', 'is_active'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    class_id = Column(Integer, ForeignKey("classes.id", ondelete="CASCADE"), nullable=False)
    teacher_id = Column(Integer, ForeignKey("teachers.id", ondelete="CASCADE"), nullable=False)
    subject = Column(String(100), nullable=False)
    is_primary = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    class_ = relationship("Class", back_populates="subject_teachers")
    teacher = relationship("Teacher", back_populates="subject_teaching")
    
class StudentAttendance(Base):
    __tablename__ = "student_attendance"
    __table_args__ = (
        Index('idx_student_attendance_student', 'student_id', 'date'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    date = Column(DateTime(timezone=True), nullable=False)
    status = Column(String(20), nullable=False)  # present, absent, late, excused
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
class RevisionMaterial(Base):
    __tablename__ = "revision_materials"
    
    id = Column(Integer, primary_key=True, index=True)
    subject = Column(String(100), nullable=False)
    title = Column(String(200), nullable=False)
    description = Column(String(500), nullable=True)
    type = Column(String(50), nullable=False)
    file_url = Column(String, nullable=True)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
class TeacherEmploymentHistory(Base):
    __tablename__ = "teacher_employment_history"
    
    id = Column(Integer, primary_key=True, index=True)
    teacher_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=False)
    school_name = Column(String(200))
    subject_taught = Column(String(500))  # e.g., "Mathematics, Physics"
    classes_taught = Column(String(500))  # e.g., "Form 1A, Form 2B, Form 3C"
    start_date = Column(Date, nullable=False)
    end_date = Column(Date)
    average_student_performance = Column(Float)  # Overall class average
    student_pass_rate = Column(Float)
    activities = Column(String(500))  # e.g., "Chess Club, Football Team"
    student_rating = Column(Float)  # 1-5 rating
    total_students_taught = Column(Integer)
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    
class AdminEmploymentHistory(Base):
    __tablename__ = "admin_employment_history"
    
    id = Column(Integer, primary_key=True, index=True)
    admin_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=False)
    school_name = Column(String(200))
    role = Column(String(30))  # admin, school
    responsibilities = Column(Text)  # What they managed
    start_date = Column(Date, nullable=False)
    end_date = Column(Date)
    total_students_managed = Column(Integer)
    total_teachers_managed = Column(Integer)
    total_workers_managed = Column(Integer)
    school_performance_avg = Column(Float)  # Overall school average during tenure
    achievements = Column(Text)  # Key achievements
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    
class WorkerEmploymentHistory(Base):
    __tablename__ = "worker_employment_history"
    
    id = Column(Integer, primary_key=True, index=True)
    worker_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    school_id = Column(Integer, ForeignKey("schools.id"), nullable=False)
    school_name = Column(String(200))
    department = Column(String(100))
    role_title = Column(String(100))
    start_date = Column(Date, nullable=False)
    end_date = Column(Date)
    performance_rating = Column(Float)  # Average rating
    attendance_rate = Column(Float)
    tasks_completed = Column(Integer)
    supervisor_name = Column(String(100))
    notes = Column(Text)
    created_at = Column(DateTime, server_default=func.now())
    
class ParentUpload(Base):
    __tablename__ = "parentupload"
    
    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer)
    student_id = Column(Integer, nullable=True)
    email = Column(String(100))
    name = Column(String(100))
    phone = Column(String(50))
    
class Lastpage(Base):
    __tablename__ = "lastpage"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer)
    book_id = Column(Integer)
    lastpage = Column(Integer)
    
class Rating(Base):
    __tablename__ = "rating"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer)
    rating = Column(Integer)
    book_id = Column(Integer)
    
class Applicant(Base):
    __tablename__ = "applicant"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer)
    job_id = Column(Integer)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        Index('idx_alerts_user', 'user_id'),
        Index('idx_alerts_read', 'is_read'),
        Index('idx_alerts_type', 'type'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    applicant_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    job_id = Column(Integer, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    type = Column(String(50), default="application")
    title = Column(String(200), nullable=True)
    message = Column(String(500), nullable=True)
    icon = Column(String(50), default="notifications")
    link = Column(String(200), nullable=True)
    is_read = Column(Boolean, default=False)
    read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    recipient = relationship("User", foreign_keys=[user_id])
    applicant = relationship("User", foreign_keys=[applicant_id])
    job = relationship("Job", foreign_keys=[job_id])

class Chat(Base):
    __tablename__ = "chats"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    to_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    message = Column(Text, nullable=False)
    reply_to_id = Column(Integer, nullable=True)
    is_read = Column(Boolean, default=False)
    status = Column(String(20), default="sent")  # sent, delivered, read
    delivered_at = Column(DateTime(timezone=True), nullable=True)
    read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships - use foreign_keys to disambiguate
    sender = relationship("User", foreign_keys=[user_id])
    recipient = relationship("User", foreign_keys=[to_id])

# Add to your models
class ApplicationDocument(Base):
    __tablename__ = "application_documents"
    
    id = Column(Integer, primary_key=True, index=True)
    alert_id = Column(Integer, ForeignKey("alerts.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_type = Column(String(50), nullable=False)
    file_size = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
class AssignmentSubmission(Base):
    __tablename__ = "assignment_submissions"
    __table_args__ = (
        Index('idx_assignment_submissions_student', 'student_id'),
        Index('idx_assignment_submissions_assignment', 'assignment_id'),
        UniqueConstraint('student_id', 'assignment_id', name='uq_student_assignment'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    assignment_id = Column(Integer, ForeignKey("assignments.id", ondelete="CASCADE"), nullable=False)
    answers = Column(Text, nullable=True)
    status = Column(String(20), default="submitted")
    grade = Column(String(10), nullable=True)
    score = Column(Integer, nullable=True)
    feedback = Column(Text, nullable=True)
    submitted_at = Column(DateTime(timezone=True), server_default=func.now())
    graded_at = Column(DateTime(timezone=True), nullable=True)
    graded_by = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    
    # Relationships
    student = relationship("User", foreign_keys=[student_id])
    assignment = relationship("Assignment")
    grader = relationship("User", foreign_keys=[graded_by])
    
class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (
        Index('idx_groups_created_by', 'created_by'),
        Index('idx_groups_school', 'school_id'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False)
    description = Column(String(500), nullable=True)
    group_type = Column(String(20), default='individual')  # individual or class
    class_name = Column(String(50), nullable=True)  # For class-based groups
    created_by = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    creator_role = Column(String(30), default='admin')
    school_id = Column(Integer, ForeignKey("schools.id", ondelete="CASCADE"), nullable=True)
    avatar = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    
    # Relationships
    creator = relationship("User", foreign_keys=[created_by])
    members = relationship("GroupMember", back_populates="group", cascade="all, delete-orphan")
    messages = relationship("GroupMessage", back_populates="group", cascade="all, delete-orphan")


class GroupMember(Base):
    __tablename__ = "group_members"
    __table_args__ = (
        UniqueConstraint('group_id', 'user_id', name='uq_group_member'),
        Index('idx_group_members_group', 'group_id'),
        Index('idx_group_members_user', 'user_id'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), default='member')  # admin, member
    joined_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    group = relationship("Group", back_populates="members")
    user = relationship("User", foreign_keys=[user_id])


class GroupMessage(Base):
    __tablename__ = "group_messages"
    __table_args__ = (
        Index('idx_group_messages_group', 'group_id'),
        Index('idx_group_messages_sender', 'sender_id'),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False)
    sender_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    reply_to_id = Column(Integer, nullable=True)
    message = Column(Text, nullable=False)
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
    # Relationships
    group = relationship("Group", back_populates="messages")
    sender = relationship("User", foreign_keys=[sender_id])
    
class SmsTopup(Base):
    __tablename__ = "sms_topups"
    __table_args__ = (
        Index("idx_sms_topups_school", "school_id"),
        Index("idx_sms_topups_checkout", "checkout_request_id", unique=True),
    )

    id = Column(Integer, primary_key=True, index=True)
    school_id = Column(Integer, ForeignKey("schools.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    phone_number = Column(String(20), nullable=False)
    amount = Column(Integer, nullable=False)            # KSh paid
    sms_count = Column(Integer, nullable=False)         # SMS credited
    checkout_request_id = Column(String(100), unique=True, nullable=True)
    mpesa_receipt = Column(String(100), nullable=True)
    status = Column(String(20), default="pending")      # pending | success | failed | cancelled
    result_desc = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    
class BookPurchase(Base):
    __tablename__ = "book_purchases"
    __table_args__ = (
        Index("idx_book_purchases_user", "user_id"),
        Index("idx_book_purchases_book", "book_id"),
        Index("idx_book_purchases_checkout", "checkout_request_id", unique=True),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    book_id = Column(Integer, ForeignKey("books.id", ondelete="CASCADE"), nullable=False)
    phone_number = Column(String(20), nullable=False)
    amount = Column(Integer, nullable=False)                # KSh
    checkout_request_id = Column(String(100), unique=True, nullable=True)
    mpesa_receipt = Column(String(100), nullable=True)
    status = Column(String(20), default="pending")           # pending | success | failed | cancelled
    result_desc = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
