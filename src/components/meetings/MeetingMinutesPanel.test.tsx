// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MeetingMinutesPanel } from './MeetingMinutesPanel'
import type { MeetingMinutes } from '../../types/meeting'

afterEach(() => {
  cleanup()
})

const baseMinutes: MeetingMinutes = {
  id: 'minutes-101',
  meeting_id: 'meeting-1',
  summary_id: 'summary-1',
  status: 'pending_approval',
  policy: 'manual_approval',
  approved_by: null,
  approved_at: null,
  content_summary: 'Reviewed the launch milestones and identified risks.',
  content_decisions: ['Postpone v1 release by two days', 'Prioritize bug squashing'],
  content_action_items: [
    {
      id: 'action-1',
      task_description: 'Run load testing',
      owner_name: 'Devon Vance',
      owner_email: 'devon@example.com',
      deadline_at: '2026-10-18T18:00:00Z',
      deadline_text: 'by Sunday 6pm',
      priority: 'high',
    },
    {
      id: 'action-2',
      task_description: 'Draft release announcement',
      owner_name: 'Maya Lin',
      owner_email: null,
      deadline_at: null,
      deadline_text: 'by Monday morning',
      priority: 'medium',
    },
  ],
  error_message: null,
  created_at: '2026-10-14T09:00:00Z',
  updated_at: '2026-10-14T09:00:00Z',
  recipients: [
    {
      id: 'rec-1',
      minutes_id: 'minutes-101',
      recipient_name: 'Devon Vance',
      recipient_email: 'devon@example.com',
      attendance_log_id: 'log-1',
      delivery_status: 'pending',
      error_message: null,
      idempotency_key: 'minutes:minutes-101:devon@example.com',
      sent_at: null,
      created_at: '2026-10-14T09:00:00Z',
    },
    {
      id: 'rec-2',
      minutes_id: 'minutes-101',
      recipient_name: 'Maya Lin',
      recipient_email: 'maya@example.com',
      attendance_log_id: 'log-2',
      delivery_status: 'sent',
      error_message: null,
      idempotency_key: 'minutes:minutes-101:maya@example.com',
      sent_at: '2026-10-14T09:30:00Z',
      created_at: '2026-10-14T09:00:00Z',
    },
    {
      id: 'rec-3',
      minutes_id: 'minutes-101',
      recipient_name: 'Bob Smith',
      recipient_email: 'bob@example.com',
      attendance_log_id: 'log-3',
      delivery_status: 'failed',
      error_message: 'SMTP 550 Mailbox unavailable',
      idempotency_key: 'minutes:minutes-101:bob@example.com',
      sent_at: null,
      created_at: '2026-10-14T09:00:00Z',
    },
  ],
}

