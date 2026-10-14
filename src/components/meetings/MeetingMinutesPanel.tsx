import {
  AlertCircle,
  AlertTriangle,
  Calendar,
  CheckCircle2,
  Clock,
  Edit3,
  FileCheck,
  FileText,
  Mail,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  Send,
  Trash2,
  User,
  X,
} from 'lucide-react'
import { useState } from 'react'
import type {
  MeetingMinutes,
  MeetingMinutesDeliveryStatus,
  MeetingMinutesRecipient,
  MeetingMinutesStatus,
  MeetingMinutesUpdateRequest,
} from '../../types/meeting'

export interface MeetingMinutesPanelProps {
  meetingId: string
  minutes: MeetingMinutes | null
  isOrganizer: boolean
  loading: boolean
  generating: boolean
  approving: boolean
  sending: boolean
  retrying: boolean
  updating?: boolean
  refreshingContent?: boolean
  error?: string
  feedback?: {
    kind: 'success' | 'error'
    message: string
  } | null
  onGenerate: () => void
  onApprove: (notes?: string) => void
  onSend: () => void
  onRetryFailed: () => void
  onRefresh: () => void
  onUpdateMinutes?: (payload: MeetingMinutesUpdateRequest) => Promise<void>
  onRefreshContent?: () => Promise<void>
}

function formatDateTime(isoString: string | null | undefined): string {
  if (!isoString) return '—'
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(new Date(isoString))
  } catch {
    return isoString
  }
}

function formatDateOnly(isoString: string | null | undefined): string {
  if (!isoString) return '—'
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
    }).format(new Date(isoString))
  } catch {
    return isoString
  }
}

function MinutesStatusBadge({ status }: { status: MeetingMinutesStatus }) {
  switch (status) {
    case 'approved':
      return <span className="status-pill" style={{ background: '#e8f7f0', color: 'var(--success)' }}>Approved</span>
    case 'sent':
      return <span className="status-pill" style={{ background: '#e8f7f0', color: 'var(--success)' }}>Sent to Attendees</span>
    case 'pending_approval':
      return <span className="status-pill warning" style={{ background: '#fff3dc', color: 'var(--warning)' }}>Pending Approval</span>
    case 'failed':
      return <span className="status-pill" style={{ background: '#fde8e8', color: 'var(--danger)' }}>Delivery Failed</span>
    case 'draft':
    default:
      return <span className="status-pill muted" style={{ background: '#edf0f4', color: '#596579' }}>Draft</span>
  }
}

