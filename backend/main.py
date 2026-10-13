from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from backend/.env
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db.database import create_tables
from app.meeting.router import router as meeting_router
from app.task_router import router as task_router


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


@app.on_event("startup")
def startup():
    load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env", override=False)
    create_tables()


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