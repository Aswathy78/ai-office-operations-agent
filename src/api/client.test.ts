import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  apiFetch,
  clearAccessToken,
  getAccessToken,
  setAccessToken,
  subscribeToUnauthorized,
} from './client'

afterEach(() => {
  clearAccessToken()
  vi.unstubAllGlobals()
})

describe('shared authenticated API client', () => {
  it('adds the in-memory bearer token to protected requests', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 204 }))
    vi.stubGlobal('fetch', fetchMock)
    setAccessToken('signed-access-token')

    await apiFetch('http://127.0.0.1:8000/api/v1/meetings/cancellation-requests')

    const requestHeaders = new Headers(fetchMock.mock.calls[0][1]?.headers)
    expect(requestHeaders.get('Authorization')).toBe('Bearer signed-access-token')
  })

  it('does not attach tokens to explicitly unauthenticated requests', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)
    setAccessToken('signed-access-token')

    await apiFetch('http://127.0.0.1:8000/api/v1/auth/login', { authenticated: false })

    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).has('Authorization')).toBe(false)
  })

  it('clears an expired token and notifies the authentication context on HTTP 401', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(new Response(null, { status: 401 }))
    vi.stubGlobal('fetch', fetchMock)
    setAccessToken('expired-access-token')
    const unauthorized = vi.fn()
    const unsubscribe = subscribeToUnauthorized(unauthorized)

    await apiFetch('http://127.0.0.1:8000/api/v1/meetings/meeting-1/participants')

    expect(getAccessToken()).toBeNull()
    expect(unauthorized).toHaveBeenCalledOnce()
    unsubscribe()
  })
})
