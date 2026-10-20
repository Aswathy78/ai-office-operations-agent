// @vitest-environment jsdom
import { act, cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import {
  getMyTasks,
  retryTaskCalendarSync,
  type TaskCalendarSync,
  type TaskCalendarSyncStatus,
  type TaskRecord,
} from '../../api/tasks'
import { demoUsers } from '../../mocks/mockUser'
import { TaskWorkspacePage } from '../../pages/TaskWorkspacePage'
import { CalendarSyncStatus } from './CalendarSyncStatus'

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: demoUsers.employee }),
}))
vi.mock('../../api/emailDrafts', () => ({
  EMAIL_STATUS_POLL_MS: 2000,
  getEmailDeliveryIssues: vi.fn().mockResolvedValue([]),
}))
vi.mock('../../api/meetings', () => ({
  getActiveEmployees: vi.fn().mockResolvedValue([]),
  getCalendarMeetings: vi.fn().mockResolvedValue({ events: [] }),
}))
vi.mock('../../api/tasks', () => ({
  getMyTasks: vi.fn(),
  getDashboardTasks: vi.fn(),
  retryAssignmentEmail: vi.fn(),
  retryTaskCalendarSync: vi.fn(),
  setTaskStatus: vi.fn(),
  taskCalendarUrl: vi.fn(),
}))

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

function sync(status: TaskCalendarSyncStatus, extra: Partial<TaskCalendarSync> = {}): TaskCalendarSync {
  return {
    status,
    provider: 'local',
    pending_reason: null,
    attempt_count: 0,
    max_attempts: 5,
    next_attempt_at: null,
    last_error: null,
    last_synced_at: null,
    last_polled_at: null,
    external_change_detected_at: null,
    external_change_detail: null,
    ...extra,
  }
}

const task: TaskRecord = {
  id: 'task-1',
  title: 'Prepare the release notes',
  assignee_id: 'rahul',
  assignee_name: 'Rahul',
  creator_id: 'user-002',
  source: 'meeting_action_item',
  source_action_item_id: 'item-1',
  meeting_id: 'meeting-1',
  meeting_title: 'Sprint planning',
  transcript_id: 'transcript-1',
  source_segment_index: 0,
  source_timestamp_seconds: 1,
  due_at: '2026-11-02T12:00:00Z',
  priority: 'high',
  status: 'pending',
  assignment_email_status: 'sent',
  assignment_email_error: null,
  created_at: '2026-10-20T00:00:00Z',
}

function shownStatuses(): string[] {
  return Array.from(document.querySelectorAll('[data-calendar-status]')).map(
    (el) => el.getAttribute('data-calendar-status') ?? '',
  )
}

describe('CalendarSyncStatus', () => {
  it.each(['pending', 'processing', 'completed', 'retrying', 'failed'] as const)(
    'shows exactly one status for %s',
    (status) => {
      render(<CalendarSyncStatus sync={sync(status)} canRetry retrying={false} onRetry={vi.fn()} />)
      expect(shownStatuses()).toEqual([status])
    },
  )

  it('shows Pending before the scheduler has picked the task up', () => {
    render(<CalendarSyncStatus sync={null} canRetry retrying={false} onRetry={vi.fn()} />)
    expect(shownStatuses()).toEqual(['pending'])
    expect(screen.getByText('Pending')).toBeInTheDocument()
  })

  it('shows a failed sync as Failed with its error and a retry button — never Completed', async () => {
    const onRetry = vi.fn()
    render(
      <CalendarSyncStatus
        sync={sync('failed', { attempt_count: 5, last_error: 'CalendarProviderError: Calendar API unavailable (503)' })}
        canRetry
        retrying={false}
        onRetry={onRetry}
      />,
    )
    expect(screen.getByText('Failed')).toBeInTheDocument()
    expect(screen.queryByText('Completed')).not.toBeInTheDocument()
    expect(screen.getByText(/Calendar API unavailable/)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry calendar sync' }))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('explains retries and external calendar changes', () => {
    render(
      <CalendarSyncStatus
        sync={sync('retrying', {
          attempt_count: 1,
          next_attempt_at: '2026-10-20T10:01:00Z',
          last_error: 'timed out',
          external_change_detected_at: '2026-10-20T10:00:00Z',
          external_change_detail: 'The deadline was moved to 09 Nov 2026 12:00 UTC in the calendar; it has been set back.',
        })}
        canRetry
        retrying={false}
        onRetry={vi.fn()}
      />,
    )
    expect(screen.getByText(/Attempt 1 of 5 failed; next try/)).toBeInTheDocument()
    expect(screen.getByText(/moved to 09 Nov 2026/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Retry calendar sync' })).not.toBeInTheDocument()
  })
})

describe('My Tasks calendar column', () => {
  it('refreshes while a sync is in progress until it completes, then stops', async () => {
    vi.useFakeTimers()
    vi.mocked(getMyTasks)
      .mockResolvedValueOnce([{ ...task, calendar_sync: sync('pending', { pending_reason: 'New task deadline' }) }])
      .mockResolvedValueOnce([{ ...task, calendar_sync: sync('processing') }])
      .mockResolvedValue([{ ...task, calendar_sync: sync('completed', { last_synced_at: '2026-10-20T10:00:00Z' }) }])

    render(<MemoryRouter><TaskWorkspacePage view="mine" /></MemoryRouter>)
    await act(async () => { await vi.advanceTimersByTimeAsync(0) })
    expect(shownStatuses()).toEqual(['pending'])

    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(shownStatuses()).toEqual(['processing'])

    await act(async () => { await vi.advanceTimersByTimeAsync(3000) })
    expect(shownStatuses()).toEqual(['completed'])

    const calls = vi.mocked(getMyTasks).mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(30_000) })
    expect(vi.mocked(getMyTasks).mock.calls.length).toBe(calls)
  })

  it('retries a failed sync from the task list', async () => {
    vi.mocked(getMyTasks).mockResolvedValue([{ ...task, calendar_sync: sync('failed', { last_error: 'boom' }) }])
    vi.mocked(retryTaskCalendarSync).mockResolvedValue({ ...task, calendar_sync: sync('pending', { pending_reason: 'Retry requested' }) })

    render(<MemoryRouter><TaskWorkspacePage view="mine" /></MemoryRouter>)
    await userEvent.click(await screen.findByRole('button', { name: 'Retry calendar sync' }))

    expect(retryTaskCalendarSync).toHaveBeenCalledWith('task-1')
    expect(await screen.findByText('Retry requested')).toBeInTheDocument()
    expect(shownStatuses()).toEqual(['pending'])
  })
})
