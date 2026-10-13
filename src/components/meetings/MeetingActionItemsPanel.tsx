import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ClipboardList,
  Edit3,
  Filter,
  RefreshCw,
  Save,
  ShieldAlert,
  X,
  XCircle,
} from 'lucide-react'
import { useState } from 'react'
import type { ActionItemReviewPayload } from '../../api/meetings'
import type {
  Employee,
  EmployeeMatchStatus,
  MeetingActionItem,
  MeetingActionItemOwnershipStatus,
  MeetingActionItemPriority,
  MeetingActionItemReviewStatus,
  MeetingActionItems,
} from '../../types/meeting'

interface MeetingActionItemsPanelProps {
  extraction: MeetingActionItems | null
  employees: Employee[]
  employeesLoading: boolean
  employeesError?: string
  isOrganizer: boolean
  loading: boolean
  triggering: boolean
  error?: string
  confirmingActionItemId: string | null
  ownerConfirmationFeedback: {
    actionItemId: string
    kind: 'success' | 'error'
    message: string
  } | null
  onExtract: () => void
  onConfirmOwner: (item: MeetingActionItem, employeeId: string) => void
  onOpenSource: (item: MeetingActionItem) => void
  onReviewActionItem?: (
    item: MeetingActionItem,
    payload: ActionItemReviewPayload,
  ) => Promise<MeetingActionItem | void>
  onRefresh?: () => void
  onRefreshEmployees?: () => void
}

const matchStatusLabels: Record<EmployeeMatchStatus, string> = {
  unique_match: 'Unique match',
  ambiguous_match: 'Ambiguous match',
  no_match: 'No match',
  missing_owner: 'Missing owner',
}

const ownershipStatusLabels: Record<MeetingActionItemOwnershipStatus, string> = {
  unresolved: 'Unresolved',
  proposed: 'Proposed',
  approval_required: 'Approval required',
  confirmed: 'Confirmed',
}

const reviewStatusLabels: Record<MeetingActionItemReviewStatus, string> = {
  pending_review: 'Pending Review',
  approval_required: 'Approval Required',
  confirmed: 'Confirmed',
  rejected: 'Rejected',
}

function matchStatusLabel(status: EmployeeMatchStatus | null | undefined): string {
  return status ? matchStatusLabels[status] : 'Not available'
}

function ownershipStatusLabel(
  status: MeetingActionItemOwnershipStatus | null | undefined,
): string {
  return status ? ownershipStatusLabels[status] : 'Unresolved'
}

