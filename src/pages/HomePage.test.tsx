// @vitest-environment jsdom
import { act, cleanup, render, screen, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { getAgentApprovals } from '../api/assistantApprovals'
import { getMeetings } from '../api/meetings'
import { getNotifications, getNotificationPreferences } from '../api/notifications'
import { getMyTasks } from '../api/tasks'
import { HomePage } from './HomePage'

const { authValue } = vi.hoisted(() => ({
  authValue: {
    user: {
      id: 'employee-1',
      name: 'Maya Employee',
      email: 'maya@example.test',
      role: 'employee',
      department: 'Engineering',
    },
  },
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => authValue,
}))

vi.mock('../api/assistantApprovals', () => ({
  getAgentApprovals: vi.fn(),
  decideAgentApproval: vi.fn(),
}))

vi.mock('../api/meetings', () => ({
  getMeetings: vi.fn(),
}))

vi.mock('../api/notifications', () => ({
  getNotifications: vi.fn(),
  getNotificationPreferences: vi.fn(),
}))

vi.mock('../api/tasks', () => ({
  getMyTasks: vi.fn(),
  getTeamDashboard: vi.fn().mockResolvedValue({
    summary: { team_name: 'Engineering', total_members: 0, total_tasks: 0, open_tasks: 0, due_soon_tasks: 0, overdue_tasks: 0, completed_tasks: 0, completion_rate: 0 },
    members: [],
    overdue_tasks: [],
  }),
}))

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('employee home dashboard', () => {
  it('shows today’s meetings, task statuses and deadlines in the employee timezone', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-11-04T10:00:00Z'))
    vi.mocked(getMeetings).mockResolvedValue([
      {
        id: 'meeting-today',
        title: 'Planning meeting',
        agenda: null,
        start_at: '2026-11-04T10:30:00Z',
        end_at: '2026-11-04T11:00:00Z',
        join_url: 'https://meet.example.test/planning',
        status: 'scheduled',
        timezone: 'UTC',
      },
      {
        id: 'meeting-no-link',
        title: 'Team check-in',
        agenda: null,
        start_at: '2026-11-04T11:30:00Z',
        end_at: '2026-11-04T12:00:00Z',
        join_url: '',
        status: 'scheduled',
        timezone: 'UTC',
      },
      {
        id: 'meeting-next-day',
        title: 'Late meeting',
        agenda: null,
        start_at: '2026-11-04T20:00:00Z',
        end_at: '2026-11-04T21:00:00Z',
        join_url: 'https://meet.example.test/late',
        status: 'scheduled',
        timezone: 'UTC',
      },
    ])
    vi.mocked(getMyTasks).mockResolvedValue([
      {
        id: 'task-pending',
        title: 'Prepare weekly report',
        assignee_id: 'employee-1',
        assignee_name: 'Maya Employee',
        creator_id: 'manager-1',
        source: 'assistant',
        source_action_item_id: null,
        meeting_id: null,
        meeting_title: null,
        transcript_id: null,
        source_segment_index: null,
        source_timestamp_seconds: null,
        due_at: '2026-11-08T18:00:00Z',
        priority: 'medium',
        status: 'pending',
        assignment_email_status: 'sent',
        assignment_email_error: null,
        created_at: '2026-11-01T10:00:00Z',
      },
      {
        id: 'task-completed',
        title: 'Review sprint notes',
        assignee_id: 'employee-1',
        assignee_name: 'Maya Employee',
        creator_id: 'manager-1',
        source: 'assistant',
        source_action_item_id: null,
        meeting_id: null,
        meeting_title: null,
        transcript_id: null,
        source_segment_index: null,
        source_timestamp_seconds: null,
        due_at: '2026-11-06T10:00:00Z',
        priority: 'low',
        status: 'completed',
        assignment_email_status: 'failed',
        assignment_email_error: 'Delivery failed',
        created_at: '2026-11-01T10:00:00Z',
      },
      {
        id: 'task-next-week',
        title: 'Next week task',
        assignee_id: 'employee-1',
        assignee_name: 'Maya Employee',
        creator_id: 'manager-1',
        source: 'assistant',
        source_action_item_id: null,
        meeting_id: null,
        meeting_title: null,
        transcript_id: null,
        source_segment_index: null,
        source_timestamp_seconds: null,
        due_at: '2026-11-08T20:00:00Z',
        priority: 'low',
        status: 'pending',
        assignment_email_status: 'pending',
        assignment_email_error: null,
        created_at: '2026-11-01T10:00:00Z',
      },
      ...[
        { id: 'task-soonest', title: 'Send meeting notes', due_at: '2026-11-05T10:00:00Z' },
        { id: 'task-second-soonest', title: 'Update project tracker', due_at: '2026-11-07T10:00:00Z' },
      ].map(({ id, title, due_at }) => ({
        id,
        title,
        assignee_id: 'employee-1',
        assignee_name: 'Maya Employee',
        creator_id: 'manager-1',
        source: 'assistant' as const,
        source_action_item_id: null,
        meeting_id: null,
        meeting_title: null,
        transcript_id: null,
        source_segment_index: null,
        source_timestamp_seconds: null,
        due_at,
        priority: 'medium' as const,
        status: 'pending' as const,
        assignment_email_status: 'pending' as const,
        assignment_email_error: null,
        created_at: '2026-11-01T10:00:00Z',
      })),
    ])
    vi.mocked(getNotifications).mockResolvedValue([
      {
        id: 'notification-1',
        event_type: 'task_due',
        summary: 'A task is due soon',
        entity_type: 'task',
        entity_id: 'task-pending',
        entity_url: '/tasks?task_id=task-pending',
        in_app_status: 'delivered',
        email_status: 'sent',
        email_attempts: 1,
        email_next_attempt_at: null,
        email_error: null,
        created_at: '2026-11-04T09:00:00Z',
        read_at: null,
      },
    ])
    vi.mocked(getAgentApprovals).mockResolvedValue([
      {
        id: 'approval-1',
        tool_name: 'task.update',
        status: 'pending',
        summary: 'Update a team task',
        risk_level: 'high',
        risk_reasons: [],
        approver_kind: 'approver',
        requester_id: 'employee-2',
        requester_name: 'Other Employee',
        requester_role: 'employee',
        requester_team: 'Engineering',
        arguments: {},
        affected_people: [],
        source: { type: 'AI Assistant', conversation_id: null },
        decided_by: null,
        decided_by_name: null,
        decided_at: null,
        alternate_approver_id: null,
        alternate_approver_name: null,
        alternate_approver_email: null,
        reminder_sent_at: null,
        decision_note: null,
        execution_id: null,
        result: null,
        error: null,
        conversation_id: null,
        expires_at: '2026-11-05T10:00:00Z',
        created_at: '2026-11-04T08:00:00Z',
        can_decide: true,
        can_cancel: false,
      },
    ])
    vi.mocked(getNotificationPreferences).mockResolvedValue({
      in_app_enabled: true,
      email_enabled: true,
      quiet_hours_start: null,
      quiet_hours_end: null,
      timezone_name: 'Asia/Kolkata',
      timezone_fallback: false,
    })

    render(<MemoryRouter><HomePage /></MemoryRouter>)

    expect(await screen.findByRole('heading', { name: 'Today’s meetings' })).toBeInTheDocument()
    const meetings = screen.getByRole('region', { name: 'Today’s meetings' })
    expect(within(meetings).getByText('Planning meeting')).toBeInTheDocument()
    expect(within(meetings).getByText('Team check-in')).toBeInTheDocument()
    expect(within(meetings).queryByText('Late meeting')).not.toBeInTheDocument()
    expect(within(meetings).getByRole('link', { name: 'Join Planning meeting' }))
      .toHaveAttribute('href', '/meetings/meeting-today')
    expect(within(meetings).queryByRole('link', { name: 'Join Team check-in' })).not.toBeInTheDocument()

    expect(screen.getByLabelText('Task counts by status')).toHaveTextContent('4 Pending')
    expect(screen.getByLabelText('Task counts by status')).toHaveTextContent('1 Completed')
    const taskSection = screen.getByRole('region', { name: 'My tasks' })
    expect(within(taskSection).getByText('Task assignment email: Sent')).toBeInTheDocument()
    expect(within(taskSection).getAllByText('Task assignment email: Pending')).toHaveLength(2)
    expect(within(taskSection).getByText('Send meeting notes')).toBeInTheDocument()
    expect(within(taskSection).getByText('Update project tracker')).toBeInTheDocument()
    expect(within(taskSection).getByText('Prepare weekly report')).toBeInTheDocument()
    expect(within(taskSection).queryByText('Next week task')).not.toBeInTheDocument()
    expect(within(taskSection).getAllByRole('listitem')).toHaveLength(3)
    expect(within(taskSection).getByRole('link', { name: 'View all tasks' })).toHaveAttribute('href', '/tasks')
    const deadlines = screen.getByRole('region', { name: 'Due this week' })
    expect(within(deadlines).getByText('Review sprint notes')).toBeInTheDocument()
    expect(within(deadlines).getByText('Send meeting notes')).toBeInTheDocument()
    expect(within(deadlines).getByText('Showing 3 of 4 tasks due this week.')).toBeInTheDocument()
    expect(within(deadlines).getAllByRole('listitem')).toHaveLength(3)
    expect(within(deadlines).getByRole('link', { name: 'View all deadlines' })).toHaveAttribute('href', '/tasks')
    expect(within(deadlines).queryByText('Next week task')).not.toBeInTheDocument()

    expect(screen.getByText('A task is due soon')).toBeInTheDocument()
    const approvals = screen.getByRole('region', { name: 'Approvals waiting for you' })
    expect(within(approvals).getByText('Update a team task')).toBeInTheDocument()
    expect(within(approvals).getByRole('button', { name: 'Approve' })).toBeInTheDocument()
    expect(getMyTasks).toHaveBeenCalledWith('maya@example.test')
    expect(getAgentApprovals).toHaveBeenCalledWith('queue', 'pending')
    expect(getAgentApprovals).not.toHaveBeenCalledWith('mine', expect.anything())
    expect(screen.getByText('Dates shown in Asia/Kolkata.')).toBeInTheDocument()
  })

  it('documents UTC as the timezone fallback when employee preferences are missing', async () => {
    vi.mocked(getMeetings).mockResolvedValue([])
    vi.mocked(getMyTasks).mockResolvedValue([])
    vi.mocked(getNotifications).mockResolvedValue([])
    vi.mocked(getAgentApprovals).mockResolvedValue([])
    vi.mocked(getNotificationPreferences).mockResolvedValue({
      in_app_enabled: true,
      email_enabled: true,
      quiet_hours_start: null,
      quiet_hours_end: null,
      timezone_name: 'UTC',
      timezone_fallback: true,
    })

    render(<MemoryRouter><HomePage /></MemoryRouter>)

    expect(await screen.findByText(/Employee timezone is unavailable; using UTC/)).toBeInTheDocument()
    expect(screen.getByText('No approvals are waiting for your decision.')).toBeInTheDocument()
  })

  it('refreshes employee dashboard data within five seconds', async () => {
    vi.useFakeTimers()
    vi.mocked(getMeetings).mockResolvedValue([])
    vi.mocked(getMyTasks).mockResolvedValue([])
    vi.mocked(getNotifications).mockResolvedValue([])
    vi.mocked(getAgentApprovals).mockResolvedValue([])
    vi.mocked(getNotificationPreferences).mockResolvedValue({
      in_app_enabled: true,
      email_enabled: true,
      quiet_hours_start: null,
      quiet_hours_end: null,
      timezone_name: 'UTC',
      timezone_fallback: true,
    })

    render(<MemoryRouter><HomePage /></MemoryRouter>)
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(getMeetings).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(getMeetings).toHaveBeenCalledTimes(2)
    expect(getMyTasks).toHaveBeenCalledTimes(2)
    expect(getNotifications).toHaveBeenCalledTimes(2)
    expect(getAgentApprovals).toHaveBeenCalledTimes(2)
    expect(getNotificationPreferences).toHaveBeenCalledTimes(2)
  })

  it('keeps the existing non-employee home page unchanged', () => {
    authValue.user = {
      id: 'admin-1',
      name: 'System Admin',
      email: 'admin@example.test',
      role: 'admin',
      department: 'Operations',
    }

    render(<MemoryRouter><HomePage /></MemoryRouter>)

    expect(screen.getByRole('heading', { name: 'Your workspace is ready' })).toBeInTheDocument()
    expect(getMeetings).not.toHaveBeenCalled()
    expect(getMyTasks).not.toHaveBeenCalled()
  })
})
