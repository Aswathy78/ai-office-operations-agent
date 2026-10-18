import { useCallback, useEffect, useState } from 'react'
import { MailWarning } from 'lucide-react'
import { EMAIL_STATUS_POLL_MS, getEmailDeliveryIssues, type EmailDraft } from '../../api/emailDrafts'
import type { TaskActor } from '../../api/tasks'
import { usePolling } from '../../hooks/usePolling'
import { EmailStatusPill } from './EmailLifecycle'

function formatTime(iso: string | null): string {
  if (!iso) return ''
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(iso))
}

/** Manager / HR / admin dashboard: every e-mail currently in Failed or Retry. */
export function EmailDeliveryPanel({ actor }: { actor: TaskActor }) {
  const [issues, setIssues] = useState<EmailDraft[] | null>(null)
  const [error, setError] = useState('')
  const { id, role, team } = actor

  const load = useCallback(async () => {
    try {
      setIssues(await getEmailDeliveryIssues({ id, role, team }))
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'E-mail delivery status could not be loaded.')
    }
  }, [id, role, team])

  useEffect(() => {
    void load()
  }, [load])

  // Failed / Retry changes reach the dashboard within 5 seconds.
  usePolling(load, EMAIL_STATUS_POLL_MS, true)

  const failed = issues?.filter((draft) => draft.status === 'failed').length ?? 0
  const retrying = issues?.filter((draft) => draft.status === 'retry').length ?? 0

  return (
    <section className="task-workspace-card email-delivery-panel" aria-labelledby="email-delivery-heading">
      <div className="task-workspace-heading">
        <div className="meeting-icon" aria-hidden="true"><MailWarning size={21} /></div>
        <div>
          <p className="eyebrow">E-mail delivery</p>
          <h2 id="email-delivery-heading">Failed and retrying e-mails</h2>
          <p>
            {issues === null
              ? 'Loading…'
              : `${failed} failed · ${retrying} retrying. Updates automatically.`}
          </p>
        </div>
      </div>
      {error && <p className="task-manage-feedback error">{error}</p>}
      {issues && issues.length === 0 && !error && (
        <p className="task-manage-hint">No e-mails are failing or waiting to retry.</p>
      )}
      {issues && issues.length > 0 && (
        <ul className="email-delivery-list">
          {issues.map((draft) => (
            <li key={draft.id}>
              <div className="email-delivery-row">
                <EmailStatusPill status={draft.status} />
                <strong>{draft.subject || '(no subject)'}</strong>
                <span className="task-manage-hint">
                  from {draft.created_by_name} · {draft.recipient_count} recipient{draft.recipient_count === 1 ? '' : 's'}
                  {' · '}attempt {draft.attempt_count} of {draft.max_attempts}
                  {draft.status === 'retry' && draft.next_attempt_at ? ` · next try ${formatTime(draft.next_attempt_at)}` : ''}
                  {draft.status === 'failed' && draft.status_changed_at ? ` · failed ${formatTime(draft.status_changed_at)}` : ''}
                </span>
              </div>
              {draft.last_error && <p className="email-status-error">{draft.last_error}</p>}
              {draft.bounces.length > 0 && (
                <p className="task-manage-hint">
                  Bounces: {draft.bounces.map((b) => `${b.recipient} (${b.type})`).join(', ')}
                </p>
              )}
              {draft.status === 'failed' && (
                <p className="task-manage-hint">
                  {draft.escalated_at
                    ? `Escalated ${formatTime(draft.escalated_at)} · ` +
                      draft.notifications
                        .filter((n) => n.channel === 'email')
                        .map((n) => `${n.audience} ${n.status === 'sent' ? 'notified' : n.status}`)
                        .join(', ')
                    : 'Not yet escalated'}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
