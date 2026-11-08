import { API_BASE_URL, apiFetch } from './client'

export type AssistantRunStatus =
  | 'running'
  | 'awaiting_approval'
  | 'completed'
  | 'failed'
  | 'skipped'
  | 'escalated'
  | 'cancelled'

export interface AssistantRunSummary {
  id: string
  user_id: string | null
  user_name: string | null
  conversation_id: string | null
  status: AssistantRunStatus
  started_at: string
  ended_at: string | null
  duration_ms: number | null
  retry_count: number
  estimated_tokens: number | null
  provider_usage: {
    prompt_tokens: number | null
    completion_tokens: number | null
    total_tokens: number | null
  }
}

export interface AssistantRunPage {
  runs: AssistantRunSummary[]
  total: number
  limit: number
  offset: number
  has_more: boolean
}

export interface AssistantRunStep {
  id: string
  step_order: number
  step_type: string
  step_name: string
  status: string
  input: Record<string, unknown> | null
  output: Record<string, unknown> | null
  started_at: string | null
  ended_at: string | null
  duration_ms: number | null
  attempt_count: number
  retry_count: number
  estimated_tokens: number | null
  provider_usage: AssistantRunSummary['provider_usage']
  tool_execution_id: string | null
  approval_id: string | null
}

export interface AssistantRunApproval {
  id: string
  tool_name: string
  status: string
  risk_level: string
  approver_kind: string
  created_at: string
  expires_at: string | null
}

export interface AssistantRunDetail extends AssistantRunSummary {
  steps: AssistantRunStep[]
  pending_approvals: AssistantRunApproval[]
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
        : `Assistant runs could not be loaded (${response.status}).`
    throw new Error(detail)
  }
  return body as T
}

export async function getAssistantRuns(
  params: {
    status?: AssistantRunStatus | ''
    user?: string
    limit?: number
    offset?: number
  },
  signal?: AbortSignal,
): Promise<AssistantRunPage> {
  const query = new URLSearchParams()
  if (params.status) query.set('status', params.status)
  if (params.user?.trim()) query.set('user', params.user.trim())
  if (params.limit) query.set('limit', String(params.limit))
  if (params.offset) query.set('offset', String(params.offset))
  const suffix = query.toString() ? `?${query}` : ''
  const response = await apiFetch(`${API_BASE_URL}/api/v1/admin/assistant-runs${suffix}`, { signal })
  return readJson<AssistantRunPage>(response)
}

export async function getAssistantRunDetail(
  runId: string,
  signal?: AbortSignal,
): Promise<AssistantRunDetail> {
  const response = await apiFetch(
    `${API_BASE_URL}/api/v1/admin/assistant-runs/${encodeURIComponent(runId)}`,
    { signal },
  )
  return readJson<AssistantRunDetail>(response)
}
