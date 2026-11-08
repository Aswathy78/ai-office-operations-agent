import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Activity, ChevronLeft, ChevronRight, RefreshCw, X } from 'lucide-react'
import {
  getAssistantRunDetail,
  getAssistantRuns,
  type AssistantRunDetail,
  type AssistantRunStatus,
  type AssistantRunPage as AssistantRunList,
} from '../api/adminAssistantRuns'
import { AccessDenied } from '../components/common/AccessDenied'
import { useAuth } from '../contexts/AuthContext'
import {
  getBackgroundWorkflowDetail,
  getBackgroundWorkflows,
  type BackgroundWorkflowDetail,
  type BackgroundWorkflowPage,
  type WorkflowRunStatus,
} from '../api/adminWorkflowRuns'

const PAGE_SIZE = 25
const STATUS_OPTIONS: { value: AssistantRunStatus | ''; label: string }[] = [
  { value: '', label: 'All statuses' },
  { value: 'running', label: 'Running' },
  { value: 'awaiting_approval', label: 'Awaiting approval' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'skipped', label: 'Skipped' },
  { value: 'escalated', label: 'Escalated' },
  { value: 'cancelled', label: 'Cancelled' },
]
const WORKFLOW_STATUS_OPTIONS: { value: WorkflowRunStatus | ''; label: string }[] = [
  { value: '', label: 'All statuses' },
  { value: 'pending', label: 'Queued' },
  { value: 'processing', label: 'Running' },
  { value: 'retrying', label: 'Retrying' },
  { value: 'completed', label: 'Completed' },
  { value: 'failed', label: 'Failed' },
  { value: 'cancelled', label: 'Cancelled' },
]

function parseUtc(iso: string): Date {
  return new Date(/(?:Z|[+-]\d{2}:?\d{2})$/.test(iso) ? iso : `${iso}Z`)
}

function formatTime(iso: string | null): string {
  if (!iso) return '—'
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(
    parseUtc(iso),
  )
}

function formatDuration(milliseconds: number | null): string {
  if (milliseconds == null) return '—'
  if (milliseconds < 1000) return `${milliseconds} ms`
  return `${(milliseconds / 1000).toFixed(2)} s`
}

function humanize(value: string): string {
  return value.replaceAll('_', ' ')
}

function statusTone(value: string): string {
  if (value === 'failed' || value === 'cancelled') return 'danger'
  if (value === 'awaiting_approval' || value === 'running' || value === 'processing' || value === 'retrying' || value === 'escalated') return 'warning'
  if (value === 'skipped') return 'muted'
  return ''
}

function workflowStatusLabel(value: WorkflowRunStatus): string {
  if (value === 'pending') return 'queued'
  if (value === 'processing') return 'running'
  return humanize(value)
}

function usage(value: number | null): string {
  return value == null ? 'Unavailable' : value.toLocaleString()
}

function TraceData({ label, value }: { label: string; value: Record<string, unknown> | null }) {
  if (value == null) return <p className="admin-run-trace-empty">{label}: unavailable</p>
  return (
    <details className="admin-run-trace">
      <summary>{label} (redacted)</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  )
}

export function AdminAssistantRunsPage() {
  const { user } = useAuth()
  const [recordType, setRecordType] = useState<'assistant' | 'workflow'>('assistant')

  if (user?.role !== 'admin') return <AccessDenied />

  return (
    <div className="task-workspace">
      <section className="task-workspace-card admin-run-switcher" aria-label="Run record type">
        <div>
          <p className="eyebrow">Run monitor</p>
          <h2>Execution records</h2>
        </div>
        <div role="tablist" aria-label="Run record type">
          <button
            type="button"
            role="tab"
            aria-selected={recordType === 'assistant'}
            className={`button ${recordType === 'assistant' ? 'primary' : 'secondary'}`}
            onClick={() => setRecordType('assistant')}
          >
            Interactive assistant
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={recordType === 'workflow'}
            className={`button ${recordType === 'workflow' ? 'primary' : 'secondary'}`}
            onClick={() => setRecordType('workflow')}
          >
            Background workflows
          </button>
        </div>
      </section>
      {recordType === 'assistant' ? <AssistantRunsPanel /> : <BackgroundWorkflowsPanel />}
    </div>
  )
}

