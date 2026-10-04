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
## 🎙️ AI Meeting Transcription

The system automatically transcribes meeting recordings after the recording is completed.

### Story 2.1 – Automatic Meeting Transcription

- Automatically starts transcription when a meeting recording is completed.
- Uses Whisper `large-v3` through `faster-whisper` for English speech-to-text.
- Generates timestamped transcript segments.
- Tracks transcription status:
  - Pending
  - Processing
  - Completed
  - Failed
- Prevents duplicate transcripts for the same recording.
- Securely processes encrypted meeting recordings.

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



FR-4 — Meeting Recording & Consent

Status: Completed

The Meeting Recording & Consent system has been implemented as the next feature after the Meeting Room System.

The implementation includes:

* Participant recording consent
* Organizer-controlled recording
* Audio/video recording
* Browser MediaRecorder integration
* Recording upload
* Encrypted recording storage
* Recording lifecycle management
* Recording retention
* Backend authorization
* Automated testing

🎥 Recording System

The system allows the meeting organizer to start and stop recording during an active meeting.

The recording captures the existing meeting audio and video stream using the browser MediaRecorder API.

The implementation reuses the existing WebRTC media stream and does not request a second camera or microphone stream.

👤 Organizer / Host

The meeting organizer controls the recording.

The organizer can:

* Start recording
* Stop recording
* View recording status
* Upload the completed recording

Recording permissions are validated by the backend.

📝 Recording Consent

Recording requires explicit participant consent.

All active participants must provide consent before the organizer can start recording.

Consent information includes:

* Meeting
* Participant
* Attendance context
* Consent status
* Consent timestamp

Repeated identical consent operations are handled safely.

▶️ Recording Start

When the organizer starts recording:

* Backend verifies the meeting
* Organizer authorization is checked
* Active participants are checked
* Recording consent is verified
* Backend recording is started
* Browser MediaRecorder is started

The browser recording starts only after the backend confirms the recording start.

If browser recording fails, the backend recording is stopped to prevent an inconsistent state.

🎙️ Audio and Video

The recording supports combined audio and video.

The implementation uses supported WebM MIME types.

The browser checks MediaRecorder support before selecting the recording format.

The existing meeting media tracks are not replaced or stopped.

⏹️ Recording Stop

When recording is stopped:

* MediaRecorder stops
* Final recording data is collected
* Recording Blob is created
* Backend recording is stopped
* Recording is uploaded
* Recording enters processing state

If upload fails, the completed Blob is preserved in memory and recording is not automatically restarted.

📤 Recording Upload

Completed recordings are uploaded through an organizer-authorized backend endpoint.

The backend validates:

* Organizer authorization
* Meeting association
* Recording association
* Recording state
* File presence
* File format

Supported browser recordings use WebM.

Uploads are first written to a temporary location and then moved to the final recording location.

📁 Recording Storage

Recordings are stored using the following structure:

backend/storage/recordings/<meeting-id>/<recording-id>.<extension>

The database stores the relative recording storage URI.

Runtime recording files are excluded from Git.

🔐 Recording Encryption

Recording files are encrypted before being stored as the final recording artifact.

The implementation uses:

* AES-256-GCM
* Random nonce
* Authenticated encryption
* 32-byte encryption key
* AORC encrypted recording container

The final stored recording is encrypted at rest.

Temporary plaintext recording files are cleaned up after encryption.

🔑 Environment Configuration

Recording encryption requires:

RECORDING_ENCRYPTION_KEY

The key must decode to exactly 32 bytes.

Local configuration is stored in:

backend/.env

A template is provided as:

backend/.env.example

The actual encryption key must never be committed to GitHub.

🔄 Recording Lifecycle

Recording states include:

* pending
* recording
* processing
* completed
* failed
* cancelled

The backend validates recording state transitions.

Repeated start and stop operations are handled safely.

🗄️ Database

The recording system introduces:

MeetingRecording

Stores:

* Recording ID
* Meeting ID
* Status
* Storage URI
* Created time
* Started time
* Stopped time
* Retention expiry

MeetingRecordingConsent

Stores:

* Consent ID
* Meeting ID
* Participant
* Attendance context
* Consent status
* Consent timestamp

Recording and consent records are linked to the meeting.