function DeliveryStatusBadge({ status }: { status: MeetingMinutesDeliveryStatus }) {
  switch (status) {
    case 'sent':
      return (
        <span
          className="status-pill"
          style={{ background: '#e8f7f0', color: 'var(--success)', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
        >
          <CheckCircle2 size={12} aria-hidden="true" /> Sent
        </span>
      )
    case 'failed':
      return (
        <span
          className="status-pill"
          style={{ background: '#fde8e8', color: 'var(--danger)', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
        >
          <AlertCircle size={12} aria-hidden="true" /> Failed
        </span>
      )
    case 'pending':
    default:
      return (
        <span
          className="status-pill warning"
          style={{ background: '#fff3dc', color: 'var(--warning)', display: 'inline-flex', alignItems: 'center', gap: '4px' }}
        >
          <Clock size={12} aria-hidden="true" /> Pending
        </span>
      )
  }
}

interface EditableActionItem {
  id?: string
  task_description: string
  owner_name: string
  owner_email?: string
  deadline_text: string
  priority: string
}

export function MeetingMinutesPanel({
  minutes,
  isOrganizer,
  loading,
  generating,
  approving,
  sending,
  retrying,
  updating = false,
  refreshingContent = false,
  error = '',
  feedback = null,
  onGenerate,
  onApprove,
  onSend,
  onRetryFailed,
  onRefresh,
  onUpdateMinutes,
  onRefreshContent,
}: MeetingMinutesPanelProps) {
  const [approvalNotes, setApprovalNotes] = useState('')
  const [showNotesInput, setShowNotesInput] = useState(false)

  // Edit mode state
  const [isEditing, setIsEditing] = useState(false)
  const [editSummary, setEditSummary] = useState('')
  const [editDecisions, setEditDecisions] = useState<string[]>([])
  const [editActionItems, setEditActionItems] = useState<EditableActionItem[]>([])
  const [savingEdit, setSavingEdit] = useState(false)
  const [editError, setEditError] = useState('')

  const isApproved = minutes?.status === 'approved' || minutes?.status === 'sent'
  const canApprove = isOrganizer && (minutes?.status === 'pending_approval' || minutes?.status === 'draft')
  const canSend = isOrganizer && isApproved
  const hasFailedRecipients = Boolean(
    minutes?.recipients && minutes.recipients.some((r) => r.delivery_status === 'failed'),
  )

  const handleApproveClick = () => {
    onApprove(approvalNotes.trim() ? approvalNotes.trim() : undefined)
    setShowNotesInput(false)
  }

  const handleStartEdit = () => {
    setEditSummary(minutes?.content_summary || '')
    setEditDecisions(minutes?.content_decisions ? [...minutes.content_decisions] : [])
    setEditActionItems(
      minutes?.content_action_items
        ? minutes.content_action_items.map((ai) => ({
            id: ai.id,
            task_description: ai.task_description || '',
            owner_name: ai.owner_name || '',
            owner_email: ai.owner_email || '',
            deadline_text: ai.deadline_text || '',
            priority: ai.priority || 'medium',
          }))
        : [],
    )
    setEditError('')
    setIsEditing(true)
  }

  const handleCancelEdit = () => {
    setIsEditing(false)
    setEditError('')
  }

  const handleSaveEdit = async () => {
    if (!onUpdateMinutes) return
    setSavingEdit(true)
    setEditError('')
    try {
      await onUpdateMinutes({
        content_summary: editSummary.trim() || null,
        content_decisions: editDecisions.map((d) => d.trim()).filter(Boolean),
        content_action_items: editActionItems
          .filter((ai) => ai.task_description.trim())
          .map((ai) => ({
            id: ai.id,
            task_description: ai.task_description.trim(),
            owner_name: ai.owner_name.trim() || null,
            owner_email: ai.owner_email?.trim() || null,
            deadline_at: null,
            deadline_text: ai.deadline_text.trim() || null,
            priority: ai.priority || 'medium',
          })),
      })
      setIsEditing(false)
    } catch (err) {
      setEditError(err instanceof Error ? err.message : 'Failed to save minutes changes')
    } finally {
      setSavingEdit(false)
    }
  }

  const handleAddDecision = () => {
    setEditDecisions([...editDecisions, ''])
  }

  const handleUpdateDecision = (index: number, val: string) => {
    const updated = [...editDecisions]
    updated[index] = val
    setEditDecisions(updated)
  }

  const handleRemoveDecision = (index: number) => {
    setEditDecisions(editDecisions.filter((_, i) => i !== index))
  }

  const handleAddActionItem = () => {
    setEditActionItems([
      ...editActionItems,
      {
        task_description: '',
        owner_name: '',
        deadline_text: '',
        priority: 'medium',
      },
    ])
  }

  const handleUpdateActionItem = (index: number, field: keyof EditableActionItem, val: string) => {
    const updated = [...editActionItems]
    updated[index] = { ...updated[index], [field]: val }
    setEditActionItems(updated)
  }

  const handleRemoveActionItem = (index: number) => {
    setEditActionItems(editActionItems.filter((_, i) => i !== index))
  }

  return (
    <section
      className="meeting-minutes-panel"
      aria-labelledby="meeting-minutes-title"
      style={{
        background: 'var(--surface)',
        padding: '1.5rem',
        borderRadius: '8px',
        border: '1px solid var(--border)',
        display: 'flex',
        flexDirection: 'column',
        gap: '1.5rem',
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: '1rem',
          borderBottom: '1px solid var(--border)',
          paddingBottom: '1rem',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', flexWrap: 'wrap' }}>
          <h2
            id="meeting-minutes-title"
            style={{ margin: 0, display: 'flex', alignItems: 'center', gap: '0.5rem', fontSize: '1.25rem' }}
          >
            <FileCheck size={20} aria-hidden="true" /> Meeting Minutes
          </h2>
          {minutes && <MinutesStatusBadge status={minutes.status} />}
        </div>

        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap' }}>
          {minutes && !isEditing && (
            <button
              type="button"
              className="button secondary"
              onClick={onRefresh}
              disabled={loading || generating || approving || sending || retrying || updating || refreshingContent}
              aria-label="Refresh minutes"
              style={{ minHeight: '34px', padding: '0 10px', fontSize: '12px' }}
            >
              <RefreshCw size={14} className={loading ? 'spin' : ''} aria-hidden="true" /> Refresh
            </button>
          )}

          {isOrganizer && minutes && !isEditing && onRefreshContent && (
            <button
              type="button"
              className="button secondary"
              onClick={() => void onRefreshContent()}
              disabled={refreshingContent || loading || generating || approving || sending || retrying}
              title="Re-sync summary and confirmed action items from the meeting"
              style={{ minHeight: '34px', padding: '0 10px', fontSize: '12px' }}
            >
              <RefreshCw size={14} className={refreshingContent ? 'spin' : ''} aria-hidden="true" />
              {refreshingContent ? 'Syncing…' : 'Sync from Meeting'}
            </button>
          )}

          {isOrganizer && !minutes && (
            <button
              type="button"
              className="button primary"
              onClick={onGenerate}
              disabled={generating || loading}
              style={{ minHeight: '34px', padding: '0 12px', fontSize: '13px' }}
            >
              <FileText size={14} aria-hidden="true" />
              {generating ? 'Generating Minutes…' : 'Generate Meeting Minutes'}
            </button>
          )}

          {isOrganizer && minutes && !isEditing && minutes.status !== 'sent' && (
            <button
              type="button"
              className="button secondary"
              onClick={handleStartEdit}
              disabled={loading || generating || approving || sending || retrying}
              style={{ minHeight: '34px', padding: '0 12px', fontSize: '13px' }}
            >
              <Edit3 size={14} aria-hidden="true" /> Edit Minutes
            </button>
          )}

          {isOrganizer && minutes && !isEditing && canApprove && (
            <div style={{ display: 'inline-flex', gap: '0.5rem', alignItems: 'center' }}>
              {showNotesInput ? (
                <div style={{ display: 'inline-flex', gap: '0.25rem', alignItems: 'center' }}>
                  <input
                    type="text"
                    placeholder="Approval notes (optional)"
                    value={approvalNotes}
                    onChange={(e) => setApprovalNotes(e.target.value)}
                    aria-label="Approval notes"
                    style={{
                      height: '34px',
                      padding: '0 8px',
                      borderRadius: '6px',
                      border: '1px solid var(--border)',
                      fontSize: '12px',
                    }}
                  />
                  <button
                    type="button"
                    className="button primary"
                    onClick={handleApproveClick}
                    disabled={approving}
                    style={{ minHeight: '34px', padding: '0 12px', fontSize: '13px' }}
                  >
                    {approving ? 'Approving…' : 'Confirm Approval'}
                  </button>
                  <button
                    type="button"
                    className="button secondary"
                    onClick={() => setShowNotesInput(false)}
                    style={{ minHeight: '34px', padding: '0 8px', fontSize: '12px' }}
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="button primary"
                  onClick={() => setShowNotesInput(true)}
                  disabled={approving}
                  style={{ minHeight: '34px', padding: '0 12px', fontSize: '13px' }}
                >
                  <FileCheck size={14} aria-hidden="true" />
                  {approving ? 'Approving…' : 'Approve Minutes'}
                </button>
              )}
            </div>
          )}

          {isOrganizer && minutes && !isEditing && (
            <button
              type="button"
              className="button primary"
              onClick={onSend}
              disabled={!canSend || sending || retrying}
              title={!canSend ? 'Minutes must be approved before sending' : 'Send minutes to attendees'}
              aria-disabled={!canSend}
              style={{
                minHeight: '34px',
                padding: '0 12px',
                fontSize: '13px',
                opacity: !canSend ? 0.6 : 1,
                cursor: !canSend ? 'not-allowed' : 'pointer',
              }}
            >
              <Send size={14} aria-hidden="true" />
              {sending ? 'Sending Minutes…' : 'Send Minutes'}
            </button>
          )}

          {isOrganizer && minutes && !isEditing && hasFailedRecipients && (
            <button
              type="button"
              className="button secondary"
              onClick={onRetryFailed}
              disabled={retrying || sending}
              style={{ minHeight: '34px', padding: '0 12px', fontSize: '13px', color: 'var(--danger)', borderColor: '#e7b6b6' }}
            >
              <RotateCcw size={14} aria-hidden="true" />
              {retrying ? 'Retrying…' : 'Retry Failed Deliveries'}
            </button>
          )}
        </div>
      </div>

      {feedback && (
        <div
          role={feedback.kind === 'error' ? 'alert' : 'status'}
          style={{
            padding: '0.75rem 1rem',
            borderRadius: '6px',
            fontSize: '0.9rem',
            background: feedback.kind === 'error' ? '#fde8e8' : '#e8f7f0',
            color: feedback.kind === 'error' ? 'var(--danger)' : 'var(--success)',
            border: `1px solid ${feedback.kind === 'error' ? '#f8b4b4' : '#a3e635'}`,
            display: 'flex',
            alignItems: 'center',
            gap: '0.5rem',
          }}
        >
          {feedback.kind === 'error' ? <AlertCircle size={16} /> : <CheckCircle2 size={16} />}
          <span>{feedback.message}</span>
        </div>
      )}

      {error && (
        <div
          role="alert"
          style={{
            padding: '0.75rem 1rem',
            borderRadius: '6px',
            fontSize: '0.9rem',
            background: '#fde8e8',
            color: 'var(--danger)',
            border: '1px solid #f8b4b4',
            display: 'flex',
            alignItems: 'center',
            gap: '0.5rem',
          }}
        >
          <AlertCircle size={16} />
          <span>{error}</span>
        </div>
      )}

      {editError && (
        <div
          role="alert"
          style={{
            padding: '0.75rem 1rem',
            borderRadius: '6px',
            fontSize: '0.9rem',
            background: '#fde8e8',
            color: 'var(--danger)',
            border: '1px solid #f8b4b4',
            display: 'flex',
            alignItems: 'center',
            gap: '0.5rem',
          }}
        >
          <AlertCircle size={16} />
          <span>{editError}</span>
        </div>
      )}

      {loading ? (
        <p role="status" style={{ color: 'var(--muted)' }}>Loading meeting minutes…</p>
      ) : !minutes ? (
        <div
          style={{
            padding: '2rem',
            textAlign: 'center',
            background: 'var(--surface)',
            border: '1px dashed var(--border)',
            borderRadius: '8px',
          }}
        >
          <FileText size={36} style={{ color: 'var(--muted)', marginBottom: '0.5rem' }} />
          <p style={{ margin: '0 0 0.5rem', fontWeight: 600 }}>No Meeting Minutes Generated Yet</p>
          <p style={{ margin: 0, color: 'var(--muted)', fontSize: '0.9rem' }}>
            {isOrganizer
              ? 'Click "Generate Meeting Minutes" to snapshot the summary, decisions, and confirmed action items.'
              : 'Meeting minutes have not yet been prepared by the organizer.'}
          </p>
        </div>
      ) : isEditing ? (
        /* Edit Mode */
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
          <div
            style={{
              padding: '0.75rem 1rem',
              background: '#eff6ff',
              borderRadius: '6px',
              border: '1px solid #bfdbfe',
              color: '#1e40af',
              fontSize: '0.9rem',
              display: 'flex',
              alignItems: 'center',
              gap: '0.5rem',
            }}
          >
            <Edit3 size={16} />
            <span>
              <strong>Edit Mode:</strong> You can edit the overview summary, add or modify decisions, and adjust action items. Saving will update the minutes and permit organizer approval before delivery.
            </span>
          </div>

          {/* Edit Summary */}
          <section>
            <label
              htmlFor="edit-minutes-summary"
              style={{ display: 'block', fontWeight: 600, marginBottom: '0.4rem', color: 'var(--navy)' }}
            >
              Summary Overview
            </label>
            <textarea
              id="edit-minutes-summary"
              rows={4}
              value={editSummary}
              onChange={(e) => setEditSummary(e.target.value)}
              placeholder="Enter comprehensive meeting summary overview..."
              style={{
                width: '100%',
                padding: '0.75rem',
                borderRadius: '6px',
                border: '1px solid var(--border)',
                fontFamily: 'inherit',
                fontSize: '0.95rem',
                lineHeight: 1.5,
              }}
            />
          </section>

          {/* Edit Decisions */}
          <section>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
              <label style={{ fontWeight: 600, color: 'var(--navy)' }}>Decisions</label>
              <button
                type="button"
                className="button secondary"
                onClick={handleAddDecision}
                style={{ padding: '0 8px', minHeight: '28px', fontSize: '12px' }}
              >
                <Plus size={14} /> Add Decision
              </button>
            </div>
            {editDecisions.length === 0 ? (
              <p style={{ margin: 0, color: 'var(--muted)', fontStyle: 'italic', fontSize: '0.9rem' }}>
                No decisions listed. Click "Add Decision" to record an agreed outcome.
              </p>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.5rem' }}>
                {editDecisions.map((dec, idx) => (
                  <div key={idx} style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                    <input
                      type="text"
                      value={dec}
                      onChange={(e) => handleUpdateDecision(idx, e.target.value)}
                      placeholder={`Decision #${idx + 1}`}
                      style={{
                        flex: 1,
                        height: '36px',
                        padding: '0 10px',
                        borderRadius: '6px',
                        border: '1px solid var(--border)',
                        fontSize: '0.9rem',
                      }}
                    />
                    <button
                      type="button"
                      className="button secondary"
                      onClick={() => handleRemoveDecision(idx)}
                      title="Remove decision"
                      aria-label="Remove decision"
                      style={{ height: '36px', padding: '0 8px', color: 'var(--danger)' }}
                    >
                      <Trash2 size={14} />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* Edit Action Items */}
          <section>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.5rem' }}>
              <label style={{ fontWeight: 600, color: 'var(--navy)' }}>Action Items & Deliverables</label>
              <button
                type="button"
                className="button secondary"
                onClick={handleAddActionItem}
                style={{ padding: '0 8px', minHeight: '28px', fontSize: '12px' }}
              >
                <Plus size={14} /> Add Action Item
              </button>
            </div>
            {editActionItems.length === 0 ? (
              <p style={{ margin: 0, color: 'var(--muted)', fontStyle: 'italic', fontSize: '0.9rem' }}>
                No action items listed. Click "Add Action Item" to record commitments and deliverables.
              </p>
            ) : (
              <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                {editActionItems.map((item, idx) => (
                  <div
                    key={idx}
                    style={{
                      padding: '0.75rem',
                      borderRadius: '6px',
                      border: '1px solid var(--border)',
                      background: '#f8fafc',
                      display: 'flex',
                      flexDirection: 'column',
                      gap: '0.5rem',
                    }}
                  >
                    <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                      <input
                        type="text"
                        placeholder="Task description (required)"
                        value={item.task_description}
                        onChange={(e) => handleUpdateActionItem(idx, 'task_description', e.target.value)}
                        style={{
                          flex: 2,
                          height: '34px',
                          padding: '0 8px',
                          borderRadius: '6px',
                          border: '1px solid var(--border)',
                          fontSize: '0.9rem',
                        }}
                      />
                      <button
                        type="button"
                        className="button secondary"
                        onClick={() => handleRemoveActionItem(idx)}
                        title="Remove action item"
                        aria-label="Remove action item"
                        style={{ height: '34px', padding: '0 8px', color: 'var(--danger)' }}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                    <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
                      <input
                        type="text"
                        placeholder="Owner name (e.g. Rohan)"
                        value={item.owner_name}
                        onChange={(e) => handleUpdateActionItem(idx, 'owner_name', e.target.value)}
                        style={{
                          flex: 1,
                          minWidth: '140px',
                          height: '32px',
                          padding: '0 8px',
                          borderRadius: '6px',
                          border: '1px solid var(--border)',
                          fontSize: '0.85rem',
                        }}
                      />
                      <input
                        type="text"
                        placeholder="Owner email (optional)"
                        value={item.owner_email || ''}
                        onChange={(e) => handleUpdateActionItem(idx, 'owner_email', e.target.value)}
                        style={{
                          flex: 1,
                          minWidth: '160px',
                          height: '32px',
                          padding: '0 8px',
                          borderRadius: '6px',
                          border: '1px solid var(--border)',
                          fontSize: '0.85rem',
                        }}
                      />
                      <input
                        type="text"
                        placeholder="Deadline (e.g. tomorrow, 2026-10-18)"
                        value={item.deadline_text}
                        onChange={(e) => handleUpdateActionItem(idx, 'deadline_text', e.target.value)}
                        style={{
                          flex: 1,
                          minWidth: '140px',
                          height: '32px',
                          padding: '0 8px',
                          borderRadius: '6px',
                          border: '1px solid var(--border)',
                          fontSize: '0.85rem',
                        }}
                      />
                      <select
                        value={item.priority}
                        onChange={(e) => handleUpdateActionItem(idx, 'priority', e.target.value)}
                        style={{
                          height: '32px',
                          padding: '0 8px',
                          borderRadius: '6px',
                          border: '1px solid var(--border)',
                          fontSize: '0.85rem',
                        }}
                      >
                        <option value="low">Low</option>
                        <option value="medium">Medium</option>
                        <option value="high">High</option>
                        <option value="urgent">Urgent</option>
                      </select>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>

          {/* Edit Actions */}
          <div style={{ display: 'flex', gap: '0.75rem', justifyContent: 'flex-end', borderTop: '1px solid var(--border)', paddingTop: '1rem' }}>
            <button
              type="button"
              className="button secondary"
              onClick={handleCancelEdit}
              disabled={savingEdit}
            >
              Cancel
            </button>
            <button
              type="button"
              className="button primary"
              onClick={handleSaveEdit}
              disabled={savingEdit}
              style={{ display: 'inline-flex', alignItems: 'center', gap: '6px' }}
            >
              <Save size={14} />
              {savingEdit ? 'Saving Changes…' : 'Save Minutes'}
            </button>
          </div>
        </div>
      ) : (
        /* View Mode */
        <div style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
          {/* Approval Meta Information */}
          <div
            style={{
              padding: '0.75rem 1rem',
              background: '#f8fafc',
              borderRadius: '6px',
              border: '1px solid var(--border)',
              display: 'flex',
              flexWrap: 'wrap',
              gap: '1.5rem',
              fontSize: '0.85rem',
              color: 'var(--muted)',
            }}
          >
            <div>
              <strong>Approval Policy:</strong>{' '}
              {minutes.policy === 'manual_approval' ? 'Manual Organizer Approval' : 'Automatic Send'}
            </div>
            <div>
              <strong>Status:</strong>{' '}
              <span style={{ textTransform: 'capitalize' }}>{minutes.status.replace('_', ' ')}</span>
            </div>
            {minutes.approved_at && (
              <div>
                <strong>Approved At:</strong> {formatDateTime(minutes.approved_at)}
              </div>
            )}
            {minutes.approved_by && (
              <div>
                <strong>Approved By:</strong> {minutes.approved_by}
              </div>
            )}
            {!isApproved && (
              <div style={{ color: 'var(--warning)', display: 'flex', alignItems: 'center', gap: '4px' }}>
                <AlertTriangle size={14} /> Approval required before delivery
              </div>
            )}
          </div>

          {/* Summary / Overview */}
          <section aria-labelledby="minutes-summary-section">
            <h3
              id="minutes-summary-section"
              style={{ fontSize: '1rem', margin: '0 0 0.5rem', color: 'var(--navy)' }}
            >
              Summary Overview
            </h3>
            <p style={{ margin: 0, lineHeight: 1.6, color: '#334155' }}>
              {minutes.content_summary || 'No overview summary available.'}
            </p>
          </section>

          {/* Decisions */}
          <section aria-labelledby="minutes-decisions-section">
            <h3
              id="minutes-decisions-section"
              style={{ fontSize: '1rem', margin: '0 0 0.5rem', color: 'var(--navy)' }}
            >
              Decisions
            </h3>
            {minutes.content_decisions && minutes.content_decisions.length > 0 ? (
              <ul style={{ margin: 0, paddingLeft: '1.25rem', lineHeight: 1.6, color: '#334155' }}>
                {minutes.content_decisions.map((decision, idx) => (
                  <li key={idx}>{decision}</li>
                ))}
              </ul>
            ) : (
              <p style={{ margin: 0, color: 'var(--muted)', fontStyle: 'italic' }}>
                No decisions recorded.
              </p>
            )}
          </section>

          {/* Confirmed Action Items */}
          <section aria-labelledby="minutes-action-items-section">
            <h3
              id="minutes-action-items-section"
              style={{ fontSize: '1rem', margin: '0 0 0.5rem', color: 'var(--navy)' }}
            >
              Action Items & Deliverables
            </h3>
            {minutes.content_action_items && minutes.content_action_items.length > 0 ? (
              <div style={{ overflowX: 'auto' }}>
                <table
                  style={{
                    width: '100%',
                    borderCollapse: 'collapse',
                    textAlign: 'left',
                    fontSize: '0.9rem',
                  }}
                >
                  <thead>
                    <tr style={{ borderBottom: '2px solid var(--border)' }}>
                      <th style={{ padding: '0.6rem 0.5rem' }}>Description</th>
                      <th style={{ padding: '0.6rem 0.5rem' }}>Owner</th>
                      <th style={{ padding: '0.6rem 0.5rem' }}>Resolved Date / Deadline</th>
                      <th style={{ padding: '0.6rem 0.5rem' }}>Priority</th>
                    </tr>
                  </thead>
                  <tbody>
                    {minutes.content_action_items.map((item, idx) => (
                      <tr key={item.id || idx} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '0.6rem 0.5rem', fontWeight: 500 }}>
                          {item.task_description}
                        </td>
                        <td style={{ padding: '0.6rem 0.5rem', color: 'var(--muted)' }}>
                          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                            <User size={13} /> {item.owner_name || 'Unassigned'}
                            {item.owner_email && (
                              <span style={{ fontSize: '0.8rem', color: 'var(--muted)' }}>
                                ({item.owner_email})
                              </span>
                            )}
                          </span>
                        </td>
                        <td style={{ padding: '0.6rem 0.5rem', color: 'var(--muted)' }}>
                          <span style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>
                            <Calendar size={13} />
                            {item.deadline_at
                              ? formatDateOnly(item.deadline_at)
                              : item.deadline_text || 'No deadline'}
                          </span>
                        </td>
                        <td style={{ padding: '0.6rem 0.5rem' }}>
                          <span
                            className="status-pill muted"
                            style={{
                              textTransform: 'capitalize',
                              fontSize: '11px',
                              padding: '2px 6px',
                            }}
                          >
                            {item.priority || 'medium'}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p style={{ margin: 0, color: 'var(--muted)', fontStyle: 'italic' }}>
                No action items recorded in these minutes.
              </p>
            )}
          </section>

          {/* Attendees / Recipient Delivery Status */}
          <section aria-labelledby="minutes-recipients-section">
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                margin: '0 0 0.5rem',
              }}
            >
              <h3
                id="minutes-recipients-section"
                style={{ fontSize: '1rem', margin: 0, color: 'var(--navy)', display: 'flex', alignItems: 'center', gap: '0.5rem' }}
              >
                <Mail size={16} aria-hidden="true" /> Recipient Deliveries ({minutes.recipients.length})
              </h3>
            </div>

            {minutes.recipients && minutes.recipients.length > 0 ? (
              <div style={{ overflowX: 'auto' }}>
                <table
                  style={{
                    width: '100%',
                    borderCollapse: 'collapse',
                    textAlign: 'left',
                    fontSize: '0.9rem',
                  }}
                >
                  <thead>
                    <tr style={{ borderBottom: '2px solid var(--border)' }}>
                      <th style={{ padding: '0.6rem 0.5rem' }}>Attendee</th>
                      <th style={{ padding: '0.6rem 0.5rem' }}>Email Address</th>
                      <th style={{ padding: '0.6rem 0.5rem' }}>Status</th>
                      <th style={{ padding: '0.6rem 0.5rem' }}>Sent At</th>
                      <th style={{ padding: '0.6rem 0.5rem' }}>Details</th>
                    </tr>
                  </thead>
                  <tbody>
                    {minutes.recipients.map((recipient: MeetingMinutesRecipient) => (
                      <tr key={recipient.id} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '0.6rem 0.5rem', fontWeight: 500 }}>
                          {recipient.recipient_name}
                        </td>
                        <td style={{ padding: '0.6rem 0.5rem', color: '#334155' }}>
                          {recipient.recipient_email}
                        </td>
                        <td style={{ padding: '0.6rem 0.5rem' }}>
                          <DeliveryStatusBadge status={recipient.delivery_status} />
                        </td>
                        <td style={{ padding: '0.6rem 0.5rem', color: 'var(--muted)', fontSize: '0.85rem' }}>
                          {formatDateTime(recipient.sent_at)}
                        </td>
                        <td style={{ padding: '0.6rem 0.5rem', fontSize: '0.85rem' }}>
                          {recipient.error_message ? (
                            <span style={{ color: 'var(--danger)' }}>{recipient.error_message}</span>
                          ) : (
                            <span style={{ color: 'var(--muted)' }}>—</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p style={{ margin: 0, color: 'var(--muted)', fontStyle: 'italic' }}>
                No attendee recipients recorded for this meeting.
              </p>
            )}
          </section>
        </div>
      )}
    </section>
  )
}
