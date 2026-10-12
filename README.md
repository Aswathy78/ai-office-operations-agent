
# AI Office Operations Agent

An AI-powered office operations platform designed to automate meeting management, attendance, recording, transcription, speaker identification, and follow-up workflows.

The system combines a React/Vite frontend with a FastAPI backend and SQLAlchemy database layer.

---

## 🚀 Project Overview

The AI Office Operations Agent manages the complete lifecycle of an office meeting:

```text
Create Meeting
      ↓
Participants Join
      ↓
Attendance Tracking
      ↓
Recording Consent
      ↓
Meeting Recording
      ↓
Participant Audio Tracks
      ↓
Automatic Transcription
      ↓
Speaker Identification
      ↓
Speaker-labelled Transcript
      ↓
Meeting Intelligence
      ↓
Action Items / Follow-up
```

The goal is to reduce manual meeting administration and transform meeting conversations into structured, searchable and actionable information.

---

# 🏗️ Technology Stack

## Frontend

- React
- TypeScript
- Vite
- WebRTC
- MediaRecorder API
- REST API integration

## Backend

- Python
- FastAPI
- SQLAlchemy
- SQLite
- Pydantic
- Pytest

## AI / Speech

- faster-whisper
- Whisper `large-v3`
- Automatic speech-to-text
- Participant-track based speaker identification

## Security

- AES-256-GCM recording encryption
- Temporary plaintext processing
- Secure recording storage
- Recording consent validation

---

# 📁 Project Structure

```text
ai-office-operations-agent/
│
├── backend/
│   ├── app/
│   │   ├── meeting/
│   │   │   ├── models.py
│   │   │   ├── router.py
│   │   │   ├── schemas.py
│   │   │   ├── service.py
│   │   │   ├── speaker_identification.py
│   │   │   ├── transcription_worker.py
│   │   │   ├── transcription_media.py
│   │   │   └── recording_encryption.py
│   │   │
│   │   └── ...
│   │
│   ├── tests/
│   │   ├── test_meeting_participant_tracks.py
│   │   ├── test_speaker_identification.py
│   │   ├── test_meeting_transcription.py
│   │   ├── test_meeting_recording_api.py
│   │   └── ...
│   │
│   └── main.py
│
├── src/
│   ├── api/
│   │   └── meetings.ts
│   │
│   ├── hooks/
│   │   └── meetingRecordingFlow.ts
│   │
│   ├── pages/
│   │   └── MeetingRoomPage.tsx
│   │
│   └── ...
│
├── tests/
│   └── frontend/
│       └── participantTrackUpload.test.mjs
│
└── README.md
```

---

# 🎯 Meeting Workflow

## 1. Meeting Creation

An organizer creates a meeting with:

- Title
- Agenda
- Meeting type
- Start time
- End time
- Participants

The backend validates scheduled and instant meetings.

---

## 2. Attendance Tracking

When a participant joins:

```text
Participant joins
       ↓
Attendance API
       ↓
MeetingAttendanceLog
       ↓
joined_at recorded
```

When the participant leaves:

```text
Participant leaves
       ↓
Attendance API
       ↓
left_at recorded
```

Attendance records contain:

- Participant name
- Email
- User ID
- External guest status
- Join time
- Leave time

---

# 🎙️ Recording

Before recording begins, participant recording consent is checked.

The organizer starts the recording and the backend creates a recording record.

The browser uses the `MediaRecorder` API to capture audio.

After recording stops:

```text
MediaRecorder
      ↓
Audio Blob
      ↓
Backend Upload
      ↓
AES-256-GCM Encryption
      ↓
Encrypted Storage
      ↓
Recording Completed
      ↓
Transcript Created
```

A completed recording automatically creates a pending transcript.

---

# 📝 Automatic Transcription — FR-7

The transcription worker continuously checks for pending transcripts.

```text
Pending Transcript
       ↓
Claim Transcript
       ↓
Processing
       ↓
Decrypt Recording
       ↓
faster-whisper
       ↓
Timestamped Segments
       ↓
Speaker Metadata
       ↓
Completed Transcript
```

Whisper produces timestamped segments such as:

```json
{
  "start": 0.25,
  "end": 1.50,
  "text": "Hello everyone."
}
```

