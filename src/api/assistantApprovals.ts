/**
 * AI assistant action approvals (Story 6.6).
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
  decided_by: string | null
  decided_by_name: string | null
  decided_at: string | null
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
}

const base = () => `${API_BASE_URL}/api/v1/assistant/approvals`

export function getAgentApprovals(
  scope: 'mine' | 'queue',
  status: AgentApprovalStatus | '' = 'pending',
): Promise<AgentActionApproval[]> {
  const query = new URLSearchParams({ scope, status })
  return apiFetch(`${base()}?${query}`).then((response) => readJson<AgentActionApproval[]>(response))
}

export function decideAgentApproval(
  id: string,
  decision: 'approve' | 'reject',
  note: string,
): Promise<AgentApprovalDecision> {
  return apiFetch(`${base()}/${encodeURIComponent(id)}/decide`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, note: note.trim() || null }),
  }).then((response) => readJson<AgentApprovalDecision>(response))
}

export function cancelAgentApproval(id: string): Promise<AgentActionApproval> {
  return apiFetch(`${base()}/${encodeURIComponent(id)}/cancel`, { method: 'POST' })
    .then((response) => readJson<AgentActionApproval>(response))
}
