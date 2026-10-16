// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { getCalendarMeetings } from '../api/meetings'
import { getDashboardTasks, getMyTasks, setTaskStatus, taskCalendarUrl, type TaskRecord } from '../api/tasks'
import { demoUsers } from '../mocks/mockUser'
import { TaskWorkspacePage } from './TaskWorkspacePage'

const { authValue } = vi.hoisted(() => ({
  authValue: { user: null as null | { id: string; name: string; email: string; role: string; department: string } },
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => authValue,
}))

vi.mock('../api/emailDrafts', () => ({
  EMAIL_STATUS_POLL_MS: 2000,
  getEmailDeliveryIssues: vi.fn().mockResolvedValue([]),
}))

vi.mock('../api/meetings', () => ({
  getActiveEmployees: vi.fn().mockResolvedValue([]),
  getCalendarMeetings: vi.fn(),
}))

vi.mock('../api/tasks', () => ({
  getMyTasks: vi.fn(),
  getDashboardTasks: vi.fn(),
  retryAssignmentEmail: vi.fn(),
  setTaskStatus: vi.fn(),
  taskCalendarUrl: vi.fn(),
}))

beforeEach(() => {
  vi.mocked(getCalendarMeetings).mockResolvedValue({
    timezone: 'UTC',
    meetings: [],
  })
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('TaskWorkspacePage employee identity', () => {
  it('renders the calendar grid and preserves export and manual refresh', async () => {
    authValue.user = demoUsers.employee
    vi.mocked(getMyTasks).mockResolvedValue([])
    vi.mocked(taskCalendarUrl).mockReturnValue('/api/v1/tasks/calendar.ics')
    const user = userEvent.setup()

    render(
      <MemoryRouter>
        <TaskWorkspacePage view="calendar" />
      </MemoryRouter>,
    )

    expect(await screen.findByRole('table', { name: /task calendar/i })).toBeInTheDocument()
    expect(
      screen.getByRole('heading', {
        name: new Intl.DateTimeFormat('en-US', {
          month: 'long',
          year: 'numeric',
        }).format(new Date()),
      }),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Add deadlines to calendar' }))
      .toHaveAttribute('href', '/api/v1/tasks/calendar.ics')
    expect(getMyTasks).toHaveBeenCalledWith(demoUsers.employee.email)
    const now = new Date()
    expect(getCalendarMeetings).toHaveBeenCalledWith(
      demoUsers.employee.id,
      demoUsers.employee.email,
      now.getFullYear(),
      now.getMonth() + 1,
    )

    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => {
      expect(getMyTasks).toHaveBeenCalledTimes(2)
      expect(getCalendarMeetings).toHaveBeenCalledTimes(2)
    })
  })

  it('shows a task on its due date after Calendar Refresh fetches it', async () => {
    authValue.user = demoUsers.employee
    const dueAt = new Date()
    dueAt.setDate(15)
    dueAt.setHours(16, 0, 0, 0)
    const dueDateLabel = new Intl.DateTimeFormat('en-US', {
      weekday: 'long',
      month: 'long',
      day: 'numeric',
      year: 'numeric',
    }).format(dueAt)
    vi.mocked(getMyTasks)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        {
          id: 'task-refresh',
          title: 'Newly confirmed calendar task',
          assignee_id: 'employee-1',
          assignee_name: 'Rahul',
          creator_id: 'manager-1',
          source: 'meeting_action_item',
          source_action_item_id: 'action-1',
          meeting_id: 'meeting-1',
          meeting_title: 'Planning meeting',
          transcript_id: 'transcript-1',
          source_segment_index: 0,
          source_timestamp_seconds: 5,
          due_at: dueAt.toISOString(),
          priority: 'medium',
          status: 'pending',
          assignment_email_status: 'sent',
          assignment_email_error: null,
          created_at: new Date().toISOString(),
        },
      ])
    const user = userEvent.setup()

    render(
      <MemoryRouter>
        <TaskWorkspacePage view="calendar" />
      </MemoryRouter>,
    )

    expect(await screen.findByRole('table', { name: /task calendar/i })).toBeInTheDocument()
    expect(screen.queryByText('Newly confirmed calendar task')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Refresh' }))

    const dueDay = await screen.findByRole('cell', { name: dueDateLabel })
    expect(within(dueDay).getByRole('button', {
      name: /Newly confirmed calendar task/,
    })).toBeInTheDocument()
  })

  it('displays backend meetings with task deadlines and reloads meetings on Refresh', async () => {
    authValue.user = demoUsers.employee
    const now = new Date()
    const meetingStart = new Date(
      now.getFullYear(),
      now.getMonth(),
      10,
      10,
      0,
      0,
    ).toISOString()
    vi.mocked(getMyTasks).mockResolvedValue([
      {
        id: 'task-mixed-calendar',
        title: 'Existing task deadline',
        assignee_id: demoUsers.employee.id,
        assignee_name: demoUsers.employee.name,
        creator_id: 'manager-1',
        source: 'meeting_action_item',
        source_action_item_id: 'action-1',
        meeting_id: 'source-meeting',
        meeting_title: 'Source meeting',
        transcript_id: 'transcript-1',
        source_segment_index: 1,
        source_timestamp_seconds: 4,
        due_at: meetingStart,
        priority: 'medium',
        status: 'pending',
        assignment_email_status: 'sent',
        assignment_email_error: null,
        created_at: new Date().toISOString(),
      },
    ])
    vi.mocked(getCalendarMeetings).mockResolvedValue({
      timezone: 'UTC',
      meetings: [
        {
          id: 'scheduled-meeting',
          title: 'Monthly planning',
          start_at: meetingStart,
          end_at: new Date(new Date(meetingStart).getTime() + 45 * 60_000).toISOString(),
          join_url: 'http://localhost:5173/meetings/scheduled-meeting',
          status: 'scheduled',
          organizer_id: demoUsers.employee.id,
          attendees: [
            {
              employee_id: 'attendee-1',
              name: 'Ava Patel',
              email: 'ava@example.test',
            },
          ],
        },
      ],
    })
    const user = userEvent.setup()

    render(
      <MemoryRouter>
        <TaskWorkspacePage view="calendar" />
      </MemoryRouter>,
    )

    expect(await screen.findByRole('button', {
      name: /Meeting: Monthly planning/,
    })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Existing task deadline/ })).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Meeting: Monthly planning/ }))
    expect(screen.getByRole('region', { name: 'Monthly planning' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Join Meeting' })).toHaveAttribute(
      'href',
      'http://localhost:5173/meetings/scheduled-meeting',
    )

    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(getCalendarMeetings).toHaveBeenCalledTimes(2))
  })

  it('fetches a new month of meetings when the calendar month changes', async () => {
    authValue.user = demoUsers.employee
    vi.mocked(getMyTasks).mockResolvedValue([])
    const user = userEvent.setup()
    const currentMonth = new Date()
    const nextMonth = new Date(
      currentMonth.getFullYear(),
      currentMonth.getMonth() + 1,
      1,
    )

    render(
      <MemoryRouter>
        <TaskWorkspacePage view="calendar" />
      </MemoryRouter>,
    )

    expect(await screen.findByRole('table', { name: /task calendar with meetings/i }))
      .toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Next Month' }))

    await waitFor(() => {
      expect(getCalendarMeetings).toHaveBeenLastCalledWith(
        demoUsers.employee.id,
        demoUsers.employee.email,
        nextMonth.getFullYear(),
        nextMonth.getMonth() + 1,
      )
    })
  })

  it('selects and focuses the task requested by the task deep link', async () => {
    authValue.user = demoUsers.employee
    vi.mocked(getMyTasks).mockResolvedValue([
      {
        id: 'task-1',
        title: 'Prepare the project status report',
        assignee_id: 'rahul-employee-id',
        assignee_name: 'Rahul',
        creator_id: 'manager-id',
        source: 'meeting_action_item',
        source_action_item_id: 'action-item-1',
        meeting_id: 'meeting-1',
        meeting_title: 'Project status',
        transcript_id: 'transcript-1',
        source_segment_index: 0,
        source_timestamp_seconds: 11,
        due_at: '2026-10-15T00:00:00Z',
        priority: 'medium',
        status: 'pending',
        assignment_email_status: 'pending',
        assignment_email_error: null,
        created_at: '2026-10-13T00:00:00Z',
      },
    ])

    render(
      <MemoryRouter initialEntries={['/tasks?task_id=task-1']}>
        <TaskWorkspacePage view="mine" />
      </MemoryRouter>,
    )

    const selectedRow = await screen.findByRole('row', {
      name: /Prepare the project status report/,
    })
    await waitFor(() => expect(selectedRow).toHaveAttribute('aria-selected', 'true'))
    expect(selectedRow).toHaveClass('task-row-selected')
    expect(selectedRow).toHaveFocus()
  })

  it('loads tasks for the employee directory email of the signed-in demo employee', async () => {
    authValue.user = demoUsers.employee
    vi.mocked(getMyTasks).mockResolvedValue([
      {
        id: 'task-1',
        title: 'prepare the project status report',
        assignee_id: 'rahul-employee-id',
        assignee_name: 'Rahul',
        creator_id: 'manager-id',
        source: 'meeting_action_item',
        source_action_item_id: 'action-item-1',
        meeting_id: 'meeting-1',
        meeting_title: 'Project status',
        transcript_id: 'transcript-1',
        source_segment_index: 0,
        source_timestamp_seconds: 11,
        due_at: '2026-10-15T00:00:00Z',
        priority: 'medium',
        status: 'pending',
        assignment_email_status: 'pending',
        assignment_email_error: null,
        created_at: '2026-10-13T00:00:00Z',
      },
    ])

    render(
      <MemoryRouter>
        <TaskWorkspacePage view="mine" />
      </MemoryRouter>,
    )

    await waitFor(() => {
      expect(getMyTasks).toHaveBeenCalledWith('rahulexample.test@gmail.com')
    })
    expect(
      await screen.findByText('prepare the project status report'),
    ).toBeInTheDocument()
    expect(screen.queryByText('No tasks yet')).not.toBeInTheDocument()
    expect(
      screen.getByRole('link', { name: 'Project status - 0:11.00' }),
    ).toHaveAttribute(
      'href',
      '/meetings/meeting-1/report?transcript_id=transcript-1&segment=0',
    )
  })

  it('shows newly available dashboard tasks after an automatic refresh cycle', async () => {
    vi.useFakeTimers()
    authValue.user = {
      id: 'manager-1',
      name: 'Maya',
      email: 'manager@example.test',
      role: 'manager',
      department: 'Engineering',
    }
    vi.mocked(getDashboardTasks)
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        {
          id: 'task-2',
          title: 'Prepare the launch brief',
          assignee_id: 'employee-2',
          assignee_name: 'Ava Patel',
          creator_id: 'manager-1',
          source: 'meeting_action_item',
          source_action_item_id: 'action-item-2',
          meeting_id: 'meeting-2',
          meeting_title: 'Launch planning',
          transcript_id: 'transcript-2',
          source_segment_index: 1,
          source_timestamp_seconds: 18.5,
          due_at: '2026-10-16T00:00:00Z',
          priority: 'high',
          status: 'pending',
          assignment_email_status: 'pending',
          assignment_email_error: null,
          created_at: '2026-10-13T00:00:00Z',
        },
      ])

    render(
      <MemoryRouter>
        <TaskWorkspacePage view="dashboard" />
      </MemoryRouter>,
    )

    await act(async () => {
      await Promise.resolve()
    })
    expect(screen.getByText('No tasks yet')).toBeInTheDocument()
    expect(getDashboardTasks).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000)
    })

    expect(screen.getByText('Prepare the launch brief')).toBeInTheDocument()
    expect(screen.getByText('Ava Patel')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Launch planning - 0:18.50' }))
      .toBeInTheDocument()
    expect(getDashboardTasks).toHaveBeenCalledTimes(2)
  })

  it('does not poll employee tasks', async () => {
    vi.useFakeTimers()
    authValue.user = demoUsers.employee
    vi.mocked(getMyTasks).mockResolvedValue([])

    render(
      <MemoryRouter>
        <TaskWorkspacePage view="mine" />
      </MemoryRouter>,
    )
    await act(async () => {
      await Promise.resolve()
    })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })

    expect(getMyTasks).toHaveBeenCalledTimes(1)
    expect(getDashboardTasks).not.toHaveBeenCalled()
  })
})