The transcript is stored in the `MeetingTranscript` database record.

Transcript states are:

```text
pending
   ↓
processing
   ↓
completed
```

or:

```text
processing
   ↓
failed
```

---

# 🗣️ Speaker Identification — FR-8

FR-8 extends the transcription pipeline by associating transcript segments with participant audio tracks.

Each participant track contains:

- `connection_id`
- `participant_name`
- optional `attendance_id`

Participant tracks are registered before their audio files are uploaded.

### Participant Track Registration

```text
Participant
     ↓
WebRTC connection
     ↓
connection_id
     ↓
Participant Track
     ↓
Backend Registration
     ↓
MeetingRecording
```

The backend provides:

```http
POST /api/v1/meetings/{meeting_id}/recording/tracks
```

to register participant tracks.

Individual participant recordings are uploaded through:

```http
POST /api/v1/meetings/{meeting_id}/recording/{recording_id}/upload
```

---

## Speaker Metadata

Transcript segments now contain speaker information:

```json
{
  "start": 0.0,
  "end": 4.0,
  "text": "Hello.",
  "speaker": "Rahul",
  "flagged_for_correction": false
}
```

If the participant cannot be identified:

```json
{
  "start": 0.0,
  "end": 4.0,
  "text": "Hello.",
  "speaker": "Unknown Speaker",
  "flagged_for_correction": true
}
```

The speaker resolver is implemented in:

```text
backend/app/meeting/speaker_identification.py
```

The resolver provides:

```python
identify_recording_speaker(
    participant_name="Rahul"
)
```

which returns:

```text
SpeakerIdentity(
    name="Rahul",
    flagged_for_correction=False
)
```

For an unidentified participant:

```text
SpeakerIdentity(
    name="Unknown Speaker",
    flagged_for_correction=True
)
```

---

# 🔗 FR-8 Participant Track API

### Register participant tracks

```http
POST /api/v1/meetings/{meeting_id}/recording/tracks
```

Example request:

```json
{
  "tracks": [
    {
      "connection_id": "conn-1",
      "participant_name": "Alice",
      "attendance_id": "att-1"
    },
    {
      "connection_id": "conn-2",
      "participant_name": "Bob",
      "attendance_id": "att-2"
    }
  ]
}
```

The backend creates separate recording records for the participant tracks.

---

### Upload participant track

```http
POST /api/v1/meetings/{meeting_id}/recording/{recording_id}/upload
```

The recording ID is explicitly associated with the participant track.

The implementation maps uploaded tracks using `connection_id`, rather than relying on array order.

This prevents participant audio from being accidentally assigned to another participant.

---

# 🧠 Speaker Identification Architecture

```text
             Meeting
                │
       ┌────────┴────────┐
       │                 │
 Attendance Logs     Participant Tracks
       │                 │
       │           connection_id
       │           participant_name
       │                 │
       └────────┬────────┘
                │
         Transcription
                │
          Whisper STT
                │
        Timestamped Text
                │
        Speaker Resolver
                │
       ┌────────┴────────┐
       │                 │
 Known Participant   Unknown Speaker
       │                 │
 speaker = name     speaker = Unknown Speaker
 flagged = false   flagged = true
```

---

# 🧪 Testing

The project contains backend and frontend tests covering meeting recording, participant tracks, transcription and speaker identification.

### Speaker identification tests

```text
test_known_participant_is_identified
test_missing_participant_becomes_unknown
test_blank_participant_becomes_unknown
test_participant_name_is_trimmed
```

### Participant track tests

The FR-8 participant-track test suite covers:

- Participant track registration
- Authorization
- Participant-track uploads
- Recording ID validation
- Meeting ownership validation
- Known speaker identification
- Unknown speaker handling
- Existing recording endpoint compatibility
- Meeting deletion cleanup
- Multiple participant tracks
- Upload failure handling

### Frontend tests

Frontend tests verify:

- Participant track registration
- Correct connection ID mapping
- Correct Blob-to-recording mapping
- Multiple participant uploads
- Failed track isolation
- Empty track handling

---

# ✅ Test Results

The final backend test suite completed successfully with:

```text
119 passed
1 skipped
2 warnings
```

The FR-8 focused tests passed:

```text
test_meeting_participant_tracks.py
12 passed

test_speaker_identification.py
4 passed

test_meeting_transcription.py
5 passed
```

