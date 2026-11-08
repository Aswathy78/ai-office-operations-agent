// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type {
  AssistantRunDetail,
  AssistantRunPage,
} from '../api/adminAssistantRuns'
import type {
  BackgroundWorkflowDetail,
  BackgroundWorkflowPage,
} from '../api/adminWorkflowRuns'
import { AdminAssistantRunsPage } from './AdminAssistantRunsPage'

const { authState, api } = vi.hoisted(() => ({
  authState: { user: { id: 'admin-1', name: 'Ada Admin', role: 'admin' } as { id: string; name: string; role: string } },
  api: {
    getAssistantRuns: vi.fn(),
    getAssistantRunDetail: vi.fn(),
    getBackgroundWorkflows: vi.fn(),
    getBackgroundWorkflowDetail: vi.fn(),
  },
}))

vi.mock('../contexts/AuthContext', () => ({ useAuth: () => authState }))
vi.mock('../api/adminAssistantRuns', () => api)
vi.mock('../api/adminWorkflowRuns', () => api)

const run = {
  id: 'run-1',
  user_id: 'user-1',
  user_name: 'Ava North',
  conversation_id: 'conversation-1',
  status: 'completed' as const,
  started_at: '2026-11-08T10:00:00Z',
  ended_at: '2026-11-08T10:00:02Z',
  duration_ms: 2000,
  retry_count: 2,
  estimated_tokens: 80,
  provider_usage: { prompt_tokens: 60, completion_tokens: 25, total_tokens: 85 },
}

const list = (overrides: Partial<AssistantRunPage> = {}): AssistantRunPage => ({
  runs: [run],
  total: 30,
  limit: 25,
  offset: 0,
  has_more: true,
  ...overrides,
})

const detail: AssistantRunDetail = {
  ...run,
  ...{
    raw_prompt: 'private raw prompt',
    checkpoint: 'private checkpoint data',
    tool_data: 'private raw tool arguments and results',
    approval_payload: 'private approval payload',
  },
  steps: [
    {
      id: 'step-1',
      step_order: 1,
      step_type: 'tool',
      step_name: 'Search documents',
      status: 'completed',
      input: { query: '[REDACTED]' },
      output: { matches: '[REDACTED]' },
      started_at: '2026-11-08T10:00:00Z',
      ended_at: '2026-11-08T10:00:01Z',
      duration_ms: 1000,
      attempt_count: 2,
      retry_count: 1,
      estimated_tokens: 40,
      provider_usage: { prompt_tokens: 30, completion_tokens: 10, total_tokens: 40 },
      tool_execution_id: 'must-not-be-rendered',
      approval_id: 'approval-1',
      ...{
        checkpoint: 'private step checkpoint',
        tool_arguments: 'private tool arguments',
        tool_result: 'private tool result',
      },
    },
    {
      id: 'step-2',
      step_order: 2,
      step_type: 'response',
      step_name: 'Assistant response',
      status: 'completed',
      input: null,
      output: { result: 'Safe redacted result' },
      started_at: '2026-11-08T10:00:01Z',
      ended_at: '2026-11-08T10:00:02Z',
      duration_ms: 1000,
      attempt_count: 1,
      retry_count: 0,
      estimated_tokens: 40,
      provider_usage: { prompt_tokens: 30, completion_tokens: 15, total_tokens: 45 },
      tool_execution_id: null,
      approval_id: null,
    },
  ],
  pending_approvals: [{
    id: 'approval-1',
    tool_name: 'email.send',
    status: 'pending',
    risk_level: 'high',
    approver_kind: 'manager',
    created_at: '2026-11-08T10:00:00Z',
    expires_at: '2026-11-08T11:00:00Z',
    ...{ payload: 'private approval payload detail' },
  }],
}

