import { useEffect, useState } from 'react'
import { useNavigate, useParams, Link, useSearchParams } from 'react-router-dom'
import { ArrowLeft, Clock, Users, FileText, FileCheck } from 'lucide-react'
import {
  approveMeetingMinutes,
  confirmActionItemOwner,
  extractMeetingActionItems,
  generateMeetingMinutes,
  getActiveEmployees,
  getMeetingActionItems,
  getMeetingMinutes,
  getMeetingReport,
  getMeetingSummary,
  MeetingApiError,
  refreshMeetingMinutes,
  reviewActionItem,
  sendMeetingMinutes,
  updateMeetingMinutes,
  type ActionItemReviewPayload,
} from '../api/meetings'
import { TranscriptPanel } from '../components/meetings/TranscriptPanel'
import { MeetingSummaryPanel } from '../components/meetings/MeetingSummaryPanel'
import { MeetingActionItemsPanel } from '../components/meetings/MeetingActionItemsPanel'
import { MeetingMinutesPanel } from '../components/meetings/MeetingMinutesPanel'
import { MeetingWorkflowStatus } from '../components/meetings/MeetingWorkflowStatus'
import { useAuth } from '../contexts/AuthContext'
import type {
  MeetingActionItem,
  MeetingActionItems,
  Employee,
  MeetingMinutes,
  MeetingMinutesUpdateRequest,
  MeetingReport,
  MeetingSummary,
} from '../types/meeting'