Combined FR-8/transcription/participant-track validation:

```text
21 passed
```

The remaining warnings are FastAPI deprecation warnings related to the existing:

```python
@app.on_event("startup")
```

implementation and are not FR-8 test failures.

---

# 📊 FR-8 Implementation Summary

| Component | Status |
|---|---|
| Participant track registration | ✅ |
| Participant name association | ✅ |
| Connection ID association | ✅ |
| Participant track upload | ✅ |
| Explicit recording ID upload | ✅ |
| Speaker identification module | ✅ |
| Known speaker labeling | ✅ |
| Unknown speaker fallback | ✅ |
| Correction flag | ✅ |
| Whisper integration | ✅ |
| Transcript speaker metadata | ✅ |
| Frontend participant-track upload flow | ✅ |
| Backend tests | ✅ |
| Frontend tests | ✅ |
| Existing recording flow compatibility | ✅ |

---

# 🔐 Recording Security

Recordings are encrypted before storage using AES-256-GCM.

The transcription worker:

1. Retrieves the encrypted recording.
2. Decrypts it into a temporary file.
3. Processes the file using Whisper.
4. Stores the transcript.
5. Deletes the temporary plaintext file.

This prevents decrypted recordings from remaining permanently on disk.

---

# 🛠️ Running the Backend

From the backend directory:

```powershell
cd backend
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Run the API:

```powershell
uvicorn main:app --reload
```

---

# 🧪 Running Tests

Run the complete backend test suite:

```powershell
cd backend
python -m pytest -q
```

Run FR-8 tests:

```powershell
python -m pytest tests/test_meeting_participant_tracks.py -q
```

Run speaker identification tests:

```powershell
python -m pytest tests/test_speaker_identification.py -q
```

Run transcription tests:

```powershell
python -m pytest tests/test_meeting_transcription.py -q
```

---

# 🌿 Git Workflow

FR-8 was developed on:

```text
feature/story-2.2-speaker-identification
```

The implementation was committed and pushed to GitHub.

The feature branch was then merged into:

```text
main
```

Final FR-8 commit:

```text
05c320b feat: complete meeting participant track identification
```

---

# 📌 Current Project Status

### Epic 1 — Meeting Management

- Meeting creation
- Meeting room
- WebRTC meeting functionality
- Attendance
- Guest lobby
- Recording consent
- Meeting recording

### Epic 2 — AI Transcription & Meeting Intelligence

- FR-7 — Automatic transcription ✅
- FR-8 — Speaker identification ✅
- FR-9 — Transcript correction/search
- FR-10 — AI meeting summary
- FR-11 — Action item extraction ✅
- Story 3.1 — Owner identification ✅

---

# 🚧 Future Work

The next stages of the platform can build on the speaker-labelled transcript:

```text
Speaker-labelled Transcript
          ↓
       FR-9
Transcript Search & Correction
          ↓
       FR-10
AI Meeting Summary
          ↓
       FR-11
Action Item Extraction ✅
          ↓
Story 3.1 — Owner Identification ✅
          ↓
Task Automation — Story 3.4 (future)
```

Possible future improvements include:

- Advanced speaker diarization
- Speaker embeddings
- Confidence scores
- Better multi-speaker overlap handling
- Transcript correction audit logs
- Searchable meeting history
- Meeting intelligence dashboards
- Automatic task creation from confirmed action items (Story 3.4)

---

# 👨‍💻 Project

**AI Office Operations Agent**

An autonomous office operations platform that connects meetings, attendance, recordings, transcription and AI-powered meeting intelligence into one workflow.

Built using:

```text
React + TypeScript
FastAPI + Python
SQLAlchemy
SQLite
WebRTC
faster-whisper
AES-256-GCM
Pytest
```
```

```


# 🔎 Searchable & Editable Transcript — Story 2.3 / FR-9

Story 2.3 extends the speaker-labelled transcript with searchable and editable transcript functionality.

The completed transcript can be viewed from the meeting room by the meeting organizer.

## Transcript Features

The transcript interface displays:

* Speaker name
* Timestamp
* Transcript text
* Correction flag
* Search results
* Editable transcript segments

Example transcript segment:

```json
{
  "start": 0.0,
  "end": 4.0,
  "text": "Hello everyone.",
  "speaker": "Rahul",
  "flagged_for_correction": false
}
```

## 🔍 Transcript Search

Organizers can search the completed transcript using keywords.

The search operates across transcript segments and returns matching segments while preserving their timestamps and speaker information.

Example:

```text
Search: joining

