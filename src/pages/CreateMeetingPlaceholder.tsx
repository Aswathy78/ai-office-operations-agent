import { FormEvent, useState } from 'react'
import { CalendarPlus } from 'lucide-react'

type Participant = {
  user_id?: string
  email?: string
}

export function CreateMeetingPlaceholder() {
  const [title, setTitle] = useState('')
  const [agenda, setAgenda] = useState('')
  const [meetingType, setMeetingType] = useState<'scheduled' | 'instant'>(
    'scheduled',
  )
  const [startAt, setStartAt] = useState('')
  const [endAt, setEndAt] = useState('')
  const [participantEmail, setParticipantEmail] = useState('')
  const [participants, setParticipants] = useState<Participant[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [success, setSuccess] = useState('')
  const [joinUrl, setJoinUrl] = useState('')

  function addParticipant() {
    const email = participantEmail.trim()

    if (!email) {
      return
    }

    setParticipants((current) => [
      ...current,
      { email },
    ])

    setParticipantEmail('')
  }

  function removeParticipant(index: number) {
    setParticipants((current) =>
      current.filter((_, participantIndex) => participantIndex !== index),
    )
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
  event.preventDefault()

  setError('')
  setSuccess('')
  setJoinUrl('')
  setLoading(true)

  if (!title.trim()) {
    setError('Meeting title is required.')
    setLoading(false)
    return
  }

  if (!startAt || !endAt) {
    setError('Start time and end time are required.')
    setLoading(false)
    return
  }

  if (new Date(endAt) <= new Date(startAt)) {
    setError('End time must be after start time.')
    setLoading(false)
    return
  }

  try {
    const response = await fetch(
      'http://127.0.0.1:9000/api/v1/meetings/',
      {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          title: title.trim(),
          agenda: agenda.trim() || null,
          meeting_type: meetingType,
          start_at: startAt,
          end_at: endAt,
          participants,
        }),
      },
    )

    const data = await response.json()

    if (!response.ok) {
      throw new Error(
        data.detail || 'Failed to create meeting.',
      )
    }

    setSuccess('Meeting created successfully!')
    setJoinUrl(data.join_url)

    setTitle('')
    setAgenda('')
    setParticipantEmail('')
    setParticipants([])
  } catch (err) {
    setError(
      err instanceof Error
        ? err.message
        : 'Unable to connect to the meeting server.',
    )
  } finally {
    setLoading(false)
  }
}

  return (
    <section className="create-meeting-page" aria-labelledby="create-meeting-title">
      <div className="state-icon" aria-hidden="true">
        <CalendarPlus size={28} />
      </div>

      <p className="eyebrow">Story 1.2</p>

      <h2 id="create-meeting-title">Create Meeting</h2>

      <form className="create-meeting-form" onSubmit={handleSubmit}>
        <div>
          <label htmlFor="meeting-title">Meeting Title</label>
          <input
            id="meeting-title"
            type="text"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Enter meeting title"
          />
        </div>

        <div>
          <label htmlFor="meeting-agenda">Agenda</label>
          <textarea
            id="meeting-agenda"
            value={agenda}
            onChange={(event) => setAgenda(event.target.value)}
            placeholder="Enter meeting agenda"
            rows={4}
          />
        </div>

        <div>
          <label htmlFor="meeting-type">Meeting Type</label>
          <select
            id="meeting-type"
            value={meetingType}
            onChange={(event) =>
              setMeetingType(
                event.target.value as 'scheduled' | 'instant',
              )
            }
          >
            <option value="scheduled">Scheduled</option>
            <option value="instant">Instant</option>
          </select>
        </div>

        <div>
          <label htmlFor="start-at">Start Time</label>
          <input
            id="start-at"
            type="datetime-local"
            value={startAt}
            onChange={(event) => setStartAt(event.target.value)}
          />
        </div>

        <div>
          <label htmlFor="end-at">End Time</label>
          <input
            id="end-at"
            type="datetime-local"
            value={endAt}
            onChange={(event) => setEndAt(event.target.value)}
          />
        </div>

        <div>
          <label htmlFor="participant-email">Participant Email</label>

          <div>
            <input
              id="participant-email"
              type="email"
              value={participantEmail}
              onChange={(event) => setParticipantEmail(event.target.value)}
              placeholder="participant@example.com"
            />

            <button type="button" onClick={addParticipant}>
              Add Participant
            </button>
          </div>
        </div>

        {participants.length > 0 && (
          <div>
            <p>Participants</p>

            <ul>
              {participants.map((participant, index) => (
                <li key={`${participant.email}-${index}`}>
                  {participant.email}

                  <button
                    type="button"
                    onClick={() => removeParticipant(index)}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}

        {error && (
          <p role="alert">
            {error}
          </p>
        )}
{success && (
  <p
    role="status"
    style={{
      padding: '10px 12px',
      borderRadius: '8px',
      background: '#ecfdf5',
      color: '#047857',
    }}
  >
    {success}
  </p>
)}

{joinUrl && (
  <div>
    <label>Meeting Join Link</label>
    <input
      type="text"
      value={joinUrl}
      readOnly
    />
  </div>
)}
        <button type="submit" disabled={loading}>
  {loading ? 'Creating Meeting...' : 'Create Meeting'}
</button>
      </form>
    </section>
  )
}
