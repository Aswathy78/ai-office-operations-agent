import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  createMeeting,
  getCalendarMeetings,
  extractMeetingActionItems,
  getMeetingActionItems,
  getActiveEmployees,
  getMeetingReport,
  getMeetingTranscript,
  getMeetingTranscriptCorrections,
  reviewActionItem,
  updateMeetingTranscript,
  getMeetingMinutes,
  generateMeetingMinutes,
  approveMeetingMinutes,
  sendMeetingMinutes,
  getMeetingMinutesRecipients,
  addMeetingParticipant,
  addMeetingParticipants,
  cancelMeeting,
  decideMeetingCancellation,
  getMeetingAudit,
  getMeetingParticipants,
  getPendingMeetingCancellations,
  removeMeetingParticipant,
} from './meetings'
import type {
  MeetingActionItems,
  MeetingTranscript,
  MeetingTranscriptUpdateRequest,
  TranscriptCorrection,
  TranscriptSegment,
  MeetingMinutes,
  MeetingMinutesDeliveryResponse,
  MeetingMinutesRecipient,
} from '../types/meeting'
import { clearAccessToken, setAccessToken } from './client'

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

const actionItemsFixture: MeetingActionItems = {
  id: 'extraction-1',
  meeting_id: 'meeting-1',
  transcript_id: 'transcript-1',
  status: 'completed',
  created_at: '2026-10-05T10:00:00Z',
  updated_at: '2026-10-05T10:01:00Z',
  processing_started_at: '2026-10-05T10:00:30Z',
  completed_at: '2026-10-05T10:01:00Z',
  error_message: null,
  action_items: [
    {
      id: 'item-1',
      owner_name: null,
      task_description: 'Send the notes.',
      deadline_at: null,
      deadline_text: 'by Friday',
      priority: 'medium',
      confidence: 0.85,
      meeting_id: 'meeting-1',
      transcript_id: 'transcript-1',
      source_segment_index: 2,
      source_start_seconds: 12.5,
      source_end_seconds: 14.25,
      source_quote_start_seconds: 12.75,
      source_quote_end_seconds: 14.1,
      source_quote: 'I will send the notes by Friday.',
    },
  ],
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
  clearAccessToken()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('employee directory API', () => {
  it('decodes the direct JSON array returned by the employee endpoint', async () => {
    const employees = [
      { id: 'employee-1', name: 'Rahul', email: 'rahul@example.test' },
    ]
    const fetchMock = stubFetch(employees)

    const result = await getActiveEmployees()

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/employees',
    )
    expect(Array.isArray(result)).toBe(true)
    expect(result).toEqual(employees)
  })
})

describe('meeting creation API', () => {
  it('POSTs an instant meeting without scheduled date fields to the configured backend', async () => {
    const fetchMock = stubFetch({
      id: 'meeting-1',
      title: 'Standup',
      join_url: 'http://localhost:5173/meetings/meeting-1',
      status: 'instant',
      start_at: '2026-10-13T10:00:00Z',
      end_at: '2026-10-13T14:00:00Z',
      organizer_id: 'manager 1',
      timezone: 'Asia/Kolkata',
    })

    await createMeeting(
      {
        title: 'Standup',
        agenda: null,
        meeting_type: 'instant',
        participants: [{ email: 'teammate@example.test' }],
      },
      'manager 1',
    )

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/?organizer_id=manager+1',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: 'Standup',
          agenda: null,
          meeting_type: 'instant',
          participants: [{ email: 'teammate@example.test' }],
        }),
      }),
    )
  })

  it('preserves scheduled meeting dates in the request body', async () => {
    const fetchMock = stubFetch()
    const data = {
      title: 'Planning',
      agenda: 'Roadmap',
      meeting_type: 'scheduled',
      start_at: '2026-10-14T09:00',
      end_at: '2026-10-14T10:00',
      participants: [],
    }

    await createMeeting(data, 'manager-1')

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/?organizer_id=manager-1',
      expect.objectContaining({ body: JSON.stringify(data) }),
    )
  })
})

describe('meeting calendar API', () => {
  it('requests month-scoped meetings for the supplied employee and returns the configured timezone', async () => {
    const responseBody = {
      timezone: 'Asia/Kolkata',
      meetings: [
        {
          id: 'meeting-calendar-1',
          title: 'Planning',
          start_at: '2026-10-22T04:30:00Z',
          end_at: '2026-10-22T05:00:00Z',
          join_url: 'https://office.example.test/meetings/meeting-calendar-1',
          status: 'scheduled',
          organizer_id: 'employee-1',
          attendees: [
            {
              employee_id: 'employee-2',
              name: 'Ava Patel',
              email: 'ava@example.test',
            },
          ],
        },
      ],
    }
    const fetchMock = stubFetch(responseBody)

    const result = await getCalendarMeetings(2026, 10)

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/calendar?year=2026&month=10',
    )
    expect(result.timezone).toBe('Asia/Kolkata')
    expect(result.meetings[0].attendees[0].name).toBe('Ava Patel')
  })
})

