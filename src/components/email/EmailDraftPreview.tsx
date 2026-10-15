import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { AlertTriangle, Mail, RotateCcw, Send, ShieldAlert, Trash2, Users, X } from 'lucide-react'
import {
  cancelEmailDraft,
  discardEmailDraft,
  retryEmailDraft,
  sendEmailDraft,
  submitEmailDraft,
  updateEmailDraft,
  type DirectoryGroup,
  type EmailDraft,
  type EmailDraftStatus,
} from '../../api/emailDrafts'
import type { TaskActor } from '../../api/tasks'
import { EmailHistory, EmailLifecycleTracker, EmailStatusDetail, EmailStatusPill } from './EmailLifecycle'

/** Content can change only before delivery starts; editing during Approval withdraws the request. */
const EDITABLE: EmailDraftStatus[] = ['draft', 'approval']

interface Props {
  draft: EmailDraft
  actor: TaskActor
  groups?: DirectoryGroup[]
  /** Approvers see the full preview but cannot edit or send. */
  readOnly?: boolean
  onChange?: (draft: EmailDraft) => void
}

export { EmailStatusPill as EmailDraftStatusPill }

export function EmailDraftPreview({ draft, actor, groups = [], readOnly = false, onChange }: Props) {
  const [subject, setSubject] = useState(draft.subject)
  const [body, setBody] = useState(draft.body)
  const [newRecipient, setNewRecipient] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [acknowledged, setAcknowledged] = useState(false)

  useEffect(() => {
    setSubject(draft.subject)
    setBody(draft.body)
    setAcknowledged(false)
    setConfirming(false)
  }, [draft.id, draft.updated_at, draft.subject, draft.body])

  const editable = !readOnly && EDITABLE.includes(draft.status)
  const dirty = subject !== draft.subject || body !== draft.body
  const findings = draft.sensitive_findings
  const approved = draft.approval_decision === 'approved'
  const awaitingApproval = draft.status === 'approval'
  const needsApproval = draft.approval_required && draft.status === 'draft' && !approved
  const canSend =
    !readOnly &&
    !dirty &&
    draft.recipient_count > 0 &&
    draft.status === 'draft' &&
    (!draft.approval_required || approved)
  const canRetry = !readOnly && draft.status === 'failed'
  const canCancel = !readOnly && draft.status === 'queued'
  const failedResults = draft.delivery_results.filter((result) => result.status === 'failed')

  async function run(action: () => Promise<EmailDraft>) {
    setBusy(true)
    setError('')
    try {
      const updated = await action()
      onChange?.(updated)
      return true
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Something went wrong.')
      return false
    } finally {
      setBusy(false)
    }
  }

  function removeRecipient(email: string) {
    const remaining = draft.recipients.filter((r) => r.email !== email).map((r) => r.email)
    void run(() => updateEmailDraft(draft.id, { subject, body, recipient_emails: remaining }, actor))
  }

  async function addRecipient(event: FormEvent) {
    event.preventDefault()
    const value = newRecipient.trim()
    if (!value) return
    const group = groups.find((g) => g.name.toLowerCase() === value.toLowerCase())
    // Unsaved subject/body edits go along so the refreshed preview keeps them.
    const edits = group
      ? { subject, body, add_groups: [group.name] }
      : { subject, body, recipient_emails: [...draft.recipients.map((r) => r.email), value] }
    if (await run(() => updateEmailDraft(draft.id, edits, actor))) setNewRecipient('')
  }

  const titleId = `email-draft-${draft.id}`

  return (
    <article className="email-draft" aria-labelledby={titleId}>
      <header className="email-draft-header">
        <div className="email-draft-title">
          <Mail size={18} aria-hidden="true" />
          <strong id={titleId}>E-mail preview</strong>
          <span className="email-draft-source">
            {draft.source === 'llm' ? 'Written by AI' : `From template${draft.template_key ? ` · ${draft.template_key.replace(/_/g, ' ')}` : ''}`}
          </span>
        </div>
        <EmailStatusPill status={draft.status} />
      </header>

      <EmailLifecycleTracker draft={draft} />
      <EmailStatusDetail draft={draft} />

      {draft.generation_note && <p className="email-draft-note">{draft.generation_note}</p>}

      <section className="email-draft-field" aria-label="Recipients">
        <div className="email-draft-label">
          <Users size={14} aria-hidden="true" /> To
          <span className="email-draft-count">
            {draft.recipient_count} recipient{draft.recipient_count === 1 ? '' : 's'}
          </span>
        </div>
        {draft.recipient_groups.length > 0 && (
          <p className="email-draft-groups">
            {draft.recipient_groups.map((group) => `${group.name}: ${group.count}`).join(' · ')}
          </p>
        )}
        <ul className="email-recipient-list">
          {draft.recipients.map((recipient) => (
            <li key={recipient.email} className={`email-recipient${recipient.internal ? '' : ' external'}`}>
              <span title={`${recipient.email} · ${recipient.source}`}>
                {recipient.name === recipient.email ? recipient.email : `${recipient.name} <${recipient.email}>`}
              </span>
              {!recipient.internal && <span className="email-recipient-tag">External</span>}
              {editable && (
                <button
                  type="button"
                  aria-label={`Remove ${recipient.email}`}
                  onClick={() => removeRecipient(recipient.email)}
                  disabled={busy}
                >
                  <X size={12} aria-hidden="true" />
                </button>
              )}
            </li>
          ))}
          {draft.recipients.length === 0 && (
            <li className="email-draft-empty">No recipients found in your request. Add one below.</li>
          )}
        </ul>
        {draft.unresolved_recipients.length > 0 && (
          <p className="task-manage-feedback error">
            Could not resolve: {draft.unresolved_recipients.join(', ')}
          </p>
        )}
        {editable && (
          <form className="email-add-recipient" onSubmit={(event) => void addRecipient(event)}>
            <input
              list={`groups-${draft.id}`}
              aria-label="Add recipient e-mail or team"
              placeholder="Add an e-mail address or team"
              value={newRecipient}
              onChange={(event) => setNewRecipient(event.target.value)}
              disabled={busy}
            />
            <datalist id={`groups-${draft.id}`}>
              {groups.map((group) => (
                <option key={group.name} value={group.name}>{`${group.name} (${group.count})`}</option>
              ))}
            </datalist>
            <button className="button secondary" type="submit" disabled={busy || !newRecipient.trim()}>
              Add
            </button>
          </form>
        )}
      </section>

      <label className="email-draft-field">
        <span className="email-draft-label">Subject</span>
        <input
          value={subject}
          onChange={(event) => setSubject(event.target.value)}
          readOnly={!editable}
          maxLength={255}
        />
      </label>

      <label className="email-draft-field">
        <span className="email-draft-label">Message</span>
        <textarea
          value={body}
          onChange={(event) => setBody(event.target.value)}
          readOnly={!editable}
          rows={Math.min(18, Math.max(8, body.split('\n').length + 1))}
        />
      </label>

      {draft.approval_required && (
        <section className="email-draft-alert warning" aria-label="Approval required">
          <ShieldAlert size={16} aria-hidden="true" />
          <div>
            <strong>Approval required</strong>
            <ul>
              {draft.approval_reasons.map((reason) => <li key={reason}>{reason}</li>)}
            </ul>
            {draft.approval_decision === 'rejected' && (
              <p>Rejected{draft.decision_note ? ` · Reviewer note: ${draft.decision_note}` : ''}. Edit it and request approval again.</p>
            )}
            {approved && draft.status === 'draft' && (
              <p>Approved{draft.decision_note ? ` · ${draft.decision_note}` : ''}. Editing the e-mail will require approval again.</p>
            )}
          </div>
        </section>
      )}

      {findings.length > 0 && (
        <section className="email-draft-alert danger" aria-label="Sensitive data check">
          <AlertTriangle size={16} aria-hidden="true" />
          <div>
            <strong>Possible sensitive data found</strong>
            <ul>
              {findings.map((finding, index) => (
                <li key={`${finding.type}-${index}`}>
                  <span className={`email-severity ${finding.severity}`}>{finding.severity}</span>{' '}
                  {finding.label} in {finding.field}: <code>{finding.match}</code>
                </li>
              ))}
            </ul>
            {canSend && (
              <label className="email-draft-ack">
                <input
                  type="checkbox"
                  checked={acknowledged}
                  onChange={(event) => setAcknowledged(event.target.checked)}
                />
                I have reviewed the flagged items and want to send this e-mail anyway.
              </label>
            )}
          </div>
        </section>
      )}
      {findings.length === 0 && !readOnly && (
        <p className="email-draft-scan-ok">Sensitive-data check: no issues found.</p>
      )}

      {failedResults.length > 0 && (
        <p className="task-manage-feedback error">
          Not delivered to: {failedResults.map((result) => `${result.email} (${result.error ?? 'error'})`).join(', ')}
        </p>
      )}
      {error && <p className="task-manage-feedback error" role="alert">{error}</p>}

      {!readOnly && (editable || canRetry || canCancel) && (
        <footer className="email-draft-actions">
          {dirty && (
            <>
              <button
                className="button primary"
                type="button"
                disabled={busy}
                onClick={() => void run(() => updateEmailDraft(draft.id, { subject, body }, actor))}
              >
                Save changes
              </button>
              <button
                className="button secondary"
                type="button"
                disabled={busy}
                onClick={() => { setSubject(draft.subject); setBody(draft.body) }}
              >
                Undo changes
              </button>
              <span className="task-manage-hint">Save to re-run the checks before sending.</span>
            </>
          )}
          {!dirty && needsApproval && (
            <button
              className="button primary"
              type="button"
              disabled={busy || draft.recipient_count === 0}
              onClick={() => void run(() => submitEmailDraft(draft.id, actor))}
            >
              Request approval
            </button>
          )}
          {!dirty && awaitingApproval && (
            <span className="task-manage-pending">Waiting for a manager, HR / Operations or admin to approve.</span>
          )}
          {canCancel && (
            <button
              className="button secondary"
              type="button"
              disabled={busy}
              onClick={() => void run(() => cancelEmailDraft(draft.id, actor))}
            >
              Cancel sending
            </button>
          )}
          {canRetry && (
            <button
              className="button primary"
              type="button"
              disabled={busy}
              onClick={() => void run(() => retryEmailDraft(draft.id, actor))}
            >
              <RotateCcw size={15} aria-hidden="true" /> Retry
            </button>
          )}
          {canSend && !confirming && (
            <button
              className="button primary"
              type="button"
              disabled={busy || (findings.length > 0 && !acknowledged)}
              onClick={() => setConfirming(true)}
            >
              <Send size={15} aria-hidden="true" />
              Send e-mail
            </button>
          )}
          {canSend && confirming && (
            <div className="email-draft-confirm" role="group" aria-label="Confirm send">
              <span>
                Send “{draft.subject}” to {draft.recipient_count} recipient{draft.recipient_count === 1 ? '' : 's'}?
              </span>
              <button
                className="button primary"
                type="button"
                disabled={busy}
                onClick={() => void run(() => sendEmailDraft(draft.id, acknowledged, actor)).then(() => setConfirming(false))}
              >
                {busy ? 'Queueing…' : 'Confirm send'}
              </button>
              <button className="button secondary" type="button" disabled={busy} onClick={() => setConfirming(false)}>
                Cancel
              </button>
            </div>
          )}
          {editable && (
            <button
              className="button secondary email-draft-discard"
              type="button"
              disabled={busy}
              onClick={() => void run(() => discardEmailDraft(draft.id, actor))}
            >
              <Trash2 size={15} aria-hidden="true" /> Discard
            </button>
          )}
        </footer>
      )}

      <EmailHistory draft={draft} actor={actor} />
    </article>
  )
}
