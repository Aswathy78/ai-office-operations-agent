import { useState } from 'react'
import { History } from 'lucide-react'
import {
  getEmailDraftEvents,
  type EmailDraft,
  type EmailDraftEvent,
  type EmailDraftStatus,
  type EmailFailureNotice,
} from '../../api/emailDrafts'
import type { TaskActor } from '../../api/tasks'

export const EMAIL_STATUS_META: Record<EmailDraftStatus, { label: string; tone: string; hint: string }> = {
  draft: { label: 'Draft', tone: 'muted', hint: 'Not sent. You can still edit it.' },
  approval: { label: 'Approval', tone: 'warning', hint: 'Waiting for a manager, HR / Operations or admin.' },
  queued: { label: 'Queued', tone: 'info', hint: 'Waiting to be handed to the mail provider.' },
  sending: { label: 'Sending', tone: 'info', hint: 'Being handed to the mail provider now.' },
  sent: { label: 'Sent', tone: '', hint: 'Accepted by the mail provider for every recipient.' },
  failed: { label: 'Failed', tone: 'danger', hint: 'Not delivered to every recipient. You can retry.' },
  retry: { label: 'Retry', tone: 'warning', hint: 'A delivery attempt failed; it will be retried automatically.' },
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return ''
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'medium' }).format(new Date(iso))
}

/** The e-mail's single current lifecycle status. */
export function EmailStatusPill({ status }: { status: EmailDraftStatus }) {
  const meta = EMAIL_STATUS_META[status]
  return (
    <span className={`status-pill email-status ${meta.tone}`} data-status={status} title={meta.hint}>
      {meta.label}
    </span>
  )
}

/**
 * Draft → (Approval) → Queued → Sending → Sent, with Failed / Retry shown in place of
 * Sent when delivery went wrong. Exactly one step is marked current.
 */
export function EmailLifecycleTracker({ draft }: { draft: EmailDraft }) {
  const ending: EmailDraftStatus = draft.status === 'failed' || draft.status === 'retry' ? draft.status : 'sent'
  const steps: EmailDraftStatus[] = [
    'draft',
    ...(draft.approval_required ? (['approval'] as EmailDraftStatus[]) : []),
    'queued',
    'sending',
    ending,
  ]
  const currentIndex = steps.indexOf(draft.status)

  return (
    <ol className="email-lifecycle" aria-label="Delivery status">
      {steps.map((step, index) => {
        const state = index === currentIndex ? 'current' : index < currentIndex ? 'done' : 'todo'
        return (
          <li
            key={step}
            className={`email-lifecycle-step ${state} ${EMAIL_STATUS_META[step].tone}`}
            aria-current={state === 'current' ? 'step' : undefined}
          >
            <span className="email-lifecycle-dot" aria-hidden="true" />
            {EMAIL_STATUS_META[step].label}
          </li>
        )
      })}
    </ol>
  )
}

/** One-line explanation of where the e-mail is and what happens next. */
export function EmailStatusDetail({ draft }: { draft: EmailDraft }) {
  const total = draft.recipient_count
  const accepted = draft.delivery_results.filter((result) => result.status === 'sent').length
  const viaFallback = draft.delivery_results.some((result) => result.status === 'sent' && result.fallback_used)
  let text = EMAIL_STATUS_META[draft.status].hint
  if (draft.status === 'draft' && draft.approval_decision === 'approved') text = 'Approved — ready to send.'
  if (draft.status === 'draft' && draft.approval_decision === 'rejected') text = 'Approval was rejected. Edit it and request approval again.'
  if (draft.status === 'sent') {
    text = `Accepted by the mail provider for all ${total} recipient${total === 1 ? '' : 's'} at ${formatTime(draft.sent_at)}` +
      (viaFallback ? ' (using the backup mail provider).' : '.')
  }
  if (draft.status === 'retry') {
    text = `Attempt ${draft.attempt_count} of ${draft.max_attempts} failed` +
      (draft.next_attempt_at ? `; retrying automatically at ${formatTime(draft.next_attempt_at)}.` : '; retrying now.')
  }
  if (draft.status === 'failed') {
    text = `Failed after ${draft.attempt_count} attempt${draft.attempt_count === 1 ? '' : 's'}: ` +
      `delivered to ${accepted} of ${total} recipient${total === 1 ? '' : 's'}.` +
      (draft.escalated_at ? ' You and the administrator have been notified.' : '')
  }
  return (
    <p className={`email-status-detail ${EMAIL_STATUS_META[draft.status].tone}`} role="status">
      {text}
      {(draft.status === 'failed' || draft.status === 'retry') && draft.last_error && (
        <span className="email-status-error"> {draft.last_error}</span>
      )}
    </p>
  )
}

