import { FormEvent, useState } from 'react'
import { CalendarPlus, CalendarClock, Zap, Check, Copy, Share2, Video } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { demoUsers } from '../mocks/mockUser'
import { useAuth } from '../contexts/AuthContext'
import { createMeeting, getCalendarMeetings } from '../api/meetings'

type Mode = 'pick' | 'instant' | 'scheduled' | 'created'

type Participant = { email?: string; user_id?: string }

const EMAIL_PATTERN = /^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,63}$/i

function normalizeParticipantEmail(email: string): string {
  return email.trim().toLowerCase()
}

function organizationLocalDateTimeToUtc(value: string, timeZone: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value)
  if (!match) {
    throw new Error('Enter a valid start and end time.')
  }

  const [, yearText, monthText, dayText, hourText, minuteText] = match
  const target = {
    year: Number(yearText),
    month: Number(monthText),
    day: Number(dayText),
    hour: Number(hourText),
    minute: Number(minuteText),
  }
  const targetAsUtc = Date.UTC(
    target.year,
    target.month - 1,
    target.day,
    target.hour,
    target.minute,
  )
  const targetDate = new Date(targetAsUtc)
  if (
    targetDate.getUTCFullYear() !== target.year
    || targetDate.getUTCMonth() + 1 !== target.month
    || targetDate.getUTCDate() !== target.day
    || target.hour > 23
    || target.minute > 59
  ) {
    throw new Error('Enter a valid start and end time.')
  }

  const formatter = new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  })

  let candidate = targetAsUtc
  for (let attempt = 0; attempt < 4; attempt += 1) {
    const parts = formatter.formatToParts(new Date(candidate))
    const partValue = (type: Intl.DateTimeFormatPartTypes) => {
      const valuePart = parts.find((part) => part.type === type)?.value
      if (!valuePart) throw new Error(`Could not resolve ${type} in ${timeZone}.`)
      return Number(valuePart)
    }
    const displayedAsUtc = Date.UTC(
      partValue('year'),
      partValue('month') - 1,
      partValue('day'),
      partValue('hour'),
      partValue('minute'),
      partValue('second'),
    )
    const correction = targetAsUtc - displayedAsUtc
    if (correction === 0) return new Date(candidate).toISOString()
    candidate += correction
  }

  throw new Error(`The selected local time is not valid in ${timeZone}.`)
}

interface CreatedMeeting {
  id: string
  title: string
  join_url: string
  status: string
  invitationsSent: string[]
  invitationFailures: string[]
}

