export type MeetingRecordingState = 'idle' | 'recording' | 'stopped' | 'error'

export interface MeetingRecordingSnapshot {
  state: MeetingRecordingState
  durationMs: number
  blob: Blob | null
  chunks: readonly Blob[]
  error: string | null
  tracks?: readonly RecordedParticipantTrack[]
}

import type { ParticipantInfo } from '../types/meeting'

export type MeetingMediaRecorderConstructor = typeof MediaRecorder

export interface RecordedParticipantTrack {
  trackId: string
  connectionId: string
  participantName: string
  attendanceId?: string
  blob: Blob
  track_id?: string
  connection_id?: string
  participant_name?: string
  attendance_id?: string
}

export interface MultiTrackSourceConfig {
  localParticipant?: {
    connectionId?: string | null
    participantName?: string
    attendanceId?: string
  }
  remoteStreams?: Map<string, MediaStream>
  participantInfos?: Map<string, ParticipantInfo> | Map<string, { displayName?: string }>
}

const AUDIO_VIDEO_MIME_TYPES = [
  'video/webm;codecs=vp8,opus',
  'video/webm;codecs=vp9,opus',
  'video/webm',
]

const AUDIO_ONLY_MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/ogg;codecs=opus',
  'audio/ogg',
  'audio/mp4',
]

let trackCounter = 0
export function generateTrackId(prefix: string = 'track'): string {
  trackCounter += 1
  const randomPart = Math.random().toString(36).slice(2, 8)
  return `${prefix}-${Date.now()}-${trackCounter}-${randomPart}`
}

export function createAudioOnlyMediaRecorder(
  stream: MediaStream,
  RecorderConstructor: MeetingMediaRecorderConstructor | undefined = globalThis.MediaRecorder,
): MediaRecorder {
  if (!RecorderConstructor) {
    throw new Error('MediaRecorder is not supported by this browser.')
  }

  const audioTracks = stream.getAudioTracks()
  if (!audioTracks.some((track) => track.readyState === 'live')) {
    throw new Error('A live audio track is required to record participant audio.')
  }

  const supportedType = AUDIO_ONLY_MIME_TYPES.find((mimeType) => {
    try {
      return RecorderConstructor.isTypeSupported(mimeType)
    } catch {
      return false
    }
  })

  if (supportedType) {
    try {
      return new RecorderConstructor(stream, { mimeType: supportedType })
    } catch {
      // Let the browser choose its default format if its support probe was optimistic.
    }
  }

  return new RecorderConstructor(stream)
}

export function createIsolatedAudioStream(track: MediaStreamTrack): MediaStream {
  if (typeof MediaStream !== 'undefined') {
    try {
      return new MediaStream([track])
    } catch {
      // Fallback if environment mock or platform differs
    }
  }
  return {
    getAudioTracks: () => [track],
    getVideoTracks: () => [],
    getTracks: () => [track],
  } as unknown as MediaStream
}

export function createAudioVideoMediaRecorder(
  stream: MediaStream,
  RecorderConstructor: MeetingMediaRecorderConstructor | undefined = globalThis.MediaRecorder,
): MediaRecorder {
  if (!RecorderConstructor) {
    throw new Error('MediaRecorder is not supported by this browser.')
  }

  const audioTracks = stream.getAudioTracks()
  if (!audioTracks.some((track) => track.readyState === 'live')) {
    throw new Error('A live microphone track is required to record audio.')
  }

  const supportedType = AUDIO_VIDEO_MIME_TYPES.find((mimeType) => {
    try {
      return RecorderConstructor.isTypeSupported(mimeType)
    } catch {
      return false
    }
  })

  if (supportedType) {
    try {
      return new RecorderConstructor(stream, { mimeType: supportedType })
    } catch {
      // Let the browser choose its default format if its support probe was optimistic.
    }
  }

  return new RecorderConstructor(stream)
}

const INITIAL_SNAPSHOT: MeetingRecordingSnapshot = {
  state: 'idle',
  durationMs: 0,
  blob: null,
  chunks: [],
  error: null,
  tracks: [],
}

interface ActiveTrackTarget {
  connectionId: string
  participantName: string
  attendanceId?: string
  track: MediaStreamTrack
}

interface TrackRecorderInstance {
  trackId: string
  connectionId: string
  participantName: string
  attendanceId?: string
  recorder: MediaRecorder
  chunks: Blob[]
  track: MediaStreamTrack
  onTrackEnded: () => void
}

