from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from backend/.env
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.database import create_tables
from app.email_draft_router import router as email_draft_router
from app.meeting.router import router as meeting_router
from app.task_router import router as task_router
from app.email_dispatcher import start_email_dispatcher, stop_email_dispatcher
from app.task_reminder_scheduler import (
    start_task_reminder_scheduler,
    stop_task_reminder_scheduler,
)


app = FastAPI(
    title="AI Office Operations Agent API",
    version="1.0.0",
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(meeting_router)
app.include_router(task_router)
app.include_router(email_draft_router)


@app.on_event("startup")
def startup():
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=True)
    create_tables()
    try:
        from app.db.database import SessionLocal
        from app.db.seed_employees import seed_development_employees
        with SessionLocal() as db:
            seed_development_employees(db)
    except Exception:
        pass
    start_task_reminder_scheduler()
    start_email_dispatcher()


@app.on_event("shutdown")
def shutdown():
    stop_task_reminder_scheduler()
    stop_email_dispatcher()


@app.get("/")
def root():
    return {
        "message": "AI Office Operations Agent API is running"
    }


@app.get("/health")
def health_check():
    return {
        "status": "healthy"
    }