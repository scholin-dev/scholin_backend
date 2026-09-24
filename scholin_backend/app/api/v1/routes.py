from fastapi import APIRouter
from .endpoints import auth, password_reset, admin, me
from fastapi.responses import HTMLResponse
from datetime import datetime

api_router = APIRouter()

api_router.include_router(auth.router)
api_router.include_router(password_reset.router)
api_router.include_router(admin.router)
api_router.include_router(me.router)

# Optional routers
try:
    from .endpoints import users
    api_router.include_router(users.router)
except:
    pass
try:
    from .endpoints import students
    api_router.include_router(students.router)
except:
    pass