Result:

Rahul
04:00 – 04:29
Thank you for joining the meeting.
```

This allows users to quickly locate specific discussions without manually reading the complete transcript.

## ✏️ Transcript Editing

Meeting organizers can correct transcript segments directly from the transcript panel.

Both of the following can be edited:

* Transcript text
* Speaker name

The original timestamp and other segment metadata are preserved during editing.

Only authorized meeting organizers can modify the transcript.

## 📝 Transcript Correction Audit History

Every transcript modification is recorded in a dedicated correction history table.

The system stores:

* Meeting ID
* Transcript ID
* Segment index
* Original transcript text
* Corrected transcript text
* Original speaker
* Corrected speaker
* Editor ID
* Correction timestamp

Example correction record:

```text
Segment: 8

Original text:
Thank you.

Corrected text:
Thank you for joining the meeting.

Original speaker:
Unknown Speaker

Corrected speaker:
Rahul
```

Multiple corrections to the same transcript segment are preserved as separate audit records.

This provides traceability for transcript changes and supports future AI evaluation of human corrections.

## 🗄️ Transcript Correction Storage

A new database model was introduced:

```text
MeetingTranscriptCorrection
```

The correction records are associated with both the meeting and the corresponding transcript.

The database migration is managed using Alembic:

```text
backend/migrations/versions/
20261005_01_add_meeting_transcript_corrections.py
```

The migration creates:

```text
meeting_transcript_corrections
```

with indexes for meeting and transcript correction history.

## 🔐 Organizer Authorization

Transcript operations require organizer authorization.

Organizer authorization is checked for:

```text
GET  /api/v1/meetings/{meeting_id}/transcript
PATCH /api/v1/meetings/{meeting_id}/transcript
GET  /api/v1/meetings/{meeting_id}/transcript/corrections
```

Unauthorized users cannot view or modify protected transcript information.

## 🔗 Story 2.3 API

### Get completed transcript

```http
GET /api/v1/meetings/{meeting_id}/transcript
```

Returns the completed transcript with:

* Transcript metadata
* Timestamped segments
* Speaker information
* Correction flags

### Update transcript

```http
PATCH /api/v1/meetings/{meeting_id}/transcript
```

Allows the organizer to update transcript text and speaker names.

Example request:

```json
{
  "segments": [
    {
      "text": "Thank you for joining the meeting.",
      "speaker": "Rahul"
    }
  ]
}
```

The segment count must match the existing transcript to prevent accidental structural changes.

### Get correction history

```http
GET /api/v1/meetings/{meeting_id}/transcript/corrections
```

Returns the audit history of transcript corrections for the meeting.

## 🧩 Frontend Implementation

The searchable and editable transcript UI is implemented in:

```text
src/components/meetings/TranscriptPanel.tsx
```

The transcript panel is integrated into:

```text
src/pages/MeetingRoomPage.tsx
```

The frontend API integration is implemented in:

```text
src/api/meetings.ts
```

The transcript supports:

* Loading completed transcripts
* Keyword search
* Segment editing
* Speaker editing
* Save/update operations
* Correction history display
* Loading and error states

## 🧪 Story 2.3 Testing

Backend transcript API tests cover:

* Completed transcript retrieval
* Organizer authorization
* Transcript updates
* Speaker-name corrections
* Text corrections
* Correction history
* Invalid transcript updates
* Missing transcript handling

Backend focused transcript API tests:

```text
7 passed
```

Frontend transcript tests cover:

* Transcript rendering
* Keyword search
* Timestamp display
* Speaker display
* Text editing
* Speaker editing
* Save/update flow
* Correction history
* Error handling

Frontend transcript tests:

```text
9 passed
```

The related frontend API tests also passed:

```text
3 passed
```

Combined focused frontend validation:

```text
12 passed
```

## 🌐 Browser Validation

The complete Story 2.3 flow was validated through the actual meeting browser flow.

Validation included:

```text
Meeting
   ↓
