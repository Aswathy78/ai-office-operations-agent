import { apiFetch, API_BASE_URL, registerSessionRefresher, setAccessToken } from './client'
import type { UserRole } from '../types/user'

export interface AuthenticatedUser {
  id: string
  account_id: string
  name: string
  email: string
  role: UserRole
  team: string | null
}

export interface LoginResponse {
  access_token: string
  token_type: 'bearer'
  expires_in: number
  /** Story 10.1: single-use; every refresh returns a new one. Kept in memory only. */
  refresh_token?: string | null
  refresh_expires_in?: number | null
}

export type OAuthProviderId = 'google' | 'microsoft'

export interface OAuthProvider {
  id: OAuthProviderId
  name: string
  enabled: boolean
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

function checkLoginResponse(result: LoginResponse): LoginResponse {
  if (!result.access_token || result.token_type !== 'bearer') {
    throw new Error('The authentication server returned an invalid login response.')
  }
  return result
}

// ---------------------------------------------------------------------------
// The refresh token lives in memory only, like the access token: nothing about the
// session is written to browser storage. Closing or reloading the tab ends the session.
// ---------------------------------------------------------------------------

let refreshToken: string | null = null
let refreshInFlight: Promise<{ token: string | null; message?: string }> | null = null

/** Remember the tokens from a sign-in or refresh. */
export function storeSessionTokens(result: LoginResponse): void {
  setAccessToken(result.access_token)
  refreshToken = result.refresh_token ?? null
}

export function clearSessionTokens(): void {
  refreshToken = null
}

export function hasRefreshToken(): boolean {
  return refreshToken !== null
}

/**
 * Swap the refresh token for a new access token. Concurrent callers share one request,
 * because a refresh token works only once.
 */
export function refreshSession(): Promise<{ token: string | null; message?: string }> {
  if (refreshInFlight) return refreshInFlight
  const presented = refreshToken
  if (!presented) {
    return Promise.resolve({ token: null })
  }
  refreshInFlight = (async () => {
    try {
      const response = await apiFetch(`${authUrl()}/refresh`, {
        method: 'POST',
        authenticated: false,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: presented }),
      })
      const result = checkLoginResponse(await readResponse<LoginResponse>(response))
      storeSessionTokens(result)
      return { token: result.access_token }
    } catch (error) {
      clearSessionTokens()
      return { token: null, message: error instanceof Error ? error.message : undefined }
    } finally {
      refreshInFlight = null
    }
  })()
  return refreshInFlight
}

registerSessionRefresher(refreshSession)

// ---------------------------------------------------------------------------

export async function login(email: string, password: string): Promise<LoginResponse> {
  const response = await apiFetch(`${authUrl()}/login`, {
    method: 'POST',
    authenticated: false,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  return checkLoginResponse(await readResponse<LoginResponse>(response))
}

/** Which "Sign in with …" buttons to show. A failure just means none are shown. */
export async function getAuthProviders(): Promise<OAuthProvider[]> {
  try {
    const response = await apiFetch(`${authUrl()}/providers`, { authenticated: false })
    const body = await readResponse<{ providers?: OAuthProvider[] }>(response)
    return Array.isArray(body.providers) ? body.providers : []
  } catch {
    return []
  }
}

/** The provider's own sign-in address; the page then navigates there. */
export async function startOAuthSignIn(provider: OAuthProviderId): Promise<string> {
  const response = await apiFetch(`${authUrl()}/oauth/${provider}/start`, {
    method: 'POST',
    authenticated: false,
  })
  const body = await readResponse<{ authorization_url?: string }>(response)
  if (!body.authorization_url) {
    throw new Error('The authentication server did not return a sign-in address.')
  }
  return body.authorization_url
}

export function goToProvider(url: string): void {
  window.location.assign(url)
}

/** Swap the one-time code from the redirect for this app's tokens. */
export async function exchangeOAuthCode(code: string): Promise<LoginResponse> {
  const response = await apiFetch(`${authUrl()}/oauth/exchange`, {
    method: 'POST',
    authenticated: false,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  })
  return checkLoginResponse(await readResponse<LoginResponse>(response))
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