describe('TaskWorkspacePage task completion (Story 4.4)', () => {
  const overdueTask: TaskRecord = {
    id: 'task-9',
    title: 'Send the vendor contract',
    assignee_id: 'rahul',
    assignee_name: 'Rahul',
    creator_id: 'user-002',
    source: 'meeting_action_item',
    source_action_item_id: 'item-9',
    meeting_id: 'meeting-9',
    meeting_title: 'Vendor sync',
    transcript_id: 'transcript-9',
    source_segment_index: 0,
    source_timestamp_seconds: 3,
    due_at: '2020-01-01T09:00:00Z',
    priority: 'high',
    status: 'pending',
    assignment_email_status: 'sent',
    assignment_email_error: null,
    created_at: '2019-12-30T00:00:00Z',
  }

  it('lets the assignee mark an overdue task complete, which stops reminders', async () => {
    authValue.user = demoUsers.employee
    vi.mocked(getMyTasks).mockResolvedValue([overdueTask])
    vi.mocked(setTaskStatus).mockResolvedValue({
      outcome: 'applied',
      message: 'Task marked as Completed. No further reminders will be sent.',
      task: { ...overdueTask, status: 'completed', completed_at: '2026-10-15T10:00:00Z' },
      reassignment_request: null,
      notifications: [],
    })
    const user = userEvent.setup()

    render(
      <MemoryRouter>
        <TaskWorkspacePage view="mine" />
      </MemoryRouter>,
    )

    expect(await screen.findByText('Overdue')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Mark complete' }))

    expect(setTaskStatus).toHaveBeenCalledWith('task-9', 'completed', {
      id: demoUsers.employee.id,
      role: 'employee',
      team: demoUsers.employee.department,
      email: demoUsers.employee.email,
    })
    expect(await screen.findByText('Completed')).toBeInTheDocument()
    expect(screen.queryByText('Overdue')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reopen' })).toBeInTheDocument()
  })
})
