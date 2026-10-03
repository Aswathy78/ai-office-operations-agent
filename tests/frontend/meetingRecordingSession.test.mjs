import assert from 'node:assert/strict'
import test from 'node:test'
import {
  createAudioVideoMediaRecorder,
  MeetingRecordingSession,
} from '../../src/hooks/meetingRecordingSession.ts'

const audioTrack = { kind: 'audio', readyState: 'live' }
const videoTrack = { kind: 'video', readyState: 'live' }
const meetingStream = {
  getAudioTracks: () => [audioTrack],
  getVideoTracks: () => [videoTrack],
  getTracks: () => [audioTrack, videoTrack],
}

class MockMediaRecorder {
  static supportedTypes = ['audio/webm;codecs=opus']
  static failConstruction = false
  static failStart = false
  static failStop = false
  static asyncStop = false
  static instances = []

  static isTypeSupported(type) {
    return this.supportedTypes.includes(type)
  }

  constructor(stream, options) {
    if (MockMediaRecorder.failConstruction) throw new Error('Recorder construction failed')
    this.stream = stream
    this.options = options
    this.mimeType = options?.mimeType ?? 'audio/default'
    this.state = 'inactive'
    this.ondataavailable = null
    this.onerror = null
    this.onstop = null
    this.stopCalls = 0
    MockMediaRecorder.instances.push(this)
  }

  start() {
    if (MockMediaRecorder.failStart) throw new Error('Recorder start failed')
    this.state = 'recording'
  }

  stop() {
    this.stopCalls += 1
    if (MockMediaRecorder.failStop) throw new Error('Recorder stop failed')
    const complete = () => {
      this.state = 'inactive'
      this.ondataavailable?.({ data: new Blob(['recorded audio'], { type: this.mimeType }) })
      this.onstop?.(new Event('stop'))
    }
    if (MockMediaRecorder.asyncStop) setTimeout(complete, 5)
    else complete()
  }
}

function resetRecorder() {
  MockMediaRecorder.supportedTypes = ['audio/webm;codecs=opus']
  MockMediaRecorder.isTypeSupported = (type) => MockMediaRecorder.supportedTypes.includes(type)
  MockMediaRecorder.failConstruction = false
  MockMediaRecorder.failStart = false
  MockMediaRecorder.failStop = false
  MockMediaRecorder.asyncStop = false
  MockMediaRecorder.instances = []
}

function createSession(stream = meetingStream) {
  const changes = []
  const session = new MeetingRecordingSession(
    stream,
    (snapshot) => changes.push(snapshot),
    MockMediaRecorder,
  )
  return { session, changes }
}

test('handles MediaRecorder being unavailable', () => {
  const session = new MeetingRecordingSession(meetingStream, () => {}, null)

  const started = session.start()

  assert.equal(started, false)
  assert.equal(session.snapshot.state, 'error')
  assert.match(session.snapshot.error, /not supported/i)
  session.dispose()
})

test('requires a live audio track', () => {
  resetRecorder()
  const { session } = createSession({
    getAudioTracks: () => [{ kind: 'audio', readyState: 'ended' }],
    getVideoTracks: () => [videoTrack],
  })

  assert.equal(session.start(), false)
  assert.match(session.snapshot.error, /live microphone track/i)
  assert.equal(MockMediaRecorder.instances.length, 0)
  session.dispose()
})

test('creates recorder with the complete audio-video stream and preferred MIME type', () => {
  resetRecorder()
  MockMediaRecorder.supportedTypes = [
    'video/webm;codecs=vp8,opus',
    'video/webm;codecs=vp9,opus',
  ]

  const recorder = createAudioVideoMediaRecorder(meetingStream, MockMediaRecorder)

  assert.equal(recorder.stream, meetingStream)
  assert.deepEqual(recorder.stream.getTracks(), [audioTrack, videoTrack])
  assert.deepEqual(recorder.options, { mimeType: 'video/webm;codecs=vp8,opus' })
})

test('selects VP9 audio-video WebM when it is the first supported option', () => {
  resetRecorder()
  MockMediaRecorder.supportedTypes = ['video/webm;codecs=vp9,opus']

  const recorder = createAudioVideoMediaRecorder(meetingStream, MockMediaRecorder)

  assert.equal(recorder.stream, meetingStream)
  assert.deepEqual(recorder.options, { mimeType: 'video/webm;codecs=vp9,opus' })
})

