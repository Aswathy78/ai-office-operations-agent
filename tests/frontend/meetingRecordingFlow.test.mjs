import assert from 'node:assert/strict'
import test from 'node:test'
import {
  getRecordingUploadMessage,
  getRecordingUploadState,
  isMeetingOrganizer,
  persistRecordingConsent,
  shouldShowRecordingControls,
  shouldShowStartRecording,
  startRecordingAfterConsent,
  stopRecordingAndUpload,
} from '../../src/hooks/meetingRecordingFlow.ts'

const meetingRecording = { status: 'recording' }
const processingRecording = { status: 'processing' }

function consentResponse(consent_status) {
  return { consent_status }
}

test('meeting creator is recognized from organizer_id returned by meeting GET', () => {
  const meetingFromGet = { organizer_id: 'creator-user-17' }

  assert.equal(isMeetingOrganizer(meetingFromGet.organizer_id, 'creator-user-17'), true)
})

test('consented organizer sees recording controls', () => {
  const meetingFromGet = { organizer_id: 'creator-user-17' }
  const isOrganizer = isMeetingOrganizer(meetingFromGet.organizer_id, 'creator-user-17')

  assert.equal(shouldShowRecordingControls(isOrganizer, 'consented'), true)
})

test('non-organizer does not see organizer recording controls after consent', () => {
  const meetingFromGet = { organizer_id: 'creator-user-17' }
  const isOrganizer = isMeetingOrganizer(meetingFromGet.organizer_id, 'other-user-22')

  assert.equal(shouldShowRecordingControls(isOrganizer, 'consented'), false)
})

test('completed recording hides Start recording while other lifecycle states preserve it', () => {
  assert.equal(shouldShowStartRecording(true, 'consented', 'idle'), true)
  assert.equal(shouldShowStartRecording(true, 'consented', 'processing'), true)
  assert.equal(shouldShowStartRecording(true, 'consented', 'completed'), false)
})

test('recording cannot start before explicit consent', async () => {
  let backendStartCalls = 0
  let browserStartCalls = 0

  await assert.rejects(
    startRecordingAfterConsent(
      false,
      async () => { backendStartCalls += 1; return meetingRecording },
      () => { browserStartCalls += 1; return true },
      async () => processingRecording,
    ),
    /explicit recording consent is required/i,
  )

  assert.equal(backendStartCalls, 0)
  assert.equal(browserStartCalls, 0)
})

test('successful persisted consent enables the recording flow', async () => {
  const calls = []
  const confirmed = await persistRecordingConsent(
    async (...args) => {
      calls.push(['consent', ...args])
      return consentResponse('consented')
    },
    'meeting-1',
    'attendance-1',
    'consented',
  )

  assert.equal(confirmed, true)
  await startRecordingAfterConsent(
    confirmed,
    async () => { calls.push(['backend-start']); return meetingRecording },
    () => { calls.push(['browser-start']); return true },
    async () => processingRecording,
  )

  assert.deepEqual(calls, [
    ['consent', 'meeting-1', 'attendance-1', 'consented'],
    ['backend-start'],
    ['browser-start'],
  ])
})

test('declined consent does not enable recording', async () => {
  let browserStartCalls = 0
  const confirmed = await persistRecordingConsent(
    async () => consentResponse('declined'),
    'meeting-1',
    'attendance-1',
    'declined',
  )

  assert.equal(confirmed, false)
  await assert.rejects(
    startRecordingAfterConsent(
      confirmed,
      async () => meetingRecording,
      () => { browserStartCalls += 1; return true },
      async () => processingRecording,
    ),
    /explicit recording consent is required/i,
  )
  assert.equal(browserStartCalls, 0)
})

test('backend start failure does not start MediaRecorder', async () => {
  let browserStartCalls = 0

  await assert.rejects(
    startRecordingAfterConsent(
      true,
      async () => { throw new Error('Consent from everyone is required') },
      () => { browserStartCalls += 1; return true },
      async () => processingRecording,
    ),
    /consent from everyone is required/i,
  )

  assert.equal(browserStartCalls, 0)
})

test('successful backend start then starts MediaRecorder', async () => {
  const calls = []

  await startRecordingAfterConsent(
    true,
    async () => { calls.push('backend-start'); return meetingRecording },
    () => { calls.push('browser-start'); return true },
    async () => processingRecording,
  )

  assert.deepEqual(calls, ['backend-start', 'browser-start'])
})

test('browser recorder failure compensates with backend stop', async () => {
  const calls = []

  await assert.rejects(
    startRecordingAfterConsent(
      true,
      async () => meetingRecording,
      () => false,
      async () => { calls.push('backend-stop'); return processingRecording },
    ),
    /browser could not start/i,
  )

  assert.deepEqual(calls, ['backend-stop'])
})

test('upload waits for MediaRecorder stop and backend stop', async () => {
  const calls = []
  const completedBlob = new Blob(['final chunk'], { type: 'audio/webm' })
  let finishMediaRecorderStop
  const mediaRecorderStopped = new Promise((resolve) => {
    finishMediaRecorderStop = resolve
  })

  const workflow = stopRecordingAndUpload(
    () => {
      calls.push('browser-stop-requested')
      return mediaRecorderStopped
    },
    async () => { calls.push('backend-stop'); return processingRecording },
    async (blob) => {
      calls.push(['upload', blob])
      return { status: 'processing' }
    },
    () => {},
  )

  await Promise.resolve()
  assert.deepEqual(calls, ['browser-stop-requested'])
  finishMediaRecorderStop(completedBlob)
  const uploadedBlob = await workflow

  assert.equal(uploadedBlob, completedBlob)
  assert.deepEqual(calls, [
    'browser-stop-requested',
    'backend-stop',
    ['upload', completedBlob],
  ])
})

for (const [status, expectedMessage] of [
  ['completed', 'Uploaded / Completed'],
  ['processing', 'Uploaded / Processing'],
  ['failed', 'Upload failed. The recording was marked as failed.'],
]) {
  test(`upload response status ${status} produces the matching recording display`, async () => {
    const states = []
    const completedBlob = new Blob(['recorded audio'], { type: 'audio/webm' })

    await stopRecordingAndUpload(
      async () => completedBlob,
      async () => processingRecording,
      async (blob) => {
        assert.equal(blob, completedBlob)
        return { status }
      },
      (state) => states.push(state),
    )

    assert.deepEqual(states, ['uploading', status])
    assert.equal(getRecordingUploadState(status), status)
    assert.equal(getRecordingUploadMessage(states.at(-1)), expectedMessage)
  })
}

test('failed upload preserves completed Blob and reports error state', async () => {
  const states = []
  const completedBlob = new Blob(['recorded audio'], { type: 'audio/webm' })

  await assert.rejects(
    stopRecordingAndUpload(
      async () => completedBlob,
      async () => processingRecording,
      async (blob) => {
        assert.equal(blob, completedBlob)
        throw new Error('Upload unavailable')
      },
      (state) => states.push(state),
    ),
    /upload unavailable/i,
  )

  assert.equal(completedBlob.size, 14)
  assert.deepEqual(states, ['uploading', 'error'])
})

test('does not upload when the recorder produced no Blob', async () => {
  let backendStopCalls = 0
  let uploadCalls = 0
  const states = []

  const blob = await stopRecordingAndUpload(
    async () => null,
    async () => { backendStopCalls += 1; return processingRecording },
    async () => { uploadCalls += 1 },
    (state) => states.push(state),
  )

  assert.equal(blob, null)
  assert.equal(backendStopCalls, 1)
  assert.equal(uploadCalls, 0)
  assert.deepEqual(states, [])
})
