import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  getMeetingTranscript,
  getMeetingTranscriptCorrections,
  updateMeetingTranscript,
} from './meetings'
import type {
  MeetingTranscript,
  MeetingTranscriptUpdateRequest,
  TranscriptCorrection,
  TranscriptSegment,
} from '../types/meeting'

const transcriptFixture: MeetingTranscript = {
  id: 'transcript-1',
  meeting_id: 'meeting-1',
  recording_id: 'recording-1',
  status: 'completed',
  language: 'en',
  transcript_text: 'Hello there.',
  segments: [
    {
      start: 0.25,
      end: 1.5,
      text: 'Hello there.',
      speaker: 'Speaker A',
      flagged_for_correction: true,
    },
  ],
  error_message: null,
  created_at: '2026-10-05T10:00:00Z',
  processing_started_at: null,
  completed_at: '2026-10-05T10:01:00Z',
}

const correctionFixture: TranscriptCorrection = {
  id: 'correction-1',
  meeting_id: 'meeting-1',
  transcript_id: 'transcript-1',
  segment_index: 0,
  original_text: 'Hello there.',
  corrected_text: 'Hello everyone.',
  original_speaker: 'Speaker A',
  corrected_speaker: 'Speaker B',
  editor_id: 'organizer-1',
  created_at: '2026-10-05T10:02:00Z',
}

function stubFetch(responseBody: unknown = transcriptFixture) {
  const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
    new Response(JSON.stringify(responseBody), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  )
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('transcript API', () => {
  it('requests the transcript with the organizer query parameter and types its response', async () => {
    const fetchMock = stubFetch()

    const transcript = await getMeetingTranscript('meeting/one', 'organizer 1')

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:9000/api/v1/meetings/meeting%2Fone/transcript?organizer_id=organizer+1',
    )
    const segment: TranscriptSegment = transcript.segments[0]
    expect(segment.flagged_for_correction).toBe(true)
    expect(transcript.completed_at).toBe('2026-10-05T10:01:00Z')
  })

  it('PATCHes only editable text and speaker values', async () => {
    const fetchMock = stubFetch()
    const segmentWithProtectedFields = {
      text: 'Corrected words.',
      speaker: 'Speaker C',
      start: 10,
      end: 11,
      flagged_for_correction: false,
    }
    const request: MeetingTranscriptUpdateRequest = {
      segments: [segmentWithProtectedFields],
    }

    await updateMeetingTranscript('meeting-1', 'organizer-1', request.segments)

    const [url, options] = fetchMock.mock.calls[0]
    expect(url).toBe(
      'http://127.0.0.1:9000/api/v1/meetings/meeting-1/transcript?organizer_id=organizer-1',
    )
    expect(options?.method).toBe('PATCH')
    expect(options?.headers).toEqual({ 'Content-Type': 'application/json' })
    expect(JSON.parse(String(options?.body))).toEqual({
      segments: [{ text: 'Corrected words.', speaker: 'Speaker C' }],
    })
    expect(String(options?.body)).not.toContain('start')
    expect(String(options?.body)).not.toContain('end')
    expect(String(options?.body)).not.toContain('flagged_for_correction')
  })

  it('requests and types transcript correction history', async () => {
    const fetchMock = stubFetch([correctionFixture])

    const corrections = await getMeetingTranscriptCorrections(
      'meeting-1',
      'organizer-1',
    )

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:9000/api/v1/meetings/meeting-1/transcript/corrections?organizer_id=organizer-1',
    )
    const correction: TranscriptCorrection = corrections[0]
    expect(correction.segment_index).toBe(0)
    expect(correction.original_speaker).toBe('Speaker A')
    expect(correction.corrected_speaker).toBe('Speaker B')
    expect(correction.editor_id).toBe('organizer-1')
  })
})
