// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DeadLetterDetail, DeadLetterEntry, DeadLetterQueue } from '../api/workflows'
import { WorkflowQueuePage } from './WorkflowQueuePage'

const { authState, api } = vi.hoisted(() => ({
  authState: { user: { id: 'admin-1', name: 'Ada Admin', role: 'admin' } as { id: string; name: string; role: string } },
  api: {
    getDeadLetterQueue: vi.fn(),
    getDeadLetterDetail: vi.fn(),
    actOnDeadLetter: vi.fn(),
  },
}))

vi.mock('../contexts/AuthContext', () => ({ useAuth: () => authState }))
vi.mock('../api/workflows', () => api)
vi.mock('react-router-dom', () => ({ Link: ({ children }: { children: unknown }) => children }))

const entry: DeadLetterEntry = {
  id: 'dl-1',
  run_id: 'run-1',
  workflow_type: 'meeting_follow_up',
  workflow_label: 'Meeting follow-up workflow',
  meeting_id: 'meeting-1',
  meeting_title: 'Quarterly review',
  failed_step_key: 'summary',
  error_message: 'Groq request timed out',
  attempt_count: 5,
  max_attempts: 5,
  status: 'open',
  run_status: 'failed',
  notified_count: 1,
  created_at: '2026-11-02T10:00:00Z',
  resolved_at: null,
  resolved_by: null,
  resolution_note: null,
}

const queue = (entries: DeadLetterEntry[]): DeadLetterQueue => ({
  entries,
  counts: { open: entries.filter((e) => e.status === 'open').length, retried: 0, resumed: 0, cancelled: 0 },
})

