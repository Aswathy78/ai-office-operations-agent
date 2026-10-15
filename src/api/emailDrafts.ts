import { API_BASE_URL, actorQuery, apiFetch, readJson, type TaskActor } from './tasks'

export type EmailDraftStatus =
  | 'draft'
  | 'pending_approval'
  | 'approved'
  | 'rejected'
  | 'sent'
  | 'partially_sent'
  | 'failed'
  | 'discarded'

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
  decided_by: string | null
  decided_at: string | null
  decision_note: string | null
  delivery_results: { email: string; status: 'sent' | 'failed'; error: string | null }[]
  sent_at: string | null
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
  status: 'pending_approval' | '',
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
