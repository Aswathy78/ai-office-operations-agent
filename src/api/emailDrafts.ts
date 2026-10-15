import { API_BASE_URL, actorQuery, apiFetch, readJson, type TaskActor } from './tasks'

/** The delivery lifecycle (FR-22): every e-mail has exactly one of these. */
export type EmailDraftStatus =
  | 'draft'
  | 'approval'
  | 'queued'
  | 'sending'
  | 'sent'
  | 'failed'
  | 'retry'

export const EMAIL_LIFECYCLE: readonly EmailDraftStatus[] = [
  'draft',
  'approval',
  'queued',
  'sending',
  'sent',
  'failed',
  'retry',
]

/** Statuses that change without the sender acting; the UI polls while any are shown. */
export const IN_FLIGHT_STATUSES: ReadonlySet<EmailDraftStatus> = new Set([
  'approval',
  'queued',
  'sending',
  'retry',
])

/** Status changes must reach the UI within 5 seconds; poll comfortably inside that. */
export const EMAIL_STATUS_POLL_MS = 2000

export function isInFlight(draft: Pick<EmailDraft, 'status'>): boolean {
  return IN_FLIGHT_STATUSES.has(draft.status)
}

export interface EmailDeliveryResult {
  email: string
  /** "sent" only once the mail provider accepted the message for this recipient. */
  status: 'sent' | 'failed'
  error: string | null
  message_id?: string | null
  accepted_at?: string | null
  attempt?: number | null
}

export interface EmailDraftEvent {
  id: string
  from_status: EmailDraftStatus | null
  to_status: EmailDraftStatus
  actor_id: string
  detail: string | null
  created_at: string
}

export interface EmailRecipient {
  name: string
  email: string
  internal: boolean
  source: string
}

export interface SensitiveFinding {
  type: string
  label: string
  severity: 'high' | 'medium' | 'low'
  match: string
  field: 'subject' | 'body'
}

export interface EmailDraft {
  id: string
  created_by_id: string
  created_by_name: string
  created_by_email: string | null
  created_by_role: string
  prompt: string
  source: 'template' | 'llm'
  template_key: string | null
  generation_note: string | null
  subject: string
  body: string
  recipients: EmailRecipient[]
  recipient_groups: { name: string; count: number }[]
  unresolved_recipients: string[]
  recipient_count: number
  approval_required: boolean
  approval_reasons: string[]
  approval_threshold: number
  sensitive_findings: SensitiveFinding[]
  status: EmailDraftStatus
  status_changed_at: string | null
  approval_decision: 'approved' | 'rejected' | null
  decided_by: string | null
  decided_at: string | null
  decision_note: string | null
  delivery_results: EmailDeliveryResult[]
  sent_at: string | null
  queued_at: string | null
  attempt_count: number
  max_attempts: number
  next_attempt_at: string | null
  last_error: string | null
  acknowledged_sensitive: boolean
  discarded_at: string | null
  created_at: string
  updated_at: string
}

export interface DirectoryGroup {
  name: string
  count: number
}

export interface EmailDraftEdits {
  subject?: string
  body?: string
  recipient_emails?: string[]
  add_groups?: string[]
}

const base = () => `${API_BASE_URL}/api/v1/email-drafts`
const draftUrl = (id: string, suffix = '') => `${base()}/${encodeURIComponent(id)}${suffix}`

function jsonInit(method: string, body?: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }
}

export function createEmailDraft(
  prompt: string,
  sender: { name: string; email: string },
  actor: TaskActor,
): Promise<EmailDraft> {
  return apiFetch(
    `${base()}?${actorQuery(actor)}`,
    jsonInit('POST', { prompt, sender_name: sender.name, sender_email: sender.email }),
  ).then((response) => readJson<EmailDraft>(response))
}

export function getMyEmailDrafts(actor: TaskActor): Promise<EmailDraft[]> {
  return apiFetch(`${base()}?${actorQuery(actor)}`).then((response) => readJson<EmailDraft[]>(response))
}

export function updateEmailDraft(id: string, edits: EmailDraftEdits, actor: TaskActor): Promise<EmailDraft> {
  return apiFetch(`${draftUrl(id)}?${actorQuery(actor)}`, jsonInit('PATCH', edits))
    .then((response) => readJson<EmailDraft>(response))
}

export function submitEmailDraft(id: string, actor: TaskActor): Promise<EmailDraft> {
  return apiFetch(`${draftUrl(id, '/submit')}?${actorQuery(actor)}`, jsonInit('POST'))
    .then((response) => readJson<EmailDraft>(response))
}

export function sendEmailDraft(
  id: string,
  acknowledgeSensitive: boolean,
  actor: TaskActor,
): Promise<EmailDraft> {
  return apiFetch(
    `${draftUrl(id, '/send')}?${actorQuery(actor)}`,
    jsonInit('POST', { confirm: true, acknowledge_sensitive: acknowledgeSensitive }),
  ).then((response) => readJson<EmailDraft>(response))
}

export function discardEmailDraft(id: string, actor: TaskActor): Promise<EmailDraft> {
  return apiFetch(`${draftUrl(id)}?${actorQuery(actor)}`, { method: 'DELETE' })
    .then((response) => readJson<EmailDraft>(response))
}

export function getEmailApprovals(
  status: 'approval' | '',
  actor: TaskActor,
): Promise<EmailDraft[]> {
  const query = new URLSearchParams(actorQuery(actor))
  query.set('status', status)
  return apiFetch(`${base()}/approvals?${query}`).then((response) => readJson<EmailDraft[]>(response))
}

export function decideEmailDraft(
  id: string,
  decision: 'approve' | 'reject',
  note: string,
  actor: TaskActor,
): Promise<EmailDraft> {
  return apiFetch(
    `${draftUrl(id, '/decision')}?${actorQuery(actor)}`,
    jsonInit('POST', { decision, note: note.trim() || null }),
  ).then((response) => readJson<EmailDraft>(response))
}

export function getDirectoryGroups(): Promise<DirectoryGroup[]> {
  return apiFetch(`${base()}/directory/groups`).then((response) => readJson<DirectoryGroup[]>(response))
}

export function getEmailDraft(id: string, actor: TaskActor): Promise<EmailDraft> {
  return apiFetch(`${draftUrl(id)}?${actorQuery(actor)}`).then((response) => readJson<EmailDraft>(response))
}

/** Failed -> Retry: deliver again to recipients the provider has not accepted. */
export function retryEmailDraft(id: string, actor: TaskActor): Promise<EmailDraft> {
  return apiFetch(`${draftUrl(id, '/retry')}?${actorQuery(actor)}`, jsonInit('POST'))
    .then((response) => readJson<EmailDraft>(response))
}

/** Queued -> Draft, if delivery has not started yet. */
export function cancelEmailDraft(id: string, actor: TaskActor): Promise<EmailDraft> {
  return apiFetch(`${draftUrl(id, '/cancel')}?${actorQuery(actor)}`, jsonInit('POST'))
    .then((response) => readJson<EmailDraft>(response))
}

export function getEmailDraftEvents(id: string, actor: TaskActor): Promise<EmailDraftEvent[]> {
  return apiFetch(`${draftUrl(id, '/events')}?${actorQuery(actor)}`)
    .then((response) => readJson<EmailDraftEvent[]>(response))
}

/** Failed and Retry e-mails across the organisation (manager dashboard). */
export function getEmailDeliveryIssues(actor: TaskActor): Promise<EmailDraft[]> {
  return apiFetch(`${base()}/delivery-issues?${actorQuery(actor)}`)
    .then((response) => readJson<EmailDraft[]>(response))
}
