// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  changeTaskDueDate,
  getTaskAudit,
  getTaskNotifications,
  reassignTask,
  type TaskRecord,
} from '../../api/tasks'
import { TaskManagePanel } from './TaskManagePanel'

vi.mock('../../api/tasks', () => ({
  changeTaskDueDate: vi.fn(),
  getTaskAudit: vi.fn(),
  getTaskNotifications: vi.fn(),
  reassignTask: vi.fn(),
}))

const task: TaskRecord = {
  id: 'task-1',
  title: 'Prepare the release notes',
  assignee_id: 'rahul',
  assignee_name: 'Rahul',
  assignee_team: 'Engineering',
  creator_id: 'user-002',
  source: 'meeting_action_item',
  source_action_item_id: 'item-1',
  meeting_id: 'meeting-1',
  meeting_title: 'Sprint planning',
  transcript_id: 'transcript-1',
  source_segment_index: 0,
  source_timestamp_seconds: 3,
  due_at: '2026-10-20T10:00:00Z',
  priority: 'high',
  status: 'pending',
  assignment_email_status: 'sent',
  assignment_email_error: null,
  created_at: '2026-10-13T00:00:00Z',
  pending_reassignment: null,
}

const employees = [
  { id: 'rahul', name: 'Rahul', email: 'rahul@example.test', team: 'Engineering' },
  { id: 'ava', name: 'Ava Patel', email: 'ava@example.test', team: 'Engineering' },
  { id: 'priya', name: 'Priya Sharma', email: 'priya@example.test', team: 'Finance' },
]

const manager = { id: 'user-002', role: 'manager', team: 'Engineering' }

beforeEach(() => {
  vi.mocked(getTaskAudit).mockResolvedValue([])
  vi.mocked(getTaskNotifications).mockResolvedValue([])
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('TaskManagePanel', () => {
  it('reassigns within the team and reports that both people were notified', async () => {
    const onTaskChanged = vi.fn()
    vi.mocked(reassignTask).mockResolvedValue({
      outcome: 'applied',
      message: 'Task reassigned from Rahul to Ava Patel. Both have been notified.',
      task: { ...task, assignee_id: 'ava', assignee_name: 'Ava Patel' },
      reassignment_request: null,
      notifications: [],
    })
    const user = userEvent.setup()
    render(<TaskManagePanel task={task} actor={manager} employees={employees} onTaskChanged={onTaskChanged} />)

    await user.selectOptions(screen.getByLabelText('New assignee'), 'ava')
    await user.type(screen.getByLabelText('Reason (optional)', { selector: '#reassign-reason-task-1' }), 'Rahul on leave')
    await user.click(screen.getByRole('button', { name: 'Reassign' }))

    expect(reassignTask).toHaveBeenCalledWith('task-1', 'ava', 'Rahul on leave', manager)
    expect(await screen.findByRole('status')).toHaveTextContent('Both have been notified.')
    expect(onTaskChanged).toHaveBeenCalledWith(expect.objectContaining({ assignee_id: 'ava' }))
  })

  it('labels cross-team reassignment as needing approval', async () => {
    vi.mocked(reassignTask).mockResolvedValue({
      outcome: 'pending_approval',
      message: 'Priya Sharma is in Finance, outside your team, so this reassignment was sent to HR / Operations for approval.',
      task,
      reassignment_request: null,
      notifications: [],
    })
    const user = userEvent.setup()
    render(<TaskManagePanel task={task} actor={manager} employees={employees} onTaskChanged={vi.fn()} />)

    await user.selectOptions(screen.getByLabelText('New assignee'), 'priya')

    expect(screen.getByText(/outside your team\. This will be sent for approval/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Request approval' }))
    expect(await screen.findByText(/sent to HR \/ Operations for approval/)).toBeInTheDocument()
  })

  it('shows the explanation when a change is denied', async () => {
    vi.mocked(changeTaskDueDate).mockRejectedValue(
      new Error('This task belongs to Priya Sharma (Finance). Managers can only change due dates for their own team (Engineering).'),
    )
    const user = userEvent.setup()
    render(<TaskManagePanel task={task} actor={manager} employees={employees} onTaskChanged={vi.fn()} />)

    const dueInput = screen.getByLabelText('New due date')
    await user.clear(dueInput)
    await user.type(dueInput, '2026-10-25T09:30')
    await user.click(screen.getByRole('button', { name: 'Update due date' }))

    expect(changeTaskDueDate).toHaveBeenCalledWith(
      'task-1',
      new Date('2026-10-25T09:30').toISOString(),
      '',
      manager,
    )
    expect(await screen.findByRole('alert')).toHaveTextContent('Managers can only change due dates for their own team')
    await waitFor(() => expect(getTaskAudit).toHaveBeenCalledTimes(2))
  })

  it('hides the reassign form while a cross-team request is pending', async () => {
    render(
      <TaskManagePanel
        task={{
          ...task,
          pending_reassignment: {
            id: 'request-1',
            task_id: 'task-1',
            task_title: task.title,
            from_assignee_id: 'rahul',
            from_assignee_name: 'Rahul',
            to_assignee_id: 'priya',
            to_assignee_name: 'Priya Sharma',
            to_assignee_team: 'Finance',
            requested_by: 'user-002',
            requester_role: 'manager',
            requester_team: 'Engineering',
            reason: null,
            status: 'pending',
            decided_by: null,
            decided_at: null,
            decision_note: null,
            created_at: '2026-10-14T00:00:00Z',
          },
        }}
        actor={manager}
        employees={employees}
        onTaskChanged={vi.fn()}
      />,
    )

    expect(screen.getByText(/Awaiting approval to move this task to/)).toBeInTheDocument()
    expect(screen.queryByLabelText('New assignee')).not.toBeInTheDocument()
  })
})