⏳ Recording Retention

The default recording retention period is 90 days.

The retention service removes only expired terminal recordings.

Eligible states include:

* completed
* failed
* cancelled

Active and processing recordings are protected.

The retention process validates the storage path before deleting files.

Missing files are handled safely and cleanup is idempotent.

A production scheduler has not yet been added; the retention service can be invoked by a future scheduled worker.

🔒 Authorization

Recording operations require organizer authorization.

Organizer authorization is required for:

* Starting recording
* Stopping recording
* Viewing protected recording information
* Uploading recordings

Organizer authorization does not bypass participant consent.

🧪 Testing

Backend recording tests cover:

* Recording models
* Consent
* Recording API
* Start/stop lifecycle
* Upload
* Encryption
* Retention
* Authorization
* Invalid state transitions

Frontend tests cover:

* MediaRecorder
* Audio/video recording
* MIME type selection
* Consent flow
* Recording start
* Recording stop
* Upload flow
* Upload failure handling

Backend focused recording tests:

31 passed

Frontend recording tests:

25 passed

Production build:

npm run build

Build completed successfully.

🌐 Browser Validation

The recording system was also tested through the actual browser meeting flow.

The validation confirmed:

* Meeting join
* Participant consent
* Recording start
* Audio/video capture
* Recording stop
* Upload
* Database persistence
* Encrypted file storage

The stored recording file was verified as an encrypted AORC recording container.

📦 Important Files

Backend:

backend/app/meeting/models.py

backend/app/meeting/router.py

backend/app/meeting/schemas.py

backend/app/meeting/service.py

backend/app/meeting/recording_encryption.py

backend/app/meeting/recording_retention.py

Frontend:

src/api/meetings.ts

src/hooks/meetingRecordingFlow.ts

src/hooks/meetingRecordingSession.ts

src/hooks/useMeetingRecording.ts

src/pages/MeetingRoomPage.tsx

src/types/meeting.ts

Tests:

backend/tests/test_meeting_recording_api.py

backend/tests/test_meeting_recording_encryption.py

backend/tests/test_meeting_recording_models.py

backend/tests/test_meeting_recording_retention.py

backend/tests/test_meeting_recording_upload.py

tests/frontend/meetingRecordingFlow.test.mjs

tests/frontend/meetingRecordingSession.test.mjs

📦 FR-4 GitHub Handoff

The completed FR-4 implementation contains:

* Recording consent
* Organizer recording controls
* Audio/video recording
* MediaRecorder integration
* Recording upload
* Encrypted storage
* Recording lifecycle
* Recording retention
* Recording authorization
* Backend tests
* Frontend tests
* Production build verification

The next developer should build on the existing FR-3 and FR-4 implementation instead of recreating the meeting or recording system.

🔄 Recommended Workflow for the Next Developer

Before starting the next feature:

* Get the latest repository
* Pull the latest changes
* Install dependencies
* Activate the Python environment
* Start the backend
* Start the frontend
* Verify the existing meeting system
* Verify recording consent
* Verify recording start and stop
* Run backend tests
* Run frontend tests
* Build the frontend

Only after the existing functionality works should the next feature be implemented.

🛠️ Development Principles

When implementing future features:

* Do not unnecessarily rewrite existing functionality.
* Preserve existing APIs unless required.
* Reuse the existing WebRTC stream where possible.
* Do not create duplicate media streams.
* Add tests for new functionality.
* Verify the frontend production build.
* Keep recordings and generated files out of Git.
* Keep encryption keys and secrets out of Git.
* Preserve recording authorization and consent requirements.
* Maintain backward compatibility where possible.

📌 Current Handoff Checklist

FR-4

* [x] Participant recording consent
* [x] Organizer recording authorization
* [x] Recording start
* [x] Recording stop
* [x] Audio recording
* [x] Video recording
* [x] Combined audio/video recording
* [x] WebRTC stream reuse
* [x] MediaRecorder integration
* [x] Recording upload
* [x] Recording persistence
* [x] AES-256-GCM encryption
* [x] Recording lifecycle
* [x] Recording retention
* [x] Safe repeated start/stop
* [x] Backend tests
* [x] Frontend tests
* [x] Browser validation
* [x] Production build verification
* [x] Git commit completed