export function CreateMeetingPlaceholder() {
  const { user } = useAuth()
  const navigate = useNavigate()
  const [mode, setMode] = useState<Mode>('pick')
  const [title, setTitle] = useState('')
  const [agenda, setAgenda] = useState('')
  const [startAt, setStartAt] = useState('')
  const [endAt, setEndAt] = useState('')
  const [participantEmail, setParticipantEmail] = useState('')
  const [participants, setParticipants] = useState<Participant[]>([])
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [created, setCreated] = useState<CreatedMeeting | null>(null)
  const [copied, setCopied] = useState(false)

  function addParticipant() {
    const email = normalizeParticipantEmail(participantEmail)
    if (!email) return
    if (!EMAIL_PATTERN.test(email) || email.length > 254) {
      setError('Enter a valid email address for the participant.')
      return
    }
    setParticipants((current) => (
      current.some((participant) => normalizeParticipantEmail(participant.email ?? '') === email)
        ? current
        : [...current, { email }]
    ))
    setError('')
    setParticipantEmail('')
  }

  async function handleCopy(url: string) {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      setTimeout(() => setCopied(false), 2500)
    } catch { /* ignore */ }
  }

  async function handleShare(meeting: CreatedMeeting) {
    try {
      if (navigator.share) {
        await navigator.share({ title: meeting.title, text: 'Join my meeting', url: meeting.join_url })
      } else {
        await handleCopy(meeting.join_url)
      }
    } catch { /* ignore */ }
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError('')
    const isInstant = mode === 'instant'
    const pendingParticipantEmail = normalizeParticipantEmail(participantEmail)
    if (pendingParticipantEmail && (
      pendingParticipantEmail.length > 254 ||
      !EMAIL_PATTERN.test(pendingParticipantEmail)
    )) {
      setError('Enter a valid email address for the participant.')
      return
    }

    if (!title.trim()) {
      setError('Meeting title is required.')
      return
    }

    if (!isInstant) {
      if (!startAt || !endAt) {
        setError('Start and end times are required for scheduled meetings.')
        return
      }
      if (endAt <= startAt) {
        setError('End time must be after start time.')
        return
      }
    }

    const submittedParticipants = new Map<string, Participant>()
    for (const participant of participants) {
      if (!participant.email) continue
      const email = normalizeParticipantEmail(participant.email)
      if (email) submittedParticipants.set(email, { ...participant, email })
    }
    if (pendingParticipantEmail && !submittedParticipants.has(pendingParticipantEmail)) {
      submittedParticipants.set(pendingParticipantEmail, { email: pendingParticipantEmail })
    }

    setLoading(true)
    try {
      const body: {
        title: string
        agenda: string | null
        meeting_type: 'instant' | 'scheduled'
        participants: Participant[]
        start_at?: string
        end_at?: string
      } = {
        title: title.trim(),
        agenda: agenda.trim() || null,
        meeting_type: isInstant ? 'instant' : 'scheduled',
        participants: [...submittedParticipants.values()],
      }
      if (!isInstant) {
        const now = new Date()
        const calendar = await getCalendarMeetings(now.getFullYear(), now.getMonth() + 1)
        body.start_at = organizationLocalDateTimeToUtc(startAt, calendar.timezone)
        body.end_at = organizationLocalDateTimeToUtc(endAt, calendar.timezone)
      }

      const data = await createMeeting(body, user?.id ?? 'demo-organizer')

      setCreated({
        id: data.id,
        title: data.title,
        join_url: data.join_url,
        status: data.status,
        invitationsSent: data.invitations_sent,
        invitationFailures: data.invitation_failures,
      })
      setMode('created')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to connect to the meeting server.')
    } finally {
      setLoading(false)
    }
  }

  // ── Mode picker ──────────────────────────────────────────────────────────────
  if (mode === 'pick') {
    return (
      <section className="create-meeting-page" aria-labelledby="create-meeting-title">
        <div className="state-icon" aria-hidden="true"><CalendarPlus size={28} /></div>
        <p className="eyebrow">New meeting</p>
        <h2 id="create-meeting-title">How would you like to meet?</h2>
        <div className="meeting-mode-grid">
          <button className="meeting-mode-card" onClick={() => setMode('instant')} type="button">
            <Zap size={26} className="mode-icon" aria-hidden="true" />
            <strong>Start an instant meeting</strong>
            <span>Create an instant meeting and invite others right away.</span>
          </button>
          <button className="meeting-mode-card" onClick={() => setMode('scheduled')} type="button">
            <CalendarClock size={26} className="mode-icon" aria-hidden="true" />
            <strong>Schedule for later</strong>
            <span>Set a date and time. Share the link in advance.</span>
          </button>
        </div>
      </section>
    )
  }

  // ── Created card ─────────────────────────────────────────────────────────────
  if (mode === 'created' && created) {
    const joinUrl = created.join_url
    return (
      <section className="create-meeting-page" aria-labelledby="meeting-created-title">
        <div className="state-icon success" style={{ background: '#e8f7f0', color: '#0a6b3e' }} aria-hidden="true">
          <Check size={28} />
        </div>
        <p className="eyebrow">Meeting {created.status === 'instant' ? 'ready' : 'scheduled'}</p>
        <h2 id="meeting-created-title">{created.title}</h2>
        {created.invitationFailures.length > 0 ? (
          <p role="alert">
            Meeting created. {created.invitationsSent.length} invitation(s) sent; invitations could not be
            delivered to: {created.invitationFailures.join(', ')}.
          </p>
        ) : (
          <p role="status">
            {created.invitationsSent.length > 0
              ? `Invitation email sent to ${created.invitationsSent.length} participant(s).`
              : 'No participant invitations were needed.'}
          </p>
        )}

        <div className="meeting-created-link-box">
          <label>Meeting link</label>
          <div className="meeting-link-row">
            <input type="text" value={joinUrl} readOnly aria-label="Meeting link" />
            <button className="button secondary" type="button" onClick={() => handleCopy(joinUrl)}>
              {copied ? <><Check size={15} /> Copied!</> : <><Copy size={15} /> Copy</>}
            </button>
          </div>
        </div>

        <div className="meeting-created-actions">
          {typeof navigator.share === 'function' && (
            <button className="button secondary" type="button" onClick={() => handleShare(created)}>
              <Share2 size={15} /> Share
            </button>
          )}
          <button
            className="button primary"
            type="button"
            onClick={() => navigate(`/meetings/${encodeURIComponent(created.id)}`)}
          >
            <Video size={15} /> {created.status === 'instant' ? 'Join now' : 'Open meeting'}
          </button>
        </div>

        <button
          className="button minimal"
          type="button"
          style={{ marginTop: '0.5rem', fontSize: '0.85rem', color: 'var(--muted)', border: 'none', background: 'none' }}
          onClick={() => { setMode('pick'); setTitle(''); setAgenda(''); setParticipants([]); setCreated(null) }}
        >
          Create another meeting
        </button>
      </section>
    )
  }

  // ── Create form (instant or scheduled) ───────────────────────────────────────
  const isInstant = mode === 'instant'
  return (
    <section className="create-meeting-page" aria-labelledby="create-meeting-title">
      <div className="state-icon" aria-hidden="true">
        {isInstant ? <Zap size={28} /> : <CalendarClock size={28} />}
      </div>
      <p className="eyebrow">{isInstant ? 'Instant meeting' : 'Schedule a meeting'}</p>
      <h2 id="create-meeting-title">{isInstant ? 'Start an instant meeting' : 'Schedule a meeting'}</h2>

      <form className="create-meeting-form" onSubmit={handleSubmit} noValidate>
        <div>
          <label htmlFor="meeting-title">Meeting title</label>
          <input
            id="meeting-title"
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder={isInstant ? 'E.g., Quick sync' : 'E.g., Sprint planning'}
            autoFocus
          />
        </div>

        <div>
          <label htmlFor="meeting-agenda">Agenda <span style={{ fontWeight: 400, color: 'var(--muted)' }}>(optional)</span></label>
          <textarea
            id="meeting-agenda"
            value={agenda}
            onChange={(e) => setAgenda(e.target.value)}
            placeholder="Topics to discuss…"
            rows={3}
          />
        </div>

        {!isInstant && (
          <>
            <div>
              <label htmlFor="start-at">Start time</label>
              <input id="start-at" type="datetime-local" value={startAt} onChange={(e) => setStartAt(e.target.value)} />
            </div>
            <div>
              <label htmlFor="end-at">End time</label>
              <input id="end-at" type="datetime-local" value={endAt} onChange={(e) => setEndAt(e.target.value)} />
            </div>
          </>
        )}

        <div>
          <label htmlFor="participant-email">
            Invite participants <span style={{ fontWeight: 400, color: 'var(--muted)' }}>(optional)</span>
          </label>
          <small className="meeting-form-hint">
            Demo accounts: {Object.values(demoUsers).map((u) => `${u.name} (${u.email})`).join(' · ')}
          </small>
          <div style={{ display: 'flex', gap: '8px' }}>
            <input
              id="participant-email"
              type="email"
              value={participantEmail}
              onChange={(e) => setParticipantEmail(e.target.value)}
              placeholder="colleague@example.com"
              style={{ flex: 1 }}
            />
            <button type="button" className="button secondary" style={{ whiteSpace: 'nowrap' }} onClick={addParticipant}>
              Add
            </button>
          </div>
          {participants.length > 0 && (
            <ul style={{ margin: '4px 0 0', padding: 0, listStyle: 'none', display: 'flex', flexWrap: 'wrap', gap: '6px' }}>
              {participants.map((p, i) => (
                <li key={i} style={{ background: '#eaf2ff', borderRadius: '999px', padding: '3px 10px', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}>
                  {p.email}
                  <button type="button" style={{ border: 'none', background: 'none', cursor: 'pointer', fontSize: '14px', lineHeight: 1, color: '#617087' }} onClick={() => setParticipants((c) => c.filter((_, j) => j !== i))}>×</button>
                </li>
              ))}
            </ul>
          )}
        </div>

        {error && <p role="alert" style={{ margin: 0, color: 'var(--danger)', fontSize: '13px' }}>{error}</p>}

        <div style={{ display: 'flex', gap: '10px', alignItems: 'center' }}>
          <button type="submit" disabled={loading}>
            {loading ? 'Creating…' : isInstant ? '🚀 Start an instant meeting' : 'Schedule meeting'}
          </button>
          <button
            type="button"
            className="button secondary"
            onClick={() => setMode('pick')}
          >
            Back
          </button>
        </div>
      </form>
    </section>
  )
}