Recording
   ↓
Recording Upload
   ↓
Whisper Transcription
   ↓
Completed Transcript
   ↓
Transcript Display
   ↓
Keyword Search
   ↓
Edit Transcript Text
   ↓
Edit Speaker Name
   ↓
Save Correction
   ↓
Correction Audit History
```

The validation confirmed that transcript edits are persisted in the database and that multiple edits to the same segment are retained in the correction history.

## 🏗️ Story 2.3 Implementation Summary

| Component                       | Status |
| ------------------------------- | ------ |
| Completed transcript retrieval  | ✅      |
| Timestamped transcript segments | ✅      |
| Speaker-labelled transcript     | ✅      |
| Keyword transcript search       | ✅      |
| Timestamped search results      | ✅      |
| Organizer transcript editing    | ✅      |
| Transcript text correction      | ✅      |
| Speaker-name correction         | ✅      |
| Correction audit logging        | ✅      |
| Correction persistence          | ✅      |
| Correction history API          | ✅      |
| Organizer authorization         | ✅      |
| Alembic migration               | ✅      |
| Backend transcript API tests    | ✅      |
| Frontend transcript tests       | ✅      |
| Browser validation              | ✅      |
| Production build verification   | ✅      |

Story 2.3 is implemented as a post-recording transcript workflow. The existing `faster-whisper` transcription pipeline produces the transcript after the recording is uploaded and processed, while Story 2.3 provides the searchable and editable interface for the completed transcript.

The implementation is designed to be reused by the following meeting-intelligence features without recreating the transcription or transcript-management pipeline.


# 🤖 FR-10 — AI Meeting Summary

The AI Meeting Summary feature generates a structured summary from the completed meeting transcript.

### Summary includes

- Meeting overview
- Decisions
- Discussion topics
- Open questions

### Summary workflow

```text
Completed Meeting Recording
          ↓
Automatic Transcription
          ↓
Speaker Identification
          ↓
Transcript Search / Correction
          ↓
FR-10 — AI Meeting Summary
          ↓
Persisted Meeting Summary
          ↓
Meeting Report / View History
```

# 👤 Story 3.1 — Owner Identification

Story 3.1 matches extracted meeting action-item owner names against the
active employee directory and lets the organizer resolve ownership when
the result is uncertain.

### Owner identification workflow

```text
Extracted Action Item
          ↓
Match Owner Against Active Employees
          ↓
Single Confident Match ──→ Proposed Owner
          ↓
Multiple Matches or No Match
          ↓
Approval Required
          ↓
Organizer Selects and Confirms Owner
```

A unique match is proposed, not automatically confirmed. Ambiguous and
unmatched owners require organizer action. The original extracted owner
name and matching result remain available after confirmation.

### Implementation

- Employee directory records include a unique employee ID, name, email,
  and active status.
- Employee and action-item ownership schema changes are applied through
  database migrations.
- The meeting action-items UI displays matching and ownership status and
  supports selecting and confirming an active employee.
- Backend tests cover employee matching and owner confirmation; frontend
  tests cover owner status display and selection flow.

### Story 3.1 implementation summary

| Component                               | Status          |
| --------------------------------------- | --------------- |
| Active employee directory integration  | ✅               |
| Unique-match owner proposal             | ✅               |
| Ambiguous-match approval handling       | ✅               |
| No-match approval handling              | ✅               |
| Organizer owner selection/confirmation  | ✅               |
| Employee and ownership migrations       | ✅               |
| Backend and frontend tests              | ✅               |
| Task creation                           | Not implemented |

Story 3.1 does not create tasks. AC4—verifying that no task is created
before owner selection—requires end-to-end verification after Story 3.4
introduces the task-creation workflow.


# 📌 Current Project Status

### Epic 1 — Meeting Management

- Meeting creation
- Meeting room
- WebRTC meeting functionality
- Attendance
- Guest lobby
- Recording consent
- Meeting recording

### Epic 2 — AI Transcription & Meeting Intelligence

- FR-7 — Automatic transcription ✅
- FR-8 — Speaker identification ✅
- FR-9 — Transcript correction/search ✅
- FR-10 — AI meeting summary ✅
- FR-11 — Action item extraction ✅
- Story 3.1 — Owner identification ✅

---