# AI Office Operations Agent

> An AI-powered office operations platform designed to centralize workplace workflows, communication, meetings, and operational activities.

---

## 📌 Project Overview

**AI Office Operations Agent** is a full-stack office operations platform being developed to simplify and automate common organizational workflows.

The system is designed around different organizational roles such as:

- Employee
- Manager
- HR
- Admin

The application provides a centralized platform where users can interact with office operations features through a modern web interface.

The project follows a modular architecture so that new features can be added incrementally through Feature Requirements (FRs).

---

# 🎯 Project Goals

The main goals of the project are to:

- Centralize office operations
- Improve communication between employees and management
- Provide role-based workflows
- Enable real-time online meetings
- Support meeting scheduling and instant meetings
- Provide participant admission and moderation
- Track meeting attendance
- Generate meeting reports
- Provide a scalable foundation for future AI-powered office automation

---

# 🏗️ Technology Stack

## Frontend

- React
- TypeScript
- Vite
- CSS
- WebRTC
- WebSocket

## Backend

- Python
- FastAPI
- Uvicorn
- SQLAlchemy
- SQLite
- WebSocket

## Testing

- Pytest
- Backend integration tests
- WebRTC/WebSocket signaling tests
- Frontend production build verification

---

# 🧩 System Architecture

The application consists of three major layers:

