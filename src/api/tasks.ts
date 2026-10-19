import { API_BASE_URL, apiFetch } from './client'
export { API_BASE_URL, apiFetch } from './client'

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
  status: 'pending' | 'completed'
  completed_at?: string | null
  assignment_email_status: 'pending' | 'sent' | 'failed'
  assignment_email_error: string | null
  created_at: string
  assignee_email?: string | null
  assignee_team?: string | null
  updated_at?: string | null
  calendar_sequence?: number
  pending_reassignment?: TaskReassignmentRequest | null
}

export interface TaskReassignmentRequest {
  id: string
  task_id: string
  task_title: string
  from_assignee_id: string
  from_assignee_name: string
  to_assignee_id: string
  to_assignee_name: string
  to_assignee_team: string | null
  requested_by: string
  requester_role: string
  requester_team: string | null
  reason: string | null
  status: 'pending' | 'approved' | 'rejected'
  decided_by: string | null
  decided_at: string | null
  decision_note: string | null
  created_at: string
}

export interface TaskAuditEntry {
  id: string
  task_id: string
  actor_id: string
  actor_role: string
  action: string
  outcome: 'applied' | 'pending_approval' | 'denied' | 'rejected'
  detail: string
  created_at: string
}

export interface TaskNotification {
  id: string
  recipient_employee_id: string
  recipient_name: string
  kind: string
  subject: string
  status: 'pending' | 'sent' | 'failed'
  error: string | null
  created_at: string
}

export interface TaskChangeResult {
  outcome: 'applied' | 'pending_approval'
  message: string
  task: TaskRecord
  reassignment_request: TaskReassignmentRequest | null
  notifications: TaskNotification[]
}

/** The signed-in user making a change; the backend authorises by role and team. */
export interface TaskActor {
  id: string
  role: string
  team: string | null
  /** Lets an employee complete their own tasks. */
  email?: string | null
}

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
  return apiFetch(`${API_BASE_URL}/api/v1/tasks/?${query}`).then(readTasks)
}

export function getDashboardTasks(): Promise<TaskRecord[]> {
  return apiFetch(`${API_BASE_URL}/api/v1/tasks/dashboard`).then(readTasks)
}

export function taskCalendarUrl(assigneeEmail: string): string {
  const query = new URLSearchParams({ assignee_email: assigneeEmail })
  return `${API_BASE_URL}/api/v1/tasks/calendar.ics?${query}`
}

export async function retryAssignmentEmail(taskId: string): Promise<TaskRecord> {
  const response = await apiFetch(
    `${API_BASE_URL}/api/v1/tasks/${encodeURIComponent(taskId)}/assignment-email/retry`,
    { method: 'POST' },
  )
  return readTask(response)
}

export async function readJson<T>(response: Response): Promise<T> {
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

export function actorQuery(actor: TaskActor): string {
  const query = new URLSearchParams({ actor_id: actor.id, actor_role: actor.role })
  if (actor.team) query.set('actor_team', actor.team)
  if (actor.email) query.set('actor_email', actor.email)
  return query.toString()
}

export function reassignTask(
  taskId: string,
  assigneeId: string,
  reason: string,
  actor: TaskActor,
): Promise<TaskChangeResult> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/tasks/${encodeURIComponent(taskId)}/assignee?${actorQuery(actor)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ assignee_id: assigneeId, reason: reason.trim() || null }),
    },
  ).then((response) => readJson<TaskChangeResult>(response))
}

export function changeTaskDueDate(
  taskId: string,
  dueAt: string,
  reason: string,
  actor: TaskActor,
): Promise<TaskChangeResult> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/tasks/${encodeURIComponent(taskId)}/due-date?${actorQuery(actor)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ due_at: dueAt, reason: reason.trim() || null }),
    },
  ).then((response) => readJson<TaskChangeResult>(response))
}

export function getReassignmentRequests(
  status: 'pending' | 'approved' | 'rejected' | '' = 'pending',
): Promise<TaskReassignmentRequest[]> {
  const query = new URLSearchParams({ status })
  return apiFetch(`${API_BASE_URL}/api/v1/tasks/reassignment-requests?${query}`)
    .then((response) => readJson<TaskReassignmentRequest[]>(response))
}

export function decideReassignmentRequest(
  requestId: string,
  decision: 'approve' | 'reject',
  note: string,
  actor: TaskActor,
): Promise<TaskChangeResult> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/tasks/reassignment-requests/${encodeURIComponent(requestId)}/decision?${actorQuery(actor)}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision, note: note.trim() || null }),
    },
  ).then((response) => readJson<TaskChangeResult>(response))
}

export function getTaskAudit(taskId: string): Promise<TaskAuditEntry[]> {
  return apiFetch(`${API_BASE_URL}/api/v1/tasks/${encodeURIComponent(taskId)}/audit`)
    .then((response) => readJson<TaskAuditEntry[]>(response))
}

export function getTaskNotifications(taskId: string): Promise<TaskNotification[]> {
  return apiFetch(`${API_BASE_URL}/api/v1/tasks/${encodeURIComponent(taskId)}/notifications`)
    .then((response) => readJson<TaskNotification[]>(response))
}

/** Mark a task Completed (stops reminder / escalation e-mails) or reopen it. */
export function setTaskStatus(
  taskId: string,
  status: 'pending' | 'completed',
  actor: TaskActor,
): Promise<TaskChangeResult> {
  return apiFetch(
    `${API_BASE_URL}/api/v1/tasks/${encodeURIComponent(taskId)}/status?${actorQuery(actor)}`,
    {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    },
  ).then((response) => readJson<TaskChangeResult>(response))
}