describe('meeting event change API (Story 5.4)', () => {
  it('loads the authorized roster and sends participant changes to the meeting routes', async () => {
    const fetchMock = stubFetch({ status: 'applied', message: 'Done', notifications_sent: [], notification_failures: [] })

    await getMeetingParticipants('meeting/one')
    expect(fetchMock).toHaveBeenLastCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/meeting%2Fone/participants',
    )

    await addMeetingParticipant('meeting-1', { email: 'guest@example.test' })
    expect(fetchMock).toHaveBeenLastCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/meeting-1/participants',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ email: 'guest@example.test' }) }),
    )

    await addMeetingParticipants('meeting-1', ['guest1@example.test', 'guest2@example.test'])
    expect(fetchMock).toHaveBeenLastCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/meeting-1/participants/bulk',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ emails: ['guest1@example.test', 'guest2@example.test'] }),
      }),
    )

    await removeMeetingParticipant('meeting-1', 'participant-1')
    expect(fetchMock).toHaveBeenLastCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/meeting-1/participants/participant-1',
      { method: 'DELETE' },
    )
  })

  it('requests cancellation, lists pending requests, and submits approval decisions', async () => {
    const fetchMock = stubFetch([])

    await cancelMeeting('meeting-1', 'No longer needed')
    expect(fetchMock).toHaveBeenLastCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/meeting-1/cancel',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ reason: 'No longer needed' }) }),
    )

    await getPendingMeetingCancellations()
    expect(fetchMock).toHaveBeenLastCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/cancellation-requests',
    )

    await decideMeetingCancellation('request-1', 'approve')
    expect(fetchMock).toHaveBeenLastCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/cancellation-requests/request-1/decide',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ decision: 'approve', note: null }) }),
    )

    await decideMeetingCancellation('request/reject', 'reject')
    expect(fetchMock).toHaveBeenLastCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/cancellation-requests/request%2Freject/decide',
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ decision: 'reject', note: null }) }),
    )
  })

  it('loads meeting audit history with the authenticated API client', async () => {
    const fetchMock = stubFetch([])
    setAccessToken('verified-session-token')

    await getMeetingAudit('meeting-1')

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/meeting-1/audit',
      expect.objectContaining({
        headers: expect.any(Headers),
      }),
    )
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get('Authorization'))
      .toBe('Bearer verified-session-token')
  })
})

