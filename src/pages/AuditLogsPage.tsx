import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { RefreshCw, ScrollText } from 'lucide-react'
import { getAuditLog, type AuditEntry, type AuditLog, type AuditSource } from '../api/adminAudit'
import { AccessDenied } from '../components/common/AccessDenied'
import { useAuth } from '../contexts/AuthContext'
import { usePolling } from '../hooks/usePolling'

export const AUDIT_REFRESH_MS = 15_000
const PAGE_SIZE = 200

const SOURCES: AuditSource[] = ['task', 'meeting', 'assistant', 'email', 'workflow']

function parseUtc(iso: string): Date {
  return new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`)
}

function formatTime(iso: string): string {
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(parseUtc(iso))
}

function humanize(value: string): string {
  return value.replaceAll('_', ' ')
}

function who(entry: AuditEntry): string {
  const name = entry.actor_name ?? entry.actor_id ?? 'System'
  return name.toLowerCase() === 'system' ? 'System' : name
}

function outcomeTone(outcome: string | null): string {
  if (!outcome) return 'muted'
  if (['denied', 'failed', 'high', 'conflict_blocked'].includes(outcome)) return 'danger'
  if (['medium', 'retry', 'system_event'].includes(outcome)) return 'warning'
  return ''
}

export function AuditLogsPage() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [source, setSource] = useState<AuditSource | ''>('')
  const [draft, setDraft] = useState('')
  const [search, setSearch] = useState('')
  const [log, setLog] = useState<AuditLog | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const requestId = useRef(0)

  const load = useCallback(async () => {
    const mine = ++requestId.current
    try {
      const next = await getAuditLog({ source, q: search, limit: PAGE_SIZE })
      if (mine !== requestId.current) return
      setLog(next)
      setError('')
    } catch (reason) {
      if (mine !== requestId.current) return
      setError(reason instanceof Error ? reason.message : 'The audit log could not be loaded.')
    } finally {
      if (mine === requestId.current) setLoading(false)
    }
  }, [source, search])

  useEffect(() => {
    if (isAdmin) void load()
  }, [isAdmin, load])

  usePolling(load, AUDIT_REFRESH_MS, isAdmin)

  if (!isAdmin) return <AccessDenied />

  function submitSearch(event: FormEvent) {
    event.preventDefault()
    setSearch(draft)
  }

  const entries = log?.entries ?? []
  const total = log ? Object.values(log.counts).reduce((sum, count) => sum + count, 0) : 0

  return (
    <div className="task-workspace">
      <section className="task-workspace-card" aria-label="Audit log">
        <div className="task-workspace-heading">
          <div className="meeting-icon" aria-hidden="true"><ScrollText size={21} /></div>
          <div>
            <p className="eyebrow">Administration</p>
            <h2>Audit logs</h2>
            <p>
              Who did what across tasks, meetings, the AI assistant, e-mail and workflows, newest first.
              Routine workflow step completions are left out. Read-only, and refreshed automatically.
            </p>
          </div>
        </div>
        <div className="task-workspace-actions">
          <form className="audit-search" onSubmit={submitSearch} role="search">
            <input
              type="search"
              aria-label="Search the audit log"
              placeholder="Search action or detail"
              value={draft}
              maxLength={100}
              onChange={(event) => setDraft(event.target.value)}
            />
            <button className="button secondary" type="submit">Search</button>
          </form>
          <select
            aria-label="Audit source filter"
            value={source}
            onChange={(event) => setSource(event.target.value as AuditSource | '')}
          >
            <option value="">All sources{log ? ` (${total})` : ''}</option>
            {SOURCES.map((key) => (
              <option key={key} value={key}>
                {log?.source_labels[key] ?? key}
                {log ? ` (${log.counts[key]})` : ''}
              </option>
            ))}
          </select>
          <button className="button secondary" type="button" onClick={() => void load()}>
            <RefreshCw size={16} aria-hidden="true" /> Refresh
          </button>
        </div>
      </section>

      {error && (
        <div className="meetings-error" role="alert">
          <p>{log ? `The audit log could not be refreshed: ${error} Showing the last data it loaded.` : error}</p>
          {!log && (
            <button className="button secondary" type="button" onClick={() => void load()}>Try again</button>
          )}
        </div>
      )}

      {loading && !log && !error ? (
        <div className="meetings-loading" role="status">Loading the audit log…</div>
      ) : log && entries.length === 0 ? (
        <div className="meetings-empty" role="status">
          <h3>No entries</h3>
          <p>{search || source ? 'Nothing matches this search or filter.' : 'No activity has been recorded yet.'}</p>
        </div>
      ) : (
        log && (
          <>
            <div className="task-table-wrap">
              <table className="task-table" aria-label="Audit entries">
                <thead>
                  <tr>
                    <th>When</th><th>Who</th><th>Action</th><th>Outcome</th><th>Details</th>
                  </tr>
                </thead>
                <tbody>
                  {entries.map((entry) => (
                    <tr key={entry.id}>
                      <td className="audit-when">{formatTime(entry.at)}</td>
                      <td className="audit-who">
                        <strong>{who(entry)}</strong>
                        {entry.actor_role && entry.actor_role.toLowerCase() !== who(entry).toLowerCase() && (
                          <small>{humanize(entry.actor_role)}</small>
                        )}
                      </td>
                      <td className="audit-action">
                        <strong>{entry.action_label}</strong>
                        <small>
                          {log.source_labels[entry.source] ?? entry.source}
                          {entry.target ? ` · ${entry.target}` : ''}
                        </small>
                      </td>
                      <td>
                        {entry.outcome ? (
                          <span className={`status-pill ${outcomeTone(entry.outcome)}`}>{humanize(entry.outcome)}</span>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td className="audit-detail" title={entry.detail}>
                        <span>{entry.detail || '—'}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {total > entries.length && source === '' && !search && (
              <p className="admin-note">Showing the latest {entries.length} entries.</p>
            )}
          </>
        )
      )}
    </div>
  )
}
