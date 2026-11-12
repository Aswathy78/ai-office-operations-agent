import { useEffect, useState, type FormEvent, type ReactNode } from 'react'
import { ArrowRight, DoorOpen, Mail, UserRound } from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  getMeeting,
  guestLobbyWebSocketUrl,
  MeetingApiError,
  requestGuestJoin,
} from '../api/meetings'
import type {
  GuestLobbyState,
  GuestLobbyWebSocketMessage,
  GuestRequest,
  MeetingDetails,
} from '../types/meeting'

const storedRequestKey = (meetingId: string) => `meeting-guest-request:${meetingId}`

type GuestMeetingStatus = 'loading' | 'valid' | 'invalid' | 'expired' | 'unavailable'

function parseStoredRequest(value: string | null, meetingId: string): GuestRequest | null {
  if (!value) return null
  try {
    const parsed: unknown = JSON.parse(value)
    if (
      typeof parsed === 'object' && parsed !== null &&
      'id' in parsed && typeof parsed.id === 'string' &&
      'meeting_id' in parsed && parsed.meeting_id === meetingId &&
      'guest_name' in parsed && typeof parsed.guest_name === 'string' &&
      'lobby_token' in parsed && typeof parsed.lobby_token === 'string'
    ) {
      return parsed as GuestRequest
    }
  } catch {
    return null
  }
  return null
}

function parseSocketMessage(data: string): GuestLobbyWebSocketMessage | null {
  try {
    const parsed: unknown = JSON.parse(data)
    if (typeof parsed === 'object' && parsed !== null && 'type' in parsed && typeof parsed.type === 'string') {
      return parsed as GuestLobbyWebSocketMessage
    }
  } catch {
    return null
  }
  return null
}