export class ParticipantTrackRecordingSession {
  private readonly localStream: MediaStream | null
  private config: MultiTrackSourceConfig | null
  private readonly RecorderConstructor: MeetingMediaRecorderConstructor | undefined
  private instances: TrackRecorderInstance[] = []
  private active = false

  constructor(
    localStream: MediaStream | null,
    config: MultiTrackSourceConfig | null = null,
    RecorderConstructor: MeetingMediaRecorderConstructor | undefined = globalThis.MediaRecorder,
  ) {
    this.localStream = localStream
    this.config = config
    this.RecorderConstructor = RecorderConstructor
  }

  updateConfig(config: MultiTrackSourceConfig | null): void {
    this.config = config
  }

  get isRecording(): boolean {
    return this.active
  }

  start(): boolean {
    if (this.active) return true
    this.instances = []

    const targets: ActiveTrackTarget[] = []

    // 1. Snapshot local participant audio track
    if (this.localStream) {
      const liveAudioTracks = this.localStream.getAudioTracks().filter((t) => t.readyState === 'live')
      if (liveAudioTracks.length > 0) {
        targets.push({
          connectionId: this.config?.localParticipant?.connectionId || 'local',
          participantName: this.config?.localParticipant?.participantName || 'Organizer',
          attendanceId: this.config?.localParticipant?.attendanceId,
          track: liveAudioTracks[0],
        })
      }
    }

    // 2. Snapshot remote participant audio tracks
    if (this.config?.remoteStreams) {
      for (const [connectionId, remoteStream] of this.config.remoteStreams.entries()) {
        const liveAudioTracks = remoteStream?.getAudioTracks().filter((t) => t.readyState === 'live') ?? []
        if (liveAudioTracks.length > 0) {
          const participantInfo = this.config.participantInfos?.get(connectionId)
          targets.push({
            connectionId,
            participantName: participantInfo?.displayName || `Participant ${connectionId.slice(0, 6)}`,
            attendanceId: undefined,
            track: liveAudioTracks[0],
          })
        }
      }
    }

    for (const target of targets) {
      try {
        const isolatedStream = createIsolatedAudioStream(target.track)
        const recorder = createAudioOnlyMediaRecorder(isolatedStream, this.RecorderConstructor)
        const chunks: Blob[] = []
        const trackId = generateTrackId(`track-${target.connectionId}`)

        recorder.ondataavailable = (event: BlobEvent) => {
          if (event.data && event.data.size > 0) {
            chunks.push(event.data)
          }
        }

        const onTrackEnded = () => {
          if (recorder.state === 'recording') {
            try {
              recorder.stop()
            } catch {
              // Ignore stop errors if already stopped
            }
          }
        }

        target.track.addEventListener?.('ended', onTrackEnded)

        const instance: TrackRecorderInstance = {
          trackId,
          connectionId: target.connectionId,
          participantName: target.participantName,
          attendanceId: target.attendanceId,
          recorder,
          chunks,
          track: target.track,
          onTrackEnded,
        }

        recorder.start(1000)
        this.instances.push(instance)
      } catch {
        // Individual participant recorder start failure should not fail other participant recorders
      }
    }

    this.active = true
    return true
  }

  stop(): boolean {
    if (!this.active) return true
    this.active = false
    for (const instance of this.instances) {
      instance.track.removeEventListener?.('ended', instance.onTrackEnded)
      if (instance.recorder.state === 'recording') {
        try {
          instance.recorder.stop()
        } catch {
          // Ignore error on stop
        }
      }
    }
    return true
  }

  stopAndWait(): Promise<RecordedParticipantTrack[]> {
    if (!this.active && this.instances.length === 0) {
      return Promise.resolve([])
    }

    this.active = false
    const promises = this.instances.map((instance) => {
      instance.track.removeEventListener?.('ended', instance.onTrackEnded)
      if (instance.recorder.state !== 'recording') {
        const blob = instance.chunks.length
          ? new Blob(instance.chunks, instance.recorder.mimeType ? { type: instance.recorder.mimeType } : { type: 'audio/webm' })
          : new Blob([], { type: 'audio/webm' })
        return Promise.resolve(this.buildRecordedTrack(instance, blob))
      }

      return new Promise<RecordedParticipantTrack>((resolve) => {
        const cleanup = () => {
          instance.recorder.onstop = null
          instance.recorder.onerror = null
          const blob = instance.chunks.length
            ? new Blob(instance.chunks, instance.recorder.mimeType ? { type: instance.recorder.mimeType } : { type: 'audio/webm' })
            : new Blob([], { type: 'audio/webm' })
          resolve(this.buildRecordedTrack(instance, blob))
        }
        instance.recorder.onstop = cleanup
        instance.recorder.onerror = cleanup
        try {
          instance.recorder.stop()
        } catch {
          cleanup()
        }
      })
    })

    return Promise.all(promises).then((tracks) => {
      this.instances = []
      return tracks
    })
  }

