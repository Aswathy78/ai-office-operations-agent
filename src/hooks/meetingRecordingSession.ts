export type MeetingRecordingState = 'idle' | 'recording' | 'stopped' | 'error'

export interface MeetingRecordingSnapshot {
  state: MeetingRecordingState
  durationMs: number
  blob: Blob | null
  chunks: readonly Blob[]
  error: string | null
}

export type MeetingMediaRecorderConstructor = typeof MediaRecorder

const AUDIO_VIDEO_MIME_TYPES = [
  'video/webm;codecs=vp8,opus',
  'video/webm;codecs=vp9,opus',
  'video/webm',
]

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

  constructor(
    stream: MediaStream | null,
    onChange: (snapshot: MeetingRecordingSnapshot) => void,
    RecorderConstructor: MeetingMediaRecorderConstructor | undefined = globalThis.MediaRecorder,
  ) {
    this.stream = stream
    this.onChange = onChange
    this.RecorderConstructor = RecorderConstructor
  }

  get snapshot(): MeetingRecordingSnapshot {
    return this.currentSnapshot
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
      this.publish({ state: 'recording', durationMs: 0, blob: null, chunks: [], error: null })
      this.timer = setInterval(() => this.updateDuration(), 250)
      return true
    } catch (error) {
      this.clearRecorderHandlers()
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

  dispose(): void {
    if (this.disposed) return
    this.disposed = true
    this.clearTimer()
    const recorder = this.recorder
    this.clearRecorderHandlers()
    this.recorder = null
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
    this.publish({ state: 'stopped', durationMs, blob, chunks: [...this.chunks], error: null })
    this.clearRecorderHandlers()
    this.recorder = null
    this.resolveCompletion(blob)
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