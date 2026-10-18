// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  cancelEmailDraft,
  retryEmailDraft,
  sendEmailDraft,
  submitEmailDraft,
  updateEmailDraft,
  type EmailDraft,
} from '../../api/emailDrafts'
import { EmailDraftPreview } from './EmailDraftPreview'
import { sampleDraft } from './emailTestFixtures'

vi.mock('../../api/emailDrafts', () => ({
  cancelEmailDraft: vi.fn(),
  discardEmailDraft: vi.fn(),
  getEmailDraftEvents: vi.fn().mockResolvedValue([]),
  retryEmailDraft: vi.fn(),
  sendEmailDraft: vi.fn(),
  submitEmailDraft: vi.fn(),
  updateEmailDraft: vi.fn(),
}))

const actor = { id: 'user-001', role: 'employee', team: 'Engineering' }

const baseDraft: EmailDraft = sampleDraft

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('EmailDraftPreview', () => {
  it('shows the full preview and only sends after explicit confirmation', async () => {
    const onChange = vi.fn()
    vi.mocked(sendEmailDraft).mockResolvedValue({ ...baseDraft, status: 'queued' })
    render(<EmailDraftPreview draft={baseDraft} actor={actor} onChange={onChange} />)

    expect(screen.getByText('2 recipients')).toBeInTheDocument()
    expect(screen.getByText('Engineering team: 2')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Reminder: Sprint demo')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /send e-mail/i }))
    expect(sendEmailDraft).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'Confirm send' }))
    expect(sendEmailDraft).toHaveBeenCalledWith('draft-1', false, actor)
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ status: 'queued' }))
  })

  it('requires saving edits before sending', async () => {
    vi.mocked(updateEmailDraft).mockResolvedValue({ ...baseDraft, subject: 'Updated' })
    render(<EmailDraftPreview draft={baseDraft} actor={actor} />)

    const subject = screen.getByDisplayValue('Reminder: Sprint demo')
    await userEvent.clear(subject)
    await userEvent.type(subject, 'Updated')

    expect(screen.queryByRole('button', { name: /send e-mail/i })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(updateEmailDraft).toHaveBeenCalledWith('draft-1', { subject: 'Updated', body: baseDraft.body }, actor)
  })

  it('asks for approval instead of sending when approval is required', async () => {
    const draft: EmailDraft = {
      ...baseDraft,
      recipients: [...baseDraft.recipients, { name: 'p@vendor.com', email: 'p@vendor.com', internal: false, source: 'Added manually' }],
      recipient_count: 3,
      approval_required: true,
      approval_reasons: ['External recipient(s): p@vendor.com.'],
    }
    vi.mocked(submitEmailDraft).mockResolvedValue({ ...draft, status: 'approval' })
    render(<EmailDraftPreview draft={draft} actor={actor} />)

    expect(screen.getByText('External')).toBeInTheDocument()
    expect(screen.getByText('External recipient(s): p@vendor.com.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /send e-mail/i })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Request approval' }))
    expect(submitEmailDraft).toHaveBeenCalledWith('draft-1', actor)
  })

  it('blocks sending until flagged sensitive data is acknowledged', async () => {
    const draft: EmailDraft = {
      ...baseDraft,
      sensitive_findings: [
        { type: 'password', label: 'Password or PIN', severity: 'high', match: 'pa••••••d1', field: 'body' },
      ],
    }
    vi.mocked(sendEmailDraft).mockResolvedValue({ ...draft, status: 'queued' })
    render(<EmailDraftPreview draft={draft} actor={actor} />)

    const send = screen.getByRole('button', { name: /send e-mail/i })
    expect(send).toBeDisabled()

    await userEvent.click(screen.getByRole('checkbox'))
    await userEvent.click(send)
    await userEvent.click(screen.getByRole('button', { name: 'Confirm send' }))
    expect(sendEmailDraft).toHaveBeenCalledWith('draft-1', true, actor)
  })

  it('is read-only for approvers', () => {
    render(<EmailDraftPreview draft={{ ...baseDraft, status: 'approval' }} actor={actor} readOnly />)
    expect(screen.getByDisplayValue('Reminder: Sprint demo')).toHaveAttribute('readonly')
    expect(screen.queryByRole('button', { name: /send|discard|remove/i })).not.toBeInTheDocument()
  })
})

