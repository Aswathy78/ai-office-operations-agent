import { API_BASE_URL, apiFetch } from './client'

export type WorkflowStatus =
  | 'pending'
  | 'processing'
  | 'retrying'
  | 'completed'
  | 'failed'
  | 'cancelled'
export type WorkflowStepStatus = 'pending' | 'processing' | 'completed' | 'failed' | 'retrying'

export interface WorkflowStep {
  id: string
  step_key: string
  status: WorkflowStepStatus
  attempt_count: number
  checkpoint: Record<string, unknown> | null
  last_error: string | null
  updated_at: string
  completed_at: string | null
}

export interface WorkflowRun {
  id: string
  workflow_type: string
  meeting_id: string | null
  priority: number
  status: WorkflowStatus
  attempt_count: number
  /** Story 8.4: attempts allowed before the run is dead-lettered (at most 5). */
  max_attempts?: number
  next_attempt_at?: string | null
  last_error: string | null
  created_at: string
  updated_at: string
  completed_at: string | null
  steps: WorkflowStep[]
}

export interface MeetingWorkflowResponse {
  workflow: WorkflowRun | null
}

async function readJson<T>(response: Response): Promise<T> {
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail =
      typeof body === 'object' &&
      body !== null &&
      'detail' in body &&
      typeof body.detail === 'string'
        ? body.detail
        : `Request failed (${response.status})`
    throw new Error(detail)
  }
  return body as T
}

export async function getMeetingWorkflow(meetingId: string): Promise<MeetingWorkflowResponse> {
  const response = await apiFetch(
    `${API_BASE_URL}/api/v1/workflows/meetings/${encodeURIComponent(meetingId)}`,
  )
  return readJson<MeetingWorkflowResponse>(response)
}

export async function streamMeetingWorkflow(
  meetingId: string,
  onUpdate: (state: MeetingWorkflowResponse) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await apiFetch(
    `${API_BASE_URL}/api/v1/workflows/meetings/${encodeURIComponent(meetingId)}/events`,
    { headers: { Accept: 'text/event-stream' }, signal },
  )
  if (!response.ok) {
    await readJson<unknown>(response)
    throw new Error(`Workflow event stream closed (${response.status}).`)
  }
  if (!response.body) throw new Error('Workflow event stream is unavailable.')

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  try {
    while (!signal.aborted) {
      const { done, value } = await reader.read()
      if (done) throw new Error('Workflow event stream ended unexpectedly.')
      buffer += decoder.decode(value, { stream: true }).replaceAll('\r\n', '\n')
      let boundary = buffer.indexOf('\n\n')
      while (boundary >= 0) {
        const event = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        const data = event
          .split('\n')
          .filter((line) => line.startsWith('data:'))
          .map((line) => line.slice(5).trimStart())
          .join('\n')
        if (data) onUpdate(JSON.parse(data) as MeetingWorkflowResponse)
        boundary = buffer.indexOf('\n\n')
      }
    }
  } finally {
    await reader.cancel().catch(() => undefined)
  }
}

// ---------------------------------------------------------------------------
// Story 8.4 - dead-letter queue (administrators)
// ---------------------------------------------------------------------------

export type DeadLetterStatus = 'open' | 'retried' | 'resumed' | 'cancelled'
export type DeadLetterAction = 'retry' | 'resume' | 'cancel'

export interface DeadLetterEntry {
  id: string
  run_id: string
  workflow_type: string
  workflow_label: string
  meeting_id: string | null
  meeting_title: string | null
  failed_step_key: string | null
  error_message: string | null
  attempt_count: number
  max_attempts: number
  status: DeadLetterStatus
  run_status: WorkflowStatus | null
  notified_count: number
  created_at: string
  resolved_at: string | null
  resolved_by: string | null
  resolution_note: string | null
}

export interface DeadLetterQueue {
  entries: DeadLetterEntry[]
  counts: Record<DeadLetterStatus, number>
}

export interface WorkflowEvent {
  id: string
  step_key: string | null
  attempt_number: number | null
  event_type: string
  message: string | null
  error: string | null
  retry_delay_seconds: number | null
  next_attempt_at: string | null
  actor_id: string | null
  created_at: string
}

export interface DeadLetterDetail {
  workflow: WorkflowRun
  payload: Record<string, unknown>
  idempotency_key: string
  meeting_title: string | null
  dead_letters: DeadLetterEntry[]
  events: WorkflowEvent[]
}

export async function getDeadLetterQueue(
  status: DeadLetterStatus | 'all' = 'open',
): Promise<DeadLetterQueue> {
  const response = await apiFetch(
    `${API_BASE_URL}/api/v1/workflows/dead-letter?status=${encodeURIComponent(status)}`,
  )
  return readJson<DeadLetterQueue>(response)
}

export async function getDeadLetterDetail(runId: string): Promise<DeadLetterDetail> {
  const response = await apiFetch(
    `${API_BASE_URL}/api/v1/workflows/dead-letter/${encodeURIComponent(runId)}`,
  )
  return readJson<DeadLetterDetail>(response)
}

/**
 * retry  - run again from the first step with a fresh attempt budget
 * resume - continue from the last checkpoint
 * cancel - give up; the run ends as cancelled
 */
export async function actOnDeadLetter(
  runId: string,
  action: DeadLetterAction,
  note?: string,
): Promise<DeadLetterDetail> {
  const response = await apiFetch(
    `${API_BASE_URL}/api/v1/workflows/dead-letter/${encodeURIComponent(runId)}/${action}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ note: note?.trim() || null }),
    },
  )
  return readJson<DeadLetterDetail>(response)
}
