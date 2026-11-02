import { FormEvent, useEffect, useState } from 'react'
import { UserRoundMinus, X } from 'lucide-react'
import {
  addMeetingParticipants,
  cancelMeeting,
  getMeetingAudit,
  getMeetingParticipants,
  removeMeetingParticipant,
} from '../../api/meetings'
import type { MeetingAuditEntry, MeetingParticipantRecord } from '../../types/meeting'

interface MeetingManageDialogProps {
  meetingId: string
  meetingTitle: string
  meetingStatus: string
  cancellationPending: boolean
  onClose: () => void
  onChanged: () => void
}

function resultMessage(message: string, failures: string[]): string {
  return failures.length > 0
    ? `${message} Notification failed for: ${failures.join(', ')}`
    : message
}

const EMAIL_PATTERN = /^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,63}$/i
const PARTICIPANT_ACTIONS: Record<string, string> = {
  participant_added: 'Participant Added',
  participant_removed: 'Participant Removed',
}

function detailText(details: Record<string, unknown>, key: string): string {
  const value = details[key]
  return typeof value === 'string' && value.trim() ? value : 'Not available'
}

function actionTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Not available' : date.toLocaleString()
}

function participantEmailStatus(entry: MeetingAuditEntry): string {
  const notifications = entry.details.notifications
  if (!Array.isArray(notifications)) return 'Not Sent'

  const email = entry.details.email
  const matchingNotification = notifications.find((notification) => (
    typeof notification === 'object'
    && notification !== null
    && 'recipient' in notification
    && typeof notification.recipient === 'string'
    && typeof email === 'string'
    && notification.recipient.toLowerCase() === email.toLowerCase()
  ))
  const notification = matchingNotification ?? notifications[0]
  if (typeof notification !== 'object' || notification === null || !('status' in notification)) {
    return 'Not Sent'
  }

  switch (notification.status) {
    case 'sent': return 'Sent'
    case 'failed': return 'Failed'
    case 'pending': return 'Pending'
    default: return 'Not Sent'
  }
}