function formatTimestamp(seconds: number): string {
  const centiseconds = Math.round(Math.max(0, seconds) * 100)
  const wholeSeconds = Math.floor(centiseconds / 100)
  const hours = Math.floor(wholeSeconds / 3600)
  const minutes = Math.floor((wholeSeconds % 3600) / 60)
  const remainingSeconds = wholeSeconds % 60
  const fractionalCentiseconds = centiseconds % 100
  const secondsLabel = `${String(remainingSeconds).padStart(2, '0')}${
    fractionalCentiseconds > 0
      ? `.${String(fractionalCentiseconds).padStart(2, '0')}`
      : ''
  }`
  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${secondsLabel}`
  }
  return `${minutes}:${secondsLabel}`
}

function statusMessage(
  status: MeetingActionItems['status'],
  actionItemCount: number,
): string {
  switch (status) {
    case 'pending':
      return 'Action item extraction is pending.'
    case 'processing':
      return 'Action items are being extracted.'
    case 'failed':
      return 'Action item extraction failed.'
    case 'completed': {
      if (actionItemCount === 0) return 'No action items were identified.'
      const itemLabel = actionItemCount === 1 ? 'action item' : 'action items'
      return `${actionItemCount} ${itemLabel} extracted.`
    }
  }
}

type FilterOption = 'all' | 'needs_review' | 'confirmed' | 'rejected'

export function MeetingActionItemsPanel({
  extraction,
  employees,
  employeesLoading,
  employeesError = '',
  isOrganizer,
  loading,
  triggering,
  error = '',
  confirmingActionItemId,
  ownerConfirmationFeedback,
  onExtract,
  onConfirmOwner,
  onOpenSource,
  onReviewActionItem,
  onRefresh,
  onRefreshEmployees,
}: MeetingActionItemsPanelProps) {
  const [selectedEmployeeIds, setSelectedEmployeeIds] = useState<Record<string, string>>({})
  const [activeFilter, setActiveFilter] = useState<FilterOption>('all')
  const [editingItemId, setEditingItemId] = useState<string | null>(null)
  const [editFormData, setEditFormData] = useState<{
    task_description: string
    owner_name: string
    confirmed_employee_id: string
    deadline_text: string
    deadline_at: string
    priority: MeetingActionItemPriority
  }>({
    task_description: '',
    owner_name: '',
    confirmed_employee_id: '',
    deadline_text: '',
    deadline_at: '',
    priority: 'medium',
  })
  const [reviewingItemId, setReviewingItemId] = useState<string | null>(null)
  const [itemFeedback, setItemFeedback] = useState<
    Record<string, { kind: 'success' | 'error'; message: string }>
  >({})

  const status = extraction?.status
  const canExtract = !loading && !triggering && status !== 'processing'
  const actionItems = extraction?.action_items ?? []

  const totalCount = actionItems.length
  const needsReviewCount = actionItems.filter(
    (item) =>
      item.review_status === 'approval_required' ||
      item.review_status === 'pending_review' ||
      item.requires_approval ||
      !item.review_status,
  ).length
  const confirmedCount = actionItems.filter((item) => item.review_status === 'confirmed').length
  const rejectedCount = actionItems.filter((item) => item.review_status === 'rejected').length

  const filteredItems = actionItems.filter((item) => {
    if (activeFilter === 'all') return true
    if (activeFilter === 'needs_review') {
      return (
        item.review_status === 'approval_required' ||
        item.review_status === 'pending_review' ||
        item.requires_approval ||
        !item.review_status
      )
    }
    if (activeFilter === 'confirmed') return item.review_status === 'confirmed'
    if (activeFilter === 'rejected') return item.review_status === 'rejected'
    return true
  })

  function startEdit(item: MeetingActionItem) {
    setEditingItemId(item.id)
    const deadline = item.deadline_at ? new Date(item.deadline_at) : null
    setEditFormData({
      task_description: item.task_description,
      owner_name: item.owner_name ?? '',
      confirmed_employee_id: item.confirmed_employee_id ?? item.proposed_employee_id ?? '',
      deadline_text: item.deadline_text ?? '',
      deadline_at: deadline
        ? new Date(deadline.getTime() - deadline.getTimezoneOffset() * 60_000)
            .toISOString()
            .slice(0, 16)
        : '',
      priority: item.priority,
    })
  }

  function cancelEdit() {
    setEditingItemId(null)
  }

  async function handleSaveEdit(item: MeetingActionItem) {
    if (!onReviewActionItem) return
    if (!editFormData.task_description.trim()) {
      setItemFeedback((prev) => ({
        ...prev,
        [item.id]: { kind: 'error', message: 'Task description cannot be empty.' },
      }))
      return
    }

    setReviewingItemId(item.id)
    try {
      await onReviewActionItem(item, {
        action: 'edit',
        task_description: editFormData.task_description.trim(),
        owner_name: editFormData.owner_name.trim() || null,
        confirmed_employee_id: editFormData.confirmed_employee_id || null,
        deadline_text: editFormData.deadline_text.trim() || null,
        deadline_at: editFormData.deadline_at
          ? new Date(editFormData.deadline_at).toISOString()
          : null,
        priority: editFormData.priority,
      })
      setItemFeedback((prev) => ({
        ...prev,
        [item.id]: { kind: 'success', message: 'Changes saved successfully.' },
      }))
      setEditingItemId(null)
    } catch (err) {
      setItemFeedback((prev) => ({
        ...prev,
        [item.id]: {
          kind: 'error',
          message: err instanceof Error ? err.message : 'Failed to save changes.',
        },
      }))
    } finally {
      setReviewingItemId(null)
    }
  }

  async function handleConfirmReview(item: MeetingActionItem) {
    if (!onReviewActionItem) return
    setReviewingItemId(item.id)
    try {
      await onReviewActionItem(item, { action: 'confirm' })
      setItemFeedback((prev) => ({
        ...prev,
        [item.id]: {
          kind: 'success',
          message: 'Action item confirmed (eligible for task creation).',
        },
      }))
      if (editingItemId === item.id) setEditingItemId(null)
    } catch (err) {
      setItemFeedback((prev) => ({
        ...prev,
        [item.id]: {
          kind: 'error',
          message: err instanceof Error ? err.message : 'Failed to confirm action item.',
        },
      }))
    } finally {
      setReviewingItemId(null)
    }
  }

  async function handleRejectReview(item: MeetingActionItem) {
    if (!onReviewActionItem) return
    setReviewingItemId(item.id)
    try {
      await onReviewActionItem(item, { action: 'reject' })
      setItemFeedback((prev) => ({
        ...prev,
        [item.id]: {
          kind: 'success',
          message: 'Action item rejected (will never become a task).',
        },
      }))
      if (editingItemId === item.id) setEditingItemId(null)
    } catch (err) {
      setItemFeedback((prev) => ({
        ...prev,
        [item.id]: {
          kind: 'error',
          message: err instanceof Error ? err.message : 'Failed to reject action item.',
        },
      }))
    } finally {
      setReviewingItemId(null)
    }
  }

  return (
    <section
      className="report-summary meeting-action-items"
      aria-labelledby="meeting-action-items-title"
      style={{
        background: 'var(--surface)',
        padding: '1.5rem',
        borderRadius: '8px',
        border: '1px solid var(--border)',
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: '1rem',
          marginBottom: '1rem',
        }}
      >
        <h2
          id="meeting-action-items-title"
          style={{ margin: 0, display: 'flex', alignItems: 'center', gap: '0.5rem' }}
        >
          <ClipboardList size={20} aria-hidden="true" /> Meeting Action Items
        </h2>

        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          {onRefresh && (
            <button
              className="button secondary"
              type="button"
              onClick={onRefresh}
              disabled={loading || triggering}
              title="Refresh action items"
              style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', padding: '0.4rem 0.75rem' }}
            >
              <RefreshCw size={14} className={loading ? 'spin' : ''} aria-hidden="true" />
              Refresh
            </button>
          )}

          {(!status || status === 'failed') && (
            <button
              className="button secondary"
              type="button"
              onClick={onExtract}
              disabled={!canExtract}
              style={{ display: 'flex', alignItems: 'center', gap: '0.25rem' }}
            >
              <RefreshCw size={14} aria-hidden="true" />
              {triggering
                ? 'Extracting…'
                : status === 'failed'
                  ? 'Retry Extraction'
                  : 'Extract Action Items'}
            </button>
          )}
        </div>
      </div>

      {status && (
        <div style={{ marginBottom: '1rem', color: 'var(--text-muted)' }}>
          <p role="status" style={{ margin: 0 }}>
            Extraction status: {status}. {statusMessage(status, actionItems.length)}
          </p>
        </div>
      )}

      {/* Filter Tabs */}
      {status === 'completed' && actionItems.length > 0 && (
        <nav
          aria-label="Action item review status filter"
          style={{
            display: 'flex',
            gap: '0.5rem',
            flexWrap: 'wrap',
            marginBottom: '1.25rem',
            paddingBottom: '0.75rem',
            borderBottom: '1px solid var(--border)',
          }}
        >
          <button
            type="button"
            className={`button ${activeFilter === 'all' ? 'primary' : 'secondary'}`}
            onClick={() => setActiveFilter('all')}
            style={{ fontSize: '0.85rem', padding: '0.35rem 0.75rem' }}
          >
            All ({totalCount})
          </button>
          <button
            type="button"
            className={`button ${activeFilter === 'needs_review' ? 'primary' : 'secondary'}`}
            onClick={() => setActiveFilter('needs_review')}
            style={{ fontSize: '0.85rem', padding: '0.35rem 0.75rem' }}
          >
            Needs Review ({needsReviewCount})
          </button>
          <button
            type="button"
            className={`button ${activeFilter === 'confirmed' ? 'primary' : 'secondary'}`}
            onClick={() => setActiveFilter('confirmed')}
            style={{ fontSize: '0.85rem', padding: '0.35rem 0.75rem' }}
          >
            Confirmed ({confirmedCount})
          </button>
          <button
            type="button"
            className={`button ${activeFilter === 'rejected' ? 'primary' : 'secondary'}`}
            onClick={() => setActiveFilter('rejected')}
            style={{ fontSize: '0.85rem', padding: '0.35rem 0.75rem' }}
          >
            Rejected ({rejectedCount})
          </button>
        </nav>
      )}

      {loading ? (
        <p role="status">Loading meeting action items…</p>
      ) : error ? (
        <div role="alert">
          <p>Unable to load meeting action items.</p>
          <p>{error}</p>
          <button
            className="button secondary"
            type="button"
            onClick={onExtract}
            disabled={!canExtract}
          >
            Retry
          </button>
        </div>
      ) : extraction?.status === 'failed' ? (
        <div role="alert">
          {extraction.error_message && <p>{extraction.error_message}</p>}
        </div>
      ) : extraction?.status === 'completed' && actionItems.length > 0 ? (
        filteredItems.length === 0 ? (
          <p role="status" style={{ color: 'var(--text-muted)', padding: '1rem 0' }}>
            No action items match the selected filter ({activeFilter.replace('_', ' ')}).
          </p>
        ) : (
          <ul
            className="meeting-action-item-list"
            style={{
              display: 'grid',
              gap: '1.25rem',
              paddingLeft: '0',
              listStyle: 'none',
              margin: 0,
            }}
          >
            {filteredItems.map((item) => {
              const isApprovalRequired =
                item.review_status === 'approval_required' ||
                item.requires_approval ||
                item.confidence < 0.85
              const isConfirmed = item.review_status === 'confirmed'
              const isRejected = item.review_status === 'rejected'
              const isEditing = editingItemId === item.id
              const isActionBusy = reviewingItemId === item.id

              const feedback = itemFeedback[item.id]

              return (
                <li
                  key={item.id}
                  style={{
                    border: isApprovalRequired && !isConfirmed && !isRejected
                      ? '1px solid #f59e0b'
                      : isConfirmed
                        ? '1px solid #22c55e'
                        : isRejected
                          ? '1px solid #ef4444'
                          : '1px solid var(--border)',
                    borderRadius: '8px',
                    padding: '1.25rem',
                    background: isApprovalRequired && !isConfirmed && !isRejected
                      ? 'rgba(245, 158, 11, 0.04)'
                      : isConfirmed
                        ? 'rgba(34, 197, 94, 0.04)'
                        : isRejected
                          ? 'rgba(239, 68, 68, 0.04)'
                          : 'var(--surface)',
                  }}
                >
                  <article>
                    {/* Header Badges */}
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'flex-start',
                        flexWrap: 'wrap',
                        gap: '0.5rem',
                        marginBottom: '0.75rem',
                      }}
                    >
                      <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap', alignItems: 'center' }}>
                        {/* Review Status Badge */}
                        {isConfirmed ? (
                          <span
                            role="status"
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '0.35rem',
                              padding: '0.25rem 0.6rem',
                              borderRadius: '4px',
                              background: '#dcfce7',
                              color: '#166534',
                              fontWeight: 600,
                              fontSize: '0.85rem',
                            }}
                          >
                            <CheckCircle2 size={14} aria-hidden="true" /> Confirmed
                          </span>
                        ) : isRejected ? (
                          <span
                            role="status"
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '0.35rem',
                              padding: '0.25rem 0.6rem',
                              borderRadius: '4px',
                              background: '#fee2e2',
                              color: '#991b1b',
                              fontWeight: 600,
                              fontSize: '0.85rem',
                            }}
                          >
                            <XCircle size={14} aria-hidden="true" /> Rejected
                          </span>
                        ) : isApprovalRequired ? (
                          <span
                            role="status"
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '0.35rem',
                              padding: '0.25rem 0.6rem',
                              borderRadius: '4px',
                              background: '#fff3cd',
                              color: '#92400e',
                              border: '1px solid #f59e0b',
                              fontWeight: 700,
                              fontSize: '0.85rem',
                            }}
                          >
                            <AlertTriangle size={14} aria-hidden="true" /> Approval Required
                          </span>
                        ) : (
                          <span
                            role="status"
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '0.35rem',
                              padding: '0.25rem 0.6rem',
                              borderRadius: '4px',
                              background: '#e0f2fe',
                              color: '#075985',
                              fontWeight: 600,
                              fontSize: '0.85rem',
                            }}
                          >
                            Pending Review
                          </span>
                        )}

                        {/* Priority Badge */}
                        <span
                          style={{
                            padding: '0.2rem 0.5rem',
                            borderRadius: '4px',
                            background: 'var(--surface-raised, rgba(0,0,0,0.06))',
                            fontSize: '0.8rem',
                            textTransform: 'capitalize',
                          }}
                        >
                          Priority: {item.priority}
                        </span>

                        {/* Confidence */}
                        <span
                          style={{
                            padding: '0.2rem 0.5rem',
                            borderRadius: '4px',
                            background: 'var(--surface-raised, rgba(0,0,0,0.06))',
                            fontSize: '0.8rem',
                          }}
                        >
                          Confidence: {Math.round(item.confidence * 100)}%
                        </span>
                      </div>

                      {/* Approval Warning Alert */}
                      {isApprovalRequired && !isConfirmed && !isRejected && (
                        <div
                          role="alert"
                          style={{
                            width: '100%',
                            marginTop: '0.5rem',
                            padding: '0.5rem 0.75rem',
                            background: '#fef3c7',
                            border: '1px solid #f59e0b',
                            borderRadius: '4px',
                            color: '#92400e',
                            fontSize: '0.85rem',
                            display: 'flex',
                            alignItems: 'center',
                            gap: '0.5rem',
                          }}
                        >
                          <ShieldAlert size={16} aria-hidden="true" />
                          <span>
                            Confidence score is below the 0.85 threshold. Organizer review & approval is required before this item can become a task.
                          </span>
                        </div>
                      )}

                      {/* Ineligible Task Warning for Rejected items */}
                      {isRejected && (
                        <div
                          role="status"
                          style={{
                            width: '100%',
                            marginTop: '0.5rem',
                            padding: '0.5rem 0.75rem',
                            background: '#fee2e2',
                            border: '1px solid #ef4444',
                            borderRadius: '4px',
                            color: '#991b1b',
                            fontSize: '0.85rem',
                            display: 'flex',
                            alignItems: 'center',
                            gap: '0.5rem',
                          }}
                        >
                          <XCircle size={16} aria-hidden="true" />
                          <span>
                            This action item has been rejected and can never be converted into a task.
                          </span>
                        </div>
                      )}
                    </div>

                    {/* Task Description (Editable or View) */}
                    {isEditing ? (
                      <div style={{ display: 'grid', gap: '0.75rem', margin: '1rem 0' }}>
                        <div>
                          <label
                            htmlFor={`edit-desc-${item.id}`}
                            style={{ display: 'block', fontWeight: 600, marginBottom: '0.25rem' }}
                          >
                            Task Description
                          </label>
                          <textarea
                            id={`edit-desc-${item.id}`}
                            value={editFormData.task_description}
                            onChange={(e) =>
                              setEditFormData((prev) => ({ ...prev, task_description: e.target.value }))
                            }
                            rows={2}
                            style={{
                              width: '100%',
                              padding: '0.5rem',
                              borderRadius: '4px',
                              border: '1px solid var(--border)',
                              fontFamily: 'inherit',
                            }}
                          />
                        </div>

                        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '0.75rem' }}>
                          <div>
                            <label
                              htmlFor={`edit-owner-name-${item.id}`}
                              style={{ display: 'block', fontWeight: 600, marginBottom: '0.25rem' }}
                            >
                              Assignee Name
                            </label>
                            <input
                              id={`edit-owner-name-${item.id}`}
                              type="text"
                              value={editFormData.owner_name}
                              onChange={(e) =>
                                setEditFormData((prev) => ({ ...prev, owner_name: e.target.value }))
                              }
                              style={{
                                width: '100%',
                                padding: '0.45rem',
                                borderRadius: '4px',
                                border: '1px solid var(--border)',
                              }}
                            />
                          </div>

                          <div>
                            <label
                              htmlFor={`edit-employee-${item.id}`}
                              style={{ display: 'block', fontWeight: 600, marginBottom: '0.25rem' }}
                            >
                              Linked Employee
                            </label>
                            <select
                              id={`edit-employee-${item.id}`}
                              value={editFormData.confirmed_employee_id}
                              onChange={(e) =>
                                setEditFormData((prev) => ({
                                  ...prev,
                                  confirmed_employee_id: e.target.value,
                                }))
                              }
                              style={{
                                width: '100%',
                                padding: '0.45rem',
                                borderRadius: '4px',
                                border: '1px solid var(--border)',
                              }}
                            >
                              <option value="">Unassigned</option>
                              {employees.map((emp) => (
                                <option key={emp.id} value={emp.id}>
                                  {emp.name} ({emp.email})
                                </option>
                              ))}
                            </select>
                          </div>

                          <div>
                            <label
                              htmlFor={`edit-priority-${item.id}`}
                              style={{ display: 'block', fontWeight: 600, marginBottom: '0.25rem' }}
                            >
                              Priority
                            </label>
                            <select
                              id={`edit-priority-${item.id}`}
                              value={editFormData.priority}
                              onChange={(e) =>
                                setEditFormData((prev) => ({
                                  ...prev,
                                  priority: e.target.value as MeetingActionItemPriority,
                                }))
                              }
                              style={{
                                width: '100%',
                                padding: '0.45rem',
                                borderRadius: '4px',
                                border: '1px solid var(--border)',
                              }}
                            >
                              <option value="low">Low</option>
                              <option value="medium">Medium</option>
                              <option value="high">High</option>
                              <option value="urgent">Urgent</option>
                            </select>
                          </div>

                          <div>
                            <label
                              htmlFor={`edit-deadline-text-${item.id}`}
                              style={{ display: 'block', fontWeight: 600, marginBottom: '0.25rem' }}
                            >
                              Deadline Mention
                            </label>
                            <input
                              id={`edit-deadline-text-${item.id}`}
                              type="text"
                              value={editFormData.deadline_text}
                              onChange={(e) =>
                                setEditFormData((prev) => ({ ...prev, deadline_text: e.target.value }))
                              }
                              placeholder="e.g. by Friday"
                              style={{
                                width: '100%',
                                padding: '0.45rem',
                                borderRadius: '4px',
                                border: '1px solid var(--border)',
                              }}
                            />
                          </div>
                          <div>
                            <label
                              htmlFor={`edit-deadline-at-${item.id}`}
                              style={{ display: 'block', fontWeight: 600, marginBottom: '0.25rem' }}
                            >
                              Due date and time
                            </label>
                            <input
                              id={`edit-deadline-at-${item.id}`}
                              type="datetime-local"
                              value={editFormData.deadline_at}
                              onChange={(e) =>
                                setEditFormData((prev) => ({
                                  ...prev,
                                  deadline_at: e.target.value,
                                }))
                              }
                              style={{
                                width: '100%',
                                padding: '0.45rem',
                                borderRadius: '4px',
                                border: '1px solid var(--border)',
                              }}
                            />
                          </div>
                        </div>

                        {/* Edit Action Buttons */}
                        <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.5rem' }}>
                          <button
                            className="button primary"
                            type="button"
                            onClick={() => void handleSaveEdit(item)}
                            disabled={isActionBusy}
                            style={{ display: 'flex', alignItems: 'center', gap: '0.25rem' }}
                          >
                            <Save size={15} aria-hidden="true" />
                            {isActionBusy ? 'Saving…' : 'Save Changes'}
                          </button>
                          <button
                            className="button secondary"
                            type="button"
                            onClick={cancelEdit}
                            disabled={isActionBusy}
                          >
                            Cancel
                          </button>
                        </div>
                      </div>
                    ) : (
                      <p style={{ fontSize: '1.05rem', margin: '0.5rem 0' }}>
                        <strong>{item.task_description}</strong>
                      </p>
                    )}

                    {/* Metadata Definition List */}
                    <dl
                      style={{
                        display: 'grid',
                        gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
                        gap: '0.75rem',
                        margin: '0.75rem 0',
                        fontSize: '0.9rem',
                      }}
                    >
                      <div>
                        <dt style={{ color: 'var(--text-muted)' }}>Owner</dt>
                        <dd style={{ margin: 0, fontWeight: 500 }}>
                          {item.owner_name || 'Unassigned'}
                        </dd>
                      </div>
                      <div>
                        <dt style={{ color: 'var(--text-muted)' }}>Automatic match status</dt>
                        <dd style={{ margin: 0, fontWeight: 500 }}>
                          {matchStatusLabel(item.employee_match_status)}
                        </dd>
                      </div>
                      <div>
                        <dt style={{ color: 'var(--text-muted)' }}>Ownership status</dt>
                        <dd style={{ margin: 0, fontWeight: 500 }}>
                          {ownershipStatusLabel(item.ownership_status)}
                        </dd>
                      </div>
                      <div>
                        <dt style={{ color: 'var(--text-muted)' }}>Deadline</dt>
                        <dd style={{ margin: 0 }}>
                          {item.deadline_at ? (
                            <>
                              <time dateTime={item.deadline_at}>
                                {new Intl.DateTimeFormat(undefined, {
                                  dateStyle: 'medium',
                                  timeZone: 'Asia/Kolkata',
                                }).format(new Date(item.deadline_at))}
                              </time>
                              {item.deadline_text && (
                                <small style={{ display: 'block' }}>
                                  Mentioned: <span>{item.deadline_text}</span>
                                </small>
                              )}
                            </>
                          ) : item.deadline_text ? (
                            <span>{item.deadline_text}</span>
                          ) : (
                            <span
                              role="status"
                              style={{
                                display: 'inline-block',
                                padding: '0.2rem 0.45rem',
                                borderRadius: '4px',
                                backgroundColor: '#fff3cd',
                                color: '#664d03',
                                fontWeight: 600,
                              }}
                            >
                              Not specified
                            </span>
                          )}
                        </dd>
                      </div>
                      <div>
                        <dt style={{ color: 'var(--text-muted)' }}>Priority</dt>
                        <dd style={{ margin: 0, fontWeight: 500 }}>{item.priority}</dd>
                      </div>
                      <div>
                        <dt style={{ color: 'var(--text-muted)' }}>Confidence</dt>
                        <dd style={{ margin: 0, fontWeight: 500 }}>
                          {Math.round(item.confidence * 100)}%
                        </dd>
                      </div>
                      <div>
                        <dt style={{ color: 'var(--text-muted)' }}>Review Status</dt>
                        <dd style={{ margin: 0, fontWeight: 600 }}>
                          {reviewStatusLabels[item.review_status ?? 'pending_review'] ??
                            item.review_status}
                        </dd>
                      </div>
                    </dl>

                    {/* Durable Decision Metadata */}
                    {item.reviewed_by && (
                      <p
                        style={{
                          fontSize: '0.8rem',
                          color: 'var(--text-muted)',
                          margin: '0.25rem 0 0.5rem 0',
                          borderTop: '1px dashed var(--border)',
                          paddingTop: '0.4rem',
                        }}
                      >
                        Decision by <strong>{item.reviewed_by}</strong>
                        {item.reviewed_at && (
                          <>
                            {' '}
                            on <time dateTime={item.reviewed_at}>
                              {new Date(item.reviewed_at).toLocaleString()}
                            </time>
                          </>
                        )}
                      </p>
                    )}

                    {/* Source Transcript Link */}
                    <button
                      className="meet-transcript-action"
                      type="button"
                      onClick={() => onOpenSource(item)}
                      style={{
                        background: 'none',
                        border: 'none',
                        padding: 0,
                        color: 'var(--primary, #2563eb)',
                        cursor: 'pointer',
                        textAlign: 'left',
                        fontSize: '0.85rem',
                        textDecoration: 'underline',
                        marginBottom: '0.5rem',
                      }}
                    >
                      {item.source_quote_start_seconds === null
                        ? `Source transcript segment starts at ${formatTimestamp(item.source_start_seconds)}`
                        : `Source transcript phrase: ${formatTimestamp(item.source_quote_start_seconds)}`}
                    </button>
                    {item.source_quote && (
                      <blockquote
                        style={{
                          margin: '0.25rem 0 0.75rem 0',
                          paddingLeft: '0.75rem',
                          borderLeft: '3px solid var(--border)',
                          color: 'var(--text-muted)',
                          fontStyle: 'italic',
                          fontSize: '0.875rem',
                        }}
                      >
                        {item.source_quote}
                      </blockquote>
                    )}

                    {/* Organizer Ownership Assignment Form (Story 3.1) */}
                    {isOrganizer && item.ownership_status !== 'confirmed' && (
                      <div
                        style={{
                          display: 'grid',
                          gap: '0.5rem',
                          marginTop: '0.75rem',
                          padding: '0.75rem',
                          background: 'var(--surface-raised, rgba(0,0,0,0.03))',
                          borderRadius: '6px',
                        }}
                      >
                        {employeesLoading ? (
                          <p role="status">Loading active employees…</p>
                        ) : employeesError ? (
                          <p role="alert">Unable to load active employees: {employeesError}</p>
                        ) : employees.length === 0 ? (
                          <div>
                            <p role="status">No active employees are available to assign.</p>
                            {onRefreshEmployees && (
                              <button
                                className="button secondary"
                                type="button"
                                onClick={onRefreshEmployees}
                                disabled={employeesLoading}
                              >
                                Refresh employee list
                              </button>
                            )}
                          </div>
                        ) : (
                          <>
                            <label htmlFor={`owner-select-${item.id}`} style={{ fontWeight: 600 }}>
                              Select owner
                            </label>
                            <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
                              <select
                                id={`owner-select-${item.id}`}
                                value={selectedEmployeeIds[item.id] ?? item.proposed_employee_id ?? ''}
                                onChange={(event) => {
                                  setSelectedEmployeeIds((current) => ({
                                    ...current,
                                    [item.id]: event.target.value,
                                  }))
                                }}
                                disabled={confirmingActionItemId === item.id}
                                style={{ flex: 1, minWidth: '180px', padding: '0.4rem' }}
                              >
                                <option value="">Select an employee</option>
                                {employees.map((employee) => (
                                  <option key={employee.id} value={employee.id}>
                                    {employee.name} ({employee.email})
                                  </option>
                                ))}
                              </select>
                              <button
                                className="button secondary"
                                type="button"
                                onClick={() => {
                                  const employeeId =
                                    selectedEmployeeIds[item.id] ??
                                    item.proposed_employee_id ??
                                    ''
                                  onConfirmOwner(item, employeeId)
                                }}
                                disabled={
                                  !(
                                    selectedEmployeeIds[item.id] ??
                                    item.proposed_employee_id ??
                                    ''
                                  ) || confirmingActionItemId === item.id
                                }
                              >
                                {confirmingActionItemId === item.id
                                  ? 'Confirming…'
                                  : 'Confirm Owner'}
                              </button>
                            </div>
                          </>
                        )}
                      </div>
                    )}

                    {/* Owner Confirmation Feedback */}
                    {ownerConfirmationFeedback?.actionItemId === item.id && (
                      <p
                        role={ownerConfirmationFeedback.kind === 'error' ? 'alert' : 'status'}
                        style={{
                          margin: '0.5rem 0',
                          fontSize: '0.85rem',
                          color:
                            ownerConfirmationFeedback.kind === 'error'
                              ? '#dc2626'
                              : '#16a34a',
                        }}
                      >
                        {ownerConfirmationFeedback.message}
                      </p>
                    )}

                    {/* Organizer Review Actions (Story 3.3) */}
                    {isOrganizer && onReviewActionItem && !isEditing && (
                      <div
                        style={{
                          display: 'flex',
                          gap: '0.5rem',
                          flexWrap: 'wrap',
                          alignItems: 'center',
                          marginTop: '0.75rem',
                          paddingTop: '0.75rem',
                          borderTop: '1px solid var(--border)',
                        }}
                      >
                        {/* Confirm Action */}
                        <button
                          className="button primary"
                          type="button"
                          onClick={() => void handleConfirmReview(item)}
                          disabled={isActionBusy}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '0.35rem',
                            backgroundColor: isConfirmed ? '#16a34a' : undefined,
                          }}
                        >
                          <Check size={14} aria-hidden="true" />
                          {isActionBusy && reviewingItemId === item.id
                            ? 'Processing…'
                            : isConfirmed
                              ? 'Confirmed'
                              : 'Confirm'}
                        </button>

                        {/* Edit Action */}
                        <button
                          className="button secondary"
                          type="button"
                          onClick={() => startEdit(item)}
                          disabled={isActionBusy}
                          style={{ display: 'flex', alignItems: 'center', gap: '0.35rem' }}
                        >
                          <Edit3 size={14} aria-hidden="true" />
                          Edit
                        </button>

                        {/* Reject Action */}
                        <button
                          className="button danger"
                          type="button"
                          onClick={() => void handleRejectReview(item)}
                          disabled={isActionBusy}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '0.35rem',
                            opacity: isRejected ? 0.7 : 1,
                          }}
                        >
                          <X size={14} aria-hidden="true" />
                          {isActionBusy && reviewingItemId === item.id
                            ? 'Processing…'
                            : isRejected
                              ? 'Rejected'
                              : 'Reject'}
                        </button>
                      </div>
                    )}

                    {/* Review Feedback Message */}
                    {feedback && (
                      <div
                        role={feedback.kind === 'error' ? 'alert' : 'status'}
                        style={{
                          marginTop: '0.5rem',
                          padding: '0.4rem 0.75rem',
                          borderRadius: '4px',
                          fontSize: '0.85rem',
                          background: feedback.kind === 'error' ? '#fee2e2' : '#dcfce7',
                          color: feedback.kind === 'error' ? '#991b1b' : '#166534',
                          border:
                            feedback.kind === 'error'
                              ? '1px solid #f87171'
                              : '1px solid #86efac',
                        }}
                      >
                        {feedback.message}
                      </div>
                    )}
                  </article>
                </li>
              )
            })}
          </ul>
        )
      ) : extraction?.status === 'completed' ? (
        <p role="status">No action items were identified.</p>
      ) : null}
    </section>
  )
}
