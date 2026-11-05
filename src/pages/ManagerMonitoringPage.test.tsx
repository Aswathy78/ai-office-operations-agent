// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { getOperationalMonitoring, type OperationalMonitoringData } from '../api/tasks'
import { ManagerMonitoringPage } from './ManagerMonitoringPage'

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
    getOperationalMonitoring: vi.fn(),
  }
})

// Mock apiFetch for CSV export
vi.mock('../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    apiFetch: vi.fn().mockResolvedValue({
      ok: true,
      blob: () => Promise.resolve(new Blob(['task_id,title\n1,Test'], { type: 'text/csv' })),
    }),
  }
})

const mockMonitoringData: OperationalMonitoringData = {
  summary: {
    team_name: 'Engineering',
    total_action_items: 2,
    action_items_pending_review: 1,
    total_tasks_monitored: 3,
    email_failed_count: 1,
    calendar_failed_count: 1,
    workflow_failed_count: 1,
    total_failures: 3,
  },
  action_items: [
    {
      action_item_id: 'ai-101',
      task_description: 'Audit authentication latency',
      owner_name: 'Alice Smith',
      owner_email: 'alice@example.test',
      deadline_at: '2026-11-10T12:00:00Z',
      ownership_status: 'confirmed',
      review_status: 'pending_review',
      meeting_id: 'meet-1',
      meeting_title: 'Architecture Review',
    },
    {
      action_item_id: 'ai-102',
      task_description: 'Write database migrations',
      owner_name: 'Bob Jones',
      owner_email: 'bob@example.test',
      deadline_at: '2026-11-12T15:00:00Z',
      ownership_status: 'confirmed',
      review_status: 'confirmed',
      meeting_id: 'meet-1',
      meeting_title: 'Architecture Review',
    },
  ],
  task_status_rows: [
    {
      task_id: 'task-1',
      title: 'Deploy API Service',
      assignee_name: 'Alice Smith',
      assignee_email: 'alice@example.test',
      priority: 'urgent',
      due_at: '2026-11-08T18:00:00Z',
      status: 'pending',
      email_status: 'failed',
      email_error: 'SMTP auth 535 authentication failed',
      calendar_status: 'failed',
      calendar_error: 'Google Calendar API rate limit exceeded',
      calendar_provider: 'google',
      calendar_attempt_count: 3,
    },
    {
      task_id: 'task-2',
      title: 'Update Documentation',
      assignee_name: 'Bob Jones',
      assignee_email: 'bob@example.test',
      priority: 'medium',
      due_at: '2026-11-15T18:00:00Z',
      status: 'pending',
      email_status: 'sent',
      email_error: null,
      calendar_status: 'completed',
      calendar_error: null,
      calendar_provider: 'local',
      calendar_attempt_count: 1,
    },
  ],
  workflow_failures: [
    {
      run_id: 'wf-run-77',
      workflow_type: 'meeting_processing',
      status: 'failed',
      last_error: 'Transcription service timeout after 5 attempts',
      attempt_count: 5,
      max_attempts: 5,
      created_at: '2026-11-05T10:00:00Z',
      updated_at: '2026-11-05T10:30:00Z',
      run_link: '/admin/workflows#run=wf-run-77',
    },
  ],
  failure_reasons: [
    {
      category: 'email',
      error_message: 'SMTP auth 535 authentication failed',
      count: 3,
      last_occurred_at: '2026-11-05T11:00:00Z',
    },
    {
      category: 'calendar',
      error_message: 'Google Calendar API rate limit exceeded',
      count: 2,
      last_occurred_at: '2026-11-05T11:15:00Z',
    },
    {
      category: 'workflow',
      error_message: 'Transcription service timeout after 5 attempts',
      count: 1,
      last_occurred_at: '2026-11-05T10:30:00Z',
    },
  ],
}

