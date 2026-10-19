export let API_BASE_URL =
  (typeof import.meta !== 'undefined' && import.meta.env?.VITE_API_BASE_URL) ||
  'http://127.0.0.1:8000'

let accessToken: string | null = null
const unauthorizedListeners = new Set<() => void>()

export interface ApiRequestInit extends RequestInit {
  authenticated?: boolean
}

export function setAccessToken(token: string | null): void {
  accessToken = token
}

export function getAccessToken(): string | null {
  return accessToken
}

export function clearAccessToken(): void {
  accessToken = null
}

export function subscribeToUnauthorized(listener: () => void): () => void {
  unauthorizedListeners.add(listener)
  return () => unauthorizedListeners.delete(listener)
}

function getFallbackBaseUrl(current: string): string | null {
  if (current.includes(':8000')) {
    return current.replace(':8000', ':9000')
  }
  if (current.includes(':9000')) {
    return current.replace(':9000', ':8000')
  }
  return null
}

export async function apiFetch(
  input: RequestInfo | URL,
  init?: ApiRequestInit,
): Promise<Response> {
  const originalRequest = input instanceof Request ? input : null
  const requestUrl = originalRequest?.url ?? input.toString()
  const { authenticated = true, ...requestInit } = init ?? {}
  const headers = new Headers(originalRequest?.headers)
  new Headers(requestInit.headers).forEach((value, key) => headers.set(key, value))

  const token = authenticated ? accessToken : null
  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  const options: RequestInit = originalRequest
    ? {
        method: originalRequest.method,
        body: originalRequest.body,
        mode: originalRequest.mode,
        credentials: originalRequest.credentials,
        cache: originalRequest.cache,
        redirect: originalRequest.redirect,
        referrer: originalRequest.referrer,
        referrerPolicy: originalRequest.referrerPolicy,
        integrity: originalRequest.integrity,
        keepalive: originalRequest.keepalive,
        signal: originalRequest.signal,
        ...requestInit,
      }
    : { ...requestInit }
  if (token || originalRequest) {
    options.headers = headers
  }
  const perform = (url: string) =>
    Object.keys(options).length > 0 ? fetch(url, options) : fetch(url)

  let response: Response
  try {
    response = await perform(requestUrl)
  } catch (error) {
    const fallback = getFallbackBaseUrl(API_BASE_URL)
    if (!fallback || !requestUrl.startsWith(API_BASE_URL)) {
      throw error
    }

    const fallbackUrl = requestUrl.replace(API_BASE_URL, fallback)
    try {
      response = await perform(fallbackUrl)
      API_BASE_URL = fallback
    } catch {
      throw error
    }
  }

  if (response.status === 401 && token && accessToken === token) {
    accessToken = null
    unauthorizedListeners.forEach((listener) => listener())
  }
  return response
}
