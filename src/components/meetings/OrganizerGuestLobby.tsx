import { useEffect, useState } from 'react'
import { Check, Clock3, Mail, RefreshCw, X } from 'lucide-react'
import { decideGuestRequest, getPendingGuestRequests } from '../../api/meetings'
import type {
  MeetingConnectionState,
  MeetingWebSocketMessage,
  GuestRequest,
} from '../../types/meeting'

function formatRequestedAt(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Request time unavailable'
  const diff = Date.now() - date.getTime()
  if (diff < 60_000) return 'just now'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} min ago`
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

interface OrganizerGuestLobbyProps {
  meetingId: string
  organizerId: string
  connectionState: MeetingConnectionState
  lastMessage: MeetingWebSocketMessage | null
}

export function OrganizerGuestLobby({
  meetingId,
  organizerId,
  connectionState,
  lastMessage,
}: OrganizerGuestLobbyProps) {
  const [requests, setRequests] = useState<GuestRequest[]>([])
  const [loading, setLoading] = useState(true)
  const [refreshError, setRefreshError] = useState('')
  const [actionError, setActionError] = useState('')
  const [busyRequestId, setBusyRequestId] = useState<string | null>(null)

  useEffect(() => {
    let active = true
    const refresh = async () => {
      try {
        const nextRequests = await getPendingGuestRequests(meetingId)
        if (active) {
          setRequests(nextRequests)
          setRefreshError('')
        }
      } catch (error) {
        if (active) setRefreshError(error instanceof Error ? error.message : 'Unable to load guest requests.')
      } finally {
        if (active) setLoading(false)
      }
    }

    void refresh()
    return () => {
      active = false
    }
  }, [meetingId])

  useEffect(() => {
    if (lastMessage?.type === 'guest_arrived' || lastMessage?.type === 'guest_request_notify') {
      void getPendingGuestRequests(meetingId)
        .then(setRequests)
        .then(() => setRefreshError(''))
        .catch((error: unknown) => {
          setRefreshError(error instanceof Error ? error.message : 'Unable to load guest requests.')
        })
    }
  }, [lastMessage, meetingId])

  async function handleDecision(requestId: string, decision: 'admit' | 'reject') {
    setBusyRequestId(requestId)
    setActionError('')
    try {
      await decideGuestRequest(meetingId, requestId, { decision }, organizerId)
      setRequests((current) => current.filter((request) => request.id !== requestId))
    } catch (error) {
      setActionError(error instanceof Error ? error.message : 'Unable to update this guest request.')
    } finally {
      setBusyRequestId(null)
    }
  }

  return (
    <section className="organizer-lobby" aria-labelledby="organizer-lobby-title">
      <div className="organizer-lobby-heading">
        <div>
          <p className="eyebrow">Meeting access</p>
          <h2 id="organizer-lobby-title">Requests to join</h2>
        </div>
        <div className={`lobby-connection ${connectionState}`} role="status">
          <span className="connection-dot" />
          {connectionState === 'connected'
            ? 'Live'
            : connectionState === 'connecting'
              ? 'Connecting'
              : connectionState === 'idle'
                ? 'Offline'
                  : connectionState === 'error'
                    ? 'Connection error'
                : 'Reconnecting'}
        </div>
      </div>

      {refreshError && <p className="guest-error" role="alert">{refreshError}</p>}
      {actionError && <p className="guest-error" role="alert">{actionError}</p>}
      {loading ? (
        <p className="organizer-lobby-empty" role="status">Loading guest requests…</p>
      ) : requests.length === 0 ? (
        <p className="organizer-lobby-empty">No guests are waiting.</p>
      ) : (
        <div className="guest-request-list">
          {requests.map((request) => (
            <article className="guest-request-row" key={request.id}>
              <div className="guest-request-info">
                <strong>{request.guest_name}</strong>
                <span><Mail size={14} aria-hidden="true" /> {request.guest_email || 'No email provided'}</span>
                <span><Clock3 size={14} aria-hidden="true" /> Requested {formatRequestedAt(request.requested_at)}</span>
              </div>
              <span className="guest-waiting-status">Waiting</span>
              <div className="guest-request-actions">
                <button
                  className="button admit-button"
                  type="button"
                  disabled={busyRequestId !== null}
                  onClick={() => void handleDecision(request.id, 'admit')}
                >
                  <Check size={16} aria-hidden="true" /> {busyRequestId === request.id ? 'Saving…' : 'Admit'}
                </button>
                <button
                  className="button reject-button"
                  type="button"
                  disabled={busyRequestId !== null}
                  onClick={() => void handleDecision(request.id, 'reject')}
                >
                  <X size={16} aria-hidden="true" /> Deny
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
      {(connectionState === 'disconnected' || connectionState === 'error') && (
        <p className="lobby-reconnect-note"><RefreshCw size={14} aria-hidden="true" /> Live updates are reconnecting; pending requests remain available above.</p>
      )}
    </section>
  )
}