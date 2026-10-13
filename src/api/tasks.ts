export interface TaskRecord {
  id: string
  title: string
  assignee_id: string
  assignee_name: string
  creator_id: string
  source: string
  source_action_item_id: string
  meeting_id: string
  meeting_title: string
  transcript_id: string
  source_segment_index: number
  source_timestamp_seconds: number
  due_at: string
  priority: 'low' | 'medium' | 'high' | 'urgent'
  status: 'pending'
  assignment_email_status: 'pending' | 'sent' | 'failed'
  assignment_email_error: string | null
  created_at: string
}

const API_BASE_URL = 'http://127.0.0.1:8000'

async function readTasks(response: Response): Promise<TaskRecord[]> {
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
  return body as TaskRecord[]
}

async function readTask(response: Response): Promise<TaskRecord> {
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
  return body as TaskRecord
}

export function getMyTasks(assigneeEmail: string): Promise<TaskRecord[]> {
  const query = new URLSearchParams({ assignee_email: assigneeEmail })
  return fetch(`${API_BASE_URL}/api/v1/tasks/?${query}`).then(readTasks)
}

export function getDashboardTasks(): Promise<TaskRecord[]> {
  return fetch(`${API_BASE_URL}/api/v1/tasks/dashboard`).then(readTasks)
}

export function taskCalendarUrl(assigneeEmail: string): string {
  const query = new URLSearchParams({ assignee_email: assigneeEmail })
  return `${API_BASE_URL}/api/v1/tasks/calendar.ics?${query}`
}

export async function retryAssignmentEmail(taskId: string): Promise<TaskRecord> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/tasks/${encodeURIComponent(taskId)}/assignment-email/retry`,
    { method: 'POST' },
  )
  return readTask(response)
}
