/**
 * AI assistant action approvals (Stories 6.6 and 9.6).
 *
 * Assistant writes that are not low risk pause with "Approval Required":
 * medium risk waits for the requester's own confirmation, high risk for an
 * approver (HR / Operations, an admin, or the requester's team manager).
 * Approving runs the action with the requester's permissions.
 */

import { API_BASE_URL, apiFetch } from './client'
import { readJson } from './tasks'

export type AgentApprovalStatus =
  | 'pending'
  | 'executing'
  | 'rejected'
  | 'cancelled'
  | 'expired'
  | 'executed'
  | 'failed'

export type AgentRiskLevel = 'low' | 'medium' | 'high'

export interface AgentActionApproval {
  id: string
  tool_name: string
  status: AgentApprovalStatus
  summary: string
  risk_level: AgentRiskLevel
  risk_reasons: string[]
  approver_kind: 'requester' | 'approver'
  requester_id: string
  requester_name: string
  requester_role: string
  requester_team: string | null
  arguments: Record<string, unknown>
  affected_people: string[]
  affected_recipients?: Array<{ name: string; email: string; source?: string | null }>
  recipient_snapshot_missing?: boolean
  source: { type: string; conversation_id: string | null }
  decided_by: string | null
  decided_by_name: string | null
  decided_at: string | null
  history_actor_name?: string | null
  history_at?: string | null
  alternate_approver_id: string | null
  alternate_approver_name: string | null
  alternate_approver_email: string | null
  reminder_sent_at: string | null
  decision_note: string | null
  execution_id: string | null
  result: Record<string, unknown> | null
  error: string | null
  conversation_id: string | null
  expires_at: string
  created_at: string
  can_decide: boolean
  can_cancel: boolean
}

export interface AgentApprovalDecision {
  message: string
  approval: AgentActionApproval
  execution: Record<string, unknown> | null
  continuation?: { resumed: boolean } | null
}

export interface AgentApprovalHistoryFilters {
  status?: AgentApprovalStatus | ''
  approver?: string
  from_date?: string
  to_date?: string
}

const base = () => `${API_BASE_URL}/api/v1/assistant/approvals`

export function getAgentApprovals(
  scope: 'mine' | 'queue',
  status: AgentApprovalStatus | '' = 'pending',
): Promise<AgentActionApproval[]> {
  const query = new URLSearchParams({ scope, status })
  return apiFetch(`${base()}?${query}`).then((response) => readJson<AgentActionApproval[]>(response))
}

export function getAgentApprovalFromEmail(token: string): Promise<AgentActionApproval> {
  return apiFetch(`${base()}/email-preview`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token }),
  })
    .then((response) => readJson<AgentActionApproval>(response))
}

export function decideAgentApproval(
  id: string,
  decision: 'approve' | 'reject',
  note: string,
  arguments_?: Record<string, unknown>,
): Promise<AgentApprovalDecision> {
  return apiFetch(`${base()}/${encodeURIComponent(id)}/decide`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      decision,
      note: note.trim() || null,
      ...(arguments_ ? { arguments: arguments_ } : {}),
    }),
  }).then((response) => readJson<AgentApprovalDecision>(response))
}

export function decideAgentApprovalFromEmail(
  token: string,
  decision: 'approve' | 'reject',
  note: string,
  arguments_?: Record<string, unknown>,
): Promise<AgentApprovalDecision> {
  return apiFetch(`${base()}/email-decision`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      token,
      decision,
      note: note.trim() || null,
      ...(arguments_ ? { arguments: arguments_ } : {}),
    }),
  }).then((response) => readJson<AgentApprovalDecision>(response))
}

export function cancelAgentApproval(id: string): Promise<AgentActionApproval> {
  return apiFetch(`${base()}/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
    .then((response) => readJson<AgentActionApproval>(response))
}

export function assignAgentAlternateApprover(
  id: string,
  email: string | null,
): Promise<AgentActionApproval> {
  return apiFetch(`${base()}/${encodeURIComponent(id)}/alternate-approver`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  }).then((response) => readJson<AgentActionApproval>(response))
}

export function getAgentApprovalHistory(
  filters: AgentApprovalHistoryFilters = {},
): Promise<AgentActionApproval[]> {
  const query = new URLSearchParams()
  if (filters.status) query.set('status', filters.status)
  if (filters.approver?.trim()) query.set('approver', filters.approver.trim())
  if (filters.from_date) query.set('from_date', filters.from_date)
  if (filters.to_date) query.set('to_date', filters.to_date)
  const suffix = query.size ? `?${query}` : ''
  return apiFetch(`${base()}/history${suffix}`)
    .then((response) => readJson<AgentActionApproval[]>(response))
}