describe('transcript API', () => {
  it('requests the transcript with the organizer query parameter and types its response', async () => {
    const fetchMock = stubFetch()

    const transcript = await getMeetingTranscript('meeting/one', 'organizer 1')

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/meeting%2Fone/transcript?organizer_id=organizer+1',
    )
    const segment: TranscriptSegment = transcript.segments[0]
    expect(segment.flagged_for_correction).toBe(true)
    expect(transcript.completed_at).toBe('2026-10-05T10:01:00Z')
  })

  it('requests the exact transcript when a source reference supplies its id', async () => {
    const fetchMock = stubFetch()

    await getMeetingTranscript('meeting-1', 'organizer-1', 'transcript-2')

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/meeting-1/transcript?organizer_id=organizer-1&transcript_id=transcript-2',
    )
  })

  it('includes the employee identity when loading an assigned task source', async () => {
    const fetchMock = stubFetch()

    await getMeetingTranscript(
      'meeting-1',
      'employee-user',
      'transcript-2',
      'rahul@example.test',
    )

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/meeting-1/transcript?organizer_id=employee-user&transcript_id=transcript-2&employee_email=rahul%40example.test',
    )
  })

  describe('meeting action items API', () => {
    it('GETs action items with the organizer id and decodes the response', async () => {
      const fetchMock = stubFetch(actionItemsFixture)

      const result = await getMeetingActionItems('meeting/one', 'organizer 1')

      expect(fetchMock).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/api/v1/meetings/meeting%2Fone/action-items?organizer_id=organizer+1',
      )
      expect(result.status).toBe('completed')
      expect(result.action_items[0].source_segment_index).toBe(2)
      expect(result.action_items[0].owner_name).toBeNull()
    })

    describe('meeting report API', () => {
      it('includes employee identity for a task-assignee source report request', async () => {
        const fetchMock = stubFetch({})

        await getMeetingReport('meeting-1', 'employee-user', 'rahul@example.test')

        expect(fetchMock).toHaveBeenCalledWith(
          'http://127.0.0.1:8000/api/v1/meetings/meeting-1/report?organizer_id=employee-user&employee_email=rahul%40example.test',
        )
      })
    })

    it('POSTs an extraction request without a request body', async () => {
      const fetchMock = stubFetch(actionItemsFixture)

      const result = await extractMeetingActionItems('meeting-1', 'organizer-1')

      expect(fetchMock).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/api/v1/meetings/meeting-1/action-items/extract?organizer_id=organizer-1',
        { method: 'POST' },
      )
      expect(result.action_items[0].source_start_seconds).toBe(12.5)
    })

    it('surfaces API errors using MeetingApiError', async () => {
      vi.stubGlobal(
        'fetch',
        vi.fn<typeof fetch>().mockResolvedValue(
          new Response(JSON.stringify({ detail: 'Completed transcript not found' }), {
            status: 404,
            headers: { 'Content-Type': 'application/json' },
          }),
        ),
      )

      await expect(getMeetingActionItems('meeting-1', 'organizer-1')).rejects.toMatchObject({
        name: 'MeetingApiError',
        status: 404,
        message: 'Completed transcript not found',
      })
    })

    it('PATCHes a review decision for an action item', async () => {
      const reviewedItem = {
        ...actionItemsFixture.action_items[0],
        review_status: 'confirmed',
        reviewed_by: 'organizer-1',
        is_task_eligible: true,
      }
      const fetchMock = stubFetch(reviewedItem)

      const result = await reviewActionItem(
        'meeting-1',
        'item-1',
        { action: 'confirm' },
        'organizer-1',
      )

      expect(fetchMock).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/api/v1/meetings/meeting-1/action-items/item-1/review?organizer_id=organizer-1',
        expect.objectContaining({
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ action: 'confirm' }),
        }),
      )
      expect(result.review_status).toBe('confirmed')
      expect(result.is_task_eligible).toBe(true)
    })
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
      'http://127.0.0.1:8000/api/v1/meetings/meeting-1/transcript?organizer_id=organizer-1',
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

  it('PATCHes the specifically selected transcript when one is supplied', async () => {
    const fetchMock = stubFetch()

    await updateMeetingTranscript(
      'meeting-1',
      'organizer-1',
      [{ text: 'Corrected words.' }],
      'transcript-2',
    )

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/meeting-1/transcript?organizer_id=organizer-1&transcript_id=transcript-2',
      expect.objectContaining({ method: 'PATCH' }),
    )
  })

  it('requests and types transcript correction history', async () => {
    const fetchMock = stubFetch([correctionFixture])

    const corrections = await getMeetingTranscriptCorrections(
      'meeting-1',
      'organizer-1',
    )

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/meeting-1/transcript/corrections?organizer_id=organizer-1',
    )
    const correction: TranscriptCorrection = corrections[0]
    expect(correction.segment_index).toBe(0)
    expect(correction.original_speaker).toBe('Speaker A')
    expect(correction.corrected_speaker).toBe('Speaker B')
    expect(correction.editor_id).toBe('organizer-1')
  })

  it('filters correction history to the selected transcript when supplied', async () => {
    const fetchMock = stubFetch([correctionFixture])

    await getMeetingTranscriptCorrections('meeting-1', 'organizer-1', 'transcript-2')

    expect(fetchMock).toHaveBeenCalledWith(
      'http://127.0.0.1:8000/api/v1/meetings/meeting-1/transcript/corrections?organizer_id=organizer-1&transcript_id=transcript-2',
    )
  })

  describe('meeting minutes client (Story 4.2)', () => {
    const minutesFixture: MeetingMinutes = {
      id: 'minutes-1',
      meeting_id: 'meeting-1',
      summary_id: 'summary-1',
      status: 'pending_approval',
      policy: 'manual_approval',
      approved_by: null,
      approved_at: null,
      content_summary: 'Discussed sprint items.',
      content_decisions: ['Agreed on feature delivery date'],
      content_action_items: [
        {
          id: 'item-1',
          task_description: 'Write frontend tests',
          owner_name: 'Alice',
          owner_email: 'alice@example.com',
          deadline_at: '2026-10-20T17:00:00Z',
          deadline_text: 'by next Tuesday',
          priority: 'high',
        },
      ],
      error_message: null,
      created_at: '2026-10-14T10:00:00Z',
      updated_at: '2026-10-14T10:00:00Z',
      recipients: [
        {
          id: 'recipient-1',
          minutes_id: 'minutes-1',
          recipient_name: 'Alice',
          recipient_email: 'alice@example.com',
          attendance_log_id: 'log-1',
          delivery_status: 'pending',
          error_message: null,
          idempotency_key: 'minutes:minutes-1:alice@example.com',
          sent_at: null,
          created_at: '2026-10-14T10:00:00Z',
        },
      ],
    }

    it('requests meeting minutes for an organizer', async () => {
      const fetchMock = stubFetch(minutesFixture)

      const result = await getMeetingMinutes('meeting-1', 'organizer-1')

      expect(fetchMock).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/api/v1/meetings/meeting-1/minutes?organizer_id=organizer-1',
        expect.objectContaining({
          headers: expect.objectContaining({ 'X-User-Id': 'organizer-1' }),
        }),
      )
      expect(result.id).toBe('minutes-1')
      expect(result.status).toBe('pending_approval')
      expect(result.recipients).toHaveLength(1)
    })

    it('generates meeting minutes snapshot', async () => {
      const fetchMock = stubFetch(minutesFixture)

      const result = await generateMeetingMinutes('meeting-1', 'organizer-1')

      expect(fetchMock).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/api/v1/meetings/meeting-1/minutes/generate?organizer_id=organizer-1',
        expect.objectContaining({
          method: 'POST',
          headers: expect.objectContaining({ 'X-User-Id': 'organizer-1' }),
        }),
      )
      expect(result.content_summary).toBe('Discussed sprint items.')
    })

    it('approves meeting minutes with optional notes', async () => {
      const approvedMinutes: MeetingMinutes = {
        ...minutesFixture,
        status: 'approved',
        approved_by: 'organizer-1',
        approved_at: '2026-10-14T11:00:00Z',
      }
      const fetchMock = stubFetch(approvedMinutes)

      const result = await approveMeetingMinutes('meeting-1', 'organizer-1', { notes: 'Approved for sending' })

      expect(fetchMock).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/api/v1/meetings/meeting-1/minutes/approve?organizer_id=organizer-1',
        expect.objectContaining({
          method: 'POST',
          headers: expect.objectContaining({
            'Content-Type': 'application/json',
            'X-User-Id': 'organizer-1',
          }),
          body: JSON.stringify({ notes: 'Approved for sending' }),
        }),
      )
      expect(result.status).toBe('approved')
      expect(result.approved_by).toBe('organizer-1')
    })

    it('sends meeting minutes to attendee recipients without retry', async () => {
      const deliveryResponse: MeetingMinutesDeliveryResponse = {
        minutes_id: 'minutes-1',
        meeting_id: 'meeting-1',
        total: 1,
        sent: 1,
        failed: 0,
        pending: 0,
        recipients: [
          {
            ...minutesFixture.recipients[0],
            delivery_status: 'sent',
            sent_at: '2026-10-14T11:05:00Z',
          },
        ],
      }
      const fetchMock = stubFetch(deliveryResponse)

      const result = await sendMeetingMinutes('meeting-1', 'organizer-1', false)

      expect(fetchMock).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/api/v1/meetings/meeting-1/minutes/send?organizer_id=organizer-1&retry_failed=false',
        expect.objectContaining({
          method: 'POST',
          headers: expect.objectContaining({ 'X-User-Id': 'organizer-1' }),
        }),
      )
      expect(result.sent).toBe(1)
      expect(result.failed).toBe(0)
    })

    it('retries sending meeting minutes for failed recipients only', async () => {
      const deliveryResponse: MeetingMinutesDeliveryResponse = {
        minutes_id: 'minutes-1',
        meeting_id: 'meeting-1',
        total: 1,
        sent: 1,
        failed: 0,
        pending: 0,
        recipients: [
          {
            ...minutesFixture.recipients[0],
            delivery_status: 'sent',
            sent_at: '2026-10-14T11:10:00Z',
          },
        ],
      }
      const fetchMock = stubFetch(deliveryResponse)

      const result = await sendMeetingMinutes('meeting-1', 'organizer-1', true)

      expect(fetchMock).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/api/v1/meetings/meeting-1/minutes/send?organizer_id=organizer-1&retry_failed=true',
        expect.objectContaining({
          method: 'POST',
          headers: expect.objectContaining({ 'X-User-Id': 'organizer-1' }),
        }),
      )
      expect(result.sent).toBe(1)
    })

    it('retrieves delivery status of meeting minutes recipients', async () => {
      const fetchMock = stubFetch(minutesFixture.recipients)

      const recipients = await getMeetingMinutesRecipients('meeting-1', 'organizer-1')

      expect(fetchMock).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/api/v1/meetings/meeting-1/minutes/recipients?organizer_id=organizer-1',
        expect.objectContaining({
          headers: expect.objectContaining({ 'X-User-Id': 'organizer-1' }),
        }),
      )
      expect(recipients).toHaveLength(1)
      expect(recipients[0].recipient_email).toBe('alice@example.com')
    })
  })
})
