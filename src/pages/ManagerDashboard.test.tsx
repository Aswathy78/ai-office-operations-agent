// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { getTeamDashboard, type TeamDashboardData } from '../api/tasks'
import { HomePage } from './HomePage'
import { ManagerDashboard } from './ManagerDashboard'

const { authValue } = vi.hoisted(() => ({
  authValue: {
    user: {
      id: 'manager-1',
      name: 'Maya Manager',
      email: 'maya@example.test',
      role: 'manager',
      department: 'Engineering',
    },
  },
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => authValue,
}))

vi.mock('../api/tasks', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/tasks')>()
  return {
    ...actual,
    getTeamDashboard: vi.fn(),
    getMyTasks: vi.fn().mockResolvedValue([]),
  }
})

vi.mock('../api/assistantApprovals', () => ({
  getAgentApprovals: vi.fn().mockResolvedValue([]),
  decideAgentApproval: vi.fn(),
}))

vi.mock('../api/meetings', () => ({
  getMeetings: vi.fn().mockResolvedValue([]),
}))

vi.mock('../api/notifications', () => ({
  getNotifications: vi.fn().mockResolvedValue([]),
  getNotificationPreferences: vi.fn().mockResolvedValue({
    timezone_name: 'UTC',
    timezone_fallback: false,
    channel_email: true,
    channel_in_app: true,
  }),
}))

const mockDashboardData: TeamDashboardData = {
  summary: {
    team_name: 'Engineering',
    total_members: 3,
    total_tasks: 6,
    open_tasks: 4,
    due_soon_tasks: 2,
    overdue_tasks: 1,
    completed_tasks: 2,
    completion_rate: 33.3,
  },
  members: [
    {
      employee_id: 'emp-1',
      employee_name: 'Alice Johnson',
      employee_email: 'alice@example.test',
      total_tasks: 3,
      open_tasks: 2,
      due_soon_tasks: 1,
      overdue_tasks: 1,
      completed_tasks: 1,
      completion_rate: 33.3,
    },
    {
      employee_id: 'emp-2',
      employee_name: 'Bob Smith',
      employee_email: 'bob@example.test',
      total_tasks: 3,
      open_tasks: 2,
      due_soon_tasks: 1,
      overdue_tasks: 0,
      completed_tasks: 1,
      completion_rate: 33.3,
    },
    {
      employee_id: 'emp-3',
      employee_name: 'Charlie Brown',
      employee_email: 'charlie@example.test',
      total_tasks: 0,
      open_tasks: 0,
      due_soon_tasks: 0,
      overdue_tasks: 0,
      completed_tasks: 0,
      completion_rate: 0.0,
    },
  ],
  overdue_tasks: [
    {
      task_id: 'task-overdue-1',
      title: 'Fix Production Bug',
      assignee_id: 'emp-1',
      assignee_name: 'Alice Johnson',
      assignee_email: 'alice@example.test',
      due_at: '2026-11-04T10:00:00Z',
      overdue_hours: 24,
      priority: 'urgent',
      status: 'pending',
    },
  ],
}

