import assert from 'node:assert/strict'
import test from 'node:test'
import {
  stopRecordingAndUpload,
  uploadParticipantTracks,
} from '../../src/hooks/meetingRecordingFlow.ts'

const primaryRecording = { status: 'recording' }
const processingRecording = { status: 'processing' }

test('1. Participant tracks are registered after primary recording stops and uploads', async () => {
  const calls = []
  const primaryBlob = new Blob(['primary audio'], { type: 'audio/webm' })
  const trackBlob = new Blob(['participant audio'], { type: 'audio/webm' })

  const tracks = [
    {
      trackId: 'tr-1',
      connectionId: 'conn-1',
      participantName: 'Alice',
      attendanceId: 'att-1',
      blob: trackBlob,
    },
  ]

  await stopRecordingAndUpload(
    async () => primaryBlob,
    async () => { calls.push('backend-stop'); return processingRecording },
    async (blob) => { calls.push(['primary-upload', blob]); return { status: 'completed' } },
    () => {},
    {
      getTracks: async () => tracks,
      registerTracks: async (regs) => {
        calls.push(['register-tracks', regs])
        return [
          {
            recording_id: 'rec-p1',
            connection_id: 'conn-1',
            participant_name: 'Alice',
            status: 'processing',
            meeting_id: 'meet-1',
          },
        ]
      },
      uploadTrack: async (recId, blob) => {
        calls.push(['upload-track', recId, blob])
        return { recording_id: recId, status: 'completed', storage_uri: 's3://...', filename: 'f.webm', content_type: 'audio/webm', size_bytes: blob.size }
      },
    },
  )

  assert.deepEqual(calls, [
    'backend-stop',
    ['primary-upload', primaryBlob],
    [
      'register-tracks',
      [
        {
          connection_id: 'conn-1',
          participant_name: 'Alice',
          attendance_id: 'att-1',
        },
      ],
    ],
    ['upload-track', 'rec-p1', trackBlob],
  ])
})

test('2. Registration request contains connection_id, participant_name, attendance_id', async () => {
  let registeredTracks = null
  const trackBlob = new Blob(['audio'], { type: 'audio/webm' })

  await uploadParticipantTracks({
    tracks: [
      {
        trackId: 't-100',
        connectionId: 'conn-abc',
        participantName: 'Bob Builder',
        attendanceId: 'att-xyz',
        blob: trackBlob,
      },
    ],
    registerTracks: async (regs) => {
      registeredTracks = regs
      return [
        {
          recording_id: 'rec-100',
          connection_id: 'conn-abc',
          participant_name: 'Bob Builder',
          status: 'processing',
          meeting_id: 'm-1',
        },
      ]
    },
    uploadTrack: async () => ({
      recording_id: 'rec-100',
      status: 'completed',
      storage_uri: 'uri',
      filename: 'f.webm',
      content_type: 'audio/webm',
      size_bytes: 10,
    }),
  })

  assert.deepEqual(registeredTracks, [
    {
      connection_id: 'conn-abc',
      participant_name: 'Bob Builder',
      attendance_id: 'att-xyz',
    },
  ])
})

test('3 & 4. Backend recording_id is mapped by connection_id (not array index) and uploads correct Blob', async () => {
  const blobAlice = new Blob(['alice audio stream'], { type: 'audio/webm' })
  const blobBob = new Blob(['bob audio stream'], { type: 'audio/webm' })

  const uploadedFiles = []

  const tracks = [
    {
      trackId: 'tr-alice',
      connectionId: 'conn-alice',
      participantName: 'Alice',
      attendanceId: 'att-alice',
      blob: blobAlice,
    },
    {
      trackId: 'tr-bob',
      connectionId: 'conn-bob',
      participantName: 'Bob',
      attendanceId: 'att-bob',
      blob: blobBob,
    },
  ]

  // Reverse array order returned by backend to test connection_id mapping
  await uploadParticipantTracks({
    tracks,
    registerTracks: async () => [
      {
        recording_id: 'rec-bob-id',
        connection_id: 'conn-bob',
        participant_name: 'Bob',
        status: 'processing',
        meeting_id: 'm-1',
      },
      {
        recording_id: 'rec-alice-id',
        connection_id: 'conn-alice',
        participant_name: 'Alice',
        status: 'processing',
        meeting_id: 'm-1',
      },
    ],
    uploadTrack: async (recordingId, blob) => {
      uploadedFiles.push({ recordingId, blob })
      return {
        recording_id: recordingId,
        status: 'completed',
        storage_uri: 'uri',
        filename: 'f.webm',
        content_type: 'audio/webm',
        size_bytes: blob.size,
      }
    },
  })

  assert.equal(uploadedFiles.length, 2)
  // Bob response came first in register response -> must map to Bob's blob
  assert.equal(uploadedFiles[0].recordingId, 'rec-bob-id')
  assert.equal(uploadedFiles[0].blob, blobBob)
  // Alice response came second -> must map to Alice's blob
  assert.equal(uploadedFiles[1].recordingId, 'rec-alice-id')
  assert.equal(uploadedFiles[1].blob, blobAlice)
})