```text
┌─────────────────────────────────────────────┐
│                Frontend                     │
│                                             │
│ React + TypeScript + Vite                   │
│ Meeting UI + User Interface                 │
└───────────────────┬─────────────────────────┘
                    │
                    │ REST API
                    │ WebSocket
                    ▼
┌─────────────────────────────────────────────┐
│                 Backend                     │
│                                             │
│ FastAPI                                     │
│ Meeting APIs                                │
│ Meeting Services                            │
│ WebSocket Signaling                         │
└───────────────────┬─────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────┐
│                 Database                    │
│                                             │
│ SQLite                                      │
│ Meetings                                    │
│ Attendance                                  │
│ Meeting State                               │
└─────────────────────────────────────────────┘

PROJECT STRUCTURE:
ai-office-operations-agent/
│
├── backend/
│   │
│   ├── app/
│   │   ├── db/
│   │   │   └── database.py
│   │   │
│   │   └── meeting/
│   │       ├── models.py
│   │       ├── router.py
│   │       ├── schemas.py
│   │       ├── service.py
│   │       └── signaling.py
│   │
│   ├── tests/
│   │   ├── test_fr2_compatibility.py
│   │   ├── test_meeting_attendance.py
│   │   ├── test_meeting_deletion.py
│   │   ├── test_meeting_lobby.py
│   │   ├── test_meeting_signaling.py
│   │   └── test_meeting_validation.py
│   │
│   └── main.py
│
├── src/
│   │
│   ├── api/
│   │   └── meetings.ts
│   │
│   ├── components/
│   │   ├── layout/
│   │   └── meetings/
│   │       ├── LocalMediaPreview.tsx
│   │       ├── OrganizerGuestLobby.tsx
│   │       ├── VideoGrid.tsx
│   │       └── MeetingCard.tsx
│   │
│   ├── contexts/
│   │   └── AuthContext.tsx
│   │
│   ├── hooks/
│   │   └── useMeetingRoom.ts
│   │
│   ├── pages/
│   │   ├── CreateMeetingPlaceholder.tsx
│   │   ├── GuestJoinPage.tsx
│   │   ├── GuestMeetingRoomPlaceholder.tsx
│   │   ├── HomePage.tsx
│   │   ├── MeetingEntryPage.tsx
│   │   ├── MeetingReportPage.tsx
│   │   ├── MeetingRoomPage.tsx
│   │   ├── MeetingRoomPlaceholder.tsx
│   │   └── MeetingsPage.tsx
│   │
│   ├── types/
│   │   └── meeting.ts
│   │
│   ├── App.tsx
│   └── styles.css
│
├── conftest.py
├── package.json
├── .gitignore
└── README.md

🚀 Current Development Status

FR-3 — Meeting Room System
Status: ✅ Completed
The Meeting Room System has been implemented as the major feature of FR-3.
The implementation includes meeting creation, meeting links, participant admission, real-time signaling, audio/video communication, attendance tracking, meeting reports, host controls, meeting completion and meeting management.
🎥 Meeting System
The meeting system supports two types of meetings.
1. Instant Meeting
An instant meeting allows the user to create a meeting immediately.
The organizer does not need to specify a future date or time.
The meeting link can then be shared with other participants.
2. Scheduled Meeting
A scheduled meeting allows the organizer to specify:
- Meeting date
- Meeting start time
- Meeting end time
The meeting becomes available according to the configured schedule.
👤 Organizer / Host
The user who creates a meeting becomes the organizer/host of that meeting.
The organizer is responsible for managing participants.
The organizer can:
- Enter the meeting
- Share the meeting link
- Receive participant requests
- Accept participants
- Reject participants
- View participants
- Moderate participants
- Mute participants
- End the meeting
- View the meeting report
🔗 Meeting Link
After creating a meeting, the organizer receives a shareable meeting link.
The organizer can:
- Copy the meeting link
- Share the meeting link
- Open the meeting directly
Participants can use the shared link to access the meeting entry flow.
🚪 Guest Meeting Flow
Participants joining through a shared meeting link follow the guest flow.
Step 1 — Open Meeting Link
The participant opens the meeting URL.
Step 2 — Enter Details
The participant provides:
- Name
- Email address
Step 3 — Request to Join
The participant selects:
Request to join

The participant is then placed in the meeting lobby.
Step 4 — Organizer Decision
The organizer receives the request.
The organizer can:
Accept

or
Deny

Step 5 — Admission
If accepted, the participant enters the meeting.
If rejected, the participant receives an access-denied state.
🎙️ Audio and Video
The meeting system uses browser-based media capabilities for real-time communication.
Participants can control:
- Microphone
- Camera
Media is handled through WebRTC.
Signaling between participants is handled through WebSockets.
🔄 WebSocket Signaling
The backend provides WebSocket-based signaling for meeting participants.
The signaling layer handles communication required for peer connections and participant state.
Examples include:
- Participant joining
- Participant leaving
- WebRTC offer
- WebRTC answer
- ICE candidates
- Participant state
- Participant moderation
- Meeting-ended events
👥 Participant Management
The meeting room maintains participant information such as:
- Participant name
- Participant identity
- Microphone state
- Camera state
- Organizer/host status
- Connection state
The video grid dynamically displays connected participants.
🔇 Host Moderation
The organizer has additional controls compared with normal participants.
The organizer can:
- Accept join requests
- Reject join requests
- Mute participants
- Monitor participant states
- End the meeting
Host moderation is validated on the backend.
📊 Attendance Tracking
The system records meeting attendance.
Attendance information includes:
- Participant
- Meeting
- Join time
- Leave time
This allows the system to determine who attended the meeting and how long they remained in the meeting.
📄 Meeting Reports
Completed meetings can have an attendance report.
The report provides information such as:
Information	Description
Participant	Person who joined
Join Time	Time participant entered
Leave Time	Time participant left
Duration	Approximate time spent in meeting


Meeting reports are restricted to the meeting organizer.
✅ Meeting Completion
The system tracks the lifecycle of a meeting.
When the meeting is finished, it should no longer behave as an active meeting.
Completed meetings are available through the previous/completed meetings area.
Completed meetings can also be deleted by the appropriate user.
🗑️ Meeting Deletion
Completed meetings can be removed from the meeting list.
Deletion is handled through the backend API.
The system prevents inappropriate deletion of meetings that are still active according to the meeting lifecycle rules.
⚠️ Invalid and Expired Meetings
The application handles invalid and expired meeting links.
Examples include:
Invalid Meeting
Meeting not found

Expired Meeting
This meeting has expired

These states prevent the application from unnecessarily starting the meeting signaling/media flow.
🧪 Testing
The project contains backend tests covering the meeting system.
Current Test Modules
FR-2 Compatibility
test_fr2_compatibility.py

Verifies that meeting functionality does not break existing FR-2 functionality.
Attendance
test_meeting_attendance.py

Tests participant attendance and join/leave tracking.
Meeting Deletion
test_meeting_deletion.py

Tests completed meeting deletion behavior.
Meeting Lobby
test_meeting_lobby.py

Tests guest requests and lobby behavior.
Meeting Signaling
test_meeting_signaling.py

Tests WebSocket signaling and multi-participant behavior.
Meeting Validation
test_meeting_validation.py

Tests valid, invalid and expired meeting states.
▶️ Running the Project Locally
The project requires two development processes:
Backend
Frontend

Both should normally be running simultaneously.
🐍 Backend Setup
Navigate to the project:
cd E:\Projects\ai-office-operations-agent

Activate the virtual environment:
.\.venv\Scripts\activate

Navigate to the backend:
cd backend

Start FastAPI:
python -m uvicorn main:app --reload --port 9000

Backend:
http://127.0.0.1:9000

🌐 Frontend Setup
Open another terminal.
Navigate to the project:
cd E:\Projects\ai-office-operations-agent

Install dependencies if required:
npm install

Start the frontend:
npm run dev

Vite will provide the frontend URL, normally:
http://localhost:5173

🧪 Running Backend Tests
From the project root:
python -m pytest backend/tests -v

If the backend test environment requires the configured project test path, run the test command from the location specified by the project configuration.
🏗️ Frontend Production Build
Before committing frontend changes, verify that the production build succeeds:
npm run build

A successful build confirms that the TypeScript/React application compiles correctly for production.
🔍 Git Checks
Before committing:
git diff --check

Check the working tree:
git status

🔐 Environment and Local Files
Local development files should not be committed to GitHub.
Examples include:
.env
*.db
__pycache__/
*.pyc
node_modules/
dist/

The local SQLite database is intended for local development/testing and should not be used as a shared production database.
🌿 Git Branching Strategy
Feature development should use separate branches.
The completed FR-3 implementation is currently associated with:
feature/story-1.3-meeting-room

The branch has been pushed to the project GitHub repository.
Repository:
https://github.com/Office-Operations-AI-Agent/ai-office-operations-agent
📦 FR-3 GitHub Handoff
The completed FR-3 implementation contains:
- Backend meeting APIs
- Meeting database models
- Meeting services
- WebSocket signaling
- Frontend meeting pages
- Guest lobby
- Participant video grid
- Meeting controls
- Attendance tracking
- Meeting reports
- Meeting deletion
- Meeting validation
- Meeting tests
The next developer should build on the existing implementation instead of recreating the meeting system.
🔄 Recommended Workflow for the Next Developer
Before starting FR-4:
1. Get the latest repository
git fetch origin

2. Check available branches
git branch -a

3. Switch to the required development branch
git checkout feature/story-1.3-meeting-room

4. Pull the latest changes
git pull

5. Install frontend dependencies
npm install

6. Activate the Python environment
.\.venv\Scripts\activate

7. Run the backend
cd backend
python -m uvicorn main:app --reload --port 9000

8. Run the frontend
In another terminal:
cd E:\Projects\ai-office-operations-agent
npm run dev

9. Verify FR-3
Before modifying the system, verify:
- Login/user access
- Meeting creation
- Instant meeting
- Scheduled meeting
- Meeting link
- Guest join request
- Organizer admission
- Audio
- Video
- Host controls
- Participant mute
- Attendance
- Meeting report
- Completed meetings
- Meeting deletion
- Invalid meeting
- Expired meeting
10. Run tests
python -m pytest backend/tests -v

11. Build frontend
npm run build

Only after the existing system works should FR-4 development begin.
🚧 Future Development
The project is being developed incrementally through Feature Requirements.
Completed functionality should remain backward compatible when implementing future FRs.
Future work may include additional:
- AI-powered office automation
- Employee workflows
- Manager workflows
- HR workflows
- Administrative workflows
- Notifications
- Intelligent scheduling
- AI assistants
- Analytics
- Workflow automation
- Enterprise integrations
🛠️ Development Principles
When implementing new features:
1. Do not unnecessarily rewrite existing functionality.
2. Preserve existing APIs unless a change is required.
3. Add tests for new backend functionality.
4. Verify the frontend production build.
5. Test existing workflows after major changes.
6. Keep generated files out of Git.
7. Keep secrets and environment variables out of Git.
8. Use feature branches for new FRs.
9. Document significant architectural changes.
10. Maintain backward compatibility where possible.
📌 Current Handoff Checklist
FR-3
- [x] Meeting creation
- [x] Instant meeting
- [x] Scheduled meeting
- [x] Meeting sharing
- [x] Guest join flow
- [x] Guest lobby
- [x] Organizer admission
- [x] Organizer rejection
- [x] WebSocket signaling
- [x] WebRTC communication
- [x] Microphone control
- [x] Camera control
- [x] Host identification
- [x] Participant moderation
- [x] Attendance tracking
- [x] Meeting reports
- [x] Meeting completion
- [x] Previous meetings
- [x] Meeting deletion
- [x] Invalid meeting handling
- [x] Expired meeting handling
- [x] Backend tests
- [x] Frontend build verification
- [x] GitHub branch pushed