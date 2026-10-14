// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { getDashboardTasks, getMyTasks, taskCalendarUrl } from '../api/tasks'
import { demoUsers } from '../mocks/mockUser'
import { TaskWorkspacePage } from './TaskWorkspacePage'

const { authValue } = vi.hoisted(() => ({
  authValue: { user: null as null | { id: string; name: string; email: string; role: string; department: string } },
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => authValue,
}))

vi.mock('../api/tasks', () => ({
  getMyTasks: vi.fn(),
  getDashboardTasks: vi.fn(),
  retryAssignmentEmail: vi.fn(),
  taskCalendarUrl: vi.fn(),
}))

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

    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(getMyTasks).toHaveBeenCalledTimes(2))
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