test('5. Two participants result in two registration items and two upload operations', async () => {
  let registerCallCount = 0
  const uploadCalls = []
  const blob1 = new Blob(['p1 audio'], { type: 'audio/webm' })
  const blob2 = new Blob(['p2 audio'], { type: 'audio/webm' })

  const result = await uploadParticipantTracks({
    tracks: [
      { trackId: 't1', connectionId: 'c1', participantName: 'P1', blob: blob1 },
      { trackId: 't2', connectionId: 'c2', participantName: 'P2', blob: blob2 },
    ],
    registerTracks: async (regs) => {
      registerCallCount = regs.length
      return [
        { recording_id: 'r1', connection_id: 'c1', participant_name: 'P1', status: 'processing', meeting_id: 'm1' },
        { recording_id: 'r2', connection_id: 'c2', participant_name: 'P2', status: 'processing', meeting_id: 'm1' },
      ]
    },
    uploadTrack: async (recId, blob) => {
      uploadCalls.push({ recId, blob })
      return { recording_id: recId, status: 'completed', storage_uri: 'uri', filename: 'f.webm', content_type: 'audio/webm', size_bytes: blob.size }
    },
  })

  assert.equal(registerCallCount, 2)
  assert.equal(uploadCalls.length, 2)
  assert.equal(result.successful, 2)
  assert.equal(result.failed, 0)
})

test('6. Empty participant-track list does not break primary recording', async () => {
  let registerCalled = false
  const primaryBlob = new Blob(['primary audio'], { type: 'audio/webm' })

  const uploadedBlob = await stopRecordingAndUpload(
    async () => primaryBlob,
    async () => processingRecording,
    async () => ({ status: 'completed' }),
    () => {},
    {
      getTracks: async () => [],
      registerTracks: async () => {
        registerCalled = true
        return []
      },
      uploadTrack: async () => ({}),
    },
  )

  assert.equal(uploadedBlob, primaryBlob)
  assert.equal(registerCalled, false)
})

test('7. A failed participant-track upload does not prevent other participant tracks from being attempted', async () => {
  const blob1 = new Blob(['p1 audio'], { type: 'audio/webm' })
  const blob2 = new Blob(['p2 audio'], { type: 'audio/webm' })
  const uploadAttempts = []
  const errorLogs = []

  const result = await uploadParticipantTracks({
    tracks: [
      { trackId: 't1', connectionId: 'c1', participantName: 'P1', blob: blob1 },
      { trackId: 't2', connectionId: 'c2', participantName: 'P2', blob: blob2 },
    ],
    registerTracks: async () => [
      { recording_id: 'r1', connection_id: 'c1', participant_name: 'P1', status: 'processing', meeting_id: 'm1' },
      { recording_id: 'r2', connection_id: 'c2', participant_name: 'P2', status: 'processing', meeting_id: 'm1' },
    ],
    uploadTrack: async (recId, blob) => {
      uploadAttempts.push(recId)
      if (recId === 'r1') {
        throw new Error('Network error uploading P1 track')
      }
      return { recording_id: recId, status: 'completed', storage_uri: 'uri', filename: 'f.webm', content_type: 'audio/webm', size_bytes: blob.size }
    },
    onTrackError: (err, track) => {
      errorLogs.push({ connectionId: track.connectionId, message: err.message })
    },
  })

  // Both r1 and r2 were attempted!
  assert.deepEqual(uploadAttempts, ['r1', 'r2'])
  assert.equal(result.successful, 1)
  assert.equal(result.failed, 1)
  assert.equal(errorLogs.length, 1)
  assert.equal(errorLogs[0].connectionId, 'c1')
  assert.match(errorLogs[0].message, /network error uploading p1 track/i)
})
