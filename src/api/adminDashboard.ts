import { API_BASE_URL, apiFetch } from './client'

/** Story 9.4: administrator dashboard. Every value is measured by the API, none is configured. */

export type HealthStatus = 'operational' | 'degraded' | 'down' | 'not_configured' | 'unknown'

/** A section the API could not read: the rest of the dashboard still loads. */
export interface UnavailableSection {
  available: false
  error: string
}

type Available<T> = { available: true; error: null } & T
export type Section<T> = Available<T> | UnavailableSection

export interface DashboardUser {
  id: string
  name: string
  email: string
  role: string
  department: string
  is_active: boolean
  last_sign_in_at: string | null
}

export interface UsersSection {
  total: number
  active: number
  disabled: number
  by_role: Record<string, number>
  items: DashboardUser[]
  truncated: boolean
}

export interface DepartmentRow {
  name: string
  employees: number
  active_employees: number
  with_login: number
}

export interface DepartmentsSection {
  total: number
  items: DepartmentRow[]
}

export interface IntegrationHealth {
  key: string
  name: string
  status: HealthStatus
  detail: string
  metrics: Record<string, string | number | boolean | null>
}

export interface IntegrationsSection {
  items: IntegrationHealth[]
  window_hours: number
}

/** `null` means "no data yet", which is different from zero. */
export interface ApiMetrics {
  window_seconds: number
  observed_seconds: number
  request_count: number
  server_error_count: number
  client_error_count: number
  error_rate_percent: number | null
  average_latency_ms: number | null
  p95_latency_ms: number | null
  max_latency_ms: number | null
}

export interface SystemSection {
  database: { latency_ms: number; schema_current: boolean | null }
  api: ApiMetrics
  queue: {
    depth: number
    due_now: number
    processing: number
    oldest_waiting_seconds: number | null
    failed_24h: number
    dead_letter_open: number
  }
  workers: { configured: number; alive: number; started: boolean }
  background_services: { name: string; running: boolean }[]
}

export interface AdminDashboard {
  generated_at: string
  refresh_interval_seconds: number
  overall_status: HealthStatus
  alerts: string[]
  users: Section<UsersSection>
  departments: Section<DepartmentsSection>
  integrations: Section<IntegrationsSection>
  system: Section<SystemSection>
}

export async function getAdminDashboard(signal?: AbortSignal): Promise<AdminDashboard> {
  const response = await apiFetch(`${API_BASE_URL}/api/v1/admin/dashboard`, { signal })
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body && typeof body.detail === 'string'
        ? body.detail
        : `The dashboard could not be loaded (${response.status}).`
    throw new Error(detail)
  }
  return body as AdminDashboard
}
