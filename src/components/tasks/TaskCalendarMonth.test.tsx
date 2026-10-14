// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import type { TaskRecord } from '../../api/tasks'
import { TaskCalendarMonth } from './TaskCalendarMonth'

function makeTask(
  id: string,
  title: string,
  dueAt: string,
  overrides: Partial<TaskRecord> = {},
): TaskRecord {
  return {
    id,
    title,
    assignee_id: 'employee-1',
    assignee_name: 'Rahul',
    assignee_team: 'Engineering',
    creator_id: 'manager-1',
    source: 'meeting_action_item',
    source_action_item_id: `action-${id}`,
    meeting_id: 'meeting-1',
    meeting_title: 'Project planning',
    transcript_id: 'transcript-1',
    source_segment_index: 2,
    source_timestamp_seconds: 75.5,
    due_at: dueAt,
    priority: 'high',
    status: 'pending',
    assignment_email_status: 'sent',
    assignment_email_error: null,
    created_at: '2026-10-01T00:00:00Z',
    ...overrides,
  }
}

function localIso(year: number, month: number, day: number, hour = 16): string {
  return new Date(year, month, day, hour, 0, 0).toISOString()
}

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date(2026, 9, 14, 12, 0, 0))
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
})

describe('TaskCalendarMonth', () => {
  it('renders Sunday through Saturday and highlights today', () => {
    render(<TaskCalendarMonth tasks={[]} />)

    expect(screen.getByRole('heading', { name: 'October 2026' })).toBeInTheDocument()
    const calendar = screen.getByRole('table', { name: 'October 2026 task calendar' })
    expect(within(calendar).getAllByRole('columnheader').map((header) => header.textContent))
      .toEqual(['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'])
    expect(calendar.querySelector('time[aria-current="date"]')).toHaveAttribute(
      'dateTime',
      '2026-10-14',
    )
  })

  it('navigates to previous and next months across year boundaries', () => {
    render(<TaskCalendarMonth tasks={[]} />)

    fireEvent.click(screen.getByRole('button', { name: 'Previous Month' }))
    expect(screen.getByRole('heading', { name: 'September 2026' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Next Month' }))
    fireEvent.click(screen.getByRole('button', { name: 'Next Month' }))
    expect(screen.getByRole('heading', { name: 'November 2026' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Next Month' }))
    expect(screen.getByRole('heading', { name: 'December 2026' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Next Month' }))
    expect(screen.getByRole('heading', { name: 'January 2027' })).toBeInTheDocument()
  })

  it('returns to the current month with Today', () => {
    render(<TaskCalendarMonth tasks={[]} />)

    fireEvent.click(screen.getByRole('button', { name: 'Next Month' }))
    expect(screen.getByRole('heading', { name: 'November 2026' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Today' }))

    expect(screen.getByRole('heading', { name: 'October 2026' })).toBeInTheDocument()
    expect(screen.getByRole('table').querySelector('time[aria-current="date"]'))
      .toHaveAttribute('dateTime', '2026-10-14')
  })

  it('places multiple tasks on their due date and shows due time, priority, status, and assignee', () => {
    const tasks = [
      makeTask('task-1', 'Prepare status report', localIso(2026, 9, 15, 16)),
      makeTask('task-2', 'Review launch brief', localIso(2026, 9, 15, 17), {
        priority: 'urgent',
        assignee_name: 'Ava Patel',
      }),
    ]
    render(<TaskCalendarMonth tasks={tasks} />)

    const dueDay = screen.getByRole('cell', { name: 'Thursday, October 15, 2026' })
    expect(within(dueDay).getByRole('button', {
      name: 'Prepare status report, 4:00 PM, high priority, pending',
    })).toBeInTheDocument()
    expect(within(dueDay).getByRole('button', {
      name: 'Review launch brief, 5:00 PM, urgent priority, pending',
    })).toBeInTheDocument()
    expect(within(dueDay).getByText('Rahul')).toBeInTheDocument()
    expect(within(dueDay).getByText('Ava Patel')).toBeInTheDocument()
  })

  it('keeps tasks without a valid due date outside the month grid', () => {
    const undated = makeTask('task-undated', 'Unscheduled follow-up', '')
    render(<TaskCalendarMonth tasks={[undated]} />)

    const undatedSection = screen.getByRole('region', { name: 'Tasks without due dates' })
    expect(within(undatedSection).getByRole('button', {
      name: 'Unscheduled follow-up, no due date',
    })).toBeInTheDocument()
    expect(screen.queryByRole('cell', { name: /Unscheduled follow-up/ })).not.toBeInTheDocument()
  })

  it('opens task details and preserves the meeting and transcript source link', () => {
    const task = makeTask('task-1', 'Prepare status report', localIso(2026, 9, 15))
    render(
      <MemoryRouter>
        <TaskCalendarMonth tasks={[task]} />
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('button', {
      name: 'Prepare status report, 4:00 PM, high priority, pending',
    }))

    const details = screen.getByRole('region', { name: 'Prepare status report' })
    expect(within(details).getByText('Project planning')).toBeInTheDocument()
    expect(within(details).getByText('Rahul (Engineering)')).toBeInTheDocument()
    expect(within(details).getByText('2 at 1:15.50')).toBeInTheDocument()
    expect(within(details).getByRole('link', {
      name: 'Open source meeting: Project planning - 1:15.50',
    })).toHaveAttribute(
      'href',
      '/meetings/meeting-1/report?transcript_id=transcript-1&segment=2',
    )

    fireEvent.click(within(details).getByRole('button', { name: 'Close task details' }))
    expect(screen.queryByRole('region', { name: 'Prepare status report' })).not.toBeInTheDocument()
  })

  it('shows undated task details without inventing a due date', () => {
    render(
      <MemoryRouter>
        <TaskCalendarMonth tasks={[makeTask('task-1', 'Unscheduled work', '')]} />
      </MemoryRouter>,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Unscheduled work, no due date' }))

    expect(screen.getByText('No due date')).toBeInTheDocument()
  })
})