function AssistantRunsPanel() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [status, setStatus] = useState<AssistantRunStatus | ''>('')
  const [draftUser, setDraftUser] = useState('')
  const [userSearch, setUserSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [list, setList] = useState<AssistantRunList | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<AssistantRunDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const listRequest = useRef(0)
  const detailRequest = useRef(0)

  const load = useCallback(async () => {
    const requestId = ++listRequest.current
    setLoading(true)
    try {
      const next = await getAssistantRuns({ status, user: userSearch, limit: PAGE_SIZE, offset })
      if (requestId !== listRequest.current) return
      setList(next)
      setError('')
    } catch (reason) {
      if (requestId !== listRequest.current) return
      setError(reason instanceof Error ? reason.message : 'Assistant runs could not be loaded.')
    } finally {
      if (requestId === listRequest.current) setLoading(false)
    }
  }, [status, userSearch, offset])

  useEffect(() => {
    if (isAdmin) void load()
    return () => { listRequest.current += 1 }
  }, [isAdmin, load])

  useEffect(() => {
    if (!selectedId || !isAdmin) return
    const requestId = ++detailRequest.current
    setDetail(null)
    setDetailError('')
    setDetailLoading(true)
    void getAssistantRunDetail(selectedId).then((next) => {
      if (requestId === detailRequest.current) setDetail(next)
    }).catch((reason: unknown) => {
      if (requestId === detailRequest.current) {
        setDetailError(reason instanceof Error ? reason.message : 'Run details could not be loaded.')
      }
    }).finally(() => {
      if (requestId === detailRequest.current) setDetailLoading(false)
    })
    return () => { detailRequest.current += 1 }
  }, [selectedId, isAdmin])

  if (!isAdmin) return <AccessDenied />

  function submitSearch(event: FormEvent) {
    event.preventDefault()
    setUserSearch(draftUser.trim())
    setOffset(0)
  }

  function changeStatus(value: AssistantRunStatus | '') {
    setStatus(value)
    setOffset(0)
  }

  const runs = list?.runs ?? []
  const pageNumber = list ? Math.floor(list.offset / list.limit) + 1 : 1
  const pageCount = list ? Math.max(1, Math.ceil(list.total / list.limit)) : 1

  return (
    <div className="task-workspace">
      <section className="task-workspace-card" aria-label="Assistant run monitor">
        <div className="task-workspace-heading">
          <div className="meeting-icon" aria-hidden="true"><Activity size={21} /></div>
          <div>
            <p className="eyebrow">Administration</p>
            <h2>Assistant runs</h2>
            <p>Inspect interactive assistant execution traces and pending approvals. Trace values shown here are redacted.</p>
          </div>
        </div>
        <div className="task-workspace-actions">
          <form className="audit-search" onSubmit={submitSearch} role="search">
            <input
              type="search"
              aria-label="Search runs by user"
              placeholder="User name, email or ID"
              value={draftUser}
              maxLength={100}
              onChange={(event) => setDraftUser(event.target.value)}
            />
            <button className="button secondary" type="submit">Search</button>
          </form>
          <select
            aria-label="Run status filter"
            value={status}
            onChange={(event) => changeStatus(event.target.value as AssistantRunStatus | '')}
          >
            {STATUS_OPTIONS.map((option) => (
              <option key={option.value || 'all'} value={option.value}>{option.label}</option>
            ))}
          </select>
          <button className="button secondary" type="button" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={16} aria-hidden="true" /> Refresh
          </button>
        </div>
      </section>

      {error && (
        <div className="meetings-error" role="alert">
          <p>{list ? `Runs could not be refreshed: ${error} Showing the last data it loaded.` : error}</p>
          {!list && <button className="button secondary" type="button" onClick={() => void load()}>Try again</button>}
        </div>
      )}

      {loading && !list ? (
        <div className="meetings-loading" role="status">Loading assistant runs…</div>
      ) : runs.length === 0 ? (
        <div className="meetings-empty" role="status">
          <h3>No assistant runs</h3>
          <p>{status || userSearch ? 'Nothing matches this search or filter.' : 'No interactive runs have been recorded yet.'}</p>
        </div>
      ) : (
        <>
          <div className="task-table-wrap">
            <table className="task-table admin-run-table" aria-label="Assistant runs">
              <thead>
                <tr>
                  <th>User</th><th>Status</th><th>Started</th><th>Duration</th>
                  <th>Retries</th><th>Estimated tokens</th><th>Provider-reported tokens</th><th>Cost</th><th />
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id} className={selectedId === run.id ? 'task-row-selected' : undefined}>
                    <td><strong>{run.user_name || run.user_id || 'Unknown user'}</strong></td>
                    <td><span className={`status-pill ${statusTone(run.status)}`}>{humanize(run.status)}</span></td>
                    <td>{formatTime(run.started_at)}</td>
                    <td>{formatDuration(run.duration_ms)}</td>
                    <td>{run.retry_count}</td>
                    <td>{usage(run.estimated_tokens)} <small>estimated</small></td>
                    <td>
                      {run.provider_usage.total_tokens == null
                        ? 'Unavailable'
                        : `${usage(run.provider_usage.total_tokens)} provider-reported`}
                    </td>
                    <td>Unavailable</td>
                    <td>
                      <button
                        className="button secondary"
                        type="button"
                        aria-label={`View run ${run.id}`}
                        onClick={() => setSelectedId(run.id)}
                      >
                        Details
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="admin-run-pagination" aria-label="Run list pagination">
            <span>Page {pageNumber} of {pageCount} · {list?.total ?? 0} runs</span>
            <div>
              <button
                className="button secondary"
                type="button"
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                disabled={offset === 0 || loading}
                aria-label="Previous page"
              >
                <ChevronLeft size={16} aria-hidden="true" /> Previous
              </button>
              <button
                className="button secondary"
                type="button"
                onClick={() => setOffset(offset + PAGE_SIZE)}
                disabled={!list?.has_more || loading}
                aria-label="Next page"
              >
                Next <ChevronRight size={16} aria-hidden="true" />
              </button>
            </div>
          </div>
        </>
      )}

      {selectedId && (
        <section className="task-workspace-card admin-run-detail" aria-label="Assistant run details">
          <div className="admin-run-detail-heading">
            <div>
              <p className="eyebrow">Run detail</p>
              <h3>{selectedId}</h3>
            </div>
            <button className="icon-button" type="button" aria-label="Close run details" onClick={() => setSelectedId(null)}>
              <X size={18} aria-hidden="true" />
            </button>
          </div>
          {detailLoading && <p role="status">Loading run details…</p>}
          {detailError && <p role="alert" className="task-manage-feedback error">{detailError}</p>}
          {detail && (
            <>
              <p>
                Status <span className={`status-pill ${statusTone(detail.status)}`}>{humanize(detail.status)}</span>
                {' · '}Started {formatTime(detail.started_at)}
                {' · '}Duration {formatDuration(detail.duration_ms)}
              </p>
              <p className="admin-note">
                Retries: {detail.retry_count} · Estimated tokens: {usage(detail.estimated_tokens)} ·
                {' '}Provider-reported tokens: {usage(detail.provider_usage.total_tokens)} · Cost: unavailable
              </p>
              <h4>Steps</h4>
              {detail.steps.length === 0 ? <p>No steps were recorded for this run.</p> : (
                <ol className="admin-run-steps">
                  {detail.steps.map((step) => (
                    <li key={step.id}>
                      <div className="admin-run-step-heading">
                        <div>
                          <strong>{step.step_order}. {step.step_name}</strong>
                          <small>{humanize(step.step_type)} · {step.attempt_count} attempt(s), {step.retry_count} retries · {formatDuration(step.duration_ms)}</small>
                        </div>
                        <span className={`status-pill ${statusTone(step.status)}`}>{humanize(step.status)}</span>
                      </div>
                      <div className="admin-run-token-detail">
                        <span>Estimated tokens: {usage(step.estimated_tokens)}</span>
                        <span>Provider-reported prompt tokens: {usage(step.provider_usage.prompt_tokens)}</span>
                        <span>Provider-reported completion tokens: {usage(step.provider_usage.completion_tokens)}</span>
                        <span>Provider-reported total tokens: {usage(step.provider_usage.total_tokens)}</span>
                      </div>
                      <div className="admin-run-traces">
                        <TraceData label="Input" value={step.input} />
                        <TraceData label="Output" value={step.output} />
                      </div>
                    </li>
                  ))}
                </ol>
              )}
              <h4>Pending approvals</h4>
              {detail.pending_approvals.length === 0 ? (
                <p>No pending approvals linked to this run.</p>
              ) : (
                <ul className="admin-run-approvals">
                  {detail.pending_approvals.map((approval) => (
                    <li key={approval.id}>
                      <strong>{approval.tool_name}</strong>
                      <span className="status-pill warning">{humanize(approval.status)}</span>
                      <small>Risk: {humanize(approval.risk_level)} · Approver: {humanize(approval.approver_kind)} · Expires {formatTime(approval.expires_at)}</small>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </section>
      )}
    </div>
  )
}

function BackgroundWorkflowsPanel() {
  const { user } = useAuth()
  const isAdmin = user?.role === 'admin'
  const [status, setStatus] = useState<WorkflowRunStatus | ''>('')
  const [draftUser, setDraftUser] = useState('')
  const [userSearch, setUserSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const [list, setList] = useState<BackgroundWorkflowPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<BackgroundWorkflowDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailError, setDetailError] = useState('')
  const listRequest = useRef(0)
  const detailRequest = useRef(0)
  const detailPanel = useRef<HTMLElement | null>(null)

  const load = useCallback(async () => {
    const requestId = ++listRequest.current
    setLoading(true)
    try {
      const next = await getBackgroundWorkflows({ status, user: userSearch, limit: PAGE_SIZE, offset })
      if (requestId !== listRequest.current) return
      setList(next)
      setError('')
    } catch (reason) {
      if (requestId !== listRequest.current) return
      setError(reason instanceof Error ? reason.message : 'Background workflows could not be loaded.')
    } finally {
      if (requestId === listRequest.current) setLoading(false)
    }
  }, [status, userSearch, offset])

  useEffect(() => {
    if (isAdmin) void load()
    return () => { listRequest.current += 1 }
  }, [isAdmin, load])

  const openDetail = useCallback(async (runId: string) => {
    const requestId = ++detailRequest.current
    setSelectedId(runId)
    setDetail(null)
    setDetailError('')
    setDetailLoading(true)
    try {
      const next = await getBackgroundWorkflowDetail(runId)
      if (requestId === detailRequest.current) setDetail(next)
    } catch (reason) {
      if (requestId === detailRequest.current) {
        setDetailError(reason instanceof Error ? reason.message : 'Workflow details could not be loaded.')
      }
    } finally {
      if (requestId === detailRequest.current) setDetailLoading(false)
    }
  }, [])

  useEffect(() => {
    if (selectedId) detailPanel.current?.scrollIntoView?.({ behavior: 'smooth', block: 'start' })
  }, [selectedId])

  if (!isAdmin) return <AccessDenied />

  function submitSearch(event: FormEvent) {
    event.preventDefault()
    setUserSearch(draftUser.trim())
    setOffset(0)
  }

  const runs = list?.runs ?? []
  const pageNumber = list ? Math.floor(list.offset / list.limit) + 1 : 1
  const pageCount = list ? Math.max(1, Math.ceil(list.total / list.limit)) : 1

  return (
    <div className="task-workspace">
      <section className="task-workspace-card" aria-label="Background workflow monitor">
        <div className="task-workspace-heading">
          <div className="meeting-icon" aria-hidden="true"><Activity size={21} /></div>
          <div>
            <p className="eyebrow">Background queue</p>
            <h2>Workflow runs</h2>
            <p>Queue lifecycle and step metadata only. Checkpoints, job payloads, and internal errors are not displayed.</p>
          </div>
        </div>
        <div className="task-workspace-actions">
          <form className="audit-search" onSubmit={submitSearch} role="search">
            <input
              type="search"
              aria-label="Search workflows by user"
              placeholder="Meeting organizer name, email or ID"
              value={draftUser}
              maxLength={100}
              onChange={(event) => setDraftUser(event.target.value)}
            />
            <button className="button secondary" type="submit">Search</button>
          </form>
          <select
            aria-label="Workflow status filter"
            value={status}
            onChange={(event) => {
              setStatus(event.target.value as WorkflowRunStatus | '')
              setOffset(0)
            }}
          >
            {WORKFLOW_STATUS_OPTIONS.map((option) => (
              <option key={option.value || 'all'} value={option.value}>{option.label}</option>
            ))}
          </select>
          <button className="button secondary" type="button" onClick={() => void load()} disabled={loading}>
            <RefreshCw size={16} aria-hidden="true" /> Refresh
          </button>
        </div>
      </section>

      {error && (
        <div className="meetings-error" role="alert">
          <p>{list ? `Workflows could not be refreshed: ${error} Showing the last data it loaded.` : error}</p>
          {!list && <button className="button secondary" type="button" onClick={() => void load()}>Try again</button>}
        </div>
      )}

      {loading && !list ? (
        <div className="meetings-loading" role="status">Loading background workflows…</div>
      ) : runs.length === 0 ? (
        <div className="meetings-empty" role="status">
          <h3>No background workflows</h3>
          <p>{status || userSearch ? 'Nothing matches this search or filter.' : 'No background workflow records have been queued.'}</p>
        </div>
      ) : (
        <>
          <div className="task-table-wrap">
            <table className="task-table admin-run-table" aria-label="Background workflow runs">
              <thead>
                <tr>
                  <th>Workflow</th><th>User</th><th>Status</th><th>Queued</th>
                  <th>Duration</th><th>Retries</th><th>Attempts</th><th>Tokens</th><th>Cost</th><th />
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id} className={selectedId === run.id ? 'task-row-selected' : undefined}>
                    <td><strong>{humanize(run.workflow_type)}</strong></td>
                    <td>{run.user_name || run.user_id || 'System'}</td>
                    <td><span className={`status-pill ${statusTone(run.status)}`}>{workflowStatusLabel(run.status)}</span></td>
                    <td>{formatTime(run.queued_at)}</td>
                    <td>{formatDuration(run.duration_ms)}</td>
                    <td>{run.retry_count}</td>
                    <td>{run.attempt_count} of {run.max_attempts}</td>
                    <td>Unavailable</td>
                    <td>Unavailable</td>
                    <td>
                      <button
                        className="button secondary"
                        type="button"
                        aria-label={`View workflow ${run.id}`}
                        onClick={() => void openDetail(run.id)}
                      >
                        Details
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="admin-run-pagination" aria-label="Workflow list pagination">
            <span>Page {pageNumber} of {pageCount} · {list?.total ?? 0} workflows</span>
            <div>
              <button
                className="button secondary"
                type="button"
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                disabled={offset === 0 || loading}
                aria-label="Previous workflow page"
              >
                <ChevronLeft size={16} aria-hidden="true" /> Previous
              </button>
              <button
                className="button secondary"
                type="button"
                onClick={() => setOffset(offset + PAGE_SIZE)}
                disabled={!list?.has_more || loading}
                aria-label="Next workflow page"
              >
                Next <ChevronRight size={16} aria-hidden="true" />
              </button>
            </div>
          </div>
        </>
      )}

      {selectedId && (
        <section
          ref={detailPanel}
          className="task-workspace-card admin-run-detail"
          aria-label="Background workflow details"
          tabIndex={-1}
        >
          <div className="admin-run-detail-heading">
            <div><p className="eyebrow">Background workflow</p><h3>{selectedId}</h3></div>
            <button
              className="icon-button"
              type="button"
              aria-label="Close workflow details"
              onClick={() => {
                detailRequest.current += 1
                setSelectedId(null)
                setDetail(null)
                setDetailError('')
                setDetailLoading(false)
              }}
            >
              <X size={18} aria-hidden="true" />
            </button>
          </div>
          {detailLoading && <p role="status">Loading workflow details…</p>}
          {detailError && <p role="alert" className="task-manage-feedback error">{detailError}</p>}
          {detail && (
            <>
              <p>
                Status <span className={`status-pill ${statusTone(detail.status)}`}>{workflowStatusLabel(detail.status)}</span>
                {' · '}Queued {formatTime(detail.queued_at)}
                {' · '}Duration {formatDuration(detail.duration_ms)}
              </p>
              <p className="admin-note">
                Retries: {detail.retry_count} · Attempts: {detail.attempt_count} of {detail.max_attempts} ·
                {' '}Token usage: unavailable · Cost: unavailable
              </p>
              <h4>Steps</h4>
              {detail.steps.length === 0 ? <p>No steps were recorded for this workflow.</p> : (
                <ol className="admin-run-steps">
                  {detail.steps.map((step) => (
                    <li key={step.id}>
                      <div className="admin-run-step-heading">
                        <div>
                          <strong>{step.step_order}. {humanize(step.step_name)}</strong>
                          <small>{step.attempt_count} attempt(s), {step.retry_count} retries · {formatDuration(step.duration_ms)}</small>
                        </div>
                        <span className={`status-pill ${statusTone(step.status)}`}>{humanize(step.status)}</span>
                      </div>
                    </li>
                  ))}
                </ol>
              )}
              <h4>Approvals</h4>
              <p>No approval links are recorded for background workflow runs.</p>
            </>
          )}
        </section>
      )}
    </div>
  )
}