describe('ManagerDashboard', () => {
  beforeEach(() => {
    authValue.user = {
      id: 'manager-1',
      name: 'Maya Manager',
      email: 'maya@example.test',
      role: 'manager',
      department: 'Engineering',
    }
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
    vi.clearAllMocks()
  })

  // 1. Dashboard renders correctly
  it('1. renders header, team name, and member count correctly', async () => {
    vi.mocked(getTeamDashboard).mockResolvedValue(mockDashboardData)

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    expect(await screen.findByRole('heading', { name: 'Team Overview' })).toBeInTheDocument()
    expect(screen.getByText('Monitor team performance, workload and deadlines.')).toBeInTheDocument()
    expect(screen.getByTestId('team-name-badge')).toHaveTextContent('Engineering')
    expect(screen.getByTestId('team-members-count')).toHaveTextContent('3 members')
    expect(screen.getByText('Live')).toBeInTheDocument()
  })

  // 2. Team statistics display correctly
  it('2. displays all five team statistics cards accurately', async () => {
    vi.mocked(getTeamDashboard).mockResolvedValue(mockDashboardData)

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    const statsSection = await screen.findByTestId('team-statistics')
    expect(within(statsSection).getByText('Total Tasks')).toBeInTheDocument()
    expect(screen.getByTestId('stat-total-tasks')).toHaveTextContent('6')

    expect(within(statsSection).getByText('Open Tasks')).toBeInTheDocument()
    expect(screen.getByTestId('stat-open-tasks')).toHaveTextContent('4')

    expect(within(statsSection).getByText('Due Soon (<24h)')).toBeInTheDocument()
    expect(screen.getByTestId('stat-due-soon-tasks')).toHaveTextContent('2')

    expect(within(statsSection).getByText('Overdue Tasks')).toBeInTheDocument()
    expect(screen.getByTestId('stat-overdue-tasks')).toHaveTextContent('1')

    expect(within(statsSection).getByText('Completion Rate')).toBeInTheDocument()
    expect(screen.getByTestId('stat-completion-rate')).toHaveTextContent('33.3%')
  })

  // 3. Team members appear in the workload table
  it('3. displays all team members in the workload table', async () => {
    vi.mocked(getTeamDashboard).mockResolvedValue(mockDashboardData)

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    const workloadSection = await screen.findByTestId('workload-section')
    expect(within(workloadSection).getByText('Alice Johnson')).toBeInTheDocument()
    expect(within(workloadSection).getByText('alice@example.test')).toBeInTheDocument()
    expect(within(workloadSection).getByText('Bob Smith')).toBeInTheDocument()
    expect(within(workloadSection).getByText('bob@example.test')).toBeInTheDocument()
    expect(within(workloadSection).getByText('Charlie Brown')).toBeInTheDocument()
    expect(within(workloadSection).getByText('charlie@example.test')).toBeInTheDocument()
  })

  // 4. Employees with zero tasks appear
  it('4. displays employees with zero tasks with count 0 and 0.0% completion', async () => {
    vi.mocked(getTeamDashboard).mockResolvedValue(mockDashboardData)

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    const charlieRow = await screen.findByTestId('workload-row-emp-3')
    expect(within(charlieRow).getByText('Charlie Brown')).toBeInTheDocument()
    expect(within(charlieRow).getByText('0.0%')).toBeInTheDocument()
  })

  // 5. Completion rates display correctly
  it('5. renders completion rate percentages and progress bar elements', async () => {
    vi.mocked(getTeamDashboard).mockResolvedValue(mockDashboardData)

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    const aliceRow = await screen.findByTestId('workload-row-emp-1')
    expect(within(aliceRow).getByText('33.3%')).toBeInTheDocument()
    const progressBar = within(aliceRow).getByRole('progressbar')
    expect(progressBar).toHaveAttribute('aria-valuenow', '33.3')
  })

  // 6. Overdue tasks appear separately
  it('6. displays overdue tasks in a dedicated section with details', async () => {
    vi.mocked(getTeamDashboard).mockResolvedValue(mockDashboardData)

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    const overdueSection = await screen.findByTestId('overdue-section')
    expect(within(overdueSection).getByText('Fix Production Bug')).toBeInTheDocument()
    expect(within(overdueSection).getByText('Alice Johnson')).toBeInTheDocument()
    expect(within(overdueSection).getByText('1d overdue')).toBeInTheDocument()
    expect(within(overdueSection).getByText('urgent')).toBeInTheDocument()
    expect(within(overdueSection).getByText('pending')).toBeInTheDocument()
  })

  // 7. Empty overdue state works
  it('7. displays a meaningful empty state when there are no overdue tasks', async () => {
    const dataWithoutOverdue: TeamDashboardData = {
      ...mockDashboardData,
      summary: {
        ...mockDashboardData.summary,
        overdue_tasks: 0,
      },
      overdue_tasks: [],
    }
    vi.mocked(getTeamDashboard).mockResolvedValue(dataWithoutOverdue)

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    expect(await screen.findByTestId('empty-overdue-state')).toBeInTheDocument()
    expect(screen.getByText('No overdue tasks')).toBeInTheDocument()
    expect(screen.getByText(/All assigned tasks are on track/)).toBeInTheDocument()
  })

  // 8. Loading state works
  it('8. renders initial loading indicator before data arrives', () => {
    vi.mocked(getTeamDashboard).mockReturnValue(new Promise(() => { }))

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    expect(screen.getByTestId('dashboard-loading')).toBeInTheDocument()
    expect(screen.getByText('Loading team dashboard…')).toBeInTheDocument()
  })

  // 9. API error state works
  it('9. renders error state when initial API request fails', async () => {
    vi.mocked(getTeamDashboard).mockRejectedValue(new Error('Network connection failed'))

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    expect(await screen.findByTestId('dashboard-error')).toBeInTheDocument()
    expect(screen.getByText('Unable to load team dashboard')).toBeInTheDocument()
    expect(screen.getByText('Network connection failed')).toBeInTheDocument()
  })

  // 10. Retry action works
  it('10. re-executes API request when Retry button is clicked', async () => {
    vi.mocked(getTeamDashboard)
      .mockRejectedValueOnce(new Error('Temporary outage'))
      .mockResolvedValueOnce(mockDashboardData)

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    const retryBtn = await screen.findByRole('button', { name: 'Retry' })
    await act(async () => {
      fireEvent.click(retryBtn)
    })

    expect(await screen.findByRole('heading', { name: 'Team Overview' })).toBeInTheDocument()
    expect(getTeamDashboard).toHaveBeenCalledTimes(2)
  })

  // 11. Automatic polling works
  it('11. polls team dashboard data every 3 seconds', async () => {
    vi.useFakeTimers()
    vi.mocked(getTeamDashboard).mockResolvedValue(mockDashboardData)

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    await act(async () => {
      await Promise.resolve()
    })
    expect(getTeamDashboard).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(getTeamDashboard).toHaveBeenCalledTimes(2)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })
    expect(getTeamDashboard).toHaveBeenCalledTimes(3)
  })

  // 12. Manual refresh works
  it('12. allows user to manually refresh data via the refresh button', async () => {
    vi.mocked(getTeamDashboard).mockResolvedValue(mockDashboardData)

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    const refreshBtn = await screen.findByRole('button', { name: 'Refresh dashboard' })
    await act(async () => {
      fireEvent.click(refreshBtn)
    })

    expect(getTeamDashboard).toHaveBeenCalledTimes(2)
  })

  // 13. Employee dashboard remains unaffected / Employee access denied
  it('13. prevents employees from accessing ManagerDashboard and shows AccessDenied', () => {
    authValue.user = {
      id: 'emp-1',
      name: 'Alex Employee',
      email: 'alex@example.test',
      role: 'employee',
      department: 'Engineering',
    }

    render(
      <MemoryRouter>
        <ManagerDashboard />
      </MemoryRouter>,
    )

    expect(screen.getByRole('heading', { name: /access denied/i })).toBeInTheDocument()
    expect(getTeamDashboard).not.toHaveBeenCalled()
  })

  // 14. Role-based routing on HomePage works
  it('14. integrates ManagerDashboard for manager role on HomePage and preserves EmployeeDashboard for employee', async () => {
    // 14a: When role is manager, HomePage renders ManagerDashboard
    authValue.user = {
      id: 'manager-1',
      name: 'Maya Manager',
      email: 'maya@example.test',
      role: 'manager',
      department: 'Engineering',
    }
    vi.mocked(getTeamDashboard).mockResolvedValue(mockDashboardData)

    const { unmount } = render(
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>,
    )

    expect(await screen.findByRole('heading', { name: 'Team Overview' })).toBeInTheDocument()
    expect(getTeamDashboard).toHaveBeenCalled()
    unmount()

    // 14b: When role is employee, HomePage renders EmployeeDashboard
    authValue.user = {
      id: 'emp-1',
      name: 'Alex Employee',
      email: 'alex@example.test',
      role: 'employee',
      department: 'Engineering',
    }

    render(
      <MemoryRouter>
        <HomePage />
      </MemoryRouter>,
    )

    expect(await screen.findByRole('heading', { name: 'Good morning, Alex Employee' })).toBeInTheDocument()
    expect(screen.getByText('Employee workspace')).toBeInTheDocument()
  })
})