  dispose(): void {
    this.active = false
    for (const instance of this.instances) {
      instance.track.removeEventListener?.('ended', instance.onTrackEnded)
      instance.recorder.ondataavailable = null
      instance.recorder.onerror = null
      instance.recorder.onstop = null
      if (instance.recorder.state === 'recording') {
        try {
          instance.recorder.stop()
        } catch {
          // Best-effort cleanup
        }
      }
    }
    this.instances = []
  }

  private buildRecordedTrack(instance: TrackRecorderInstance, blob: Blob): RecordedParticipantTrack {
    return {
      trackId: instance.trackId,
      connectionId: instance.connectionId,
      participantName: instance.participantName,
      attendanceId: instance.attendanceId,
      blob,
      track_id: instance.trackId,
      connection_id: instance.connectionId,
      participant_name: instance.participantName,
      attendance_id: instance.attendanceId,
    }
  }
}

export class MeetingRecordingSession {
  private readonly stream: MediaStream | null
  private readonly onChange: (snapshot: MeetingRecordingSnapshot) => void
  private readonly RecorderConstructor: MeetingMediaRecorderConstructor | undefined
  private recorder: MediaRecorder | null = null
  private chunks: Blob[] = []
  private startedAt: number | null = null
  private timer: ReturnType<typeof setInterval> | null = null
  private disposed = false
  private currentSnapshot = INITIAL_SNAPSHOT
  private completionResolvers: ((blob: Blob | null) => void)[] = []
  private trackSession: ParticipantTrackRecordingSession | null = null

  constructor(
    stream: MediaStream | null,
    onChange: (snapshot: MeetingRecordingSnapshot) => void,
    RecorderConstructor: MeetingMediaRecorderConstructor | undefined = globalThis.MediaRecorder,
    multiTrackConfig?: MultiTrackSourceConfig | null,
  ) {
    this.stream = stream
    this.onChange = onChange
    this.RecorderConstructor = RecorderConstructor
    if (multiTrackConfig) {
      this.trackSession = new ParticipantTrackRecordingSession(
        stream,
        multiTrackConfig,
        RecorderConstructor,
      )
    }
  }

  get snapshot(): MeetingRecordingSnapshot {
    return this.currentSnapshot
  }

  updateMultiTrackConfig(config?: MultiTrackSourceConfig | null): void {
    if (config) {
      if (!this.trackSession) {
        this.trackSession = new ParticipantTrackRecordingSession(
          this.stream,
          config,
          this.RecorderConstructor,
        )
      } else {
        this.trackSession.updateConfig(config)
      }
    }
  }

  start(): boolean {
    if (this.currentSnapshot.state === 'recording') return true
    if (!this.stream) {
      this.fail('The local microphone stream is not available.')
      return false
    }

    try {
      const recorder = createAudioVideoMediaRecorder(this.stream, this.RecorderConstructor)
      this.chunks = []
      this.recorder = recorder
      recorder.ondataavailable = (event: BlobEvent) => {
        if (event.data.size === 0) return
        this.chunks.push(event.data)
        this.publish({ chunks: [...this.chunks] })
      }
      recorder.onerror = (event: Event) => {
        const recorderError = 'error' in event ? event.error : null
        this.fail(
          recorderError instanceof Error
            ? recorderError.message
            : 'The browser recorder encountered an error.',
        )
      }
      recorder.onstop = () => this.finish(recorder)
      this.startedAt = Date.now()
      recorder.start(1000)

      if (this.trackSession) {
        this.trackSession.start()
      }

      this.publish({ state: 'recording', durationMs: 0, blob: null, chunks: [], tracks: [], error: null })
      this.timer = setInterval(() => this.updateDuration(), 250)
      return true
    } catch (error) {
      this.clearRecorderHandlers()
      if (this.trackSession) {
        this.trackSession.dispose()
      }
      this.fail(error instanceof Error ? error.message : 'Unable to start recording.')
      return false
    }
  }

