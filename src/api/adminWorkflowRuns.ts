import { API_BASE_URL, apiFetch } from './client'

export type WorkflowRunStatus =
  | 'pending'
  | 'processing'
  | 'retrying'
  | 'completed'
  | 'failed'
  | 'cancelled'

export interface BackgroundWorkflowSummary {
  id: string
  run_type: 'background_workflow'
  workflow_type: string
  meeting_id: string | null
  user_id: string | null
  user_name: string | null
  status: WorkflowRunStatus
  queued_at: string
  started_at: string | null
  ended_at: string | null
  duration_ms: number | null
  attempt_count: number
  retry_count: number
  max_attempts: number
  estimated_tokens: null
  provider_usage: null
  cost: null
}

export interface BackgroundWorkflowStep {
  id: string
  step_order: number
  step_name: string
  status: string
  started_at: string | null
  ended_at: string | null
  duration_ms: number | null
  attempt_count: number
  retry_count: number
}

export interface BackgroundWorkflowDetail extends BackgroundWorkflowSummary {
  steps: BackgroundWorkflowStep[]
  pending_approvals: []
}

export interface BackgroundWorkflowPage {
  runs: BackgroundWorkflowSummary[]
  total: number
  limit: number
  offset: number
  has_more: boolean
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
        : `Background workflows could not be loaded (${response.status}).`
    throw new Error(detail)
  }
  return body as T
}

export async function getBackgroundWorkflows(
  params: {
    status?: WorkflowRunStatus | ''
    user?: string
    limit?: number
    offset?: number
  },
  signal?: AbortSignal,
): Promise<BackgroundWorkflowPage> {
  const query = new URLSearchParams()
  if (params.status) query.set('status', params.status)
  if (params.user?.trim()) query.set('user', params.user.trim())
  if (params.limit) query.set('limit', String(params.limit))
  if (params.offset) query.set('offset', String(params.offset))
  const suffix = query.toString() ? `?${query}` : ''
  const response = await apiFetch(`${API_BASE_URL}/api/v1/admin/workflow-runs${suffix}`, { signal })
  return readJson<BackgroundWorkflowPage>(response)
}

export async function getBackgroundWorkflowDetail(
  runId: string,
  signal?: AbortSignal,
): Promise<BackgroundWorkflowDetail> {
  const response = await apiFetch(
    `${API_BASE_URL}/api/v1/admin/workflow-runs/${encodeURIComponent(runId)}`,
    { signal },
  )
  return readJson<BackgroundWorkflowDetail>(response)
}