describe('ManagerMonitoringPage', () => {
  beforeEach(() => {
    authValue.user = {
      id: 'manager-1',
      name: 'Maya Manager',
      email: 'maya@example.test',
      role: 'manager',
      department: 'Engineering',
    }
    vi.mocked(getOperationalMonitoring).mockResolvedValue(mockMonitoringData)
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders the monitoring header, title, and team name badge', async () => {
    render(
      <MemoryRouter>
        <ManagerMonitoringPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText('Operations Monitoring Portal')).toBeInTheDocument()
    expect(screen.getByTestId('team-badge')).toHaveTextContent('Engineering')
    expect(screen.getByTestId('total-failures-badge')).toHaveTextContent('3 Active Failures')
  })

  it('renders summary KPI cards with correct counts', async () => {
    render(
      <MemoryRouter>
        <ManagerMonitoringPage />
      </MemoryRouter>,
    )

    await screen.findByText('Operations Monitoring Portal')

    const kpiActionItems = screen.getByTestId('kpi-action-items')
    expect(kpiActionItems).toHaveTextContent('2')
    expect(kpiActionItems).toHaveTextContent('1 to review')

    const kpiTasks = screen.getByTestId('kpi-tasks')
    expect(kpiTasks).toHaveTextContent('3')

    const kpiEmailFailures = screen.getByTestId('kpi-email-failures')
    expect(kpiEmailFailures).toHaveTextContent('1')

    const kpiCalendarFailures = screen.getByTestId('kpi-calendar-failures')
    expect(kpiCalendarFailures).toHaveTextContent('1')

    const kpiWorkflowFailures = screen.getByTestId('kpi-workflow-failures')
    expect(kpiWorkflowFailures).toHaveTextContent('1')

    const kpiTotalFailures = screen.getByTestId('kpi-total-failures')
    expect(kpiTotalFailures).toHaveTextContent('3')
  })

  it('renders meeting action items table with owner, deadline, and review state', async () => {
    render(
      <MemoryRouter>
        <ManagerMonitoringPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText('Audit authentication latency')).toBeInTheDocument()
    const row1 = screen.getByTestId('action-item-row-ai-101')
    expect(within(row1).getByText('Alice Smith')).toBeInTheDocument()
    expect(within(row1).getByText('Architecture Review')).toBeInTheDocument()
    expect(screen.getByTestId('review-status-ai-101')).toHaveTextContent('pending_review')
    expect(screen.getByTestId('review-status-ai-102')).toHaveTextContent('confirmed')
  })

  it('renders task operational status table with email and calendar status', async () => {
    render(
      <MemoryRouter>
        <ManagerMonitoringPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText('Deploy API Service')).toBeInTheDocument()
    const row1 = screen.getByTestId('task-status-row-task-1')
    expect(within(row1).getByTestId('email-status-task-1')).toHaveTextContent('failed')
    expect(within(row1).getByText('SMTP auth 535 authentication failed')).toBeInTheDocument()
    expect(within(row1).getByTestId('calendar-status-task-1')).toHaveTextContent('failed')
    expect(within(row1).getByText('Google Calendar API rate limit exceeded')).toBeInTheDocument()

    // Second task has sent and completed
    const row2 = screen.getByTestId('task-status-row-task-2')
    expect(within(row2).getByText('Update Documentation')).toBeInTheDocument()
    expect(within(row2).getByTestId('email-status-task-2')).toHaveTextContent('sent')
    expect(within(row2).getByTestId('calendar-status-task-2')).toHaveTextContent('completed')
  })

  it('renders workflow failures table with link to run', async () => {
    render(
      <MemoryRouter>
        <ManagerMonitoringPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText('meeting_processing')).toBeInTheDocument()
    const wfRow = screen.getByTestId('workflow-failure-row-wf-run-77')
    expect(within(wfRow).getByText('wf-run-77')).toBeInTheDocument()
    expect(within(wfRow).getByText('Transcription service timeout after 5 attempts')).toBeInTheDocument()

    const link = screen.getByTestId('workflow-link-wf-run-77')
    expect(link).toHaveAttribute('href', '/admin/workflows#run=wf-run-77')
  })

  it('renders failure diagnostic root cause table with categories and counts', async () => {
    render(
      <MemoryRouter>
        <ManagerMonitoringPage />
      </MemoryRouter>,
    )

    expect(await screen.findByText('Failure Root Cause & Diagnostic Summary')).toBeInTheDocument()
    expect(screen.getByTestId('failure-count-0')).toHaveTextContent('3 incidents')
    expect(screen.getByTestId('failure-count-1')).toHaveTextContent('2 incidents')
    expect(screen.getByTestId('failure-count-2')).toHaveTextContent('1 incident')
  })

  it('filters task status table when "Failures Only" filter is clicked', async () => {
    render(
      <MemoryRouter>
        <ManagerMonitoringPage />
      </MemoryRouter>,
    )

    await screen.findByText('Deploy API Service')
    expect(screen.getByText('Update Documentation')).toBeInTheDocument()

    const failuresBtn = screen.getByTestId('filter-failures-btn')
    act(() => {
      fireEvent.click(failuresBtn)
    })

    // Failed task remains, clean task is filtered out
    expect(screen.getByText('Deploy API Service')).toBeInTheDocument()
    expect(screen.queryByText('Update Documentation')).not.toBeInTheDocument()
  })

  it('switches tabs and displays specific sections', async () => {
    render(
      <MemoryRouter>
        <ManagerMonitoringPage />
      </MemoryRouter>,
    )

    await screen.findByText('Operations Monitoring Portal')

    const tabWorkflows = screen.getByTestId('tab-workflow-failures')
    act(() => {
      fireEvent.click(tabWorkflows)
    })

    // Workflow failures is displayed, action items section is hidden
    expect(screen.getByRole('heading', { name: 'Workflow Failures' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Meeting Action Items' })).not.toBeInTheDocument()
  })

  it('triggers CSV export when Export CSV button is clicked', async () => {
    const createObjectURLMock = vi.fn().mockReturnValue('blob:mock-url')
    const revokeObjectURLMock = vi.fn()
    window.URL.createObjectURL = createObjectURLMock
    window.URL.revokeObjectURL = revokeObjectURLMock

    render(
      <MemoryRouter>
        <ManagerMonitoringPage />
      </MemoryRouter>,
    )

    await screen.findByText('Operations Monitoring Portal')

    const exportBtn = screen.getByTestId('export-csv-btn')
    expect(exportBtn).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(exportBtn)
    })

    expect(createObjectURLMock).toHaveBeenCalled()
  })

  it('renders AccessDenied for employee users', async () => {
    authValue.user = {
      id: 'emp-1',
      name: 'Alice Employee',
      email: 'alice@example.test',
      role: 'employee',
      department: 'Engineering',
    }

    render(
      <MemoryRouter>
        <ManagerMonitoringPage />
      </MemoryRouter>,
    )

    expect(screen.getByText(/Access Denied/i)).toBeInTheDocument()
    expect(screen.queryByText('Operations Monitoring Portal')).not.toBeInTheDocument()
  })
})
