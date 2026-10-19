import { useState } from 'react'
import { Clock3, Users, Video, FileText, Link as LinkIcon, Trash2, UserRoundCog } from 'lucide-react'
import { Link } from 'react-router-dom'
import { deleteMeeting } from '../../api/meetings'
import { useAuth } from '../../contexts/AuthContext'
import type { Meeting } from '../../types/meeting'
import { MeetingManageDialog } from './MeetingManageDialog'

interface MeetingCardProps {
  meeting: Meeting
  onDelete?: (meetingId: string) => void
  cancellationPending?: boolean
  onChanged?: () => void
}

export function MeetingCard({ meeting, onDelete, cancellationPending = false, onChanged }: MeetingCardProps) {
  const { user } = useAuth()
  const isOrganizer = user?.id === meeting.organizerId
  const canManage = meeting.canManage ?? user?.id === meeting.organizerId
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState('')
  const [showManage, setShowManage] = useState(false)

  const lifecycle = meeting.lifecycle ?? (
    meeting.status.toLowerCase() === 'cancelled'
      ? 'cancelled'
      :
    meeting.status === 'In progress' || meeting.status === 'Starting soon'
      ? 'active'
      : meeting.status === 'Completed'
        ? 'completed'
        : 'upcoming'
  )

  const actionLabel = lifecycle === 'active'
    ? 'Join meeting'
    : 'Join meeting'

  async function handleDelete() {
    if (!user) return
    setDeleting(true)
    setDeleteError('')
    try {
      await deleteMeeting(meeting.id, user.id)
      setShowDeleteConfirm(false)
      onDelete?.(meeting.id)
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : 'Could not delete meeting.')
      setDeleting(false)
    }
  }

  return (
    <article className="meeting-card">
      {/* Delete confirmation dialog */}
      {showDeleteConfirm && (
        <div
          className="meet-modal-overlay"
          role="dialog"
          aria-modal="true"
          aria-labelledby={`delete-confirm-${meeting.id}`}
          style={{ position: 'fixed', zIndex: 1000 }}
        >
          <div className="meet-modal">
            <h3 id={`delete-confirm-${meeting.id}`}>Delete this meeting?</h3>
            <p>This will permanently remove the meeting and its attendance report.</p>
            {deleteError && <p style={{ color: 'var(--error, #e53)', fontSize: '13px' }}>{deleteError}</p>}
            <div className="meet-modal-actions">
              <button
                className="button secondary"
                onClick={() => { setShowDeleteConfirm(false); setDeleteError('') }}
                disabled={deleting}
              >
                Cancel
              </button>
              <button
                className="button danger"
                onClick={() => void handleDelete()}
                disabled={deleting}
              >
                {deleting ? 'Deleting…' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}

      {showManage && user && (
        <MeetingManageDialog
          meetingId={meeting.id}
          meetingTitle={meeting.title}
          meetingStatus={meeting.status.toLowerCase()}
          cancellationPending={cancellationPending}
          onClose={() => setShowManage(false)}
          onChanged={() => onChanged?.()}
        />
      )}

      <div className="meeting-icon" aria-hidden="true"><Video size={21} /></div>
      <div className="meeting-info">
        <div className="meeting-heading">
          <h3>{meeting.title}</h3>
          <span className={`status-pill ${lifecycle === 'active' || cancellationPending ? 'warning' : lifecycle === 'completed' || lifecycle === 'cancelled' ? 'muted' : ''}`}>
            {cancellationPending ? 'Cancellation pending' : lifecycle === 'active' ? 'In progress' : lifecycle === 'cancelled' ? 'Cancelled' : lifecycle === 'completed' ? 'Completed' : 'Upcoming'}
          </span>
        </div>
        {meeting.agenda && <p className="meeting-agenda">{meeting.agenda}</p>}
        <div className="meeting-meta">
          <span><Clock3 size={15} aria-hidden="true" /> {meeting.startTime} – {meeting.endTime}</span>
          <span className="meeting-status-text">{meeting.status}</span>
          {meeting.organizer && <span>Organizer: {meeting.organizer}</span>}
          {meeting.participants !== undefined && <span><Users size={15} aria-hidden="true" /> {meeting.participants} participants</span>}
        </div>
      </div>

      <div className="meeting-card-actions" style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
        {lifecycle === 'completed' || lifecycle === 'cancelled' ? (
          <>
            {isOrganizer && lifecycle === 'completed' && (
              <Link
                className="button secondary meeting-card-action"
                to={`/meetings/${encodeURIComponent(meeting.id)}/report`}
                aria-label={`View report for ${meeting.title}`}
              >
                <FileText size={16} /> View report
              </Link>
            )}
            {isOrganizer && (
              <button
                className="button danger meeting-card-action"
                aria-label={`Delete ${meeting.title}`}
                onClick={() => setShowDeleteConfirm(true)}
                style={{ display: 'flex', alignItems: 'center', gap: '4px' }}
              >
                <Trash2 size={16} /> Delete
              </button>
            )}
            {(lifecycle === 'cancelled' || !isOrganizer) && (
              <span style={{ fontSize: '13px', color: 'var(--muted)' }}>{lifecycle === 'cancelled' ? 'Cancelled' : 'Completed'}</span>
            )}
          </>
        ) : (
          <>
            {canManage && (
              <button
                className="button secondary meeting-card-action"
                type="button"
                onClick={() => setShowManage(true)}
                aria-label={`Manage ${meeting.title}`}
              >
                <UserRoundCog size={16} aria-hidden="true" /> Manage
              </button>
            )}
            <Link
              className={`button ${lifecycle === 'active' ? 'primary' : 'secondary'} meeting-card-action`}
              to={`/meetings/${encodeURIComponent(meeting.id)}`}
              aria-label={`${actionLabel} ${meeting.title}`}
            >
              {actionLabel}
            </Link>
            <button
              className="button minimal icon-only"
              title="Copy link"
              onClick={() => navigator.clipboard.writeText(`${window.location.origin}/meetings/${encodeURIComponent(meeting.id)}`)}
            >
              <LinkIcon size={16} />
            </button>
          </>
        )}
      </div>
    </article>
  )
}