export function GuestJoinPage() {
  const { meetingId = '' } = useParams()
  const navigate = useNavigate()
  const [meeting, setMeeting] = useState<MeetingDetails | null>(null)
  const [meetingStatus, setMeetingStatus] = useState<GuestMeetingStatus>('loading')
  const [meetingError, setMeetingError] = useState('')
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [formError, setFormError] = useState('')
  const [state, setState] = useState<GuestLobbyState>({ status: 'loading' })

  useEffect(() => {
    let active = true
    setMeetingStatus('loading')
    setMeetingError('')
    setState({ status: 'loading' })

    getMeeting(meetingId)
      .then((details) => {
        if (!active) return
        setMeeting(details)
        setMeetingStatus('valid')
        const storedRequest = parseStoredRequest(
          sessionStorage.getItem(storedRequestKey(meetingId)),
          meetingId,
        )
        if (storedRequest) {
          setName(storedRequest.guest_name)
          setEmail(storedRequest.guest_email ?? '')
          setState({ status: 'waiting', request: storedRequest, connection: 'connecting' })
        } else {
          setState({ status: 'form' })
        }
      })
      .catch((error: unknown) => {
        if (!active) return
        const message = error instanceof Error ? error.message : 'Meeting details could not be loaded.'
        setMeetingError(message)
        setMeetingStatus(
          error instanceof MeetingApiError && error.status === 410
            ? 'expired'
            : error instanceof MeetingApiError && error.status === 404
              ? 'invalid'
              : 'unavailable',
        )
      })
    return () => { active = false }
  }, [meetingId])

  useEffect(() => {
    if (state.status !== 'waiting') return

    let active = true
    let retryTimer: ReturnType<typeof setTimeout> | undefined
    let retryDelay = 700
    let retryCount = 0
    const MAX_RETRIES = 5
    let socket: WebSocket | undefined

    const connect = () => {
      if (!active) return
      setState((current) => current.status === 'waiting'
        ? { ...current, connection: 'connecting' }
        : current)
      socket = new WebSocket(
        guestLobbyWebSocketUrl(meetingId, state.request.id),
        [`guest_lobby.${state.request.lobby_token}`],
      )
      socket.onopen = () => {
        retryDelay = 700
        retryCount = 0
        setState((current) => current.status === 'waiting'
          ? { ...current, connection: 'connected', socketError: undefined }
          : current)
      }
      socket.onmessage = (event: MessageEvent<string>) => {
        const message = parseSocketMessage(event.data)
        if (!message) return
        if (message.type === 'guest_admitted' || message.type === 'guest_rejected') {
          const finalRequest: GuestRequest = {
            ...state.request,
            status: message.type === 'guest_admitted' ? 'admitted' : 'rejected',
            decided_at: message.decided_at ?? state.request.decided_at,
          }
          sessionStorage.setItem(storedRequestKey(meetingId), JSON.stringify(finalRequest))
          const nextState: GuestLobbyState = message.type === 'guest_admitted'
            ? { status: 'admitted', request: finalRequest }
            : { status: 'rejected', request: finalRequest }
          setState(nextState)
          if (message.type === 'guest_admitted') {
            navigate(`/meetings/${encodeURIComponent(meetingId)}/guest/room?requestId=${encodeURIComponent(finalRequest.id)}`, { replace: true })
          }
          socket?.close()
        } else if (message.type === 'error') {
          setState((current) => current.status === 'waiting'
            ? { ...current, socketError: 'The lobby connection returned an error.' }
            : current)
        }
      }
      socket.onerror = () => {
        setState((current) => current.status === 'waiting'
          ? { ...current, connection: 'disconnected', socketError: 'Connection interrupted. Reconnecting…' }
          : current)
      }
      socket.onclose = (event: CloseEvent) => {
        if (!active) return
        if (event.code === 1008) {
          setState({ status: 'error', message: 'This guest request is no longer valid for this meeting.' })
          return
        }
        retryCount += 1
        if (retryCount > MAX_RETRIES) {
          setState({
            status: 'error',
            message: 'Unable to establish a connection to the meeting lobby. Please check your link or re-enter the lobby.',
          })
          return
        }
        setState((current) => current.status === 'waiting'
          ? { ...current, connection: 'disconnected', socketError: 'Connection interrupted. Reconnecting…' }
          : current)
        retryTimer = setTimeout(connect, retryDelay)
        retryDelay = Math.min(retryDelay * 2, 8000)
      }
    }

    connect()
    return () => {
      active = false
      if (retryTimer) clearTimeout(retryTimer)
      socket?.close()
    }
  }, [meetingId, navigate, state.status === 'waiting' ? state.request.id : null])

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFormError('')
    if (meetingStatus !== 'valid') return
    const cleanName = name.trim()
    const cleanEmail = email.trim()
    if (!cleanName) {
      setFormError('Enter your display name to continue.')
      return
    }
    if (cleanEmail && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(cleanEmail)) {
      setFormError('Enter a valid email address, or leave the field blank.')
      return
    }

    setState({ status: 'submitting' })
    try {
      const request = await requestGuestJoin(meetingId, {
        guest_name: cleanName,
        ...(cleanEmail ? { guest_email: cleanEmail } : {}),
      })
      if (!request.lobby_token) {
        setState({
          status: 'form',
          apiError: 'A pending lobby request already exists. Reopen the original guest lobby session to reconnect.',
        })
        return
      }
      sessionStorage.setItem(storedRequestKey(meetingId), JSON.stringify(request))
      setState({ status: 'waiting', request, connection: 'connecting' })
    } catch (error) {
      setState({
        status: 'form',
        apiError: error instanceof Error ? error.message : 'Unable to reach the meeting server.',
      })
    }
  }

  const title = meeting?.title ?? 'Join a meeting'

  if (meetingStatus === 'loading' || state.status === 'loading') {
    return <GuestFrame><p className="guest-loading" role="status">Checking meeting…</p></GuestFrame>
  }

  if (meetingStatus !== 'valid') {
    const heading = meetingStatus === 'expired'
      ? 'This meeting has expired'
      : meetingStatus === 'invalid'
        ? 'Meeting not found'
        : 'Unable to verify this meeting'
    const explanation = meetingStatus === 'expired'
      ? 'This meeting is no longer available.'
      : meetingStatus === 'invalid'
        ? 'The meeting link may be invalid or the meeting may no longer be available.'
        : 'Meeting details could not be reached. Check your connection and try the link again.'
    return (
      <GuestFrame>
        <div className="guest-state-icon rejected-icon" aria-hidden="true">!</div>
        <p className="eyebrow">Meeting unavailable</p>
        <h1>{heading}</h1>
        <p className="guest-description">{explanation}</p>
        {meetingError && <small>{meetingError}</small>}
        <Link className="guest-back-link" to="/home">Back to Home</Link>
      </GuestFrame>
    )
  }

  if (state.status === 'waiting') {
    return (
      <GuestFrame>
        <div className="guest-state-icon waiting-icon"><DoorOpen size={24} aria-hidden="true" /></div>
        <p className="eyebrow">Guest lobby</p>
        <h1>Waiting for the organizer to admit you</h1>
        <p className="guest-description">Your request is with the organizer. This page will update as soon as they decide.</p>
        <div className="guest-meeting-summary">
          <strong>{title}</strong>
          <span>{state.request.guest_name}{state.request.guest_email ? ` · ${state.request.guest_email}` : ''}</span>
        </div>
        <p className={`connection-note ${state.connection === 'connected' ? 'connected' : ''}`} role="status">
          <span className="connection-dot" />
          {state.connection === 'connected' ? 'Connected to lobby' : state.socketError ?? 'Connecting to lobby…'}
        </p>
        <button className="text-button" type="button" onClick={() => navigate(0)}>Reconnect now</button>
      </GuestFrame>
    )
  }

  if (state.status === 'rejected') {
    return (
      <GuestFrame>
        <div className="guest-state-icon rejected-icon" aria-hidden="true">×</div>
        <p className="eyebrow">Guest lobby</p>
        <h1>Access was not approved</h1>
        <p className="guest-description">The organizer declined your request to join {title}.</p>
      </GuestFrame>
    )
  }

  if (state.status === 'admitted') {
    return <GuestFrame><p className="guest-loading" role="status">You’re admitted. Opening the meeting…</p></GuestFrame>
  }

  if (state.status === 'error') {
    return (
      <GuestFrame>
        <div className="guest-state-icon rejected-icon" aria-hidden="true">!</div>
        <p className="eyebrow">Guest lobby</p>
        <h1>Unable to continue</h1>
        <p className="guest-description" role="alert">{state.message}</p>
      </GuestFrame>
    )
  }

  return (
    <GuestFrame>
      <div className="guest-state-icon"><DoorOpen size={24} aria-hidden="true" /></div>
      <p className="eyebrow">External guest</p>
      <h1>{title}</h1>
      <p className="guest-description">Enter your details to request access to this meeting.</p>
      <form className="guest-form" onSubmit={handleSubmit} noValidate>
        <label htmlFor="guest-name">Display name</label>
        <div className="guest-input-wrap">
          <UserRound size={17} aria-hidden="true" />
          <input id="guest-name" autoComplete="name" value={name} onChange={(event) => setName(event.target.value)} maxLength={255} required />
        </div>
        <label htmlFor="guest-email">Email address <span>(optional)</span></label>
        <div className="guest-input-wrap">
          <Mail size={17} aria-hidden="true" />
          <input id="guest-email" type="email" autoComplete="email" value={email} onChange={(event) => setEmail(event.target.value)} maxLength={255} />
        </div>
        {(formError || (state.status === 'form' && state.apiError)) && (
          <p className="guest-error" role="alert">{formError || (state.status === 'form' ? state.apiError : '')}</p>
        )}
        <button className="button primary guest-submit" type="submit" disabled={state.status === 'submitting'}>
          {state.status === 'submitting' ? 'Sending request…' : 'Request to Join'}
          {state.status !== 'submitting' && <ArrowRight size={17} aria-hidden="true" />}
        </button>
      </form>
    </GuestFrame>
  )
}

function GuestFrame({ children }: { children: ReactNode }) {
  return (
    <main className="guest-page">
      <div className="guest-brand"><span className="brand-mark">AO</span><span><strong>AI Office</strong><small>Operations Agent</small></span></div>
      <section className="guest-panel" aria-live="polite">{children}</section>
      <p className="guest-footer">Meeting access is managed by the organizer.</p>
    </main>
  )
}