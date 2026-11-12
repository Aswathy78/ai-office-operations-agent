import { useCallback, useEffect, useRef, useState } from 'react'
import { meetingWebSocketUrl } from '../api/meetings'
import { getAccessToken } from '../api/client'
import type {
  MeetingClientMessage,
  MeetingConnectionState,
  MeetingMediaState,
  MeetingWebSocketMessage,
  ParticipantInfo,
  ParticipantPeerState,
} from '../types/meeting'

interface UseMeetingRoomOptions {
  meetingId: string
  enabled?: boolean
  onMessage?: (message: MeetingWebSocketMessage) => void
  /** The authenticated user's display name, used for roster announcements */
  displayName?: string
  /** The authenticated user's email */
  email?: string | null
  /** The authenticated user's ID – used to determine if this client is the organizer */
  userId?: string | null
}

interface MeetingRoomController {
  connectionState: MeetingConnectionState
  localStream: MediaStream | null
  mediaState: MeetingMediaState
  mediaError: string | null
  isMicrophoneEnabled: boolean | null
  isCameraEnabled: boolean | null
  localParticipantId: string | null
  participants: string[]
  participantInfos: Map<string, ParticipantInfo>
  peerStates: Map<string, ParticipantPeerState>
  remoteStreams: Map<string, MediaStream>
  lastMessage: MeetingWebSocketMessage | null
  lastError: string | null
  organizerId: string | null
  isOrganizer: boolean
  isMutedByOrganizer: boolean
  isAskedToUnmute: boolean
  meetingEnded: boolean
  meetingEndedReason: string | null
  connect: () => void
  disconnect: () => void
  startLocalMedia: () => Promise<MediaStream | null>
  stopLocalMedia: () => void
  toggleMicrophone: () => boolean | null
  toggleCamera: () => boolean | null
  sendSignal: (message: MeetingClientMessage) => boolean
  muteParticipant: (participantId: string) => void
  askToUnmuteParticipant: (participantId: string) => void
  clearAskedToUnmute: () => void
}

interface PeerNegotiationState {
  polite: boolean
  makingOffer: boolean
  ignoreOffer: boolean
  isSettingRemoteAnswerPending: boolean
  negotiationInProgress: boolean
  revision: number
}

export const DEVELOPMENT_PEER_CONNECTION_CONFIGURATION: RTCConfiguration = {
  iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
}

function readSessionDescription(
  payload: Record<string, unknown>,
  expectedType: 'offer' | 'answer',
): RTCSessionDescriptionInit | null {
  if (payload.type !== expectedType || typeof payload.sdp !== 'string') return null
  return { type: expectedType, sdp: payload.sdp }
}

