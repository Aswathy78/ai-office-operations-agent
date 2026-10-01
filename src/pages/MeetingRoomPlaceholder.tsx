import { Video } from 'lucide-react'
import { useParams } from 'react-router-dom'

export function MeetingRoomPlaceholder() {
  const { meetingId } = useParams()
  return (
    <section className="state-card" aria-labelledby="meeting-room-title">
      <div className="state-icon" aria-hidden="true"><Video size={28} /></div>
      <p className="eyebrow">Meeting integration point</p>
      <h2 id="meeting-room-title">Meeting room</h2>
      <p>WebRTC, lobby, recording and in-meeting controls belong to the meeting implementation team.</p>
      <small>Meeting ID: {meetingId}</small>
    </section>
  )
}
