/**
 * MeetingEntryPage — the smart landing for any shared meeting URL.
 *
 * /meetings/:meetingId  →  opened by anyone
 *
 * Routing logic:
 *   - Authenticated + organizer of this meeting → skip pre-join, auto-enter meeting room
 *   - Authenticated + known participant → show internal pre-join screen → enter meeting room
 *   - Unauthenticated / external → show guest pre-join screen (name/email → request to join)
 *
 * This preserves the existing GuestJoinPage guest lobby flow and
 * the existing MeetingRoomPage full-screen meeting experience.
 */
import { useEffect, useState } from 'react'
import { Navigate, useNavigate, useParams } from 'react-router-dom'
import { getMeeting, MeetingApiError } from '../api/meetings'
import { useAuth } from '../contexts/AuthContext'
import { GuestJoinPage } from './GuestJoinPage'
import type { MeetingDetails } from '../types/meeting'

type LoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; meeting: MeetingDetails }
  | { kind: 'invalid'; message: string }
  | { kind: 'expired'; message: string }
  | { kind: 'error'; message: string }

export function MeetingEntryPage() {
  const { meetingId = '' } = useParams()
  const { user, isAuthenticated } = useAuth()
  const navigate = useNavigate()
  const [loadState, setLoadState] = useState<LoadState>({ kind: 'loading' })

  if (meetingId === 'create') {
    return <Navigate to="/meetings/create" replace />
  }

  useEffect(() => {
    if (!meetingId || meetingId === 'create') return
    let active = true
    getMeeting(meetingId)
      .then((meeting) => {
        if (!active) return
        setLoadState({ kind: 'ready', meeting })
      })
      .catch((err: unknown) => {
        if (!active) return
        if (err instanceof MeetingApiError && err.status === 410) {
          setLoadState({ kind: 'expired', message: err.message })
        } else if (err instanceof MeetingApiError && err.status === 404) {
          setLoadState({ kind: 'invalid', message: err.message })
        } else {
          setLoadState({ kind: 'error', message: err instanceof Error ? err.message : 'Could not load meeting.' })
        }
      })
    return () => { active = false }
  }, [meetingId])

  if (loadState.kind === 'loading') {
    return (
      <main className="guest-page">
        <div className="guest-brand"><span className="brand-mark">AO</span><span><strong>AI Office</strong><small>Operations Agent</small></span></div>
        <section className="guest-panel"><p className="guest-loading" role="status">Checking meeting…</p></section>
      </main>
    )
  }

  if (loadState.kind === 'invalid' || loadState.kind === 'expired' || loadState.kind === 'error') {
    const heading = loadState.kind === 'expired' ? 'This meeting has expired' : loadState.kind === 'invalid' ? 'Meeting not found' : 'Unable to verify this meeting'
    const explanation = loadState.kind === 'expired'
      ? 'This meeting is no longer available.'
      : loadState.kind === 'invalid'
        ? 'The meeting link may be invalid or the meeting may no longer exist.'
        : 'Meeting details could not be reached. Check your connection and try again.'
    return (
      <main className="guest-page">
        <div className="guest-brand"><span className="brand-mark">AO</span><span><strong>AI Office</strong><small>Operations Agent</small></span></div>
        <section className="guest-panel" role="alert">
          <div className="guest-state-icon rejected-icon" aria-hidden="true">!</div>
          <p className="eyebrow">Meeting unavailable</p>
          <h1>{heading}</h1>
          <p className="guest-description">{explanation}</p>
        </section>
      </main>
    )
  }

  const meeting = loadState.meeting

  // Authenticated users enter the meeting room directly
  if (isAuthenticated && user) {
    // Navigate immediately to the authenticated meeting room
    // (This effect is triggered once the meeting is confirmed valid)
    navigate(`/app/meetings/${encodeURIComponent(meetingId)}`, { replace: true })
    return null
  }

  // Unauthenticated / external → show guest join flow (existing GuestJoinPage component)
  // The GuestJoinPage already handles the full guest lobby experience.
  // We render it here since the URL is now /meetings/:id instead of /meetings/:id/guest
  return <GuestJoinPage />
}