export function MeetingManageDialog({
  meetingId,
  meetingTitle,
  meetingStatus,
  cancellationPending,
  onClose,
  onChanged,
}: MeetingManageDialogProps) {
  const [participants, setParticipants] = useState<MeetingParticipantRecord[]>([])
  const [auditEntries, setAuditEntries] = useState<MeetingAuditEntry[]>([])
  const [email, setEmail] = useState('')
  const [loading, setLoading] = useState(true)
  const [auditLoading, setAuditLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [auditError, setAuditError] = useState('')
  const [notice, setNotice] = useState('')

  async function reloadAudit(isActive: () => boolean = () => true) {
    try {
      const result = await getMeetingAudit(meetingId)
      if (isActive()) {
        setAuditEntries(result)
        setAuditError('')
      }
    } catch (reason: unknown) {
      if (isActive()) {
        setAuditError(reason instanceof Error ? reason.message : 'Meeting history could not be loaded.')
      }
    }
  }

  useEffect(() => {
    let active = true
    getMeetingParticipants(meetingId)
      .then((result) => { if (active) setParticipants(result) })
      .catch((reason: unknown) => {
        if (active) setError(reason instanceof Error ? reason.message : 'Participants could not be loaded.')
      })
      .finally(() => { if (active) setLoading(false) })
    void reloadAudit(() => active).finally(() => { if (active) setAuditLoading(false) })
    return () => { active = false }
  }, [meetingId])

  async function reloadParticipants() {
    const result = await getMeetingParticipants(meetingId)
    setParticipants(result)
  }

  async function handleAdd(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const addresses = email.split(',').map((address) => address.trim())
      const uniqueAddresses: string[] = []
      const duplicateAddresses: string[] = []
      const seenAddresses = new Set<string>()
      const invalidAddresses: string[] = []
      const existingAddresses = new Set(
        participants.map((participant) => participant.email?.trim().toLowerCase()).filter(
          (address): address is string => Boolean(address),
        ),
      )
      const alreadyPresent: string[] = []

      for (const address of addresses) {
        const normalized = address.toLowerCase()
        if (!EMAIL_PATTERN.test(normalized)) {
          invalidAddresses.push(address || '(empty address)')
        } else if (seenAddresses.has(normalized)) {
          duplicateAddresses.push(normalized)
        } else {
          seenAddresses.add(normalized)
          if (existingAddresses.has(normalized)) alreadyPresent.push(normalized)
          else uniqueAddresses.push(normalized)
        }
      }

      const result = uniqueAddresses.length > 0
        ? await addMeetingParticipants(meetingId, uniqueAddresses)
        : {
            message: '',
            added: [],
            already_present: [],
            duplicate_addresses: [],
            invalid_addresses: [],
            failed_addresses: [],
            notifications_sent: [],
            notification_failures: [],
          }
      const allInvalid = [...invalidAddresses, ...result.invalid_addresses]
      const allAlreadyPresent = [...alreadyPresent, ...result.already_present]
      const allDuplicates = [...duplicateAddresses, ...result.duplicate_addresses]
      const failedAddresses = [...result.failed_addresses, ...result.notification_failures]
      const failedCount = allInvalid.length + failedAddresses.length
      const skippedCount = allAlreadyPresent.length + allDuplicates.length
      const addedCount = result.added.length
      const summary = `${addedCount} participant${addedCount === 1 ? '' : 's'} added; ${failedCount} address${failedCount === 1 ? '' : 'es'} were invalid or failed.`
      const details = [
        allInvalid.length > 0 ? `Invalid: ${allInvalid.join(', ')}` : '',
        result.failed_addresses.length > 0 ? `Could not add: ${result.failed_addresses.join(', ')}` : '',
        result.notification_failures.length > 0 ? `Invitation failed: ${result.notification_failures.join(', ')}` : '',
        skippedCount > 0 ? `${skippedCount} duplicate or already invited address${skippedCount === 1 ? '' : 'es'} skipped.` : '',
      ].filter(Boolean)
      setNotice([summary, ...details].join(' '))
      setEmail('')
      await reloadParticipants()
      await reloadAudit()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Participant could not be added.')
    } finally {
      setBusy(false)
    }
  }

  async function handleRemove(participant: MeetingParticipantRecord) {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await removeMeetingParticipant(meetingId, participant.id)
      setNotice(resultMessage(`Update sent to ${participant.email ?? participant.name}.`, result.notification_failures))
      await reloadParticipants()
      await reloadAudit()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Participant could not be removed.')
    } finally {
      setBusy(false)
    }
  }

  async function handleCancelMeeting() {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await cancelMeeting(meetingId)
      const message = result.status === 'pending_approval'
        ? 'Cancellation is pending organizer or delegate approval.'
        : 'Meeting cancelled.'
      setNotice(resultMessage(message, result.notification_failures))
      await reloadAudit()
      onChanged()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Meeting could not be cancelled.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="meet-modal-overlay" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget && !busy) onClose()
    }}>
      <section
        className="meet-modal meeting-manage-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby={`manage-meeting-${meetingId}`}
      >
        <div className="meeting-manage-heading">
          <div>
            <p className="eyebrow light">Meeting controls</p>
            <h2 id={`manage-meeting-${meetingId}`}>{meetingTitle}</h2>
          </div>
          <button className="task-calendar-close" type="button" aria-label="Close meeting controls" onClick={onClose} disabled={busy}>
            <X size={18} aria-hidden="true" />
          </button>
        </div>

        {error && <p className="meeting-manage-error" role="alert">{error}</p>}
        {notice && <p className="meeting-manage-notice" role="status">{notice}</p>}

        <section className="meeting-manage-section" aria-labelledby="meeting-participants-heading">
          <h3 id="meeting-participants-heading">Participants</h3>
          {loading ? <p role="status">Loading participants…</p> : participants.length === 0 ? (
            <p>No participants are currently invited.</p>
          ) : (
            <ul className="meeting-manage-participants">
              {participants.map((participant) => (
                <li key={participant.id}>
                  <span><strong>{participant.name}</strong><small>{participant.email ?? 'No email address'}</small></span>
                  <button
                    type="button"
                    className="button minimal icon-only"
                    aria-label={`Remove ${participant.name}`}
                    title={`Remove ${participant.name}`}
                    onClick={() => void handleRemove(participant)}
                    disabled={busy}
                  >
                    <UserRoundMinus size={16} aria-hidden="true" />
                  </button>
                </li>
              ))}
            </ul>
          )}
          <form className="meeting-add-participant" onSubmit={(event) => void handleAdd(event)}>
            <label htmlFor={`participant-email-${meetingId}`}>Add by email</label>
            <div>
              <input
                id={`participant-email-${meetingId}`}
                type="text"
                inputMode="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                placeholder="name@example.com, another@example.com"
                required
              />
              <button className="button secondary" type="submit" disabled={busy || !email.trim()}>
                Invite
              </button>
            </div>
          </form>
        </section>

        <section className="meeting-manage-section" aria-labelledby="meeting-audit-heading">
          <h3 id="meeting-audit-heading">Change history</h3>
          {auditError && <p className="meeting-manage-error" role="alert">{auditError}</p>}
          {auditLoading ? <p role="status">Loading meeting history…</p> : !auditError && auditEntries.length === 0 ? (
            <p>No recorded changes.</p>
          ) : !auditError && (
            <ul className="meeting-manage-audit">
              {auditEntries.map((entry) => (
                <li key={entry.id}>
                  <strong>{PARTICIPANT_ACTIONS[entry.action] ?? entry.action.replaceAll('_', ' ')}</strong>
                  <small>Meeting ID: {entry.meeting_id || 'Not available'}</small>
                  <small>Date and time: {actionTime(entry.created_at)}</small>
                  {PARTICIPANT_ACTIONS[entry.action] ? (
                    <>
                      <small>User ID: {detailText(entry.details, 'user_id')}</small>
                      <small>User Email: {detailText(entry.details, 'email')}</small>
                      <small>Email notification status: {participantEmailStatus(entry)}</small>
                    </>
                  ) : (
                    <small>User ID: {entry.actor_id || 'Not available'}</small>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>

        <section className="meeting-manage-section meeting-cancel-section" aria-labelledby="cancel-meeting-heading">
          <h3 id="cancel-meeting-heading">Cancel event</h3>
          <p>
            {cancellationPending
              ? 'A cancellation request is awaiting organizer or delegate approval.'
              : 'Attendees will be notified. More than 20 attendees requires organizer or delegate approval.'}
          </p>
          <button
            className="button danger"
            type="button"
            onClick={() => void handleCancelMeeting()}
            disabled={busy || cancellationPending || meetingStatus === 'active' || meetingStatus === 'instant' || meetingStatus === 'cancelled'}
          >
            {busy ? 'Saving…' : cancellationPending ? 'Approval pending' : 'Cancel meeting'}
          </button>
        </section>
      </section>
    </div>
  )
}
