import { API_BASE_URL, apiFetch } from './client'

export type WorkflowStatus = 'pending' | 'processing' | 'retrying' | 'completed' | 'failed'
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