describe('EmailDraftPreview delivery lifecycle (FR-22)', () => {
  const statuses = ['draft', 'approval', 'queued', 'sending', 'sent', 'failed', 'retry'] as const

  it.each(statuses)('shows exactly one current status for %s', (status) => {
    render(<EmailDraftPreview draft={{ ...baseDraft, status, approval_required: true }} actor={actor} />)

    const pills = document.querySelectorAll('.email-status[data-status]')
    expect(pills).toHaveLength(1)
    expect(pills[0]).toHaveAttribute('data-status', status)
    expect(screen.getAllByRole('listitem').filter((li) => li.getAttribute('aria-current') === 'step')).toHaveLength(1)
  })

  it('lets the sender cancel while queued', async () => {
    vi.mocked(cancelEmailDraft).mockResolvedValue({ ...baseDraft, status: 'draft' })
    render(<EmailDraftPreview draft={{ ...baseDraft, status: 'queued' }} actor={actor} />)

    expect(screen.queryByRole('button', { name: /send e-mail/i })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Cancel sending' }))
    expect(cancelEmailDraft).toHaveBeenCalledWith('draft-1', actor)
  })

  it('offers Retry for a failed e-mail and explains what was delivered', async () => {
    const failed: EmailDraft = {
      ...baseDraft,
      status: 'failed',
      attempt_count: 1,
      last_error: '1 of 2 recipient(s) not delivered. Recipient refused by the mail server: 550 No such user',
      delivery_results: [
        { email: 'ava.patel@northstar.example', status: 'sent', error: null, message_id: '<1@x>' },
        { email: 'marcus.chen@northstar.example', status: 'failed', error: '550 No such user' },
      ],
    }
    vi.mocked(retryEmailDraft).mockResolvedValue({ ...failed, status: 'retry' })
    render(<EmailDraftPreview draft={failed} actor={actor} />)

    expect(screen.getByText(/delivered to 1 of 2 recipients/i)).toBeInTheDocument()
    expect(screen.getAllByText(/550 No such user/).length).toBeGreaterThan(0)
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(retryEmailDraft).toHaveBeenCalledWith('draft-1', actor)
  })

  it('shows automatic retry progress without offering actions', () => {
    render(
      <EmailDraftPreview
        draft={{ ...baseDraft, status: 'retry', attempt_count: 1, next_attempt_at: '2026-10-16T10:00:30Z', last_error: 'timed out' }}
        actor={actor}
      />,
    )

    expect(screen.getByText(/Attempt 1 of 3 failed; retrying automatically/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /retry|send e-mail|cancel sending/i })).not.toBeInTheDocument()
  })

  it('says Sent only with provider acceptance details', () => {
    render(<EmailDraftPreview draft={{ ...baseDraft, status: 'sent', sent_at: '2026-10-16T10:00:00Z' }} actor={actor} />)
    expect(screen.getByText(/Accepted by the mail provider for all 2 recipients/)).toBeInTheDocument()
  })

  it('allows sending once an approval-required e-mail is approved', () => {
    render(
      <EmailDraftPreview
        draft={{ ...baseDraft, approval_required: true, approval_reasons: ['External recipient(s): p@vendor.com.'], approval_decision: 'approved' }}
        actor={actor}
      />,
    )
    expect(screen.getByRole('button', { name: /send e-mail/i })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Request approval' })).not.toBeInTheDocument()
  })
})

describe('EmailDraftPreview failure escalation (FR-23)', () => {
  const failedAfterBounce: EmailDraft = {
    ...baseDraft,
    status: 'failed',
    attempt_count: 1,
    sent_at: '2026-10-19T09:00:00Z',
    escalated_at: '2026-10-19T09:05:00Z',
    last_error: '1 of 2 recipient(s) bounced. marcus.chen@northstar.example: 550 5.1.1 Mailbox does not exist',
    delivery_results: [
      { email: 'ava.patel@northstar.example', status: 'sent', error: null, message_id: '<1@x>', provider: 'gmail' },
      { email: 'marcus.chen@northstar.example', status: 'bounced', error: '550 5.1.1 Mailbox does not exist', message_id: '<2@x>' },
    ],
    bounces: [{
      recipient: 'marcus.chen@northstar.example', type: 'hard', source: 'dsn', code: 550,
      diagnostic: '550 5.1.1 Mailbox does not exist', message_id: '<2@x>', recorded_at: '2026-10-19T09:04:00Z',
    }],
    notifications: [
      { id: 'n1', failure_event_sequence: 5, audience: 'requester', channel: 'email', recipient_email: 'rahul@northstar.example',
        status: 'sent', attempts: 1, next_attempt_at: null, provider: 'gmail', error: null, sent_at: '2026-10-19T09:05:00Z', created_at: '2026-10-19T09:05:00Z' },
      { id: 'n2', failure_event_sequence: 5, audience: 'admin', channel: 'email', recipient_email: 'it-admin@northstar.example',
        status: 'failed', attempts: 1, next_attempt_at: '2026-10-19T09:06:00Z', provider: null, error: 'OSError: mail is down', sent_at: null, created_at: '2026-10-19T09:05:00Z' },
      { id: 'n3', failure_event_sequence: 5, audience: 'admin', channel: 'in_app', recipient_email: null,
        status: 'recorded', attempts: 0, next_attempt_at: null, provider: null, error: null, sent_at: null, created_at: '2026-10-19T09:05:00Z' },
    ],
  }

  it('shows a bounced e-mail as Failed — never Sent — with the bounce and who was notified', () => {
    render(<EmailDraftPreview draft={failedAfterBounce} actor={actor} />)

    const pill = document.querySelector('.email-status[data-status]')
    expect(pill).toHaveAttribute('data-status', 'failed')
    expect(pill).toHaveTextContent('Failed')
    expect(screen.queryByText(/Accepted by the mail provider for all/)).not.toBeInTheDocument()
    expect(screen.getByText(/Failed after 1 attempt: delivered to 1 of 2 recipients\. You and the administrator have been notified\./)).toBeInTheDocument()

    const bounces = screen.getByRole('region', { name: 'Bounces' })
    expect(bounces).toHaveTextContent('hard')
    expect(bounces).toHaveTextContent('marcus.chen@northstar.example')
    expect(bounces).toHaveTextContent('550')

    const notices = screen.getByRole('region', { name: 'Failure notifications' })
    expect(notices).toHaveTextContent('Requester <rahul@northstar.example>: notified')
    expect(notices).toHaveTextContent('Administrator <it-admin@northstar.example>: notification failed — will retry')
    expect(notices).toHaveTextContent('Administrator (in-app): shown on admin dashboard')
    expect(screen.getByText(/bounced: 550 5\.1\.1 Mailbox does not exist/)).toBeInTheDocument()
  })

  it('says when the backup provider delivered the e-mail', () => {
    render(
      <EmailDraftPreview
        draft={{
          ...baseDraft,
          status: 'sent',
          sent_at: '2026-10-19T09:00:00Z',
          delivery_results: baseDraft.recipients.map((r, i) => ({
            email: r.email, status: 'sent' as const, error: null, message_id: `<${i}@x>`, provider: 'ses', fallback_used: true,
          })),
        }}
        actor={actor}
      />,
    )
    expect(screen.getByText(/using the backup mail provider/)).toBeInTheDocument()
    expect(screen.getByText(/Sent through the backup mail provider \(ses\)/)).toBeInTheDocument()
  })
})
