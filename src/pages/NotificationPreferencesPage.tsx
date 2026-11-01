import { useEffect, useState } from 'react'
import { Bell, Save } from 'lucide-react'
import {
  getNotificationPreferences,
  saveNotificationPreferences,
  type NotificationPreferences,
} from '../api/notifications'

export function NotificationPreferencesPage() {
  const [preferences, setPreferences] = useState<NotificationPreferences | null>(null)
  const [timezoneName, setTimezoneName] = useState('UTC')
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    let active = true
    void getNotificationPreferences()
      .then((result) => {
        if (!active) return
        setPreferences(result)
        setTimezoneName(result.timezone_name)
      })
      .catch((reason: unknown) => {
        if (active) {
          setError(reason instanceof Error ? reason.message : 'Preferences could not be loaded.')
        }
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => { active = false }
  }, [])

  function update(patch: Partial<NotificationPreferences>) {
    setPreferences((current) => current ? { ...current, ...patch } : current)
    setSaved(false)
  }

  async function save() {
    if (!preferences) return
    if (
      Boolean(preferences.quiet_hours_start) !== Boolean(preferences.quiet_hours_end) ||
      (preferences.quiet_hours_start && preferences.quiet_hours_start === preferences.quiet_hours_end)
    ) {
      setError('Set two different quiet-hours times, or clear both times to turn quiet hours off.')
      return
    }
    setSaving(true)
    setError('')
    setSaved(false)
    try {
      const result = await saveNotificationPreferences({
        in_app_enabled: preferences.in_app_enabled,
        email_enabled: preferences.email_enabled,
        quiet_hours_start: preferences.quiet_hours_start || null,
        quiet_hours_end: preferences.quiet_hours_end || null,
        timezone_name: timezoneName.trim() || null,
      })
      setPreferences(result)
      setTimezoneName(result.timezone_name)
      setSaved(true)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Preferences could not be saved.')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="page-stack">
      <section className="task-workspace-card notification-page-heading">
        <div>
          <p className="eyebrow">Settings</p>
          <h2>Notification preferences</h2>
          <p>Choose your notification channels and local quiet hours.</p>
        </div>
        <Bell size={24} aria-hidden="true" />
      </section>

      {error && <p className="task-manage-feedback error" role="alert">{error}</p>}
      {saved && <p className="task-manage-feedback success" role="status">Preferences saved.</p>}
      {loading ? (
        <div className="meetings-loading" role="status">Loading preferences…</div>
      ) : preferences && (
        <section className="task-workspace-card notification-preferences-card">
          <div className="notification-preference-options">
            <label className="notification-preference-toggle">
              <input
                type="checkbox"
                checked={preferences.in_app_enabled}
                onChange={(event) => update({ in_app_enabled: event.target.checked })}
              />
              <span>
                <strong>In-app notifications</strong>
                <small>Show event updates in your inbox and unread count.</small>
              </span>
            </label>
            <label className="notification-preference-toggle">
              <input
                type="checkbox"
                checked={preferences.email_enabled}
                onChange={(event) => update({ email_enabled: event.target.checked })}
              />
              <span>
                <strong>Email notifications</strong>
                <small>Send notification emails and retry transient delivery failures.</small>
              </span>
            </label>
          </div>

          <div className="notification-quiet-hours">
            <h3>Quiet hours</h3>
            <p>
              Email due during this interval is deferred, not discarded. End times follow the
              selected IANA timezone across daylight-saving changes. If no employee timezone is
              set, the system safely uses UTC.
            </p>
            <label htmlFor="notification-timezone">Time zone</label>
            <input
              id="notification-timezone"
              type="text"
              value={timezoneName}
              onChange={(event) => {
                setTimezoneName(event.target.value)
                setSaved(false)
              }}
              placeholder="Asia/Kolkata"
              autoComplete="off"
            />
            {preferences.timezone_fallback && (
              <small className="task-manage-hint">
                No employee timezone is configured; quiet hours currently use UTC.
              </small>
            )}
            <div className="notification-time-grid">
              <label>
                Quiet hours start
                <input
                  type="time"
                  value={preferences.quiet_hours_start ?? ''}
                  onChange={(event) => update({ quiet_hours_start: event.target.value || null })}
                />
              </label>
              <label>
                Quiet hours end
                <input
                  type="time"
                  value={preferences.quiet_hours_end ?? ''}
                  onChange={(event) => update({ quiet_hours_end: event.target.value || null })}
                />
              </label>
            </div>
            <small className="task-manage-hint">
              Quiet periods may cross midnight, for example 22:00 to 07:00. Clear both times to
              disable quiet hours.
            </small>
          </div>
          <button className="button primary" type="button" onClick={() => void save()} disabled={saving}>
            <Save size={16} aria-hidden="true" /> {saving ? 'Saving…' : 'Save preferences'}
          </button>
        </section>
      )}
    </div>
  )
}