/** Bounce reports recorded on the e-mail (rejections at send time and later bounce notices). */
export function EmailBounces({ draft }: { draft: EmailDraft }) {
  if (!draft.bounces?.length) return null
  return (
    <section className="email-draft-alert danger" aria-label="Bounces">
      <div>
        <strong>Bounces</strong>
        <ul>
          {draft.bounces.map((bounce, index) => (
            <li key={`${bounce.recipient}-${index}`}>
              <span className={`email-severity ${bounce.type === 'hard' ? 'high' : 'medium'}`}>{bounce.type}</span>{' '}
              {bounce.recipient}
              {bounce.code ? ` · ${bounce.code}` : ''}
              {bounce.diagnostic ? ` · ${bounce.diagnostic}` : ''}
              <span className="task-manage-hint"> ({bounce.source}, {formatTime(bounce.recorded_at)})</span>
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}

const NOTICE_LABEL: Record<EmailFailureNotice['status'], string> = {
  pending: 'sending',
  sent: 'notified',
  failed: 'notification failed — will retry',
  recorded: 'shown on admin dashboard',
}

/** Who was told the e-mail failed, and whether that message got through. */
export function EmailFailureNotices({ draft }: { draft: EmailDraft }) {
  if (!draft.notifications?.length) return null
  const latest = Math.max(...draft.notifications.map((n) => n.failure_event_sequence))
  const notices = draft.notifications.filter((n) => n.failure_event_sequence === latest)
  return (
    <section className="email-escalation" aria-label="Failure notifications">
      <strong>Escalated {formatTime(draft.escalated_at)}</strong>
      <ul>
        {notices.map((notice) => (
          <li key={notice.id} className={`email-notice ${notice.status}`}>
            {notice.audience === 'requester' ? 'Requester' : 'Administrator'}
            {notice.recipient_email ? ` <${notice.recipient_email}>` : ' (in-app)'}: {NOTICE_LABEL[notice.status]}
            {notice.status === 'failed' && notice.error ? ` (${notice.error})` : ''}
          </li>
        ))}
      </ul>
    </section>
  )
}

/** Every status change, loaded on demand. */
export function EmailHistory({ draft, actor }: { draft: EmailDraft; actor: TaskActor }) {
  const [open, setOpen] = useState(false)
  const [events, setEvents] = useState<EmailDraftEvent[] | null>(null)
  const [error, setError] = useState('')

  async function toggle() {
    const next = !open
    setOpen(next)
    if (!next) return
    try {
      setEvents(await getEmailDraftEvents(draft.id, actor))
      setError('')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'History could not be loaded.')
    }
  }

  return (
    <div className="email-history">
      <button className="text-button" type="button" aria-expanded={open} onClick={() => void toggle()}>
        <History size={14} aria-hidden="true" /> {open ? 'Hide history' : 'Show history'}
      </button>
      {open && error && <p className="task-manage-feedback error">{error}</p>}
      {open && events && (
        <ol className="email-history-list">
          {events.map((event) => (
            <li key={event.id}>
              <time dateTime={event.created_at}>{formatTime(event.created_at)}</time>{' '}
              {event.from_status ? `${EMAIL_STATUS_META[event.from_status].label} → ` : ''}
              <strong>{EMAIL_STATUS_META[event.to_status].label}</strong>
              {event.detail ? ` — ${event.detail}` : ''}
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
