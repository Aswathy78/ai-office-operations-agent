// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  getAgentApprovalHistory,
  type AgentActionApproval,
} from '../../api/assistantApprovals'
import { AssistantApprovalHistory } from './AssistantApprovalHistory'

vi.mock('../../api/assistantApprovals', () => ({
  getAgentApprovalHistory: vi.fn(),
}))

const historyItem = {
  id: 'approval-history-1',
  tool_name: 'task.create',
  status: 'executed',
  summary: 'Create a task',
  risk_level: 'high',
  risk_reasons: [],
  approver_kind: 'approver',
  requester_id: 'emp-requester',
  requester_name: 'Priya Sharma',
  requester_role: 'employee',
  requester_team: 'Engineering',
  arguments: {},
  affected_people: [],
  source: { type: 'AI Assistant', conversation_id: null },
  decided_by: 'emp-approver',
  decided_by_name: 'Maya Thompson',
  decided_at: '2026-11-09T10:00:00Z',
  alternate_approver_id: null,
  alternate_approver_name: null,
  alternate_approver_email: null,
  reminder_sent_at: null,
  decision_note: null,
  execution_id: null,
  result: null,
  error: null,
  conversation_id: null,
  expires_at: '2026-11-11T10:00:00Z',
  created_at: '2026-11-09T09:00:00Z',
  can_decide: false,
  can_cancel: false,
} satisfies AgentActionApproval

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('AssistantApprovalHistory', () => {
  it('filters decision history by status, approver, and date', async () => {
    vi.mocked(getAgentApprovalHistory).mockResolvedValue([historyItem])
    const user = userEvent.setup()
    render(<AssistantApprovalHistory visible />)

    expect(await screen.findByText('Create a task')).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Approval history status'), 'executed')
    await user.type(screen.getByLabelText('Approval history approver'), 'Maya')
    await user.type(screen.getByLabelText('Approval history from date'), '2026-11-09')
    await user.type(screen.getByLabelText('Approval history to date'), '2026-11-09')
    await user.click(screen.getByRole('button', { name: 'Filter history' }))

    expect(getAgentApprovalHistory).toHaveBeenLastCalledWith({
      status: 'executed',
      approver: 'Maya',
      from_date: '2026-11-09',
      to_date: '2026-11-09',
    })
    expect(screen.getByText(/Maya Thompson/)).toBeInTheDocument()
  })

  it('does not request audit history for an unauthorized viewer', () => {
    render(<AssistantApprovalHistory visible={false} />)
    expect(getAgentApprovalHistory).not.toHaveBeenCalled()
  })
})
