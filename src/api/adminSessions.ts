import { API_BASE_URL, apiFetch } from './client'

export interface ActiveSession {
  id: string
  user: { id: string; name: string; email: string; role: string }
  provider: 'password' | 'google' | 'microsoft'
  provider_label: string
  signed_in_at: string
  last_active_at: string
  expires_at: string
  ip_address: string | null
  device: string
  is_current: boolean
}

export interface ActiveSessions {
  idle_timeout_seconds: number
  sessions: ActiveSession[]
}

async function readJson<T>(response: Response): Promise<T> {
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body && typeof body.detail === 'string'
        ? body.detail
        : `Request failed (${response.status})`
    throw new Error(detail)
  }
  return body as T
}

export async function getActiveSessions(): Promise<ActiveSessions> {
  return readJson<ActiveSessions>(await apiFetch(`${API_BASE_URL}/api/v1/admin/sessions`))
}

export async function revokeSession(sessionId: string): Promise<void> {
  await readJson<unknown>(
    await apiFetch(`${API_BASE_URL}/api/v1/admin/sessions/${encodeURIComponent(sessionId)}`, {
      method: 'DELETE',
    }),
  )
}

export async function revokeAllSessionsForUser(userId: string): Promise<number> {
  const body = await readJson<{ revoked: number }>(
    await apiFetch(`${API_BASE_URL}/api/v1/admin/users/${encodeURIComponent(userId)}/sessions`, {
      method: 'DELETE',
    }),
  )
  return body.revoked
}
