import { useEffect, useState } from 'react'
import { CalendarDays, Plus, RefreshCw } from 'lucide-react'
import { Link } from 'react-router-dom'
import { getMeetings } from '../api/meetings'
import { MeetingCard } from '../components/meetings/MeetingCard'
import { TodaysMeetings } from '../components/meetings/TodaysMeetings'
import { useAuth } from '../contexts/AuthContext'
import type { Meeting, MeetingListRecord } from '../types/meeting'

function parseMeetingDate(value: string): Date {
  const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
  return new Date(hasTimezone ? value : `${value}Z`)
}

function dateKeyInTimeZone(value: Date, timeZone: string): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(value)
  const values = Object.fromEntries(parts.map(({ type, value: part }) => [type, part]))
  return `${values.year}-${values.month}-${values.day}`
}

function lifecycleFor(meeting: MeetingListRecord, now: number): Meeting['lifecycle'] {
  const status = meeting.status.toLowerCase()
  const start = parseMeetingDate(meeting.start_at).getTime()
  const end = parseMeetingDate(meeting.end_at).getTime()

  if (status === 'completed' || status === 'cancelled' || (Number.isFinite(end) && end < now)) {
    return 'completed'
  }
  if (Number.isFinite(start) && Number.isFinite(end) && start <= now && now <= end) {
    return 'active'
  }
  return 'upcoming'
}

function formatDateTime(value: string, timeZone: string): string {
  const date = parseMeetingDate(value)
  return Number.isNaN(date.getTime())
    ? 'Time unavailable'
    : new Intl.DateTimeFormat(undefined, {
        dateStyle: 'medium',
        timeStyle: 'short',
        timeZone,
      }).format(date)
}

function isToday(value: string, timeZone: string): boolean {
  const date = parseMeetingDate(value)
  return !Number.isNaN(date.getTime()) &&
    dateKeyInTimeZone(date, timeZone) === dateKeyInTimeZone(new Date(), timeZone)
}

function toMeeting(record: MeetingListRecord, now: number): Meeting {
  return {
    id: record.id,
    title: record.title,
    agenda: record.agenda,
    startTime: formatDateTime(record.start_at, record.timezone),
    endTime: formatDateTime(record.end_at, record.timezone),
    startAt: record.start_at,
    status: record.status,
    joinUrl: record.join_url,
    organizerId: record.organizer_id,
    lifecycle: lifecycleFor(record, now),
    timezone: record.timezone,
  }
}

function MeetingGroup({ title, eyebrow, meetings, onDelete }: { title: string; eyebrow: string; meetings: Meeting[]; onDelete?: (id: string) => void }) {
  if (meetings.length === 0) return null
  return (
    <section aria-label={title}>
      <div className="section-heading">
        <div>
          <p className="eyebrow">{eyebrow}</p>
          <h2>{title}</h2>
        </div>
        <span className="date-label">{meetings.length} {meetings.length === 1 ? 'meeting' : 'meetings'}</span>
      </div>
      <div className="meeting-list">
        {meetings.map((meeting) => <MeetingCard key={meeting.id} meeting={meeting} onDelete={onDelete} />)}
      </div>
    </section>
  )
}

export function MeetingsPage() {
  const { user } = useAuth()
  const [meetings, setMeetings] = useState<Meeting[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [retryKey, setRetryKey] = useState(0)
  const [toastMessage, setToastMessage] = useState('')

  function handleDeleteMeeting(meetingId: string) {
    setMeetings((current) => current.filter((m) => m.id !== meetingId))
    setToastMessage('Meeting deleted')
    setTimeout(() => setToastMessage(''), 4000)
  }

  useEffect(() => {
    if (!user) return
    let active = true
    setLoading(true)
    setError('')
    getMeetings(user.id, user.email)
      .then((records) => {
        if (!active) return
        const now = Date.now()
        const nextMeetings = [...records]
          .sort((left, right) => parseMeetingDate(left.start_at).getTime() - parseMeetingDate(right.start_at).getTime())
          .map((record) => toMeeting(record, now))
        setMeetings(nextMeetings)
      })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : 'Meetings could not be loaded.')
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => { active = false }
  }, [user, retryKey])

  const today = meetings.filter((meeting) => meeting.lifecycle !== 'completed' && (
    isToday(meeting.startAt ?? meeting.startTime, meeting.timezone ?? 'UTC') || meeting.lifecycle === 'active'
  ))
  const upcoming = meetings.filter((meeting) => meeting.lifecycle === 'upcoming' && !isToday(meeting.startAt ?? meeting.startTime, meeting.timezone ?? 'UTC'))
  const completed = meetings.filter((meeting) => meeting.lifecycle === 'completed')

  return (
    <div className="page-stack meetings-page">
      <header className="meetings-page-header">
        <div>
          <p className="eyebrow">Schedule and access</p>
          <h2>Meetings</h2>
          <p>Join meetings you organize or have been invited to.</p>
        </div>
        <Link className="button primary" to="/meetings/create"><Plus size={17} aria-hidden="true" /> Create Meeting</Link>
      </header>

      {loading ? (
        <section className="meetings-loading" role="status" aria-live="polite">
          <CalendarDays size={20} aria-hidden="true" /> Loading your meetings…
        </section>
      ) : error ? (
        <section className="meetings-error" role="alert">
          <div><h3>Meetings couldn’t be loaded</h3><p>{error}</p></div>
          <button className="button secondary" type="button" onClick={() => setRetryKey((key) => key + 1)}>
            <RefreshCw size={16} aria-hidden="true" /> Retry
          </button>
        </section>
      ) : meetings.length === 0 ? (
        <section className="meetings-empty">
          <div className="state-icon" aria-hidden="true"><CalendarDays size={26} /></div>
          <h3>No meetings scheduled</h3>
          <p>Meetings you organize or are invited to will appear here.</p>
          <Link className="button primary" to="/meetings/create"><Plus size={17} aria-hidden="true" /> Create Meeting</Link>
        </section>
      ) : (
        <div className="meetings-groups">
          {today.length > 0 ? <TodaysMeetings meetings={today} /> : (
            <section className="meetings-today-empty"><p className="eyebrow">Schedule</p><h2>Today's meetings</h2><p>No meetings today.</p></section>
          )}
          <MeetingGroup title="Upcoming meetings" eyebrow="On your calendar" meetings={upcoming} />
          <MeetingGroup title="Completed meetings" eyebrow="Past meetings" meetings={completed} onDelete={handleDeleteMeeting} />
        </div>
      )}

      {toastMessage && (
        <div
          role="status"
          aria-live="polite"
          style={{
            position: 'fixed',
            bottom: '24px',
            right: '24px',
            background: '#1e293b',
            color: '#f8fafc',
            border: '1px solid #334155',
            padding: '10px 18px',
            borderRadius: '8px',
            boxShadow: '0 4px 12px rgba(0,0,0,0.25)',
            zIndex: 9999,
            fontSize: '14px',
            fontWeight: 500,
          }}
        >
          {toastMessage}
        </div>
      )}
    </div>
  )
}
