import { API_BASE_URL, apiFetch } from './client'
export { API_BASE_URL, apiFetch } from './client'

export interface TaskRecord {
  id: string
  title: string
  assignee_id: string
  assignee_name: string
  creator_id: string
  source: string
  /** Meeting provenance; null for tasks the AI assistant created directly (Story 6.5). */
  source_action_item_id: string | null
  meeting_id: string | null
  meeting_title: string | null
  transcript_id: string | null
  source_segment_index: number | null
  source_timestamp_seconds: number | null
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
  /** The deadline's calendar sync (Story 5.5); null until the scheduler picks the task up. */
  calendar_sync?: TaskCalendarSync | null
}

export type TaskCalendarSyncStatus = 'pending' | 'processing' | 'completed' | 'retrying' | 'failed'

export interface TaskCalendarSync {
  status: TaskCalendarSyncStatus
  provider: string
  pending_reason: string | null
  attempt_count: number
  max_attempts: number
  next_attempt_at: string | null
  last_error: string | null
  last_synced_at: string | null
  last_polled_at: string | null
  external_change_detected_at: string | null
  external_change_detail: string | null
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

/** Failed -> Pending: try writing the task deadline to the calendar again. */
export async function retryTaskCalendarSync(taskId: string): Promise<TaskRecord> {
  const response = await apiFetch(
    `${API_BASE_URL}/api/v1/tasks/${encodeURIComponent(taskId)}/calendar-sync/retry`,
    { method: 'POST' },
  )
  return readTask(response)
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

export interface EscalationRecord {
  task_id: string
  task_title: string
  assignee_id: string
  assignee_name: string
  assignee_email: string
  due_at: string
  overdue_hours: number
  missed_reminders: number
  escalation_timestamp: string
  escalation_status: string
  escalation_notification_id: string
}

export function getTaskEscalations(): Promise<EscalationRecord[]> {
  return apiFetch(`${API_BASE_URL}/api/v1/tasks/escalations`)
    .then((response) => readJson<EscalationRecord[]>(response))
}


export interface TeamDashboardSummary {
  team_name: string
  total_members: number
  total_tasks: number
  open_tasks: number
  due_soon_tasks: number
  overdue_tasks: number
  completed_tasks: number
  completion_rate: number
}

export interface TeamMemberWorkload {
  employee_id: string
  employee_name: string
  employee_email: string
  total_tasks: number
  open_tasks: number
  due_soon_tasks: number
  overdue_tasks: number
  completed_tasks: number
  completion_rate: number
}

export interface TeamOverdueTask {
  task_id: string
  title: string
  assignee_id: string
  assignee_name: string
  assignee_email: string
  due_at: string
  overdue_hours: number
  priority: 'low' | 'medium' | 'high' | 'urgent' | string
  status: 'pending' | 'completed' | string
}

export interface TeamDashboardData {
  summary: TeamDashboardSummary
  members: TeamMemberWorkload[]
  overdue_tasks: TeamOverdueTask[]
}

export function getTeamDashboard(team?: string): Promise<TeamDashboardData> {
  const query = team ? `?${new URLSearchParams({ team })}` : ''
  return apiFetch(`${API_BASE_URL}/api/v1/tasks/team-dashboard${query}`)
    .then((response) => readJson<TeamDashboardData>(response))
}

// ---------------------------------------------------------------------------
// Story 9.3 – Manager Operational Monitoring Portal
// ---------------------------------------------------------------------------

export interface ActionItemMonitorRow {
  action_item_id: string
  task_description: string
  owner_name: string | null
  owner_email: string | null
  deadline_at: string | null
  ownership_status: string
  review_status: string
  meeting_id: string
  meeting_title: string
}

export interface TaskStatusRow {
  task_id: string
  title: string
  assignee_name: string
  assignee_email: string
  priority: string
  due_at: string | null
  status: string
  email_status: string | null
  email_error: string | null
  calendar_status: string | null
  calendar_error: string | null
  calendar_provider: string | null
  calendar_attempt_count: number | null
}

export interface WorkflowFailureRow {
  run_id: string
  workflow_type: string
  status: string
  last_error: string | null
  attempt_count: number
  max_attempts: number
  created_at: string
  updated_at: string
  run_link: string
}

export interface FailureReasonRow {
  category: 'email' | 'calendar' | 'workflow' | 'notification' | string
  error_message: string
  count: number
  last_occurred_at: string
}

export interface OperationalMonitoringSummary {
  team_name: string
  total_action_items: number
  action_items_pending_review: number
  total_tasks_monitored: number
  email_failed_count: number
  calendar_failed_count: number
  workflow_failed_count: number
  total_failures: number
}

export interface OperationalMonitoringData {
  summary: OperationalMonitoringSummary
  action_items: ActionItemMonitorRow[]
  task_status_rows: TaskStatusRow[]
  workflow_failures: WorkflowFailureRow[]
  failure_reasons: FailureReasonRow[]
}

export function getOperationalMonitoring(team?: string): Promise<OperationalMonitoringData> {
  const query = team ? `?${new URLSearchParams({ team })}` : ''
  return apiFetch(`${API_BASE_URL}/api/v1/tasks/operational-monitoring${query}`)
    .then((response) => readJson<OperationalMonitoringData>(response))
}

export function getOperationalMonitoringExportUrl(team?: string): string {
  const query = team ? `?${new URLSearchParams({ team })}` : ''
  return `${API_BASE_URL}/api/v1/tasks/operational-monitoring/export.csv${query}`
}
