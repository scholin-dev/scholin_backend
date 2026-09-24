from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from ....core.database import get_db
from ....core.security import verify_password, get_password_hash
from ....core.session_auth import create_session, invalidate_session, get_current_user
from ....models.user import User, Student, Teacher, Parent, School, Session as SessionModel, Class, ParentStudent
from ....schemas.user import UserCreate, UserResponse, Token
from datetime import datetime

router = APIRouter(prefix="/auth", tags=["Authentication"])

@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(user_data: UserCreate, db: Session = Depends(get_db)):
    # Check if user exists
    if db.query(User).filter(User.username == user_data.username).first():
        raise HTTPException(status_code=400, detail="Username already registered")
    if db.query(User).filter(User.email == user_data.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Get school_id from school_name
    school_id = None
    if user_data.school_name:
        school = db.query(School).filter(School.school_name == user_data.school_name).first()
        if school:
            school_id = school.id
            
    # Create user
    hashed_password = get_password_hash(user_data.password)
    if user_data.role in ['school', 'parent']:
        db_user = User(
            username=user_data.username,
            email=user_data.email,
            hashed_password=hashed_password,
            full_name=user_data.full_name,
            role=user_data.role,
            phone=user_data.phone,
            approval_status='approved',
            school_id=school_id
        )
    else:
        db_user = User(
            username=user_data.username,
            email=user_data.email,
            hashed_password=hashed_password,
            full_name=user_data.full_name,
            role=user_data.role,
            phone=user_data.phone,
            approval_status='pending',
            school_id=school_id
        )
    db.add(db_user)
    db.flush()
    
    # Create role-specific profile
    if user_data.role == "student":
        profile = Student(
            user_id=db_user.id,
            admission_number=user_data.admission_number,
            school_name=user_data.school_name,
            class_name=user_data.class_name,
            gender=user_data.gender,
            school_id=school_id
        )
        db.add(profile)

    elif user_data.role == "parent":
        profile = Parent(
            user_id=db_user.id,
        )
        db.add(profile)
        
    elif user_data.role == "teacher":
        profile = Teacher(
            user_id=db_user.id,
            subject=user_data.subject,
            qualification=user_data.qualification,
            years_of_experience=user_data.years_of_experience,
            school_id=school_id
        )
        db.add(profile)
        
    elif user_data.role == "school":
        profile = School(
            user_id=db_user.id,
            school_name=user_data.school_name,
            address=user_data.address,
            school_type=user_data.school_type
        )
        db.add(profile)
        db.flush()
        db_user.school_id = profile.id
    
    db.commit()
    db.refresh(db_user)
    
    return db_user

@router.post("/login")
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    db.query(SessionModel).filter(
        (SessionModel.expires_at < datetime.utcnow()) | (SessionModel.is_active == False)
    ).delete()
    db.commit()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")
    if user.approval_status == 'pending':
        raise HTTPException(status_code=403, detail="Account pending admin approval")
    if user.approval_status == 'rejected':
        raise HTTPException(status_code=403, detail="Account has been rejected")
    
    school_id = None
    school_name = None
    school = None
    children = None
    
    if user.role in ["admin", "school"]:
        school = db.query(School).filter(School.user_id == user.id).first()
        if not school and user.school_id:
            school = db.query(School).filter(School.id == user.school_id).first()
        if school:
            school_id = school.id
            school_name = school.school_name
    elif user.role == "teacher":
        teacher = db.query(Teacher).filter(Teacher.user_id == user.id).first()
        if teacher:
            school_id = teacher.school_id
            school = db.query(School).filter(School.id == teacher.school_id).first()
            school_name = school.school_name if school else None
    elif user.role == "student":
        student = db.query(Student).filter(Student.user_id == user.id).first()
        if student:
            school_id = student.school_id
            school_name = student.school_name
            
        elif user.role == "parent":
            parent = db.query(Parent).filter(Parent.user_id == user.id).first()
            children = []
            if parent:
                parent_students = (
                    db.query(ParentStudent.student_id)
                    .filter(ParentStudent.parent_id == parent.id)
                    .all()
                )
                student_ids = [ps[0] for ps in parent_students]

                if student_ids:
                    students = (
                        db.query(Student)
                        .filter(Student.id.in_(student_ids))
                        .all()
                    )
                    for s in students:
                        child_user = (
                            db.query(User).filter(User.id == s.user_id).first()
                        )
                        children.append({
                            "id": s.user_id,
                            "student_id": s.id,
                            "name": child_user.full_name if child_user else None,
                            "class": s.class_name,
                            "school": s.school_name,
                            "admission_number": s.admission_number,
                        })
    
    elif user.role == "worker":
        worker = db.query(Worker).filter(Worker.user_id == user.id).first()
        if worker:
            school_id = worker.school_id
            school_name = worker.school_name
    
    session_key = create_session(user.id, db, request, school_id)
    
    return {
        "session_key": session_key,
        "user_id": user.id,
        "username": user.username,
        "role": user.role,
        "full_name": user.full_name,
        "d_p": user.profile_picture,
        "school_id": school_id if school else None,
        "school_name": school_name if school else None,
        "school_logo": school.logo if school else None,
        "school_motto": school.motto if school else None,
        "children": children
    }

@router.post("/logout")
def logout(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # Get session key from request
    from fastapi import Request as FastAPIRequest
    return {"message": "Logged out successfully"}

@router.get("/me", response_model=UserResponse)
def get_me(user: User = Depends(get_current_user)):
    return user
