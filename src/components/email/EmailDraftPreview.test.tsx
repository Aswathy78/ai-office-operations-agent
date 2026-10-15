// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  sendEmailDraft,
  submitEmailDraft,
  updateEmailDraft,
  type EmailDraft,
} from '../../api/emailDrafts'
import { EmailDraftPreview } from './EmailDraftPreview'

vi.mock('../../api/emailDrafts', () => ({
  discardEmailDraft: vi.fn(),
  sendEmailDraft: vi.fn(),
  submitEmailDraft: vi.fn(),
  updateEmailDraft: vi.fn(),
}))

const actor = { id: 'user-001', role: 'employee', team: 'Engineering' }

const baseDraft: EmailDraft = {
  id: 'draft-1',
  created_by_id: 'user-001',
  created_by_name: 'Rahul',
  created_by_email: 'rahul@northstar.example',
  created_by_role: 'employee',
  prompt: 'Email the development team about the demo',
  source: 'template',
  template_key: 'meeting_reminder',
  generation_note: null,
  subject: 'Reminder: Sprint demo',
  body: 'Hi Engineering team,\n\nDemo at 10am.\n\nRahul',
  recipients: [
    { name: 'Ava Patel', email: 'ava.patel@northstar.example', internal: true, source: 'Engineering team' },
    { name: 'Marcus Chen', email: 'marcus.chen@northstar.example', internal: true, source: 'Engineering team' },
  ],
  recipient_groups: [{ name: 'Engineering team', count: 2 }],
  unresolved_recipients: [],
  recipient_count: 2,
  approval_required: false,
  approval_reasons: [],
  approval_threshold: 10,
  sensitive_findings: [],
  status: 'draft',
  decided_by: null,
  decided_at: null,
  decision_note: null,
  delivery_results: [],
  sent_at: null,
  created_at: '2026-10-15T00:00:00Z',
  updated_at: '2026-10-15T00:00:00Z',
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('EmailDraftPreview', () => {
  it('shows the full preview and only sends after explicit confirmation', async () => {
    const onChange = vi.fn()
    vi.mocked(sendEmailDraft).mockResolvedValue({ ...baseDraft, status: 'sent' })
    render(<EmailDraftPreview draft={baseDraft} actor={actor} onChange={onChange} />)

    expect(screen.getByText('2 recipients')).toBeInTheDocument()
    expect(screen.getByText('Engineering team: 2')).toBeInTheDocument()
    expect(screen.getByDisplayValue('Reminder: Sprint demo')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /send e-mail/i }))
    expect(sendEmailDraft).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: 'Confirm send' }))
    expect(sendEmailDraft).toHaveBeenCalledWith('draft-1', false, actor)
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ status: 'sent' }))
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
    vi.mocked(submitEmailDraft).mockResolvedValue({ ...draft, status: 'pending_approval' })
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
    vi.mocked(sendEmailDraft).mockResolvedValue({ ...draft, status: 'sent' })
    render(<EmailDraftPreview draft={draft} actor={actor} />)

    const send = screen.getByRole('button', { name: /send e-mail/i })
    expect(send).toBeDisabled()

    await userEvent.click(screen.getByRole('checkbox'))
    await userEvent.click(send)
    await userEvent.click(screen.getByRole('button', { name: 'Confirm send' }))
    expect(sendEmailDraft).toHaveBeenCalledWith('draft-1', true, actor)
  })

  it('is read-only for approvers', () => {
    render(<EmailDraftPreview draft={{ ...baseDraft, status: 'pending_approval' }} actor={actor} readOnly />)
    expect(screen.getByDisplayValue('Reminder: Sprint demo')).toHaveAttribute('readonly')
    expect(screen.queryByRole('button', { name: /send|discard|remove/i })).not.toBeInTheDocument()
  })
})
