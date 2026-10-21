from pathlib import Path
import os
import tempfile
from dotenv import load_dotenv

# Load environment variables from backend/.env
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.database import DATABASE_PATH, create_tables
from app.assistant_router import router as assistant_router
from app.auth_router import router as auth_router
from app.email_draft_router import router as email_draft_router
from app.meeting.router import router as meeting_router
from app.task_router import router as task_router
from app.email_dispatcher import start_email_dispatcher, stop_email_dispatcher
from app.task_calendar_scheduler import (
    start_task_calendar_scheduler,
    stop_task_calendar_scheduler,
)
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


STORY_6_3_TEST_MODE = (
    os.getenv("APP_ENV", "").strip().casefold() == "story63-test"
)

if not STORY_6_3_TEST_MODE:
    app.include_router(meeting_router)
    app.include_router(task_router)
    app.include_router(email_draft_router)
app.include_router(auth_router)
app.include_router(assistant_router)


@app.on_event("startup")
def startup():
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)
    if STORY_6_3_TEST_MODE:
        database_path = DATABASE_PATH.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if not database_path.is_relative_to(temp_root):
            raise RuntimeError(
                "APP_ENV=story63-test requires OFFICE_OPERATIONS_DB_PATH "
                "to point inside the operating system temporary directory."
            )
        create_tables()
        return

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
    start_task_calendar_scheduler()


@app.on_event("shutdown")
def shutdown():
    stop_task_reminder_scheduler()
    stop_email_dispatcher()
    stop_task_calendar_scheduler()


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