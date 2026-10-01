import { CalendarDays } from 'lucide-react'
import type { Meeting } from '../../types/meeting'
import { MeetingCard } from './MeetingCard'

export function TodaysMeetings({ meetings }: { meetings: Meeting[] }) {
  return (
    <section aria-labelledby="todays-meetings-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Schedule</p>
          <h2 id="todays-meetings-title">Today's meetings</h2>
        </div>
        <span className="date-label"><CalendarDays size={16} aria-hidden="true" /> Today</span>
      </div>
      <div className="meeting-list">
        {meetings.length > 0 ? meetings.map((meeting) => <MeetingCard key={meeting.id} meeting={meeting} />) : (
          <div className="empty-card">
            <h3>No meetings today</h3>
            <p>Your schedule is clear. You can create a meeting when needed.</p>
          </div>
        )}
      </div>
    </section>
  )
}
