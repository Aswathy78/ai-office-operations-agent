// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { apiFetch, clearAccessToken, getAccessToken, setAccessToken, subscribeToUnauthorized } from './client'
import { clearSessionTokens, hasRefreshToken, refreshSession, storeSessionTokens } from './auth'

const REFRESH_URL = 'http://127.0.0.1:8000/api/v1/auth/refresh'
const PROTECTED_URL = 'http://127.0.0.1:8000/api/protected'

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } })
}

function tokens(access: string, refresh: string) {
  return { access_token: access, token_type: 'bearer', expires_in: 900, refresh_token: refresh, refresh_expires_in: 86400 }
}

/** A server whose protected endpoint only accepts the access token it was last given. */
function serverAccepting(valid: () => string, refreshReply: () => Response) {
  const calls: { url: string; auth: string | null; body: string | null }[] = []
  const fetchMock = vi.fn<typeof fetch>(async (input, init) => {
    const url = String(input)
    const headers = new Headers(init?.headers)
    calls.push({ url, auth: headers.get('Authorization'), body: typeof init?.body === 'string' ? init.body : null })
    if (url === REFRESH_URL) return refreshReply()
    return headers.get('Authorization') === `Bearer ${valid()}` ? json({ ok: true }) : new Response(null, { status: 401 })
  })
  vi.stubGlobal('fetch', fetchMock)
  return { calls, fetchMock }
}

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
})

afterEach(() => {
  clearAccessToken()
  clearSessionTokens()
  vi.unstubAllGlobals()
})

describe('silent session refresh', () => {
  it('renews an expired access token once and retries the request', async () => {
    let valid = 'access-2'
    const { calls } = serverAccepting(() => valid, () => json(tokens('access-2', 'refresh-2')))
    storeSessionTokens(tokens('access-1', 'refresh-1') as never)
    const unauthorized = vi.fn()
    const unsubscribe = subscribeToUnauthorized(unauthorized)

    const response = await apiFetch(PROTECTED_URL)

    expect(response.status).toBe(200)
    expect(getAccessToken()).toBe('access-2')
    expect(unauthorized).not.toHaveBeenCalled()
    expect(calls.map((call) => call.url)).toEqual([PROTECTED_URL, REFRESH_URL, PROTECTED_URL])
    expect(JSON.parse(calls[1].body ?? '{}')).toEqual({ refresh_token: 'refresh-1' })
    expect(calls[1].auth).toBeNull()                       // the refresh call carries no bearer token
    expect(calls[2].auth).toBe('Bearer access-2')
    valid = 'unused'
    unsubscribe()
  })

  it('rotates: the next refresh presents the newest refresh token, never the old one', async () => {
    serverAccepting(() => 'x', () => json(tokens('access-2', 'refresh-2')))
    storeSessionTokens(tokens('access-1', 'refresh-1') as never)
    await refreshSession()
    const { calls } = serverAccepting(() => 'x', () => json(tokens('access-3', 'refresh-3')))
    await refreshSession()
    expect(JSON.parse(calls[0].body ?? '{}').refresh_token).toBe('refresh-2')
  })

  it('shares one refresh between requests that expire together', async () => {
    const { calls } = serverAccepting(() => 'access-2', () => json(tokens('access-2', 'refresh-2')))
    storeSessionTokens(tokens('access-1', 'refresh-1') as never)

    const responses = await Promise.all([apiFetch(PROTECTED_URL), apiFetch(PROTECTED_URL), apiFetch(PROTECTED_URL)])

    expect(responses.map((response) => response.status)).toEqual([200, 200, 200])
    expect(calls.filter((call) => call.url === REFRESH_URL)).toHaveLength(1)   // a refresh token works once
  })

  it('signs the person out with the server\'s reason when the refresh is refused', async () => {
    serverAccepting(() => 'never', () =>
      json({ detail: 'You were signed out because you were inactive. Please sign in again.' }, 401))
    storeSessionTokens(tokens('access-1', 'refresh-1') as never)
    const unauthorized = vi.fn()
    const unsubscribe = subscribeToUnauthorized(unauthorized)

    const response = await apiFetch(PROTECTED_URL)

    expect(response.status).toBe(401)
    expect(getAccessToken()).toBeNull()
    expect(hasRefreshToken()).toBe(false)
    expect(unauthorized).toHaveBeenCalledWith('You were signed out because you were inactive. Please sign in again.')
    unsubscribe()
  })

  it('signs the person out when there is no refresh token', async () => {
    const { calls } = serverAccepting(() => 'never', () => json({}, 500))
    setAccessToken('legacy-access-token')
    const unauthorized = vi.fn()
    const unsubscribe = subscribeToUnauthorized(unauthorized)

    await apiFetch(PROTECTED_URL)

    expect(calls.map((call) => call.url)).toEqual([PROTECTED_URL])     // nothing to refresh with
    expect(unauthorized).toHaveBeenCalledOnce()
    expect(getAccessToken()).toBeNull()
    unsubscribe()
  })

  it('signs out if the retried request is refused again', async () => {
    serverAccepting(() => 'never', () => json(tokens('access-2', 'refresh-2')))
    storeSessionTokens(tokens('access-1', 'refresh-1') as never)
    const unauthorized = vi.fn()
    const unsubscribe = subscribeToUnauthorized(unauthorized)

    const response = await apiFetch(PROTECTED_URL)

    expect(response.status).toBe(401)
    expect(unauthorized).toHaveBeenCalledOnce()
    unsubscribe()
  })

  it('does not try to refresh requests that were never authenticated', async () => {
    const { calls } = serverAccepting(() => 'never', () => json(tokens('a', 'b')))
    storeSessionTokens(tokens('access-1', 'refresh-1') as never)
    await apiFetch(PROTECTED_URL, { authenticated: false })
    expect(calls.map((call) => call.url)).toEqual([PROTECTED_URL])
  })

  it('keeps every token out of browser storage', async () => {
    serverAccepting(() => 'access-2', () => json(tokens('access-2', 'refresh-2')))
    storeSessionTokens(tokens('access-1', 'refresh-1') as never)
    await apiFetch(PROTECTED_URL)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })
})
