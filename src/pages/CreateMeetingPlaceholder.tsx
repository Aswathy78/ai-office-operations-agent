import { CalendarPlus } from 'lucide-react'

export function CreateMeetingPlaceholder() {
  return (
    <section className="state-card" aria-labelledby="create-meeting-title">
      <div className="state-icon" aria-hidden="true"><CalendarPlus size={28} /></div>
      <p className="eyebrow">Story 1.2 integration point</p>
      <h2 id="create-meeting-title">Create Meeting</h2>
      <p>The shared UI exposes this route. The scheduled/instant meeting form and backend integration can be implemented here by the meeting owner.</p>
    </section>
  )
}