export function MeetingReportPage() {
  const { meetingId = '' } = useParams()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const { user } = useAuth()
  const [report, setReport] = useState<MeetingReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [sourceFocusRequestId, setSourceFocusRequestId] = useState(0)
  const sourceSegmentParam = searchParams.get('segment')
  const sourceSegmentIndex =
    sourceSegmentParam !== null &&
    /^\d+$/.test(sourceSegmentParam) &&
    Number.isSafeInteger(Number(sourceSegmentParam))
      ? Number(sourceSegmentParam)
      : null
  const sourceTranscriptId = searchParams.get('transcript_id')
  const [transcriptOpen, setTranscriptOpen] = useState(sourceSegmentIndex !== null)
  const [summaryOpen, setSummaryOpen] = useState(false)
  const [actionItems, setActionItems] = useState<MeetingActionItems | null>(null)
  const [actionItemsLoading, setActionItemsLoading] = useState(true)
  const [actionItemsTriggering, setActionItemsTriggering] = useState(false)
  const [actionItemsError, setActionItemsError] = useState('')
  const [employees, setEmployees] = useState<Employee[]>([])
  const [employeesLoading, setEmployeesLoading] = useState(false)
  const [employeesError, setEmployeesError] = useState('')
  const [confirmingActionItemId, setConfirmingActionItemId] = useState<string | null>(null)
  const [ownerConfirmationFeedback, setOwnerConfirmationFeedback] = useState<{
    actionItemId: string
    kind: 'success' | 'error'
    message: string
  } | null>(null)
  const [meetingSummary, setMeetingSummary] = useState<MeetingSummary | null>(null)
  const [summaryLoading, setSummaryLoading] = useState(true)
  const [summaryError, setSummaryError] = useState('')
  const [minutesOpen, setMinutesOpen] = useState(false)
  const [meetingMinutes, setMeetingMinutes] = useState<MeetingMinutes | null>(null)
  const [minutesLoading, setMinutesLoading] = useState(false)
  const [minutesGenerating, setMinutesGenerating] = useState(false)
  const [minutesApproving, setMinutesApproving] = useState(false)
  const [minutesSending, setMinutesSending] = useState(false)
  const [minutesRetrying, setMinutesRetrying] = useState(false)
  const [minutesUpdating, setMinutesUpdating] = useState(false)
  const [minutesRefreshingContent, setMinutesRefreshingContent] = useState(false)
  const [minutesError, setMinutesError] = useState('')
  const [minutesFeedback, setMinutesFeedback] = useState<{
    kind: 'success' | 'error'
    message: string
  } | null>(null)

  useEffect(() => {
    if (sourceSegmentIndex !== null) setTranscriptOpen(true)
  }, [sourceSegmentIndex])

  useEffect(() => {
    let active = true
    if (!meetingId || !user) return

    setLoading(true)
    const reportRequest = user.email
      ? getMeetingReport(meetingId, user.id, user.email)
      : getMeetingReport(meetingId, user.id)
    reportRequest
      .then((data) => {
        if (active) setReport(data)
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : 'Failed to load report')
      })
      .finally(() => {
        if (active) setLoading(false)
      })

    return () => { active = false }
  }, [meetingId, user])

  const isOrganizer = Boolean(user && report && user.id === report.organizer_id)

  useEffect(() => {
    let active = true
    if (!isOrganizer) {
      setEmployees([])
      setEmployeesError('')
      setEmployeesLoading(false)
      return
    }

    void loadActiveEmployees(() => active)

    return () => { active = false }
  }, [isOrganizer])

  function loadActiveEmployees(isActive: () => boolean = () => true) {
    setEmployeesLoading(true)
    setEmployeesError('')
    return getActiveEmployees()
      .then((activeEmployees) => {
        if (isActive()) setEmployees(activeEmployees)
      })
      .catch((reason: unknown) => {
        if (isActive()) {
          setEmployeesError(
            reason instanceof Error ? reason.message : 'Active employees could not be loaded.',
          )
        }
      })
      .finally(() => {
        if (isActive()) setEmployeesLoading(false)
      })
  }

  function handleRefreshEmployees() {
    if (isOrganizer) void loadActiveEmployees()
  }

  useEffect(() => {
    let active = true
    if (!meetingId || !user) return
    if (!isOrganizer) {
      setActionItems(null)
      setActionItemsError('')
      setActionItemsLoading(false)
      return
    }

    setActionItemsLoading(true)
    setActionItemsError('')
    getMeetingActionItems(meetingId, user.id)
      .then((result) => {
        if (active) setActionItems(result)
      })
      .catch((reason: unknown) => {
        if (!active) return
        if (reason instanceof MeetingApiError && reason.status === 404) {
          setActionItems(null)
          return
        }
        setActionItemsError(
          reason instanceof Error ? reason.message : 'Meeting action items could not be loaded.',
        )
      })
      .finally(() => {
        if (active) setActionItemsLoading(false)
      })

    return () => { active = false }
  }, [isOrganizer, meetingId, user])

  useEffect(() => {
    let active = true
    if (!summaryOpen || !meetingId || !user) return

    setSummaryLoading(true)
    setMeetingSummary(null)
    setSummaryError('')
    getMeetingSummary(meetingId, user.id)
      .then((summary) => {
        if (active) setMeetingSummary(summary)
      })
      .catch((reason: unknown) => {
        if (!active) return
        if (reason instanceof MeetingApiError && reason.status === 404) {
          setMeetingSummary(null)
          return
        }
        setSummaryError(
          reason instanceof Error ? reason.message : 'Meeting summary could not be loaded.',
        )
      })
      .finally(() => {
        if (active) setSummaryLoading(false)
      })

    return () => { active = false }
  }, [meetingId, summaryOpen, user])

  useEffect(() => {
    let active = true
    if (!minutesOpen || !meetingId || !user || !isOrganizer) return

    setMinutesLoading(true)
    setMeetingMinutes(null)
    setMinutesError('')
    getMeetingMinutes(meetingId, user.id)
      .then((minutes) => {
        if (active) setMeetingMinutes(minutes)
      })
      .catch((reason: unknown) => {
        if (!active) return
        if (reason instanceof MeetingApiError && reason.status === 404) {
          setMeetingMinutes(null)
          return
        }
        setMinutesError(
          reason instanceof Error ? reason.message : 'Meeting minutes could not be loaded.',
        )
      })
      .finally(() => {
        if (active) setMinutesLoading(false)
      })

    return () => { active = false }
  }, [isOrganizer, meetingId, minutesOpen, user])

  function formatTime(isoStr: string) {
    return new Intl.DateTimeFormat(undefined, { timeStyle: 'short' }).format(new Date(isoStr))
  }

  function formatDuration(joined: string, left: string | null) {
    if (!left) return 'Still in meeting'
    const diff = new Date(left).getTime() - new Date(joined).getTime()
    const minutes = Math.round(diff / 60000)
    if (minutes < 1) return '< 1 min'
    return `${minutes} min`
  }

  async function triggerActionItemExtraction() {
    if (!meetingId || !user || actionItemsTriggering) return
    setActionItemsTriggering(true)
    setActionItemsError('')
    try {
      const retryEmpty =
        actionItems?.status === 'completed' && actionItems.action_items.length === 0
      const result = retryEmpty
        ? await extractMeetingActionItems(meetingId, user.id, { retryEmpty })
        : await extractMeetingActionItems(meetingId, user.id)
      setActionItems(result)
    } catch (reason) {
      setActionItemsError(
        reason instanceof Error ? reason.message : 'Action item extraction failed.',
      )
    } finally {
      setActionItemsTriggering(false)
    }
  }

  async function confirmOwner(item: MeetingActionItem, employeeId: string) {
    if (
      !meetingId ||
      !user ||
      !report ||
      user.id !== report.organizer_id ||
      !employeeId ||
      confirmingActionItemId
    ) {
      return
    }

    setConfirmingActionItemId(item.id)
    setOwnerConfirmationFeedback(null)
    try {
      const confirmedItem = await confirmActionItemOwner(
        meetingId,
        item.id,
        employeeId,
        user.id,
      )
      try {
        const refreshedItems = await getMeetingActionItems(meetingId, user.id)
        setActionItems(refreshedItems)
        setOwnerConfirmationFeedback({
          actionItemId: item.id,
          kind: 'success',
          message: 'Owner confirmed successfully.',
        })
      } catch (refreshReason) {
        setActionItems((current) =>
          current
            ? {
                ...current,
                action_items: current.action_items.map((actionItem) =>
                  actionItem.id === confirmedItem.id ? confirmedItem : actionItem,
                ),
              }
            : current,
        )
        setOwnerConfirmationFeedback({
          actionItemId: item.id,
          kind: 'error',
          message: `Owner was confirmed, but the action-item list could not be refreshed: ${
            refreshReason instanceof Error ? refreshReason.message : 'Please reload the page.'
          }`,
        })
      }
    } catch (reason) {
      setOwnerConfirmationFeedback({
        actionItemId: item.id,
        kind: 'error',
        message:
          reason instanceof Error ? reason.message : 'Owner confirmation failed.',
      })
    } finally {
      setConfirmingActionItemId(null)
    }
  }

  async function handleReviewActionItem(
    item: MeetingActionItem,
    payload: ActionItemReviewPayload,
  ): Promise<MeetingActionItem | void> {
    if (!meetingId || !user) return
    const updated = await reviewActionItem(meetingId, item.id, payload, user.id)
    setActionItems((current) => {
      if (!current) return current
      return {
        ...current,
        action_items: current.action_items.map((existing) =>
          existing.id === updated.id ? updated : existing,
        ),
      }
    })
    return updated
  }

  function handleRefreshActionItems() {
    if (!meetingId || !user) return
    setActionItemsLoading(true)
    getMeetingActionItems(meetingId, user.id)
      .then((data) => setActionItems(data))
      .catch((err: unknown) =>
        setActionItemsError(err instanceof Error ? err.message : 'Failed to reload action items'),
      )
      .finally(() => setActionItemsLoading(false))
  }

  async function handleGenerateMinutes() {
    if (!meetingId || !user || !isOrganizer || minutesGenerating) return
    setMinutesGenerating(true)
    setMinutesError('')
    setMinutesFeedback(null)
    try {
      const generated = await generateMeetingMinutes(meetingId, user.id)
      setMeetingMinutes(generated)
      setMinutesFeedback({
        kind: 'success',
        message: 'Meeting minutes generated successfully.',
      })
    } catch (reason) {
      setMinutesError(
        reason instanceof Error ? reason.message : 'Failed to generate meeting minutes.',
      )
    } finally {
      setMinutesGenerating(false)
    }
  }

  async function handleApproveMinutes(notes?: string) {
    if (!meetingId || !user || !isOrganizer || minutesApproving) return
    setMinutesApproving(true)
    setMinutesError('')
    setMinutesFeedback(null)
    try {
      const approved = await approveMeetingMinutes(meetingId, user.id, notes ? { notes } : undefined)
      setMeetingMinutes(approved)
      setMinutesFeedback({
        kind: 'success',
        message: 'Meeting minutes approved successfully.',
      })
    } catch (reason) {
      setMinutesError(
        reason instanceof Error ? reason.message : 'Failed to approve meeting minutes.',
      )
    } finally {
      setMinutesApproving(false)
    }
  }

  async function handleSendMinutes() {
    if (!meetingId || !user || !isOrganizer || minutesSending) return
    setMinutesSending(true)
    setMinutesError('')
    setMinutesFeedback(null)
    try {
      const hasFailedRecipients = Boolean(
        meetingMinutes?.recipients &&
          meetingMinutes.recipients.some((r) => r.delivery_status === 'failed'),
      )
      const result = await sendMeetingMinutes(meetingId, user.id, hasFailedRecipients)
      try {
        const refreshed = await getMeetingMinutes(meetingId, user.id)
        setMeetingMinutes(refreshed)
      } catch {
        setMeetingMinutes((cur) =>
          cur
            ? {
                ...cur,
                status: result.failed > 0 && result.sent === 0 ? 'failed' : 'sent',
                recipients: result.recipients,
              }
            : cur,
        )
      }
      setMinutesFeedback({
        kind: result.failed > 0 && result.sent === 0 ? 'error' : 'success',
        message:
          result.failed > 0
            ? `Minutes sent to ${result.sent} recipient(s), with ${result.failed} failure(s).`
            : `Meeting minutes sent successfully to all ${result.sent} recipient(s).`,
      })
    } catch (reason) {
      setMinutesError(
        reason instanceof Error ? reason.message : 'Failed to send meeting minutes.',
      )
    } finally {
      setMinutesSending(false)
    }
  }

  async function handleUpdateMinutes(payload: MeetingMinutesUpdateRequest) {
    if (!meetingId || !user || !isOrganizer) return
    setMinutesUpdating(true)
    setMinutesError('')
    setMinutesFeedback(null)
    try {
      const updated = await updateMeetingMinutes(meetingId, user.id, payload)
      setMeetingMinutes(updated)
      setMinutesFeedback({
        kind: 'success',
        message: 'Meeting minutes updated successfully. You can now approve them before sending.',
      })
    } catch (reason) {
      setMinutesError(
        reason instanceof Error ? reason.message : 'Failed to update meeting minutes.',
      )
      throw reason
    } finally {
      setMinutesUpdating(false)
    }
  }

  async function handleRefreshContent() {
    if (!meetingId || !user || !isOrganizer) return
    setMinutesRefreshingContent(true)
    setMinutesError('')
    setMinutesFeedback(null)
    try {
      const refreshed = await refreshMeetingMinutes(meetingId, user.id)
      setMeetingMinutes(refreshed)
      setMinutesFeedback({
        kind: 'success',
        message: 'Meeting minutes re-synced from latest summary and action items.',
      })
    } catch (reason) {
      setMinutesError(
        reason instanceof Error ? reason.message : 'Failed to sync meeting minutes.',
      )
    } finally {
      setMinutesRefreshingContent(false)
    }
  }

  async function handleRetryFailedMinutes() {
    if (!meetingId || !user || !isOrganizer || minutesRetrying) return
    setMinutesRetrying(true)
    setMinutesError('')
    setMinutesFeedback(null)
    try {
      const result = await sendMeetingMinutes(meetingId, user.id, true)
      try {
        const refreshed = await getMeetingMinutes(meetingId, user.id)
        setMeetingMinutes(refreshed)
      } catch {
        setMeetingMinutes((cur) =>
          cur
            ? {
                ...cur,
                status: result.failed > 0 && result.sent === 0 ? 'failed' : 'sent',
                recipients: result.recipients,
              }
            : cur,
        )
      }
      setMinutesFeedback({
        kind: result.failed > 0 && result.sent === 0 ? 'error' : 'success',
        message:
          result.failed > 0
            ? `Retry finished: ${result.sent} sent, ${result.failed} still failed.`
            : `Successfully retried and sent minutes to ${result.sent} recipient(s).`,
      })
    } catch (reason) {
      setMinutesError(
        reason instanceof Error ? reason.message : 'Failed to retry delivery for failed recipients.',
      )
    } finally {
      setMinutesRetrying(false)
    }
  }

  function handleRefreshMinutes() {
    if (!meetingId || !user || !isOrganizer) return
    setMinutesLoading(true)
    setMinutesError('')
    getMeetingMinutes(meetingId, user.id)
      .then((data) => setMeetingMinutes(data))
      .catch((err: unknown) => {
        if (err instanceof MeetingApiError && err.status === 404) {
          setMeetingMinutes(null)
          return
        }
        setMinutesError(err instanceof Error ? err.message : 'Failed to reload meeting minutes')
      })
      .finally(() => setMinutesLoading(false))
  }


  function openTranscriptSegment(item: MeetingActionItem) {
    setTranscriptOpen(true)
    setSourceFocusRequestId((requestId) => requestId + 1)
    const query = new URLSearchParams({
      transcript_id: item.transcript_id,
      segment: String(item.source_segment_index),
    })
    navigate(
      `/meetings/${encodeURIComponent(item.meeting_id)}/report?${query.toString()}`,
    )
  }

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <Link to="/meetings" className="text-link" style={{ display: 'inline-flex', alignItems: 'center', gap: '0.25rem', marginBottom: '0.5rem' }}>
            <ArrowLeft size={14} /> Back to meetings
          </Link>
          <h2>Meeting Report</h2>
        </div>
      </header>
      <MeetingWorkflowStatus meetingId={meetingId} />

      {loading ? (
        <section className="state-card" role="status">Loading report…</section>
      ) : error ? (
        <section className="state-card" role="alert">
          <div className="state-icon danger">!</div>
          <p className="eyebrow">Access denied or unavailable</p>
          <h2>Report not found</h2>
          <p>{error}</p>
        </section>
      ) : report ? (
        <div className="report-content" style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
          <section className="report-summary" style={{ background: 'var(--surface)', padding: '1.5rem', borderRadius: '8px', border: '1px solid var(--border)' }}>
            <h3 style={{ marginTop: 0, marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <FileText size={18} /> {report.title}
            </h3>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', color: 'var(--text-muted)', fontSize: '0.9rem' }}>
              <div><strong>Start:</strong> {formatTime(report.start_at)}</div>
              <div><strong>End:</strong> {formatTime(report.end_at)}</div>
              <div><strong>Organizer:</strong> {report.organizer_id}</div>
              <div><strong>Participants:</strong> {report.attendance.length}</div>
            </div>
          </section>

          <section className="report-attendance">
            <h3 style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1rem' }}>
              <Users size={18} /> Attendance Log
            </h3>
            
            {report.attendance.length === 0 ? (
              <p>No participants joined this meeting.</p>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
                  <thead>
                    <tr style={{ borderBottom: '2px solid var(--border)' }}>
                      <th style={{ padding: '0.75rem 0.5rem' }}>Name</th>
                      <th style={{ padding: '0.75rem 0.5rem' }}>Email / Guest</th>
                      <th style={{ padding: '0.75rem 0.5rem' }}>Joined At</th>
                      <th style={{ padding: '0.75rem 0.5rem' }}>Left At</th>
                      <th style={{ padding: '0.75rem 0.5rem' }}>Duration</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.attendance.map((row, idx) => (
                      <tr key={idx} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '0.75rem 0.5rem', fontWeight: 500 }}>{row.participant_name}</td>
                        <td style={{ padding: '0.75rem 0.5rem', color: 'var(--text-muted)', fontSize: '0.9rem' }}>
                          {row.participant_email || (row.is_external_guest ? 'External Guest' : 'Unknown')}
                        </td>
                        <td style={{ padding: '0.75rem 0.5rem', fontSize: '0.9rem' }}>{formatTime(row.joined_at)}</td>
                        <td style={{ padding: '0.75rem 0.5rem', fontSize: '0.9rem' }}>
                          {row.left_at ? formatTime(row.left_at) : <span style={{ color: 'var(--primary)' }}>Still in meeting</span>}
                        </td>
                        <td style={{ padding: '0.75rem 0.5rem', fontSize: '0.9rem' }}>
                          <Clock size={12} style={{ verticalAlign: 'text-bottom', marginRight: '4px' }} />
                          {formatDuration(row.joined_at, row.left_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
              <button
                className="button secondary"
                type="button"
                aria-expanded={transcriptOpen}
                aria-controls="meeting-transcript-panel"
                onClick={() => setTranscriptOpen((open) => !open)}
              >
                <FileText size={16} aria-hidden="true" />
                {transcriptOpen ? 'Hide Transcript' : 'View Transcript'}
              </button>
              {isOrganizer && (
                <button
                  className="button secondary"
                  type="button"
                  aria-expanded={summaryOpen}
                  aria-controls="meeting-summary-panel"
                  onClick={() => setSummaryOpen((open) => !open)}
                >
                  <FileText size={16} aria-hidden="true" />
                  {summaryOpen ? 'Hide Summary' : 'View Summary'}
                </button>
              )}
              {isOrganizer && (
                <button
                  className="button secondary"
                  type="button"
                  aria-expanded={minutesOpen}
                  aria-controls="meeting-minutes-panel"
                  onClick={() => setMinutesOpen((open) => !open)}
                >
                  <FileCheck size={16} aria-hidden="true" />
                  {minutesOpen ? 'Hide Minutes' : 'View Minutes'}
                </button>
              )}
            </div>
          </section>

          {isOrganizer && summaryOpen && (
            <div id="meeting-summary-panel">
              <MeetingSummaryPanel
                summary={meetingSummary}
                loading={summaryLoading}
                error={summaryError}
              />
            </div>
          )}

          {isOrganizer && minutesOpen && (
            <div id="meeting-minutes-panel">
              <MeetingMinutesPanel
                meetingId={meetingId}
                minutes={meetingMinutes}
                isOrganizer={isOrganizer}
                loading={minutesLoading}
                generating={minutesGenerating}
                approving={minutesApproving}
                sending={minutesSending}
                retrying={minutesRetrying}
                updating={minutesUpdating}
                refreshingContent={minutesRefreshingContent}
                error={minutesError}
                feedback={minutesFeedback}
                onGenerate={handleGenerateMinutes}
                onApprove={handleApproveMinutes}
                onSend={handleSendMinutes}
                onRetryFailed={handleRetryFailedMinutes}
                onRefresh={handleRefreshMinutes}
                onUpdateMinutes={handleUpdateMinutes}
                onRefreshContent={handleRefreshContent}
              />
            </div>
          )}

          {isOrganizer && (
            <MeetingActionItemsPanel
              extraction={actionItems}
              employees={employees}
              employeesLoading={employeesLoading}
              employeesError={employeesError}
              isOrganizer={isOrganizer}
              loading={actionItemsLoading}
              triggering={actionItemsTriggering}
              error={actionItemsError}
              confirmingActionItemId={confirmingActionItemId}
              ownerConfirmationFeedback={ownerConfirmationFeedback}
              onExtract={() => void triggerActionItemExtraction()}
              onConfirmOwner={(item, employeeId) => void confirmOwner(item, employeeId)}
              onOpenSource={openTranscriptSegment}
              onReviewActionItem={handleReviewActionItem}
              onRefresh={handleRefreshActionItems}
              onRefreshEmployees={handleRefreshEmployees}
            />
          )}

          {transcriptOpen && (
            <TranscriptPanel
              meetingId={meetingId}
              organizerId={user?.id ?? ''}
              transcriptId={sourceTranscriptId}
              employeeEmail={user?.email}
              isOrganizer={user?.id === report.organizer_id}
              isOpen
              inline
              focusSegmentIndex={sourceSegmentIndex}
              focusRequestId={sourceFocusRequestId}
              onClose={() => setTranscriptOpen(false)}
            />
          )}
        </div>
      ) : null}
    </div>
  )
}