describe('MeetingMinutesPanel Component (Story 4.2)', () => {
  it('renders minutes overview, decisions, confirmed action items, and resolved dates', () => {
    render(
      <MeetingMinutesPanel
        meetingId="meeting-1"
        minutes={baseMinutes}
        isOrganizer={true}
        loading={false}
        generating={false}
        approving={false}
        sending={false}
        retrying={false}
        onGenerate={vi.fn()}
        onApprove={vi.fn()}
        onSend={vi.fn()}
        onRetryFailed={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    // Summary
    expect(screen.getByText('Reviewed the launch milestones and identified risks.')).toBeInTheDocument()

    // Decisions
    expect(screen.getByText('Postpone v1 release by two days')).toBeInTheDocument()
    expect(screen.getByText('Prioritize bug squashing')).toBeInTheDocument()

    // Confirmed Action Items
    expect(screen.getByText('Run load testing')).toBeInTheDocument()
    expect(screen.getAllByText('Devon Vance').length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('(devon@example.com)')).toBeInTheDocument()
    expect(screen.getByText('Draft release announcement')).toBeInTheDocument()
    expect(screen.getByText('by Monday morning')).toBeInTheDocument()
  })

  it('displays the current minutes approval state', () => {
    const { rerender } = render(
      <MeetingMinutesPanel
        meetingId="meeting-1"
        minutes={baseMinutes} // status: pending_approval
        isOrganizer={true}
        loading={false}
        generating={false}
        approving={false}
        sending={false}
        retrying={false}
        onGenerate={vi.fn()}
        onApprove={vi.fn()}
        onSend={vi.fn()}
        onRetryFailed={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    expect(screen.getByText('Pending Approval')).toBeInTheDocument()
    expect(screen.getByText('Approval required before delivery')).toBeInTheDocument()

    // Rerender as approved
    rerender(
      <MeetingMinutesPanel
        meetingId="meeting-1"
        minutes={{
          ...baseMinutes,
          status: 'approved',
          approved_by: 'organizer-1',
          approved_at: '2026-10-14T10:00:00Z',
        }}
        isOrganizer={true}
        loading={false}
        generating={false}
        approving={false}
        sending={false}
        retrying={false}
        onGenerate={vi.fn()}
        onApprove={vi.fn()}
        onSend={vi.fn()}
        onRetryFailed={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    expect(screen.getByText('Approved')).toBeInTheDocument()
    expect(screen.getByText('organizer-1')).toBeInTheDocument()
  })

  it('allows only the organizer to approve minutes', async () => {
    const user = userEvent.setup()
    const onApprove = vi.fn()

    // Non-organizer view
    const { rerender } = render(
      <MeetingMinutesPanel
        meetingId="meeting-1"
        minutes={baseMinutes}
        isOrganizer={false}
        loading={false}
        generating={false}
        approving={false}
        sending={false}
        retrying={false}
        onGenerate={vi.fn()}
        onApprove={onApprove}
        onSend={vi.fn()}
        onRetryFailed={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    expect(screen.queryByRole('button', { name: /approve minutes/i })).not.toBeInTheDocument()

    // Organizer view
    rerender(
      <MeetingMinutesPanel
        meetingId="meeting-1"
        minutes={baseMinutes}
        isOrganizer={true}
        loading={false}
        generating={false}
        approving={false}
        sending={false}
        retrying={false}
        onGenerate={vi.fn()}
        onApprove={onApprove}
        onSend={vi.fn()}
        onRetryFailed={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    const approveBtn = screen.getByRole('button', { name: /approve minutes/i })
    expect(approveBtn).toBeInTheDocument()
    await user.click(approveBtn)

    const confirmBtn = screen.getByRole('button', { name: /confirm approval/i })
    await user.click(confirmBtn)

    expect(onApprove).toHaveBeenCalled()
  })

  it('gates the Send Minutes action until minutes are approved', async () => {
    const user = userEvent.setup()
    const onSend = vi.fn()

    // When status is pending_approval, Send Minutes is disabled
    const { rerender } = render(
      <MeetingMinutesPanel
        meetingId="meeting-1"
        minutes={baseMinutes} // status: 'pending_approval'
        isOrganizer={true}
        loading={false}
        generating={false}
        approving={false}
        sending={false}
        retrying={false}
        onGenerate={vi.fn()}
        onApprove={vi.fn()}
        onSend={onSend}
        onRetryFailed={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    const sendBtn = screen.getByRole('button', { name: /send minutes/i })
    expect(sendBtn).toBeDisabled()
    await user.click(sendBtn)
    expect(onSend).not.toHaveBeenCalled()

    // When status becomes approved, Send Minutes is enabled
    rerender(
      <MeetingMinutesPanel
        meetingId="meeting-1"
        minutes={{
          ...baseMinutes,
          status: 'approved',
          approved_by: 'organizer-1',
          approved_at: '2026-10-14T10:00:00Z',
        }}
        isOrganizer={true}
        loading={false}
        generating={false}
        approving={false}
        sending={false}
        retrying={false}
        onGenerate={vi.fn()}
        onApprove={vi.fn()}
        onSend={onSend}
        onRetryFailed={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    expect(sendBtn).not.toBeDisabled()
    await user.click(sendBtn)
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('displays per-recipient email addresses and delivery statuses (pending, sent, failed)', () => {
    render(
      <MeetingMinutesPanel
        meetingId="meeting-1"
        minutes={baseMinutes}
        isOrganizer={true}
        loading={false}
        generating={false}
        approving={false}
        sending={false}
        retrying={false}
        onGenerate={vi.fn()}
        onApprove={vi.fn()}
        onSend={vi.fn()}
        onRetryFailed={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    expect(screen.getByText('devon@example.com')).toBeInTheDocument()
    expect(screen.getByText('maya@example.com')).toBeInTheDocument()
    expect(screen.getByText('bob@example.com')).toBeInTheDocument()

    expect(screen.getByText('Pending')).toBeInTheDocument()
    expect(screen.getByText('Sent')).toBeInTheDocument()
    expect(screen.getByText('Failed')).toBeInTheDocument()
    expect(screen.getByText('SMTP 550 Mailbox unavailable')).toBeInTheDocument()
  })

  it('provides a Retry Failed Deliveries action for failed recipients only', async () => {
    const user = userEvent.setup()
    const onRetryFailed = vi.fn()

    render(
      <MeetingMinutesPanel
        meetingId="meeting-1"
        minutes={{
          ...baseMinutes,
          status: 'approved',
        }}
        isOrganizer={true}
        loading={false}
        generating={false}
        approving={false}
        sending={false}
        retrying={false}
        onGenerate={vi.fn()}
        onApprove={vi.fn()}
        onSend={vi.fn()}
        onRetryFailed={onRetryFailed}
        onRefresh={vi.fn()}
      />,
    )

    const retryBtn = screen.getByRole('button', { name: /retry failed deliveries/i })
    expect(retryBtn).toBeInTheDocument()
    await user.click(retryBtn)
    expect(onRetryFailed).toHaveBeenCalledTimes(1)
  })

  it('shows useful loading states, success messages, and errors', () => {
    const { rerender } = render(
      <MeetingMinutesPanel
        meetingId="meeting-1"
        minutes={baseMinutes}
        isOrganizer={true}
        loading={true}
        generating={false}
        approving={false}
        sending={false}
        retrying={false}
        onGenerate={vi.fn()}
        onApprove={vi.fn()}
        onSend={vi.fn()}
        onRetryFailed={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    expect(screen.getByText(/loading meeting minutes…/i)).toBeInTheDocument()

    // With feedback and error
    rerender(
      <MeetingMinutesPanel
        meetingId="meeting-1"
        minutes={baseMinutes}
        isOrganizer={true}
        loading={false}
        generating={false}
        approving={false}
        sending={false}
        retrying={false}
        error="Network error reaching email service"
        feedback={{ kind: 'success', message: 'Minutes delivered successfully.' }}
        onGenerate={vi.fn()}
        onApprove={vi.fn()}
        onSend={vi.fn()}
        onRetryFailed={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    expect(screen.getByText('Network error reaching email service')).toBeInTheDocument()
    expect(screen.getByText('Minutes delivered successfully.')).toBeInTheDocument()
  })
})