test('selects generic audio-video WebM when codec-specific options are unsupported', () => {
  resetRecorder()
  MockMediaRecorder.supportedTypes = ['video/webm']

  const recorder = createAudioVideoMediaRecorder(meetingStream, MockMediaRecorder)

  assert.equal(recorder.stream, meetingStream)
  assert.deepEqual(recorder.options, { mimeType: 'video/webm' })
})

test('starts recording only when the explicit start method is called', () => {
  resetRecorder()
  const { session } = createSession()

  assert.equal(session.snapshot.state, 'idle')
  assert.equal(MockMediaRecorder.instances.length, 0)
  assert.equal(session.start(), true)
  assert.equal(session.snapshot.state, 'recording')
  assert.equal(MockMediaRecorder.instances[0].state, 'recording')
  session.dispose()
})

test('captures chunks and creates a Blob when recording stops', async () => {
  resetRecorder()
  const { session } = createSession()
  assert.equal(session.start(), true)

  assert.equal(session.stop(), true)

  assert.equal(session.snapshot.state, 'stopped')
  assert.equal(session.snapshot.chunks.length, 1)
  assert.ok(session.snapshot.blob instanceof Blob)
  assert.equal(await session.snapshot.blob.text(), 'recorded audio')
  assert.ok(session.snapshot.durationMs >= 0)
  session.dispose()
})

test('stopAndWait resolves only after final chunk delivery', async () => {
  resetRecorder()
  MockMediaRecorder.asyncStop = true
  const { session } = createSession()
  session.start()
  let resolved = false

  const completedBlobPromise = session.stopAndWait().then((blob) => {
    resolved = true
    return blob
  })

  assert.equal(resolved, false)
  const completedBlob = await completedBlobPromise
  assert.equal(resolved, true)
  assert.ok(completedBlob instanceof Blob)
  assert.equal(await completedBlob.text(), 'recorded audio')
  session.dispose()
})

test('falls back to the browser default when no audio-video MIME type is supported', () => {
  resetRecorder()
  MockMediaRecorder.supportedTypes = []

  const recorder = createAudioVideoMediaRecorder(meetingStream, MockMediaRecorder)

  assert.equal(recorder.stream, meetingStream)
  assert.equal(recorder.options, undefined)
})

test('falls back to the browser default when MIME support probing throws', () => {
  resetRecorder()
  MockMediaRecorder.isTypeSupported = () => { throw new Error('Support probe failed') }

  const recorder = createAudioVideoMediaRecorder(meetingStream, MockMediaRecorder)

  assert.equal(recorder.stream, meetingStream)
  assert.equal(recorder.options, undefined)
})

test('reports recorder start and stop failures', () => {
  resetRecorder()
  MockMediaRecorder.failStart = true
  const failedStart = createSession()
  assert.equal(failedStart.session.start(), false)
  assert.equal(failedStart.session.snapshot.state, 'error')
  assert.match(failedStart.session.snapshot.error, /start failed/i)
  failedStart.session.dispose()

  resetRecorder()
  MockMediaRecorder.failStop = true
  const failedStop = createSession()
  assert.equal(failedStop.session.start(), true)
  assert.equal(failedStop.session.stop(), false)
  assert.equal(failedStop.session.snapshot.state, 'error')
  assert.match(failedStop.session.snapshot.error, /stop failed/i)
  failedStop.session.dispose()
})

test('cleans up handlers after stop and on disposal', () => {
  resetRecorder()
  const finished = createSession()
  finished.session.start()
  const stoppedRecorder = MockMediaRecorder.instances[0]
  finished.session.stop()
  assert.equal(stoppedRecorder.ondataavailable, null)
  assert.equal(stoppedRecorder.onerror, null)
  assert.equal(stoppedRecorder.onstop, null)

  resetRecorder()
  const active = createSession()
  active.session.start()
  const activeRecorder = MockMediaRecorder.instances[0]
  active.session.dispose()
  assert.equal(activeRecorder.stopCalls, 1)
  assert.equal(activeRecorder.ondataavailable, null)
  assert.equal(activeRecorder.onerror, null)
  assert.equal(activeRecorder.onstop, null)
})