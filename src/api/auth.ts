import { apiFetch, API_BASE_URL } from './client'
import type { UserRole } from '../types/user'

export interface AuthenticatedUser {
  id: string
  account_id: string
  name: string
  email: string
  role: UserRole
  team: string | null
}

interface LoginResponse {
  access_token: string
  token_type: 'bearer'
  expires_in: number
}

const authUrl = () => `${API_BASE_URL}/api/v1/auth`

async function readResponse<T>(response: Response): Promise<T> {
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

export async function login(email: string, password: string): Promise<LoginResponse> {
  const response = await apiFetch(`${authUrl()}/login`, {
    method: 'POST',
    authenticated: false,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  const result = await readResponse<LoginResponse>(response)
  if (!result.access_token || result.token_type !== 'bearer') {
    throw new Error('The authentication server returned an invalid login response.')
  }
  return result
}

export async function getCurrentUser(): Promise<AuthenticatedUser> {
  const response = await apiFetch(`${authUrl()}/me`)
  const user = await readResponse<AuthenticatedUser>(response)
  if (
    !user.id ||
    !user.account_id ||
    !user.name ||
    !user.email ||
    !['employee', 'manager', 'hr_operations', 'admin'].includes(user.role)
  ) {
    throw new Error('The authentication server returned an invalid user profile.')
  }
  return user
}

export async function logout(): Promise<void> {
  const response = await apiFetch(`${authUrl()}/logout`, { method: 'POST' })
  if (!response.ok) {
    await readResponse<void>(response)
  }
}
