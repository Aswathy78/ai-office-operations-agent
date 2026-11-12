import { API_BASE_URL, apiFetch } from './client'

export type AuditSource = 'task' | 'meeting' | 'assistant' | 'email' | 'workflow' | 'auth'

export interface AuditEntry {
  id: string
  at: string
  source: AuditSource
  actor_id: string | null
  actor_name: string | null
  actor_role: string | null
  action: string
  /** Plain-language title for the action, e.g. "Reminder sent". */
  action_label: string
  /** What it was done to (the AI tool, the workflow step), when that adds something. */
  target: string | null
  outcome: string | null
  /** A readable sentence: no internal tags or ids. */
  detail: string
}

export interface AuditLog {
  entries: AuditEntry[]
  counts: Record<AuditSource, number>
  source_labels: Record<AuditSource, string>
  limit: number
}

export async function getAuditLog(
  params: { source?: AuditSource | ''; q?: string; limit?: number },
  signal?: AbortSignal,
): Promise<AuditLog> {
  const query = new URLSearchParams()
  if (params.source) query.set('source', params.source)
  if (params.q?.trim()) query.set('q', params.q.trim())
  if (params.limit) query.set('limit', String(params.limit))
  const suffix = query.toString() ? `?${query}` : ''
  const response = await apiFetch(`${API_BASE_URL}/api/v1/admin/audit-logs${suffix}`, { signal })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body && typeof body.detail === 'string'
        ? body.detail
        : `The audit log could not be loaded (${response.status}).`
    throw new Error(detail)
  }
  return body as AuditLog
}
