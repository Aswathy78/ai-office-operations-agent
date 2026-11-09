// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  decideReassignmentRequest,
  getReassignmentRequests,
  type TaskReassignmentRequest,
} from '../api/tasks'
import { demoUsers } from '../mocks/mockUser'
import { ApprovalsPage } from './ApprovalsPage'

const { authValue } = vi.hoisted(() => ({
  authValue: { user: null as null | { id: string; name: string; email: string; role: string; department: string } },
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => authValue,
}))

vi.mock('../api/tasks', () => ({
  decideReassignmentRequest: vi.fn(),
  getReassignmentRequests: vi.fn(),
}))

vi.mock('../api/meetings', () => ({
  getRescheduleRequests: vi.fn().mockResolvedValue([]),
  decideRescheduleRequest: vi.fn(),
}))

vi.mock('../api/assistantApprovals', () => ({
  assignAgentAlternateApprover: vi.fn(),
  getAgentApprovalHistory: vi.fn().mockResolvedValue([]),
  getAgentApprovals: vi.fn().mockResolvedValue([]),
  decideAgentApproval: vi.fn(),
  cancelAgentApproval: vi.fn(),
}))

vi.mock('../api/emailDrafts', () => ({
  EMAIL_STATUS_POLL_MS: 2000,
  decideEmailDraft: vi.fn(),
  getEmailApprovals: vi.fn().mockResolvedValue([]),
}))

const pendingRequest: TaskReassignmentRequest = {
  id: 'request-1',
  task_id: 'task-1',
  task_title: 'Prepare the release notes',
  from_assignee_id: 'rahul',
  from_assignee_name: 'Rahul',
  to_assignee_id: 'priya',
  to_assignee_name: 'Priya Sharma',
  to_assignee_team: 'Finance',
  requested_by: 'user-002',
  requester_role: 'manager',
  requester_team: 'Engineering',
  reason: 'Finance owns reporting',
  status: 'pending',
  decided_by: null,
  decided_at: null,
  decision_note: null,
  created_at: '2026-10-14T00:00:00Z',
}

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ApprovalsPage', () => {
  it('lets HR / Operations approve a cross-team reassignment', async () => {
    authValue.user = demoUsers.hr_operations
    vi.mocked(getReassignmentRequests).mockResolvedValue([pendingRequest])
    vi.mocked(decideReassignmentRequest).mockResolvedValue({
      outcome: 'applied',
      message: 'Approved. Task reassigned from Rahul to Priya Sharma; both have been notified.',
      task: {} as never,
      reassignment_request: { ...pendingRequest, status: 'approved', decided_by: 'user-003' },
      notifications: [],
    })
    const user = userEvent.setup()
    render(<ApprovalsPage />)

    expect(await screen.findByText('Prepare the release notes')).toBeInTheDocument()
    await user.type(screen.getByLabelText('Decision note for Prepare the release notes'), 'OK')
    await user.click(screen.getByRole('button', { name: 'Approve' }))

    expect(decideReassignmentRequest).toHaveBeenCalledWith(
      'request-1',
      'approve',
      'OK',
      { id: 'user-003', role: 'hr_operations', team: 'People Operations' },
    )
    expect(await screen.findByText(/both have been notified/)).toBeInTheDocument()
    expect(screen.getByText('Approved')).toBeInTheDocument()
  })

  it('shows managers their pending requests without approval controls', async () => {
    authValue.user = demoUsers.manager
    vi.mocked(getReassignmentRequests).mockResolvedValue([pendingRequest])
    render(<ApprovalsPage />)

    expect(await screen.findByText('Awaiting HR / Operations or Admin approval.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Assistant approval history' })).not.toBeInTheDocument()
  })
})

describe('ApprovalsPage — Meeting Reschedule Approvals Section', () => {
  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  const pendingReschedule = {
    id: 'reschedule-1',
    meeting_id: 'meeting-1',
    meeting_title: 'Sprint Planning',
    requested_by: 'emp-3',
    requested_by_name: 'Marcus Chen',
    new_start_at: '2026-10-20T08:30:00Z',
    new_end_at: '2026-10-20T09:00:00Z',
    reason: 'Conflicting deadline',
    status: 'pending' as const,
    decided_by: null,
    decided_at: null,
    decision_note: null,
    created_at: '2026-10-18T10:00:00Z',
    attendee_count: 5,
  }

  it('displays pending reschedule request and allows organizer to approve', async () => {
    const { getRescheduleRequests, decideRescheduleRequest } = await import('../api/meetings')
    authValue.user = demoUsers.hr_operations
    vi.mocked(getRescheduleRequests).mockResolvedValue([pendingReschedule])
    vi.mocked(getReassignmentRequests).mockResolvedValue([])
    vi.mocked(decideRescheduleRequest).mockResolvedValue({
      request: { ...pendingReschedule, status: 'approved', decided_by: 'emp-org', decided_at: '2026-10-18T11:00:00Z' },
      meeting: null,
      message: 'Sprint Planning rescheduled successfully.',
    })

    const user = userEvent.setup()
    render(<ApprovalsPage />)

    expect(await screen.findByText('Sprint Planning')).toBeInTheDocument()
    expect(screen.getByText(/Marcus Chen/)).toBeInTheDocument()
    expect(screen.getByText(/5 attendees/i)).toBeInTheDocument()

    const approveBtn = screen.getByRole('button', { name: /approve/i })
    await user.click(approveBtn)

    expect(decideRescheduleRequest).toHaveBeenCalledWith(
      'reschedule-1',
      'approve',
      '',
      expect.objectContaining({ id: demoUsers.hr_operations.id }),
    )
    expect(await screen.findByText(/Sprint Planning rescheduled successfully/i)).toBeInTheDocument()
  })

  it('shows empty state when there are no reschedule requests', async () => {
    const { getRescheduleRequests } = await import('../api/meetings')
    authValue.user = demoUsers.hr_operations
    vi.mocked(getRescheduleRequests).mockResolvedValue([])
    vi.mocked(getReassignmentRequests).mockResolvedValue([])

    render(<ApprovalsPage />)

    expect(
      await screen.findByText(/no pending reschedule requests/i),
    ).toBeInTheDocument()
  })
})