function readIceCandidate(payload: Record<string, unknown>): RTCIceCandidateInit | null {
  if (typeof payload.candidate !== 'string') return null
  const candidate: RTCIceCandidateInit = { candidate: payload.candidate }
  if (typeof payload.sdpMid === 'string' || payload.sdpMid === null) {
    candidate.sdpMid = payload.sdpMid
  }
  if (typeof payload.sdpMLineIndex === 'number' || payload.sdpMLineIndex === null) {
    candidate.sdpMLineIndex = payload.sdpMLineIndex
  }
  if (typeof payload.usernameFragment === 'string' || payload.usernameFragment === null) {
    candidate.usernameFragment = payload.usernameFragment
  }
  return candidate
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function getTrackEnabled(stream: MediaStream | null, kind: 'audio' | 'video'): boolean | null {
  const tracks = stream?.getTracks().filter((track) => track.kind === kind && track.readyState === 'live') ?? []
  return tracks.length ? tracks.every((track) => track.enabled) : null
}

function getMediaError(error: unknown): { state: MeetingMediaState; message: string } {
  const name = error instanceof Error
    ? error.name
    : isRecord(error) && typeof error.name === 'string'
      ? error.name
      : ''

  if (name === 'NotAllowedError') {
    return {
      state: 'permission_denied',
      message: 'Camera and microphone permission was denied.',
    }
  }
  if (name === 'NotFoundError') {
    return {
      state: 'unavailable',
      message: 'No camera or microphone was found.',
    }
  }
  if (name === 'NotReadableError') {
    return {
      state: 'device_error',
      message: 'Your camera or microphone is currently unavailable.',
    }
  }
  if (name === 'OverconstrainedError') {
    return {
      state: 'device_error',
      message: 'Your camera or microphone could not satisfy the requested settings.',
    }
  }
  if (name === 'SecurityError') {
    return {
      state: 'unavailable',
      message: 'Camera and microphone access requires a secure page or localhost.',
    }
  }
  return {
    state: 'device_error',
    message: 'Unable to access your camera and microphone. Check your device settings and retry.',
  }
}

function parseMeetingMessage(rawMessage: string): MeetingWebSocketMessage | null {
  let parsed: unknown
  try {
    parsed = JSON.parse(rawMessage)
  } catch {
    return null
  }
  if (!isRecord(parsed) || typeof parsed.type !== 'string') return null

  const senderId = typeof parsed.sender_id === 'string' ? parsed.sender_id : ''
  const payload = parsed.payload
  if (!isRecord(payload)) return null

  if (parsed.type === 'welcome') {
    if (
      typeof payload.connection_id !== 'string' ||
      typeof payload.meeting_id !== 'string' ||
      !Array.isArray(payload.participants) ||
      !payload.participants.every((participant) => typeof participant === 'string')
    ) return null
    return {
      type: 'welcome',
      sender_id: senderId,
      ...(typeof parsed.target_id === 'string' ? { target_id: parsed.target_id } : {}),
      payload: {
        connection_id: payload.connection_id,
        meeting_id: payload.meeting_id,
        participants: payload.participants,
        organizer_id: typeof payload.organizer_id === 'string' ? payload.organizer_id : undefined,
      },
    }
  }

  if (parsed.type === 'participant_joined' || parsed.type === 'participant_left') {
    if (typeof payload.connection_id !== 'string') return null
    return {
      type: parsed.type,
      sender_id: senderId,
      payload: { connection_id: payload.connection_id },
    }
  }

  if (parsed.type === 'join_ack') {
    if (typeof payload.connection_id !== 'string' || typeof payload.meeting_id !== 'string') return null
    return {
      type: 'join_ack',
      sender_id: senderId,
      ...(typeof parsed.target_id === 'string' ? { target_id: parsed.target_id } : {}),
      payload: { connection_id: payload.connection_id, meeting_id: payload.meeting_id },
    }
  }

  if (parsed.type === 'offer' || parsed.type === 'answer' || parsed.type === 'ice_candidate') {
    if (typeof parsed.target_id !== 'string') return null
    return {
      type: parsed.type,
      sender_id: senderId,
      target_id: parsed.target_id,
      payload,
    }
  }

  if (parsed.type === 'guest_arrived' || parsed.type === 'guest_request_notify') {
    return { type: parsed.type, sender_id: senderId, payload }
  }

  if (parsed.type === 'mute_participant') {
    if (typeof parsed.target_id !== 'string') return null
    return {
      type: 'mute_participant',
      sender_id: senderId,
      target_id: parsed.target_id,
      payload: {
        participant_id: typeof payload.participant_id === 'string' ? payload.participant_id : '',
        by_organizer: payload.by_organizer === true,
        meeting_id: typeof payload.meeting_id === 'string' ? payload.meeting_id : '',
      },
    }
  }

  if (parsed.type === 'ask_unmute') {
    if (typeof parsed.target_id !== 'string') return null
    return {
      type: 'ask_unmute',
      sender_id: senderId,
      target_id: parsed.target_id,
      payload: {
        participant_id: typeof payload.participant_id === 'string' ? payload.participant_id : '',
        meeting_id: typeof payload.meeting_id === 'string' ? payload.meeting_id : '',
      },
    }
  }

  if (parsed.type === 'meeting_ended') {
    return {
      type: 'meeting_ended',
      sender_id: senderId,
      payload: {
        meeting_id: typeof payload.meeting_id === 'string' ? payload.meeting_id : '',
        reason: typeof payload.reason === 'string' ? payload.reason : 'Meeting has ended.',
      },
    }
  }

  if (parsed.type === 'participant_state') {
    return {
      type: 'participant_state',
      sender_id: senderId,
      payload: {
        connection_id: typeof payload.connection_id === 'string' ? payload.connection_id : senderId,
        display_name: typeof payload.display_name === 'string' ? payload.display_name : undefined,
        is_microphone_enabled: typeof payload.is_microphone_enabled === 'boolean' ? payload.is_microphone_enabled : null,
        is_camera_enabled: typeof payload.is_camera_enabled === 'boolean' ? payload.is_camera_enabled : null,
        is_organizer: payload.is_organizer === true,
      },
    }
  }

  if (parsed.type === 'error' && typeof payload.detail === 'string') {
    return {
      type: 'error',
      sender_id: senderId,
      ...(typeof parsed.target_id === 'string' ? { target_id: parsed.target_id } : {}),
      payload: { detail: payload.detail },
    }
  }

  return null
}

export function useMeetingRoom({
  meetingId,
  enabled = true,
  onMessage,
  displayName,
  email,
  userId,
}: UseMeetingRoomOptions): MeetingRoomController {
  const [connectionState, setConnectionState] = useState<MeetingConnectionState>('idle')
  const [localStream, setLocalStream] = useState<MediaStream | null>(null)
  const [mediaState, setMediaState] = useState<MeetingMediaState>('idle')
  const [mediaError, setMediaError] = useState<string | null>(null)
  const [isMicrophoneEnabled, setIsMicrophoneEnabled] = useState<boolean | null>(null)
  const [isCameraEnabled, setIsCameraEnabled] = useState<boolean | null>(null)
  const [localParticipantId, setLocalParticipantId] = useState<string | null>(null)
  const [participants, setParticipants] = useState<string[]>([])
  const [participantInfos, setParticipantInfos] = useState<Map<string, ParticipantInfo>>(() => new Map())
  const [peerStates, setPeerStates] = useState<Map<string, ParticipantPeerState>>(() => new Map())
  const [remoteStreams, setRemoteStreams] = useState<Map<string, MediaStream>>(() => new Map())
  const [lastMessage, setLastMessage] = useState<MeetingWebSocketMessage | null>(null)
  const [lastError, setLastError] = useState<string | null>(null)
  const [organizerId, setOrganizerId] = useState<string | null>(null)
  const [isMutedByOrganizer, setIsMutedByOrganizer] = useState(false)
  const [isAskedToUnmute, setIsAskedToUnmute] = useState(false)
  const [meetingEnded, setMeetingEnded] = useState(false)
  const [meetingEndedReason, setMeetingEndedReason] = useState<string | null>(null)
  const socketRef = useRef<WebSocket | null>(null)
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const reconnectDelayRef = useRef(700)
  const shouldConnectRef = useRef(false)
  const generationRef = useRef(0)
  const onMessageRef = useRef(onMessage)
  const meetingIdRef = useRef(meetingId)
  const localStreamRef = useRef<MediaStream | null>(null)
  const mediaRequestIdRef = useRef(0)
  const mediaRequestInProgressRef = useRef<number | null>(null)
  const localParticipantIdRef = useRef<string | null>(null)
  const peerConnectionsRef = useRef<Map<string, RTCPeerConnection>>(new Map())
  const peerStatesRef = useRef<Map<string, ParticipantPeerState>>(new Map())
  const remoteStreamsRef = useRef<Map<string, MediaStream>>(new Map())
  const negotiationStatesRef = useRef<Map<string, PeerNegotiationState>>(new Map())
  const pendingIceCandidatesRef = useRef<Map<string, RTCIceCandidateInit[]>>(new Map())
  const participantInfosRef = useRef<Map<string, ParticipantInfo>>(new Map())
  const organizerIdRef = useRef<string | null>(null)
  const displayNameRef = useRef(displayName)
  const emailRef = useRef(email)
  const userIdRef = useRef(userId)
  meetingIdRef.current = meetingId
  displayNameRef.current = displayName
  emailRef.current = email
  userIdRef.current = userId

  useEffect(() => {
    onMessageRef.current = onMessage
  }, [onMessage])

  const publishMessage = useCallback((message: MeetingWebSocketMessage) => {
    setLastMessage(message)
    onMessageRef.current?.(message)
  }, [])

  const sendSignal = useCallback((message: MeetingClientMessage): boolean => {
    const socket = socketRef.current
    if (!socket || socket.readyState !== WebSocket.OPEN) return false
    try {
      socket.send(JSON.stringify(message))
      return true
    } catch (error) {
      setLastError(error instanceof Error ? error.message : 'Unable to send meeting signaling message.')
      return false
    }
  }, [])

  const broadcastLocalState = useCallback((
    overrides?: Partial<{ isMicEnabled: boolean | null; isCamEnabled: boolean | null }>
  ) => {
    const localId = localParticipantIdRef.current
    if (!localId) return
    const orgId = organizerIdRef.current
    sendSignal({
      type: 'participant_state',
      payload: {
        connection_id: localId,
        display_name: displayNameRef.current ?? 'Participant',
        is_microphone_enabled: overrides?.isMicEnabled !== undefined
          ? overrides.isMicEnabled
          : getTrackEnabled(localStreamRef.current, 'audio'),
        is_camera_enabled: overrides?.isCamEnabled !== undefined
          ? overrides.isCamEnabled
          : getTrackEnabled(localStreamRef.current, 'video'),
        is_organizer: orgId != null && userIdRef.current === orgId,
      },
    })
  }, [sendSignal])

  const addLocalTracksToPeer = useCallback((
    peerConnection: RTCPeerConnection,
    stream: MediaStream,
  ) => {
    stream.getTracks().forEach((track) => {
      const alreadyAdded = peerConnection.getSenders().some(
        (sender) => sender.track?.id === track.id,
      )
      if (!alreadyAdded) {
        try {
          peerConnection.addTrack(track, stream)
        } catch (error) {
          setLastError(error instanceof Error
            ? `Unable to attach a local track: ${error.message}`
            : 'Unable to attach a local track to a peer connection.')
        }
      }
    })
  }, [])

  const flushIceCandidates = useCallback(async (
    participantId: string,
    peerConnection: RTCPeerConnection,
  ) => {
    if (!peerConnection.remoteDescription) return
    const pendingCandidates = pendingIceCandidatesRef.current.get(participantId) ?? []
    pendingIceCandidatesRef.current.delete(participantId)

    for (const candidate of pendingCandidates) {
      if (peerConnectionsRef.current.get(participantId) !== peerConnection) return
      try {
        await peerConnection.addIceCandidate(candidate)
      } catch (error) {
        if (!negotiationStatesRef.current.get(participantId)?.ignoreOffer) {
          setLastError(error instanceof Error
            ? `Unable to apply an ICE candidate from ${participantId}: ${error.message}`
            : `Unable to apply an ICE candidate from ${participantId}.`)
        }
      }
    }
  }, [])

  const negotiatePeer = useCallback(async (
    participantId: string,
    peerConnection: RTCPeerConnection,
  ) => {
    const negotiation = negotiationStatesRef.current.get(participantId)
    if (
      !negotiation ||
      peerConnectionsRef.current.get(participantId) !== peerConnection ||
      negotiation.negotiationInProgress ||
      peerConnection.signalingState !== 'stable'
    ) return

    negotiation.negotiationInProgress = true
    negotiation.makingOffer = true
    const revision = negotiation.revision
    try {
      const offer = await peerConnection.createOffer()
      if (
        peerConnectionsRef.current.get(participantId) !== peerConnection ||
        negotiation.revision !== revision ||
        peerConnection.signalingState !== 'stable'
      ) return

      await peerConnection.setLocalDescription(offer)
      if (peerConnectionsRef.current.get(participantId) !== peerConnection) return
      const description = peerConnection.localDescription
      if (!description?.sdp) throw new Error('The local offer did not contain an SDP description.')

      if (!sendSignal({
        type: 'offer',
        target_id: participantId,
        payload: { type: description.type, sdp: description.sdp },
      })) {
        setLastError(`Unable to send an offer to participant ${participantId}.`)
      }
    } catch (error) {
      if (peerConnectionsRef.current.get(participantId) === peerConnection) {
        setLastError(error instanceof Error
          ? `Offer negotiation with ${participantId} failed: ${error.message}`
          : `Offer negotiation with ${participantId} failed.`)
      }
    } finally {
      negotiation.makingOffer = false
      negotiation.negotiationInProgress = false
    }
  }, [sendSignal])

  const removePeerConnection = useCallback((
    participantId: string,
    terminalState?: ParticipantPeerState,
  ) => {
    const peerConnection = peerConnectionsRef.current.get(participantId)
    if (peerConnection) {
      peerConnectionsRef.current.delete(participantId)
      peerConnection.onconnectionstatechange = null
      peerConnection.oniceconnectionstatechange = null
      peerConnection.onsignalingstatechange = null
      peerConnection.ontrack = null
      peerConnection.close()
    }

    if (terminalState) {
      peerStatesRef.current.set(participantId, terminalState)
      setPeerStates(new Map(peerStatesRef.current))
    } else {
      const hadPeerState = peerStatesRef.current.delete(participantId)
      if (hadPeerState) setPeerStates(new Map(peerStatesRef.current))
    }

    const hadRemoteStream = remoteStreamsRef.current.delete(participantId)
    if (hadRemoteStream) setRemoteStreams(new Map(remoteStreamsRef.current))
    negotiationStatesRef.current.delete(participantId)
    pendingIceCandidatesRef.current.delete(participantId)

    const hadInfo = participantInfosRef.current.delete(participantId)
    if (hadInfo) setParticipantInfos(new Map(participantInfosRef.current))
  }, [])

  const removeAllPeerConnections = useCallback(() => {
    ;[...peerConnectionsRef.current.keys()].forEach((participantId) => {
      removePeerConnection(participantId)
    })
    peerConnectionsRef.current.clear()
    peerStatesRef.current.clear()
    remoteStreamsRef.current.clear()
    negotiationStatesRef.current.clear()
    pendingIceCandidatesRef.current.clear()
    participantInfosRef.current.clear()
    setPeerStates(new Map())
    setRemoteStreams(new Map())
    setParticipantInfos(new Map())
  }, [removePeerConnection])

  const createPeerConnection = useCallback((participantId: string): RTCPeerConnection | null => {
    const localId = localParticipantIdRef.current
    if (!participantId || !localId || participantId === localId) return null

    const existingPeerConnection = peerConnectionsRef.current.get(participantId)
    if (existingPeerConnection) return existingPeerConnection

    let peerConnection: RTCPeerConnection
    try {
      peerConnection = new RTCPeerConnection(DEVELOPMENT_PEER_CONNECTION_CONFIGURATION)
    } catch (error) {
      setLastError(error instanceof Error ? error.message : 'Unable to create a peer connection.')
      return null
    }

    peerConnectionsRef.current.set(participantId, peerConnection)
    negotiationStatesRef.current.set(participantId, {
      // The lexicographically larger signaling ID is polite; the smaller ID is impolite.
      polite: localId.localeCompare(participantId) > 0,
      makingOffer: false,
      ignoreOffer: false,
      isSettingRemoteAnswerPending: false,
      negotiationInProgress: false,
      revision: 0,
    })

    const updatePeerState = () => {
      if (peerConnectionsRef.current.get(participantId) !== peerConnection) return
      const state: ParticipantPeerState = {
        participantId,
        connectionState: peerConnection.connectionState,
        iceConnectionState: peerConnection.iceConnectionState,
        signalingState: peerConnection.signalingState,
      }
      peerStatesRef.current.set(participantId, state)
      setPeerStates(new Map(peerStatesRef.current))
    }

    peerConnection.onconnectionstatechange = () => {
      updatePeerState()
      if (peerConnection.connectionState === 'failed' || peerConnection.connectionState === 'closed') {
        const state = peerStatesRef.current.get(participantId)
        if (state) removePeerConnection(participantId, state)
      }
    }
    peerConnection.oniceconnectionstatechange = () => {
      updatePeerState()
      if (peerConnection.iceConnectionState === 'failed' || peerConnection.iceConnectionState === 'closed') {
        const state = peerStatesRef.current.get(participantId)
        if (state) {
          removePeerConnection(participantId, {
            ...state,
            connectionState: peerConnection.iceConnectionState === 'failed'
              ? 'failed'
              : peerConnection.connectionState,
          })
        }
      }
    }
    peerConnection.onsignalingstatechange = updatePeerState
    peerConnection.onnegotiationneeded = () => {
      void negotiatePeer(participantId, peerConnection)
    }
    peerConnection.onicecandidate = (event: RTCPeerConnectionIceEvent) => {
      if (!event.candidate || peerConnectionsRef.current.get(participantId) !== peerConnection) return
      const candidate = event.candidate
      const payload: Record<string, unknown> = {
        candidate: candidate.candidate,
        sdpMid: candidate.sdpMid,
        sdpMLineIndex: candidate.sdpMLineIndex,
        usernameFragment: candidate.usernameFragment,
      }
      if (!sendSignal({ type: 'ice_candidate', target_id: participantId, payload })) {
        setLastError(`Unable to send an ICE candidate to participant ${participantId}.`)
      }
    }
    peerConnection.ontrack = (event: RTCTrackEvent) => {
      if (peerConnectionsRef.current.get(participantId) !== peerConnection) return

      let stream = event.streams[0] ?? remoteStreamsRef.current.get(participantId)
      if (!stream) stream = new MediaStream()
      if (event.streams.length === 0 && !stream.getTracks().some((track) => track.id === event.track.id)) {
        stream.addTrack(event.track)
      }

      remoteStreamsRef.current.set(participantId, stream)
      setRemoteStreams(new Map(remoteStreamsRef.current))
    }

    peerStatesRef.current.set(participantId, {
      participantId,
      connectionState: peerConnection.connectionState,
      iceConnectionState: peerConnection.iceConnectionState,
      signalingState: peerConnection.signalingState,
    })
    setPeerStates(new Map(peerStatesRef.current))

    if (localStreamRef.current) addLocalTracksToPeer(peerConnection, localStreamRef.current)
    return peerConnection
  }, [addLocalTracksToPeer, negotiatePeer, removePeerConnection, sendSignal])

  const processPeerSignal = useCallback(async (
    message: Extract<MeetingWebSocketMessage, { type: 'offer' | 'answer' | 'ice_candidate' }>,
  ) => {
    const localId = localParticipantIdRef.current
    const participantId = message.sender_id
    if (!localId || !participantId || participantId === localId) return
    if (message.target_id !== localId) {
      setLastError(`Ignored ${message.type} not addressed to this participant.`)
      return
    }

    const peerConnection = createPeerConnection(participantId)
    const negotiation = negotiationStatesRef.current.get(participantId)
    if (!peerConnection || !negotiation) return

    if (message.type === 'ice_candidate') {
      const candidate = readIceCandidate(message.payload)
      if (!candidate) {
        setLastError(`Received an invalid ICE candidate from ${participantId}.`)
        return
      }
      if (!peerConnection.remoteDescription) {
        const queuedCandidates = pendingIceCandidatesRef.current.get(participantId) ?? []
        queuedCandidates.push(candidate)
        pendingIceCandidatesRef.current.set(participantId, queuedCandidates)
        return
      }
      try {
        await peerConnection.addIceCandidate(candidate)
      } catch (error) {
        if (!negotiation.ignoreOffer) {
          setLastError(error instanceof Error
            ? `Unable to apply an ICE candidate from ${participantId}: ${error.message}`
            : `Unable to apply an ICE candidate from ${participantId}.`)
        }
      }
      return
    }

    const description = readSessionDescription(message.payload, message.type)
    if (!description) {
      setLastError(`Received an invalid ${message.type} description from ${participantId}.`)
      return
    }

    if (message.type === 'offer') {
      const readyForOffer = !negotiation.makingOffer && (
        peerConnection.signalingState === 'stable' || negotiation.isSettingRemoteAnswerPending
      )
      const offerCollision = !readyForOffer
      negotiation.ignoreOffer = !negotiation.polite && offerCollision
      if (negotiation.ignoreOffer) return

      if (offerCollision) {
        negotiation.revision += 1
        if (peerConnection.signalingState !== 'stable') {
          try {
            await peerConnection.setLocalDescription({ type: 'rollback' })
          } catch (error) {
            setLastError(error instanceof Error
              ? `Offer rollback for ${participantId} failed; trying remote-description rollback: ${error.message}`
              : `Offer rollback for ${participantId} failed; trying remote-description rollback.`)
          }
        }
      }

      try {
        await peerConnection.setRemoteDescription(description)
        if (peerConnectionsRef.current.get(participantId) !== peerConnection) return
        await flushIceCandidates(participantId, peerConnection)
        const answer = await peerConnection.createAnswer()
        if (peerConnectionsRef.current.get(participantId) !== peerConnection) return
        await peerConnection.setLocalDescription(answer)
        const localDescription = peerConnection.localDescription
        if (!localDescription?.sdp) throw new Error('The local answer did not contain an SDP description.')

        if (!sendSignal({
          type: 'answer',
          target_id: participantId,
          payload: { type: localDescription.type, sdp: localDescription.sdp },
        })) {
          setLastError(`Unable to send an answer to participant ${participantId}.`)
        }
      } catch (error) {
        setLastError(error instanceof Error
          ? `Answer negotiation with ${participantId} failed: ${error.message}`
          : `Answer negotiation with ${participantId} failed.`)
      }
      return
    }

    negotiation.isSettingRemoteAnswerPending = true
    try {
      await peerConnection.setRemoteDescription(description)
      if (peerConnectionsRef.current.get(participantId) !== peerConnection) return
      await flushIceCandidates(participantId, peerConnection)
      negotiation.ignoreOffer = false
    } catch (error) {
      setLastError(error instanceof Error
        ? `Unable to apply the answer from ${participantId}: ${error.message}`
        : `Unable to apply the answer from ${participantId}.`)
    } finally {
      negotiation.isSettingRemoteAnswerPending = false
    }
  }, [createPeerConnection, flushIceCandidates, sendSignal])

  const connect = useCallback(() => {
    const activeMeetingId = meetingIdRef.current
    if (!activeMeetingId) {
      setConnectionState('error')
      setLastError('A meeting ID is required to connect.')
      return
    }

    shouldConnectRef.current = true
    const currentSocket = socketRef.current
    if (currentSocket && currentSocket.readyState !== WebSocket.CLOSED) {
      return
    }

    const generation = generationRef.current + 1
    generationRef.current = generation
    setConnectionState('connecting')
    setLastError(null)
    const accessToken = getAccessToken()
    if (!accessToken) {
      setConnectionState('error')
      setLastError('Sign in is required to join this meeting.')
      return
    }

    let socket: WebSocket
    try {
      socket = new WebSocket(meetingWebSocketUrl(activeMeetingId), [`access_token.${accessToken}`])
    } catch (error) {
      setConnectionState('error')
      setLastError(error instanceof Error ? error.message : 'Unable to create meeting connection.')
      return
    }
    socketRef.current = socket

    socket.onopen = () => {
      if (generation !== generationRef.current || !shouldConnectRef.current) return
      reconnectDelayRef.current = 700
      setConnectionState('connected')
      setLastError(null)
      socket.send(JSON.stringify({ type: 'join' } satisfies MeetingClientMessage))
    }

    socket.onmessage = (event: MessageEvent<string>) => {
      if (generation !== generationRef.current) return
      const message = parseMeetingMessage(event.data)
      if (!message) {
        setLastError('Received a malformed meeting signaling message.')
        return
      }

      if (message.type === 'offer' || message.type === 'answer' || message.type === 'ice_candidate') {
        void processPeerSignal(message)
      } else if (message.type === 'welcome') {
        localParticipantIdRef.current = message.payload.connection_id
        setLocalParticipantId(message.payload.connection_id)
        // Store the meeting's organizer_id from the server's welcome payload
        if (message.payload.organizer_id) {
          organizerIdRef.current = message.payload.organizer_id
          setOrganizerId(message.payload.organizer_id)
        }
        const remoteParticipantIds = [...new Set(message.payload.participants)]
          .filter((participantId) => participantId !== message.payload.connection_id)
        setParticipants(remoteParticipantIds)
        const remoteParticipantSet = new Set(remoteParticipantIds)
        ;[...peerConnectionsRef.current.keys()]
          .filter((participantId) => !remoteParticipantSet.has(participantId))
          .forEach((participantId) => removePeerConnection(participantId))
        remoteParticipantIds.forEach(createPeerConnection)
        // Broadcast own state after joining
        setTimeout(() => broadcastLocalState(), 100)
      } else if (message.type === 'participant_joined') {
        const participantId = message.payload.connection_id
        if (participantId !== localParticipantIdRef.current) createPeerConnection(participantId)
        setParticipants((current) => (
          current.includes(participantId) ? current : [...current, participantId]
        ))
        // Re-broadcast our state so the new participant knows who we are
        setTimeout(() => broadcastLocalState(), 200)
      } else if (message.type === 'participant_left') {
        removePeerConnection(message.payload.connection_id)
        setParticipants((current) => current.filter(
          (participantId) => participantId !== message.payload.connection_id,
        ))
      } else if (message.type === 'participant_state') {
        const info = message.payload
        const connId = info.connection_id || message.sender_id
        if (connId && connId !== localParticipantIdRef.current) {
          participantInfosRef.current.set(connId, {
            connectionId: connId,
            displayName: info.display_name ?? `Participant ${connId.slice(0, 6)}`,
            isOrganizer: info.is_organizer === true,
            isMicrophoneEnabled: info.is_microphone_enabled ?? null,
            isCameraEnabled: info.is_camera_enabled ?? null,
          })
          setParticipantInfos(new Map(participantInfosRef.current))
        }
      } else if (message.type === 'mute_participant') {
        // Server has verified this is addressed to us; mute our microphone
        const localId = localParticipantIdRef.current
        if (message.target_id === localId) {
          const stream = localStreamRef.current
          if (stream) {
            stream.getAudioTracks().forEach((track) => {
              track.enabled = false
            })
            setIsMicrophoneEnabled(false)
            broadcastLocalState({ isMicEnabled: false })
          }
          setIsMutedByOrganizer(true)
        }
      } else if (message.type === 'ask_unmute') {
        // Host politely asks us to unmute; show a prompt — do NOT auto-unmute
        const localId = localParticipantIdRef.current
        if (message.target_id === localId) {
          setIsAskedToUnmute(true)
        }
      } else if (message.type === 'meeting_ended') {
        // Host ended the meeting for everyone
        setMeetingEnded(true)
        setMeetingEndedReason(message.payload.reason)
        shouldConnectRef.current = false
      } else if (message.type === 'error') {
        setLastError(message.payload.detail)
      }

      publishMessage(message)
    }

    socket.onerror = () => {
      if (generation !== generationRef.current) return
      setConnectionState('error')
      setLastError('The meeting signaling connection encountered an error.')
    }

    socket.onclose = (event: CloseEvent) => {
      if (generation !== generationRef.current) return
      socketRef.current = null
      removeAllPeerConnections()
      localParticipantIdRef.current = null
      setLocalParticipantId(null)
      setParticipants([])
      if (event.code === 1008) {
        shouldConnectRef.current = false
        setConnectionState('error')
        setLastError(event.reason || 'Meeting access was rejected by the server.')
        return
      }
      if (!shouldConnectRef.current) {
        setConnectionState('idle')
        return
      }

      setConnectionState('disconnected')
      const delay = reconnectDelayRef.current
      reconnectDelayRef.current = Math.min(delay * 2, 8000)
      reconnectTimerRef.current = setTimeout(() => {
        reconnectTimerRef.current = null
        connect()
      }, delay)
    }
  }, [
    broadcastLocalState,
    createPeerConnection,
    processPeerSignal,
    publishMessage,
    removeAllPeerConnections,
    removePeerConnection,
  ])

  const stopLocalMedia = useCallback(() => {
    mediaRequestIdRef.current += 1
    const stream = localStreamRef.current
    if (stream) stream.getTracks().forEach((track) => track.stop())
    localStreamRef.current = null
    setLocalStream(null)
    setMediaError(null)
    setMediaState('stopped')
    setIsMicrophoneEnabled(null)
    setIsCameraEnabled(null)
  }, [])

  const toggleTrackKind = useCallback((kind: 'audio' | 'video'): boolean | null => {
    const stream = localStreamRef.current
    const tracks = stream?.getTracks().filter(
      (track) => track.kind === kind && track.readyState === 'live',
    ) ?? []
    if (!tracks.length) {
      if (kind === 'audio') setIsMicrophoneEnabled(null)
      else setIsCameraEnabled(null)
      return null
    }

    const enabled = !tracks[0].enabled
    tracks.forEach((track) => {
      track.enabled = enabled
    })
    if (kind === 'audio') {
      const newVal = getTrackEnabled(stream, 'audio')
      setIsMicrophoneEnabled(newVal)
      broadcastLocalState({ isMicEnabled: newVal })
      // Clear muted-by-organizer flag if participant unmutes themselves
      if (enabled) setIsMutedByOrganizer(false)
    } else {
      const newVal = getTrackEnabled(stream, 'video')
      setIsCameraEnabled(newVal)
      broadcastLocalState({ isCamEnabled: newVal })
    }
    return enabled
  }, [broadcastLocalState])

  const toggleMicrophone = useCallback(() => toggleTrackKind('audio'), [toggleTrackKind])
  const toggleCamera = useCallback(() => toggleTrackKind('video'), [toggleTrackKind])

  const muteParticipant = useCallback((participantId: string) => {
    sendSignal({
      type: 'mute_participant',
      payload: { participant_id: participantId },
    })
  }, [sendSignal])

  const askToUnmuteParticipant = useCallback((participantId: string) => {
    sendSignal({
      type: 'ask_unmute',
      payload: { participant_id: participantId },
    })
  }, [sendSignal])

  const clearAskedToUnmute = useCallback(() => {
    setIsAskedToUnmute(false)
  }, [])

  const disconnect = useCallback(() => {
    shouldConnectRef.current = false
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }

    const socket = socketRef.current
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'leave' } satisfies MeetingClientMessage))
    }
    if (socket && socket.readyState !== WebSocket.CLOSED) socket.close()

    stopLocalMedia()
    removeAllPeerConnections()
    localParticipantIdRef.current = null
    setConnectionState('idle')
    setLocalParticipantId(null)
    setParticipants([])
  }, [removeAllPeerConnections, stopLocalMedia])

  const startLocalMedia = useCallback(async (): Promise<MediaStream | null> => {
    if (localStreamRef.current) {
      setMediaState('ready')
      return localStreamRef.current
    }
    if (mediaRequestInProgressRef.current !== null) return null

    const getUserMedia = navigator.mediaDevices?.getUserMedia
    if (!getUserMedia) {
      setMediaState('unavailable')
      setMediaError('Camera and microphone access is unavailable in this browser or context.')
      return null
    }

    const requestId = mediaRequestIdRef.current + 1
    mediaRequestIdRef.current = requestId
    mediaRequestInProgressRef.current = requestId
    setMediaState('requesting')
    setMediaError(null)

    try {
      const stream = await getUserMedia.call(navigator.mediaDevices, {
        audio: {
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
        video: true,
      })
      if (requestId !== mediaRequestIdRef.current) {
        stream.getTracks().forEach((track) => track.stop())
        return null
      }

      localStreamRef.current = stream
      setLocalStream(stream)
      setMediaState('ready')
      setIsMicrophoneEnabled(getTrackEnabled(stream, 'audio'))
      setIsCameraEnabled(getTrackEnabled(stream, 'video'))

      // Attach tracks to existing peer connections
      peerConnectionsRef.current.forEach((pc) => addLocalTracksToPeer(pc, stream))

      // Announce our state to the room
      setTimeout(() => broadcastLocalState(), 50)

      return stream
    } catch (error) {
      if (requestId === mediaRequestIdRef.current) {
        const mediaFailure = getMediaError(error)
        setMediaState(mediaFailure.state)
        setMediaError(mediaFailure.message)
      }
      return null
    } finally {
      if (mediaRequestInProgressRef.current === requestId) {
        mediaRequestInProgressRef.current = null
      }
    }
  }, [addLocalTracksToPeer, broadcastLocalState])

  useEffect(() => {
    if (!localStream) {
      setIsMicrophoneEnabled(null)
      setIsCameraEnabled(null)
      return
    }

    const syncTrackState = () => {
      if (localStreamRef.current !== localStream) return
      setIsMicrophoneEnabled(getTrackEnabled(localStream, 'audio'))
      setIsCameraEnabled(getTrackEnabled(localStream, 'video'))
    }
    const tracks = localStream.getTracks()
    tracks.forEach((track) => track.addEventListener('ended', syncTrackState))
    syncTrackState()
    return () => {
      tracks.forEach((track) => track.removeEventListener('ended', syncTrackState))
    }
  }, [localStream])

  useEffect(() => {
    if (!enabled) return
    connect()
    return disconnect
  }, [meetingId, enabled, connect, disconnect])

  useEffect(() => {
    setLocalStream(null)
    setMediaState('idle')
    setMediaError(null)
    return () => {
      mediaRequestIdRef.current += 1
      const stream = localStreamRef.current
      if (stream) stream.getTracks().forEach((track) => track.stop())
      localStreamRef.current = null
      removeAllPeerConnections()
      localParticipantIdRef.current = null
      mediaRequestInProgressRef.current = null
    }
  }, [meetingId, removeAllPeerConnections])

  useEffect(() => {
    if (!localStream) return
    peerConnectionsRef.current.forEach((peerConnection) => {
      addLocalTracksToPeer(peerConnection, localStream)
    })
  }, [addLocalTracksToPeer, localStream])

  const isOrganizer = Boolean(organizerId && userId && organizerId === userId)

  return {
    connectionState,
    localStream,
    mediaState,
    mediaError,
    isMicrophoneEnabled,
    isCameraEnabled,
    localParticipantId,
    participants,
    participantInfos,
    peerStates,
    remoteStreams,
    lastMessage,
    lastError,
    organizerId,
    isOrganizer,
    isMutedByOrganizer,
    connect,
    disconnect,
    startLocalMedia,
    stopLocalMedia,
    toggleMicrophone,
    toggleCamera,
    sendSignal,
    muteParticipant,
    askToUnmuteParticipant,
    clearAskedToUnmute,
    isAskedToUnmute,
    meetingEnded,
    meetingEndedReason,
  }
}