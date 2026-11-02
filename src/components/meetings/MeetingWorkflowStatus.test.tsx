// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { MeetingWorkflowResponse, WorkflowRun } from '../../api/workflows'
import { MeetingWorkflowStatus } from './MeetingWorkflowStatus'

const { getWorkflow, streamWorkflow } = vi.hoisted(() => ({
  getWorkflow: vi.fn(),
  streamWorkflow: vi.fn(),
}))
vi.mock('../../api/workflows', () => ({
  getMeetingWorkflow: getWorkflow,
  streamMeetingWorkflow: streamWorkflow,
}))

function workflow(status: WorkflowRun['status']): MeetingWorkflowResponse {
  return {
    workflow: {
      id: 'workflow-1',
      workflow_type: 'meeting_follow_up',
      meeting_id: 'meeting-1',
      priority: 100,
      status,
      attempt_count: 0,
      last_error: null,
      created_at: '2026-11-02T00:00:00Z',
      updated_at: '2026-11-02T00:00:00Z',
      completed_at: null,
      steps: [
        {
          id: 'step-1',
          step_key: 'transcription',
          status: status === 'processing' ? 'processing' : 'pending',
          attempt_count: 1,
          checkpoint: null,
          last_error: null,
          updated_at: '2026-11-02T00:00:00Z',
          completed_at: null,
        },
      ],
    },
  }
}

describe('MeetingWorkflowStatus', () => {
  let publish: ((state: MeetingWorkflowResponse) => void) | undefined

  beforeEach(() => {
    publish = undefined
    getWorkflow.mockResolvedValue(workflow('pending'))
    streamWorkflow.mockImplementation(
      (_meetingId: string, onUpdate: (state: MeetingWorkflowResponse) => void, signal: AbortSignal) =>
        new Promise<void>((resolve) => {
          publish = onUpdate
          signal.addEventListener('abort', () => resolve(), { once: true })
        }),
    )
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('shows live workflow transitions received through the authenticated event stream', async () => {
    render(<MeetingWorkflowStatus meetingId="meeting-1" />)
    expect(await screen.findAllByText('Pending')).toHaveLength(2)

    await act(async () => {
      publish?.(workflow('processing'))
    })

    await waitFor(() => {
      expect(screen.getAllByText('Processing')).toHaveLength(2)
      expect(screen.getByText('Transcription')).toBeInTheDocument()
    })
  })

  it('shows terminal workflow failures and their step error', async () => {
    const failed = workflow('failed')
    failed.workflow!.last_error = 'Summary provider unavailable.'
    failed.workflow!.steps[0].status = 'failed'
    failed.workflow!.steps[0].last_error = 'Summary read-back failed.'
    getWorkflow.mockResolvedValue(failed)

    render(<MeetingWorkflowStatus meetingId="meeting-1" />)

    expect(await screen.findAllByText('Failed')).toHaveLength(2)
    expect(screen.getByText('Summary provider unavailable.')).toBeInTheDocument()
    expect(screen.getByText('Summary read-back failed.')).toBeInTheDocument()
  })
})
