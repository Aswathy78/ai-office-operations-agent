import { AlertTriangle, Calendar, CheckCircle2, Clock, HelpCircle, Users, Video, XCircle } from 'lucide-react'
import type { ScheduleResult } from '../../types/meeting'

interface Props {
  result: ScheduleResult
}

/**
 * Treat naive datetime strings (no timezone suffix) as UTC by appending 'Z'.
 * The backend stores meeting timestamps as naive UTC values, e.g.
 * "2026-10-18T04:30:00". Without 'Z', the JS Date constructor interprets
 * them as local time, causing an incorrect display in non-UTC locales.
 * With 'Z' appended the instant can be formatted in the API-provided
 * organization timezone without treating the UTC value as local time.
 */
function toUTCDate(s: string): Date {
  if (s.endsWith('Z') || s.includes('+') || /[+-]\d{2}:\d{2}$/.test(s)) {
    return new Date(s)
  }
  return new Date(s + 'Z')
}

function formatMeetingDateTimeRange(
  startStr: string,
  endStr: string,
  timeZone: string,
): string {
  try {
    const start = toUTCDate(startStr)
    const end = toUTCDate(endStr)
    if (isNaN(start.getTime()) || isNaN(end.getTime())) {
      return `${startStr} – ${endStr}`
    }
    const dateFormatted = start.toLocaleDateString(undefined, {
      weekday: 'short',
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      timeZone,
    })
    const startTimeFormatted = start.toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      timeZone,
    })
    const endTimeFormatted = end.toLocaleTimeString(undefined, {
      hour: 'numeric',
      minute: '2-digit',
      timeZone,
    })
    return `${dateFormatted}, ${startTimeFormatted} – ${endTimeFormatted}`
  } catch {
    return `${startStr} – ${endStr}`
  }
}

