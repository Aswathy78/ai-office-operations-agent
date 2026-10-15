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

vi.mock('../api/emailDrafts', () => ({
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
  })
})