const workflow = {
  id: 'workflow-1',
  run_type: 'background_workflow' as const,
  workflow_type: 'meeting_follow_up',
  meeting_id: 'meeting-1',
  user_id: 'user-1',
  user_name: 'Ava North',
  status: 'pending' as const,
  queued_at: '2026-11-08T10:00:00Z',
  started_at: null,
  ended_at: null,
  duration_ms: null,
  attempt_count: 0,
  retry_count: 0,
  max_attempts: 5,
  estimated_tokens: null,
  provider_usage: null,
  cost: null,
}

const workflowList = (overrides: Partial<BackgroundWorkflowPage> = {}): BackgroundWorkflowPage => ({
  runs: [workflow],
  total: 1,
  limit: 25,
  offset: 0,
  has_more: false,
  ...overrides,
})

const workflowDetail: BackgroundWorkflowDetail = {
  ...workflow,
  status: 'failed',
  attempt_count: 3,
  retry_count: 2,
  steps: [{
    id: 'workflow-step-1',
    step_order: 1,
    step_name: 'summary',
    status: 'failed',
    started_at: '2026-11-08T10:00:00Z',
    ended_at: null,
    duration_ms: null,
    attempt_count: 3,
    retry_count: 2,
    ...{
      checkpoint: 'private checkpoint',
      last_error: 'private error',
      raw_tool_result: 'private raw tool data',
    },
  }],
  pending_approvals: [],
  ...{
    payload: 'private workflow payload',
    raw_prompt: 'private prompt',
  },
}

beforeEach(() => {
  vi.clearAllMocks()
  authState.user = { id: 'admin-1', name: 'Ada Admin', role: 'admin' }
  api.getAssistantRuns.mockResolvedValue(list())
  api.getAssistantRunDetail.mockResolvedValue(detail)
  api.getBackgroundWorkflows.mockResolvedValue(workflowList())
  api.getBackgroundWorkflowDetail.mockResolvedValue(workflowDetail)
})

afterEach(cleanup)

function renderPage() {
  return render(<MemoryRouter><AdminAssistantRunsPage /></MemoryRouter>)
}