  stop(): boolean {
    if (this.currentSnapshot.state === 'stopped') return true
    if (!this.recorder || this.currentSnapshot.state !== 'recording') {
      this.fail('There is no active recording to stop.')
      return false
    }

    try {
      this.recorder.stop()
      if (this.trackSession) {
        this.trackSession.stop()
      }
      return true
    } catch (error) {
      this.clearRecorderHandlers()
      this.fail(error instanceof Error ? error.message : 'Unable to stop recording.')
      return false
    }
  }

  stopAndWait(): Promise<Blob | null> {
    if (this.currentSnapshot.state === 'stopped') {
      return Promise.resolve(this.currentSnapshot.blob)
    }
    if (this.currentSnapshot.state !== 'recording' || !this.recorder) {
      return Promise.resolve(null)
    }

    return new Promise((resolve) => {
      this.completionResolvers.push(resolve)
      if (!this.stop()) this.resolveCompletion(null)
    })
  }

  async stopAndWaitTracks(): Promise<RecordedParticipantTrack[]> {
    if (this.currentSnapshot.state === 'stopped') {
      return this.currentSnapshot.tracks ? [...this.currentSnapshot.tracks] : []
    }
    await this.stopAndWait()
    return this.currentSnapshot.tracks ? [...this.currentSnapshot.tracks] : []
  }

  async stopAndWaitAll(): Promise<{ blob: Blob | null; tracks: readonly RecordedParticipantTrack[] }> {
    const blob = await this.stopAndWait()
    return {
      blob,
      tracks: this.currentSnapshot.tracks ?? [],
    }
  }

  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    this.clearTimer()
    const recorder = this.recorder
    this.clearRecorderHandlers()
    this.recorder = null
    if (this.trackSession) {
      this.trackSession.dispose()
      this.trackSession = null
    }
    this.resolveCompletion(null)
    if (recorder?.state === 'recording') {
      try {
        recorder.stop()
      } catch {
        // Cleanup is best-effort; the meeting room owns the MediaStream tracks.
      }
    }
  }

  private finish(recorder: MediaRecorder): void {
    if (this.recorder !== recorder) return
    this.clearTimer()
    const durationMs = this.elapsedDuration()
    const blob = this.chunks.length
      ? new Blob(this.chunks, recorder.mimeType ? { type: recorder.mimeType } : undefined)
      : null
    this.clearRecorderHandlers()
    this.recorder = null

    if (this.trackSession) {
      this.trackSession.stopAndWait().then((tracks) => {
        this.publish({ state: 'stopped', durationMs, blob, chunks: [...this.chunks], tracks, error: null })
        this.resolveCompletion(blob)
      }).catch(() => {
        this.publish({ state: 'stopped', durationMs, blob, chunks: [...this.chunks], tracks: [], error: null })
        this.resolveCompletion(blob)
      })
    } else {
      this.publish({ state: 'stopped', durationMs, blob, chunks: [...this.chunks], error: null })
      this.resolveCompletion(blob)
    }
  }

  private fail(message: string): void {
    this.clearTimer()
    this.publish({ state: 'error', durationMs: this.elapsedDuration(), error: message })
    this.resolveCompletion(null)
  }

  private updateDuration(): void {
    if (this.currentSnapshot.state === 'recording') {
      this.publish({ durationMs: this.elapsedDuration() })
    }
  }

  private elapsedDuration(): number {
    return this.startedAt === null ? 0 : Math.max(0, Date.now() - this.startedAt)
  }

  private publish(update: Partial<MeetingRecordingSnapshot>): void {
    this.currentSnapshot = { ...this.currentSnapshot, ...update }
    if (!this.disposed) this.onChange(this.currentSnapshot)
  }

  private clearTimer(): void {
    if (this.timer !== null) clearInterval(this.timer)
    this.timer = null
  }

  private clearRecorderHandlers(): void {
    if (!this.recorder) return
    this.recorder.ondataavailable = null
    this.recorder.onerror = null
    this.recorder.onstop = null
  }

  private resolveCompletion(blob: Blob | null): void {
    const resolvers = this.completionResolvers
    this.completionResolvers = []
    resolvers.forEach((resolve) => resolve(blob))
  }
}