function detail(overrides: Partial<DeadLetterDetail['workflow']> = {}, entryOverrides: Partial<DeadLetterEntry> = {}): DeadLetterDetail {
  return {
    workflow: {
      id: 'run-1',
      workflow_type: 'meeting_follow_up',
      meeting_id: 'meeting-1',
      priority: 100,
      status: 'failed',
      attempt_count: 5,
      max_attempts: 5,
      last_error: 'Groq request timed out',
      created_at: '2026-11-02T09:00:00Z',
      updated_at: '2026-11-02T10:00:00Z',
      completed_at: '2026-11-02T10:00:00Z',
      steps: [
        { id: 's1', step_key: 'transcription', status: 'completed', attempt_count: 1, checkpoint: { transcript_id: 't1' }, last_error: null, updated_at: '', completed_at: null },
        { id: 's2', step_key: 'summary', status: 'failed', attempt_count: 5, checkpoint: null, last_error: 'Groq request timed out', updated_at: '', completed_at: null },
      ],
      ...overrides,
    },
    payload: { poll: false },
    idempotency_key: 'meeting-follow-up:meeting-1',
    meeting_title: 'Quarterly review',
    dead_letters: [{ ...entry, ...entryOverrides }],
    events: [
      { id: 'e1', step_key: 'transcription', attempt_number: 1, event_type: 'step_completed', message: null, error: null, retry_delay_seconds: null, next_attempt_at: null, actor_id: null, created_at: '2026-11-02T09:01:00Z' },
      { id: 'e2', step_key: 'summary', attempt_number: 1, event_type: 'retry_scheduled', message: null, error: 'Groq request timed out', retry_delay_seconds: 4.4, next_attempt_at: null, actor_id: null, created_at: '2026-11-02T09:02:00Z' },
      { id: 'e3', step_key: 'summary', attempt_number: 5, event_type: 'dead_lettered', message: 'Retries exhausted after 5 attempt(s).', error: 'Groq request timed out', retry_delay_seconds: null, next_attempt_at: null, actor_id: null, created_at: '2026-11-02T10:00:00Z' },
    ],
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  authState.user = { id: 'admin-1', name: 'Ada Admin', role: 'admin' }
  api.getDeadLetterQueue.mockResolvedValue(queue([entry]))
  api.getDeadLetterDetail.mockResolvedValue(detail())
  vi.spyOn(window, 'confirm').mockReturnValue(true)
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

async function openDetails() {
  const user = userEvent.setup()
  render(<WorkflowQueuePage />)
  await user.click(await screen.findByRole('button', { name: /view details for meeting follow-up workflow/i }))
  await screen.findByRole('region', { name: 'Workflow run details' })
  return user
}

describe('Workflow dead-letter queue page', () => {
  it('is only for administrators', () => {
    authState.user = { id: 'e1', name: 'Eve', role: 'employee' }
    render(<WorkflowQueuePage />)

    expect(screen.getByRole('heading', { name: 'Access denied' })).toBeInTheDocument()
    expect(api.getDeadLetterQueue).not.toHaveBeenCalled()
  })

  it('lists failed runs as failed, with the error and attempts', async () => {
    render(<WorkflowQueuePage />)

    const table = await screen.findByRole('table', { name: 'Dead-lettered workflow runs' })
    const row = within(table).getAllByRole('row')[1]
    expect(within(row).getByText('Meeting follow-up workflow')).toBeInTheDocument()
    expect(within(row).getByText('Quarterly review')).toBeInTheDocument()
    expect(within(row).getByText('Groq request timed out')).toBeInTheDocument()
    expect(within(row).getByText('5 of 5')).toBeInTheDocument()
    expect(within(row).getByText('open')).toHaveClass('danger')
    expect(within(table).queryByText(/completed|success/i)).not.toBeInTheDocument()
  })

  it('reads a timestamp without a time zone as UTC, not local time', async () => {
    api.getDeadLetterQueue.mockResolvedValue(
      queue([{ ...entry, created_at: '2026-11-02T10:00:00' }]),
    )
    render(<WorkflowQueuePage />)
    await screen.findByRole('table')

    // The same instant, written with an explicit zone, is how it must be shown.
    const shown = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
      .format(new Date('2026-11-02T10:00:00Z'))
    expect(screen.getByText(shown)).toBeInTheDocument()
  })

  it('says so when nothing has failed', async () => {
    api.getDeadLetterQueue.mockResolvedValue(queue([]))
    render(<WorkflowQueuePage />)

    expect(await screen.findByText('No failed workflows')).toBeInTheDocument()
  })

  it('filters the queue by status', async () => {
    const user = userEvent.setup()
    render(<WorkflowQueuePage />)
    await screen.findByRole('table')

    await user.selectOptions(screen.getByLabelText('Dead-letter status filter'), 'cancelled')

    await waitFor(() => expect(api.getDeadLetterQueue).toHaveBeenLastCalledWith('cancelled'))
  })

  it('shows the steps, execution history and job details of a run', async () => {
    await openDetails()

    const panel = screen.getByRole('region', { name: 'Workflow run details' })
    expect(within(panel).getByText('5 of 5 attempts used', { exact: false })).toBeInTheDocument()
    const history = within(panel).getByRole('list', { name: 'Execution history' })
    expect(within(history).getByText('Step completed')).toBeInTheDocument()
    expect(within(history).getByText('Retry scheduled')).toBeInTheDocument()
    expect(within(history).getByText(/next try in 4s/)).toBeInTheDocument()
    expect(within(history).getByText('Moved to the dead-letter queue')).toBeInTheDocument()
    expect(within(panel).getByLabelText('Job payload')).toHaveTextContent('"poll": false')
  })

  it('retries from the start with the note, then refreshes the queue', async () => {
    const user = await openDetails()
    api.actOnDeadLetter.mockResolvedValue(
      detail({ status: 'pending', attempt_count: 0 }, { status: 'retried', resolved_by: 'admin-1' }),
    )

    await user.type(screen.getByLabelText('Note (optional)'), 'provider is back')
    await user.click(screen.getByRole('button', { name: 'Retry from start' }))

    await waitFor(() => expect(api.actOnDeadLetter).toHaveBeenCalledWith('run-1', 'retry', 'provider is back'))
    expect(window.confirm).toHaveBeenCalled()
    expect(await screen.findByText('The workflow was queued to run again from the start.')).toBeInTheDocument()
    expect(screen.getByText(/Resolved as/)).toHaveTextContent('retried')
    expect(screen.queryByRole('button', { name: 'Retry from start' })).not.toBeInTheDocument()
    expect(api.getDeadLetterQueue.mock.calls.length).toBeGreaterThan(1)
  })

  it('resumes and cancels through the same buttons', async () => {
    const user = await openDetails()
    api.actOnDeadLetter.mockResolvedValueOnce(detail({ status: 'pending' }, { status: 'resumed' }))

    await user.click(screen.getByRole('button', { name: 'Resume from checkpoint' }))
    await waitFor(() => expect(api.actOnDeadLetter).toHaveBeenCalledWith('run-1', 'resume', ''))
    expect(await screen.findByText(/queued to resume from its last checkpoint/)).toBeInTheDocument()

    cleanup()
    const second = await openDetails()
    api.actOnDeadLetter.mockResolvedValueOnce(detail({ status: 'cancelled' }, { status: 'cancelled' }))
    await second.click(screen.getByRole('button', { name: 'Cancel run' }))
    await waitFor(() => expect(api.actOnDeadLetter).toHaveBeenCalledWith('run-1', 'cancel', ''))
    expect(await screen.findByText('The workflow run was cancelled.')).toBeInTheDocument()
  })

  it('does nothing when the administrator declines the confirmation', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const user = await openDetails()

    await user.click(screen.getByRole('button', { name: 'Cancel run' }))

    expect(api.actOnDeadLetter).not.toHaveBeenCalled()
  })

  it('shows the error when a run was already handled and refreshes it', async () => {
    const user = await openDetails()
    api.actOnDeadLetter.mockRejectedValue(new Error('this one has already been handled'))
    api.getDeadLetterDetail.mockResolvedValue(detail({ status: 'pending' }, { status: 'retried' }))

    await user.click(screen.getByRole('button', { name: 'Retry from start' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('already been handled')
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Retry from start' })).not.toBeInTheDocument(),
    )
  })

  it('offers no actions for a run that is no longer failed', async () => {
    api.getDeadLetterDetail.mockResolvedValue(detail({ status: 'cancelled' }, { status: 'cancelled' }))
    await openDetails()

    expect(screen.queryByRole('button', { name: 'Retry from start' })).not.toBeInTheDocument()
    expect(screen.getByText(/no longer waiting for an administrator/)).toBeInTheDocument()
  })
})
