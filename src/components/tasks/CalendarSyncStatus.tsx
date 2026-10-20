import type { TaskCalendarSync, TaskCalendarSyncStatus } from '../../api/tasks'

export const CALENDAR_SYNC_META: Record<TaskCalendarSyncStatus, { label: string; tone: string; hint: string }> = {
  pending: { label: 'Pending', tone: 'muted', hint: 'Waiting to be added to the calendar.' },
  processing: { label: 'Processing', tone: 'info', hint: 'Updating the calendar now.' },
  completed: { label: 'Completed', tone: '', hint: 'The calendar entry matches the task deadline.' },
  retrying: { label: 'Retrying', tone: 'warning', hint: 'The calendar update failed and will be tried again.' },
  failed: { label: 'Failed', tone: 'danger', hint: 'The calendar could not be updated.' },
}

/** Statuses that change on their own; the task list refreshes while any are shown. */
export const CALENDAR_SYNC_IN_FLIGHT: ReadonlySet<TaskCalendarSyncStatus> = new Set([
  'pending',
  'processing',
  'retrying',
])

function formatTime(iso: string | null | undefined): string {
  if (!iso) return ''
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(iso))
}

interface Props {
  sync: TaskCalendarSync | null | undefined
  canRetry: boolean
  retrying: boolean
  onRetry: () => void
}

/** Exactly one calendar sync status for a task deadline, with what happened. */
export function CalendarSyncStatus({ sync, canRetry, retrying, onRetry }: Props) {
  // Not picked up by the sync scheduler yet: it is waiting, i.e. Pending.
  const status: TaskCalendarSyncStatus = sync?.status ?? 'pending'
  const meta = CALENDAR_SYNC_META[status]
  return (
    <div className="task-email-state calendar-sync-state">
      <span className={`status-pill ${meta.tone}`} data-calendar-status={status} title={meta.hint}>
        {meta.label}
      </span>
      {status === 'completed' && sync?.last_synced_at && (
        <small>Synced {formatTime(sync.last_synced_at)}</small>
      )}
      {(status === 'pending' || status === 'processing') && sync?.pending_reason && (
        <small>{sync.pending_reason}</small>
      )}
      {status === 'retrying' && sync && (
        <small role="status">
          Attempt {sync.attempt_count} of {sync.max_attempts} failed
          {sync.next_attempt_at ? `; next try ${formatTime(sync.next_attempt_at)}` : ''}.
        </small>
      )}
      {(status === 'retrying' || status === 'failed') && sync?.last_error && (
        <small className="calendar-sync-error">{sync.last_error}</small>
      )}
      {sync?.external_change_detail && sync.external_change_detected_at && (
        <small className="calendar-sync-external">
          {formatTime(sync.external_change_detected_at)}: {sync.external_change_detail}
        </small>
      )}
      {status === 'failed' && canRetry && (
        <button className="text-button" type="button" disabled={retrying} onClick={onRetry}>
          {retrying ? 'Retrying...' : 'Retry calendar sync'}
        </button>
      )}
    </div>
  )
}
