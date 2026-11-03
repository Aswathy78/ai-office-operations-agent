import { useCallback, useEffect, useState } from 'react'
import { AlertTriangle, Clock, ExternalLink } from 'lucide-react'
import { getTaskEscalations, type EscalationRecord } from '../../api/tasks'
import { usePolling } from '../../hooks/usePolling'

function formatDateTime(iso: string | null): string {
  if (!iso) return ''
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(iso))
}

function formatOverdueDuration(hours: number): string {
  if (hours <= 0) return 'Just overdue'
  const days = Math.floor(hours / 24)
  const remainingHours = hours % 24
  if (days === 0) return `${hours}h overdue`
  if (remainingHours === 0) return `${days}d overdue`
  return `${days}d ${remainingHours}h overdue`
}

export function EscalationPanel() {
  const [escalations, setEscalations] = useState<EscalationRecord[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    try {
      const data = await getTaskEscalations()
      setEscalations(data)
      setError('')
    } catch (reason) {
      setError(
        reason instanceof Error
          ? reason.message
          : 'Escalated tasks could not be loaded.',
      )
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  // Poll every 15 seconds
  usePolling(load, 15000, true)

  const count = escalations?.length ?? 0

  return (
    <section
      className="task-workspace-card escalation-panel"
      aria-labelledby="escalation-heading"
      style={{ marginBottom: '1.5rem' }}
    >
      <div className="task-workspace-heading">
        <div
          className="meeting-icon"
          aria-hidden="true"
          style={{ background: '#fef2f2', color: '#dc2626' }}
        >
          <AlertTriangle size={21} />
        </div>
        <div>
          <p className="eyebrow" style={{ color: '#dc2626' }}>
            Escalations
          </p>
          <h2 id="escalation-heading">Overdue Task Escalations</h2>
          <p>
            {loading && escalations === null
              ? 'Checking for escalations…'
              : count === 0
                ? 'No overdue task escalations. Your team is on track.'
                : `${count} task${count === 1 ? '' : 's'} escalated due to missed reminders. Updates automatically.`}
          </p>
        </div>
      </div>

      {error && <p className="task-manage-feedback error">{error}</p>}

      {!loading && count === 0 && !error && (
        <p className="task-manage-hint">
          Tasks are automatically escalated here when an assignee misses multiple reminder windows.
        </p>
      )}

      {count > 0 && (
        <div className="task-table-wrap">
          <table className="task-table" aria-label="Escalated tasks table">
            <thead>
              <tr>
                <th>Task Title</th>
                <th>Assigned Employee</th>
                <th>Original Deadline</th>
                <th>Overdue Duration</th>
                <th>Missed Reminders</th>
                <th>Escalated At</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {escalations!.map((item) => (
                <tr key={item.task_id} data-testid={`escalation-row-${item.task_id}`}>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                      <a
                        href={`/tasks?task_id=${encodeURIComponent(item.task_id)}`}
                        style={{ fontWeight: 600, color: 'inherit', textDecoration: 'underline' }}
                        title="View task details"
                      >
                        {item.task_title}
                      </a>
                      <ExternalLink size={13} aria-hidden="true" style={{ opacity: 0.6 }} />
                    </div>
                    <span className="task-manage-hint" style={{ fontSize: '0.75rem' }}>
                      ID: {item.task_id.slice(0, 8)}...
                    </span>
                  </td>
                  <td>
                    <div>
                      <strong>{item.assignee_name}</strong>
                    </div>
                    <span className="task-manage-hint" style={{ fontSize: '0.8rem' }}>
                      {item.assignee_email}
                    </span>
                  </td>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '0.35rem' }}>
                      <Clock size={14} aria-hidden="true" />
                      <span>{formatDateTime(item.due_at)}</span>
                    </div>
                  </td>
                  <td>
                    <span
                      style={{
                        display: 'inline-block',
                        padding: '0.2rem 0.5rem',
                        borderRadius: '4px',
                        background: '#fee2e2',
                        color: '#991b1b',
                        fontWeight: 600,
                        fontSize: '0.85rem',
                      }}
                    >
                      {formatOverdueDuration(item.overdue_hours)}
                    </span>
                  </td>
                  <td>
                    <span
                      style={{
                        display: 'inline-block',
                        padding: '0.2rem 0.5rem',
                        borderRadius: '4px',
                        background: '#fef3c7',
                        color: '#92400e',
                        fontWeight: 600,
                        fontSize: '0.85rem',
                      }}
                    >
                      {item.missed_reminders} missed
                    </span>
                  </td>
                  <td>
                    <span style={{ fontSize: '0.85rem' }}>
                      {formatDateTime(item.escalation_timestamp)}
                    </span>
                  </td>
                  <td>
                    <span
                      className={`status-pill status-${item.escalation_status}`}
                      style={{
                        display: 'inline-block',
                        padding: '0.2rem 0.5rem',
                        borderRadius: '9999px',
                        textTransform: 'capitalize',
                        fontSize: '0.8rem',
                        fontWeight: 500,
                        background:
                          item.escalation_status === 'sent'
                            ? '#dcfce7'
                            : item.escalation_status === 'failed'
                              ? '#fee2e2'
                              : '#f1f5f9',
                        color:
                          item.escalation_status === 'sent'
                            ? '#166534'
                            : item.escalation_status === 'failed'
                              ? '#991b1b'
                              : '#475569',
                      }}
                    >
                      {item.escalation_status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

