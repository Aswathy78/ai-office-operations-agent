import { Clock3, Users, Video } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { Meeting } from '../../types/meeting'

export function MeetingCard({ meeting }: { meeting: Meeting }) {
  return (
    <article className="meeting-card">
      <div className="meeting-icon" aria-hidden="true"><Video size={21} /></div>
      <div className="meeting-info">
        <div className="meeting-heading">
          <h3>{meeting.title}</h3>
          <span className={`status-pill ${meeting.status === 'Starting soon' ? 'warning' : ''}`}>{meeting.status}</span>
        </div>
        <div className="meeting-meta">
          <span><Clock3 size={15} aria-hidden="true" /> {meeting.startTime} – {meeting.endTime}</span>
          <span><Users size={15} aria-hidden="true" /> {meeting.participants} participants</span>
        </div>
      </div>
      <Link className="button secondary" to={meeting.joinUrl} aria-label={`Join ${meeting.title}`}>
        Join
      </Link>
    </article>
  )
}