describe('AdminAssistantRunsPage', () => {
  it.each(['employee', 'manager', 'hr_operations'])('denies a %s without fetching runs', (role) => {
    authState.user = { id: 'user', name: 'User', role }
    renderPage()
    expect(screen.getByRole('heading', { name: 'Access denied' })).toBeInTheDocument()
    expect(api.getAssistantRuns).not.toHaveBeenCalled()
  })

  it('shows a loading state then run status and clearly separated token usage', async () => {
    let resolveList: (value: AssistantRunPage) => void = () => undefined
    api.getAssistantRuns.mockReturnValueOnce(new Promise<AssistantRunPage>((resolve) => { resolveList = resolve }))
    renderPage()
    expect(screen.getByRole('status')).toHaveTextContent('Loading assistant runs')
    resolveList(list())

    const table = await screen.findByRole('table', { name: 'Assistant runs' })
    const row = within(table).getAllByRole('row')[1]
    expect(within(row).getByText('Ava North')).toBeInTheDocument()
    expect(within(row).getByText('completed')).toBeInTheDocument()
    expect(within(row).getByText('2')).toBeInTheDocument()
    expect(within(row).getByText('80')).toBeInTheDocument()
    expect(within(row).getByText('estimated')).toBeInTheDocument()
    expect(within(row).getByText('85 provider-reported')).toBeInTheDocument()
    expect(within(row).getByText('Unavailable')).toBeInTheDocument()
  })

  it('filters by status, searches by user, and returns to the first page', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByRole('table', { name: 'Assistant runs' })

    await user.click(screen.getByRole('button', { name: 'Next page' }))
    await waitFor(() => expect(api.getAssistantRuns).toHaveBeenLastCalledWith({
      status: '',
      user: '',
      limit: 25,
      offset: 25,
    }))
    await user.selectOptions(screen.getByLabelText('Run status filter'), 'failed')
    await waitFor(() => expect(api.getAssistantRuns).toHaveBeenLastCalledWith({
      status: 'failed',
      user: '',
      limit: 25,
      offset: 0,
    }))

    await user.type(screen.getByLabelText('Search runs by user'), 'ava@example.test')
    await user.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(api.getAssistantRuns).toHaveBeenLastCalledWith({
      status: 'failed',
      user: 'ava@example.test',
      limit: 25,
      offset: 0,
    }))
  })

  it('paginates using server has_more and page totals', async () => {
    const user = userEvent.setup()
    api.getAssistantRuns
      .mockResolvedValueOnce(list())
      .mockResolvedValueOnce(list({ runs: [{ ...run, id: 'run-2' }], offset: 25, has_more: false }))
    renderPage()
    await screen.findByRole('table', { name: 'Assistant runs' })
    expect(screen.getByText('Page 1 of 2 · 30 runs')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Next page' }))
    expect(await screen.findByRole('button', { name: 'View run run-2' })).toBeInTheDocument()
    expect(screen.getByText('Page 2 of 2 · 30 runs')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled()
    expect(api.getAssistantRuns).toHaveBeenLastCalledWith({ status: '', user: '', limit: 25, offset: 25 })
  })

  it('shows a useful empty state for unmatched filters', async () => {
    api.getAssistantRuns.mockResolvedValue(list({ runs: [], total: 0, has_more: false }))
    renderPage()
    expect(await screen.findByText('No interactive runs have been recorded yet.')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Run status filter'), { target: { value: 'failed' } })
    expect(await screen.findByText('Nothing matches this search or filter.')).toBeInTheDocument()
  })

  it('shows an error and retry action when loading fails', async () => {
    api.getAssistantRuns.mockRejectedValueOnce(new Error('Administrator access required.'))
    renderPage()
    expect(await screen.findByRole('alert')).toHaveTextContent('Administrator access required.')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Try again' }))
    expect(await screen.findByRole('table', { name: 'Assistant runs' })).toBeInTheDocument()
  })

  it('renders only the API redacted trace and pending approval metadata', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByRole('table', { name: 'Assistant runs' })
    await user.click(screen.getByRole('button', { name: 'View run run-1' }))

    const panel = await screen.findByRole('region', { name: 'Assistant run details' })
    expect(within(panel).getByText(/1\. Search documents/)).toBeInTheDocument()
    expect(within(panel).getByText('2. Assistant response')).toBeInTheDocument()
    expect(within(panel).getAllByText(/Estimated tokens: 40/)).toHaveLength(2)
    expect(within(panel).getByText('email.send')).toBeInTheDocument()
    expect(within(panel).getByText(/Risk: high · Approver: manager/)).toBeInTheDocument()

    await user.click(within(panel).getByText('Input (redacted)'))
    await user.click(within(panel).getAllByText('Output (redacted)')[0])
    const traceBlocks = panel.querySelectorAll('pre')
    expect(traceBlocks[0]).toHaveTextContent('"query": "[REDACTED]"')
    expect(traceBlocks[1]).toHaveTextContent('"matches": "[REDACTED]"')
    expect(within(panel).queryByText(/must-not-be-rendered|private raw prompt|private checkpoint|private tool arguments|private tool result|private approval payload/i)).not.toBeInTheDocument()
    expect(within(panel).queryByText('approval-1')).not.toBeInTheDocument()
    expect(within(panel).getByText(/Cost: unavailable/)).toBeInTheDocument()
    expect(api.getAssistantRunDetail).toHaveBeenCalledWith('run-1')
  })

  it('switches to background WorkflowRun records and filters their statuses and users', async () => {
    const user = userEvent.setup()
    renderPage()
    await screen.findByRole('table', { name: 'Assistant runs' })
    await user.click(screen.getByRole('tab', { name: 'Background workflows' }))

    const table = await screen.findByRole('table', { name: 'Background workflow runs' })
    expect(within(table).getByText('meeting follow up')).toBeInTheDocument()
    expect(within(table).getByText('queued')).toBeInTheDocument()
    expect(within(table).getByText('Ava North')).toBeInTheDocument()
    expect(within(table).getAllByText('Unavailable')).toHaveLength(2)

    await user.selectOptions(screen.getByLabelText('Workflow status filter'), 'failed')
    await waitFor(() => expect(api.getBackgroundWorkflows).toHaveBeenLastCalledWith({
      status: 'failed',
      user: '',
      limit: 25,
      offset: 0,
    }))
    await user.type(screen.getByLabelText('Search workflows by user'), 'ava@example.test')
    await user.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(api.getBackgroundWorkflows).toHaveBeenLastCalledWith({
      status: 'failed',
      user: 'ava@example.test',
      limit: 25,
      offset: 0,
    }))
  })

  it('labels queued, running, completed, and failed background workflow states', async () => {
    const user = userEvent.setup()
    const states = ['pending', 'processing', 'completed', 'failed'] as const
    api.getBackgroundWorkflows.mockResolvedValueOnce(workflowList({
      runs: states.map((status, index) => ({ ...workflow, id: `workflow-${index}`, status })),
      total: states.length,
    }))
    renderPage()
    await screen.findByRole('table', { name: 'Assistant runs' })
    await user.click(screen.getByRole('tab', { name: 'Background workflows' }))

    const table = await screen.findByRole('table', { name: 'Background workflow runs' })
    for (const label of ['queued', 'running', 'completed', 'failed']) {
      expect(within(table).getByText(label)).toBeInTheDocument()
    }
  })

  it('shows completed run attempts and duration derived from its recorded step', async () => {
    const user = userEvent.setup()
    api.getBackgroundWorkflows.mockResolvedValueOnce(workflowList({
      runs: [{
        ...workflow,
        status: 'completed',
        started_at: '2026-11-08T10:00:00Z',
        ended_at: '2026-11-08T10:00:00.014Z',
        duration_ms: 14,
        attempt_count: 1,
        retry_count: 0,
        max_attempts: 3,
      }],
    }))
    renderPage()
    await screen.findByRole('table', { name: 'Assistant runs' })
    await user.click(screen.getByRole('tab', { name: 'Background workflows' }))

    const table = await screen.findByRole('table', { name: 'Background workflow runs' })
    const row = within(table).getAllByRole('row')[1]
    expect(within(row).getByText('1 of 3')).toBeInTheDocument()
    expect(within(row).getByText('14 ms')).toBeInTheDocument()
  })

  it('opens and loads the selected workflow detail without checkpoints or payloads', async () => {
    const user = userEvent.setup()
    let resolveDetail: (value: BackgroundWorkflowDetail) => void = () => undefined
    api.getBackgroundWorkflowDetail.mockReturnValueOnce(
      new Promise<BackgroundWorkflowDetail>((resolve) => { resolveDetail = resolve }),
    )
    renderPage()
    await screen.findByRole('table', { name: 'Assistant runs' })
    await user.click(screen.getByRole('tab', { name: 'Background workflows' }))
    await screen.findByRole('table', { name: 'Background workflow runs' })
    await user.click(screen.getByRole('button', { name: 'View workflow workflow-1' }))

    const panel = await screen.findByRole('region', { name: 'Background workflow details' })
    expect(within(panel).getByRole('status')).toHaveTextContent('Loading workflow details')
    expect(api.getBackgroundWorkflowDetail).toHaveBeenCalledWith('workflow-1')
    resolveDetail(workflowDetail)

    expect(await within(panel).findByText('1. summary')).toBeInTheDocument()
    expect(within(panel).getByText(/Retries: 2 · Attempts: 3 of 5/)).toBeInTheDocument()
    expect(within(panel).getByText('No approval links are recorded for background workflow runs.')).toBeInTheDocument()
    expect(within(panel).queryByText(/private checkpoint|private error|private raw tool data|private workflow payload|private prompt/i)).not.toBeInTheDocument()
  })
})