export function ScheduledMeetingCard({ result }: Props) {
  if (result.status === 'Completed' && result.meeting) {
    const meeting = result.meeting
    const timeDisplay = formatMeetingDateTimeRange(
      meeting.start_at,
      meeting.end_at,
      result.timezone,
    )

    return (
      <div className="scheduled-meeting-card" aria-label="Scheduled meeting details">
        <div className="scheduled-meeting-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{ color: 'var(--success)', display: 'inline-flex' }}>
              <CheckCircle2 size={18} aria-hidden="true" />
            </span>
            <h4 className="scheduled-meeting-title">{meeting.title}</h4>
          </div>
          <span className="scheduled-meeting-provider-badge" title="Calendar provider for this event">
            {result.calendar_provider && result.calendar_provider !== 'local'
              ? result.calendar_provider
              : 'Local Calendar (Dev)'}
          </span>
        </div>

        <div className="scheduled-meeting-meta">
          <div className="scheduled-meeting-meta-item">
            <Clock size={15} style={{ color: 'var(--muted)', flexShrink: 0 }} aria-hidden="true" />
            <span>{timeDisplay}</span>
          </div>
          {meeting.agenda && (
            <div className="scheduled-meeting-meta-item">
              <Calendar size={15} style={{ color: 'var(--muted)', flexShrink: 0 }} aria-hidden="true" />
              <span>{meeting.agenda}</span>
            </div>
          )}
        </div>

        {meeting.join_url && (
          <div className="scheduled-meeting-join">
            <Video size={16} style={{ color: '#174d8e' }} aria-hidden="true" />
            <a
              href={meeting.join_url}
              target="_blank"
              rel="noopener noreferrer"
              className="scheduled-join-link"
            >
              Join Meeting Link
            </a>
            <span style={{ color: 'var(--muted)', fontSize: '11px' }}>({meeting.join_url})</span>
          </div>
        )}

        {result.resolved_attendees.length > 0 && (
          <div className="scheduled-meeting-attendees">
            <div className="scheduled-meeting-attendees-header">
              <Users size={13} style={{ display: 'inline', marginRight: '4px' }} aria-hidden="true" />
              Attendees ({result.resolved_attendees.length})
            </div>
            <ul className="scheduled-meeting-attendee-list">
              {result.resolved_attendees.map((attendee) => (
                <li key={attendee.id} className="scheduled-meeting-attendee-chip">
                  <span>{attendee.name}</span>
                  {attendee.team && <span className="chip-team">({attendee.team})</span>}
                </li>
              ))}
            </ul>
          </div>
        )}

        {result.invitations_sent.length > 0 ? (
          <div className="scheduled-meeting-invites-sent">
            <CheckCircle2 size={13} aria-hidden="true" />
            <span>Invitations sent to {result.invitations_sent.length} attendee(s)</span>
          </div>
        ) : (
          <div style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '2px' }}>
            <span>No email invitations sent (SMTP delivery unconfigured or skipped in test mode)</span>
          </div>
        )}
      </div>
    )
  }

  if (result.status === 'error' && result.meeting) {
    return (
      <div className="scheduled-meeting-card error" aria-label="Meeting created, but invitations failed">
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
          <span style={{ color: 'var(--danger)', display: 'inline-flex', marginTop: '2px' }}>
            <XCircle size={18} aria-hidden="true" />
          </span>
          <div>
            <h4 className="scheduled-meeting-title" style={{ color: 'var(--danger)', marginBottom: '4px' }}>
              Meeting created, but invitations failed
            </h4>
            <p style={{ margin: 0, fontSize: '13px', color: '#991b1b' }}>{result.message}</p>
            <p style={{ margin: '8px 0 0', fontSize: '13px' }}>
              Meeting: <strong>{result.meeting.title}</strong>
            </p>
            {result.meeting.join_url && (
              <a
                href={result.meeting.join_url}
                target="_blank"
                rel="noopener noreferrer"
                className="scheduled-join-link"
              >
                Open meeting
              </a>
            )}
            {result.invitation_failures.length > 0 && (
              <p style={{ margin: '8px 0 0', fontSize: '12px' }}>
                Invitation delivery failed for: {result.invitation_failures.join(', ')}
              </p>
            )}
          </div>
        </div>
      </div>
    )
  }

  if (result.status === 'conflict') {
    return (
      <div className="scheduled-meeting-card conflict" aria-label="Meeting scheduling conflict">
        <div className="scheduled-meeting-header">
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <span style={{ color: 'var(--warning)', display: 'inline-flex' }}>
              <AlertTriangle size={18} aria-hidden="true" />
            </span>
            <h4 className="scheduled-meeting-title" style={{ color: '#92400e' }}>
              Availability Conflict Detected
            </h4>
          </div>
          <span className="scheduled-meeting-provider-badge" style={{ background: '#fef3c7', color: '#92400e' }}>
            Not Scheduled
          </span>
        </div>

        <p style={{ margin: 0, fontSize: '13px', color: '#78350f' }}>
          To prevent double-booking, the meeting was not created. The following conflict(s) were found:
        </p>

        <div style={{ display: 'grid', gap: '8px' }}>
          {result.conflicts.map((conflict, index) => (
            <div key={index} className="scheduled-meeting-conflict-item">
              <strong>{conflict.attendee_name || conflict.attendee_email}</strong> is busy with “
              {conflict.conflicting_event_title}”
              <div style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '2px' }}>
                {formatMeetingDateTimeRange(
                  conflict.conflicting_start_at,
                  conflict.conflicting_end_at,
                  result.timezone,
                )}
              </div>
            </div>
          ))}
        </div>

        <div style={{ fontSize: '12px', color: 'var(--muted)', marginTop: '2px' }}>
          Please suggest another time or date to avoid these conflicts.
        </div>
      </div>
    )
  }

  if (result.status === 'needs_clarification') {
    return (
      <div className="scheduled-meeting-card" style={{ background: '#f0f9ff', borderColor: '#bae6fd' }} aria-label="Meeting clarification needed">
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{ color: '#0284c7', display: 'inline-flex' }}>
            <HelpCircle size={16} aria-hidden="true" />
          </span>
          <span style={{ fontSize: '12px', fontWeight: 600, color: '#0369a1' }}>
            Please reply to clarify attendee or time details
          </span>
        </div>
      </div>
    )
  }

  if (result.status === 'error') {
    return (
      <div className="scheduled-meeting-card error" aria-label="Meeting scheduling error">
        <div style={{ display: 'flex', alignItems: 'flex-start', gap: '10px' }}>
          <span style={{ color: 'var(--danger)', display: 'inline-flex', marginTop: '2px' }}>
            <XCircle size={18} aria-hidden="true" />
          </span>
          <div>
            <h4 className="scheduled-meeting-title" style={{ color: 'var(--danger)', marginBottom: '4px' }}>
              Could Not Schedule Meeting
            </h4>
            <p style={{ margin: 0, fontSize: '13px', color: '#991b1b' }}>
              {result.message || 'An unexpected error occurred while scheduling.'}
            </p>
          </div>
        </div>
      </div>
    )
  }

  return null
}
