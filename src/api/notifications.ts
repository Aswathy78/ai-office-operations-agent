import { API_BASE_URL, apiFetch } from './client'

export type NotificationEmailStatus =
  | 'disabled'
  | 'pending'
  | 'deferred'
  | 'sending'
  | 'retrying'
  | 'sent'
  | 'failed'

export interface UserNotification {
  id: string
  event_type: string
  summary: string
  entity_type: string | null
  entity_id: string | null
  entity_url: string | null
  in_app_status: 'delivered' | 'disabled'
  email_status: NotificationEmailStatus
  email_attempts: number
  email_next_attempt_at: string | null
  email_error: string | null
  created_at: string
  read_at: string | null
}

export interface NotificationPreferences {
  in_app_enabled: boolean
  email_enabled: boolean
  quiet_hours_start: string | null
  quiet_hours_end: string | null
  timezone_name: string
  timezone_fallback: boolean
}

export type NotificationPreferencesInput = Omit<
  NotificationPreferences,
  'timezone_fallback' | 'timezone_name'
> & {
  timezone_name: string | null
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
        : `Request failed (${response.status})`
    throw new Error(detail)
  }
  return body as T
}

const notificationsUrl = () => `${API_BASE_URL}/api/v1/notifications`

export async function getNotifications(unreadOnly = false): Promise<UserNotification[]> {
  const query = unreadOnly ? '?unread_only=true' : ''
  return readJson<UserNotification[]>(
    await apiFetch(`${notificationsUrl()}${query}`),
  )
}

export async function getUnreadNotificationCount(): Promise<number> {
  const result = await readJson<{ unread_count: number }>(
    await apiFetch(`${notificationsUrl()}/unread-count`),
  )
  return result.unread_count
}

export async function markNotificationRead(id: string): Promise<UserNotification> {
  return readJson<UserNotification>(
    await apiFetch(`${notificationsUrl()}/${encodeURIComponent(id)}/read`, {
      method: 'PATCH',
    }),
  )
}

export async function getNotificationPreferences(): Promise<NotificationPreferences> {
  return readJson<NotificationPreferences>(
    await apiFetch(`${notificationsUrl()}/preferences`),
  )
}

export async function saveNotificationPreferences(
  preferences: NotificationPreferencesInput,
): Promise<NotificationPreferences> {
  return readJson<NotificationPreferences>(
    await apiFetch(`${notificationsUrl()}/preferences`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(preferences),
    }),
  )
}
