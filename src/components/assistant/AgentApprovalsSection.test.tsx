// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  assignAgentAlternateApprover,
  cancelAgentApproval,
  decideAgentApproval,
  decideAgentApprovalFromEmail,
  getAgentApprovalFromEmail,
  getAgentApprovals,
  type AgentActionApproval,
} from '../../api/assistantApprovals'
import { AgentApprovalsSection } from './AgentApprovalsSection'

vi.mock('../../api/assistantApprovals', () => ({
  assignAgentAlternateApprover: vi.fn(),
  getAgentApprovals: vi.fn(),
  getAgentApprovalFromEmail: vi.fn(),
  decideAgentApproval: vi.fn(),
  decideAgentApprovalFromEmail: vi.fn(),
  cancelAgentApproval: vi.fn(),
}))

const highRisk: AgentActionApproval = {
  id: 'approval-1',
  tool_name: 'email.send',
  status: 'pending',
  summary: 'Send e-mail "Contract" to partner@outside.example',
  risk_level: 'high',
  risk_reasons: ['email.send is a medium-risk action', 'external recipient(s): partner@outside.example'],
  approver_kind: 'approver',
  requester_id: 'emp-bob',
  requester_name: 'Bob Lee',
  requester_role: 'employee',
  requester_team: 'Engineering',
  arguments: { recipient_emails: ['partner@outside.example'], subject: 'Contract', body: 'Please review.' },
  affected_people: ['partner@outside.example'],
  source: { type: 'AI Assistant', conversation_id: 'conversation-1' },
  decided_by: null,
  decided_by_name: null,
  decided_at: null,
  decision_note: null,
  execution_id: null,
  result: null,
  error: null,
  conversation_id: null,
  expires_at: '2026-10-27T10:00:00Z',
  created_at: '2026-10-24T10:00:00Z',
  can_decide: true,
  can_cancel: false,
  alternate_approver_id: null,
  alternate_approver_name: null,
  alternate_approver_email: null,
  reminder_sent_at: null,
}

afterEach(() => {
  cleanup()
  window.history.replaceState({}, '', '/')
  vi.clearAllMocks()
})

describe('AgentApprovalsSection', () => {
  it('shows the risk, the reasons and approves with a note', async () => {
    vi.mocked(getAgentApprovals).mockResolvedValue([highRisk])
    vi.mocked(decideAgentApproval).mockResolvedValue({
      message: 'Approved and done.',
      approval: { ...highRisk, status: 'executed', can_decide: false, decided_by_name: 'Priya Manager' },
      execution: {},
    })
    render(<AgentApprovalsSection />)

    expect(await screen.findByText(highRisk.summary)).toBeInTheDocument()
    expect(screen.getByText('high risk')).toBeInTheDocument()
    expect(screen.getByText('external recipient(s): partner@outside.example')).toBeInTheDocument()
    expect(getAgentApprovals).toHaveBeenCalledWith('queue', 'pending')

    await userEvent.type(screen.getByLabelText(`Decision note for ${highRisk.summary}`), 'OK')
    await userEvent.click(screen.getByRole('button', { name: 'Approve' }))

    expect(decideAgentApproval).toHaveBeenCalledWith('approval-1', 'approve', 'OK', undefined)
    expect(await screen.findByText('Approved and done.')).toBeInTheDocument()
    expect(screen.getByText('Approved and done', { selector: '.status-pill' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
  })

  it('shows resolved directory people for a frozen Everyone recipient list', async () => {
    vi.mocked(getAgentApprovals).mockResolvedValue([{
      ...highRisk,
      arguments: {
        recipient_emails: ['alice@northstar.example', 'partner@outside.example'],
        groups: [],
        subject: 'Contract',
        body: 'Please review.',
      },
      affected_people: [
        'Alice Smith <alice@northstar.example>',
        'partner@outside.example',
      ],
      affected_recipients: [
        { name: 'Alice Smith', email: 'alice@northstar.example', source: 'Everyone' },
        { name: 'partner@outside.example', email: 'partner@outside.example', source: 'Added manually' },
      ],
    }])
    render(<AgentApprovalsSection />)

    expect(await screen.findByText(
      'Affected people: Alice Smith <alice@northstar.example>, partner@outside.example',
    )).toBeInTheDocument()
  })

  it('explains that a legacy group approval has no frozen recipient list', async () => {
    vi.mocked(getAgentApprovals).mockResolvedValue([{
      ...highRisk,
      arguments: { groups: ['Everyone'], recipient_emails: [] },
      affected_people: [],
      affected_recipients: [],
      recipient_snapshot_missing: true,
    }])
    render(<AgentApprovalsSection />)

    expect(await screen.findByText('Affected people: None specified')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent(
      'no frozen recipient list. Reject it or edit the parameters',
    )
  })

  it('lets an approver edit action parameters before approving', async () => {
    vi.mocked(getAgentApprovals).mockResolvedValue([highRisk])
    vi.mocked(decideAgentApproval).mockResolvedValue({
      message: 'Approved and done.',
      approval: { ...highRisk, status: 'executed', can_decide: false },
      execution: {},
    })
    render(<AgentApprovalsSection />)

    const user = userEvent.setup()
    const editor = await screen.findByLabelText(`Action parameters for ${highRisk.summary}`)
    fireEvent.change(editor, {
      target: { value: '{"recipient_emails":["partner@outside.example"],"subject":"Updated","body":"Please review."}' },
    })
    await user.click(screen.getByRole('button', { name: 'Approve' }))

    expect(decideAgentApproval).toHaveBeenCalledWith(
      'approval-1',
      'approve',
      '',
      { recipient_emails: ['partner@outside.example'], subject: 'Updated', body: 'Please review.' },
    )
  })

  it('lets an authorized approver assign an alternate by email', async () => {
    vi.mocked(getAgentApprovals).mockResolvedValue([highRisk])
    vi.mocked(assignAgentAlternateApprover).mockResolvedValue({
      ...highRisk,
      alternate_approver_id: 'emp-alt',
      alternate_approver_name: 'Farah Finance',
      alternate_approver_email: 'farah@northstar.example',
    })
    render(<AgentApprovalsSection canAssignAlternateApprover />)

    await userEvent.type(
      await screen.findByLabelText(`Alternate approver email for ${highRisk.summary}`),
      'farah@northstar.example',
    )
    await userEvent.click(screen.getByRole('button', { name: 'Assign alternate' }))

    expect(assignAgentAlternateApprover).toHaveBeenCalledWith(
      highRisk.id,
      'farah@northstar.example',
    )
    expect(await screen.findByText(/Alternate approver: Farah Finance/)).toBeInTheDocument()
  })

  it('uses the single-use token from an email link to decide the matching request', async () => {
    window.history.replaceState({}, '', '/approvals#approvalToken=signed-token')
    vi.mocked(getAgentApprovals).mockResolvedValue([highRisk])
    vi.mocked(getAgentApprovalFromEmail).mockResolvedValue(highRisk)
    vi.mocked(decideAgentApprovalFromEmail).mockResolvedValue({
      message: 'Rejected. Nothing was run.',
      approval: { ...highRisk, status: 'rejected', can_decide: false },
      execution: null,
    })
    render(<AgentApprovalsSection />)

    await userEvent.click(await screen.findByRole('button', { name: 'Reject' }))
    expect(decideAgentApprovalFromEmail).toHaveBeenCalledWith('signed-token', 'reject', '', undefined)
  })

  it('explains a failed run after approval', async () => {
    vi.mocked(getAgentApprovals).mockResolvedValue([highRisk])
    vi.mocked(decideAgentApproval).mockResolvedValue({
      message: 'Approved, but it could not run: Permission denied: Managers can only assign tasks...',
      approval: { ...highRisk, status: 'failed', can_decide: false },
      execution: null,
    })
    render(<AgentApprovalsSection />)
    await userEvent.click(await screen.findByRole('button', { name: 'Approve' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('could not run')
  })

  it('lets the requester confirm a medium-risk request or cancel one waiting for others', async () => {
    const medium: AgentActionApproval = {
      ...highRisk, id: 'approval-2', risk_level: 'medium', approver_kind: 'requester',
      summary: 'Create task "Audit" for bob@northstar.example', can_cancel: true,
    }
    const waiting: AgentActionApproval = { ...highRisk, id: 'approval-3', can_decide: false, can_cancel: true }
    vi.mocked(getAgentApprovals).mockResolvedValue([medium, waiting])
    vi.mocked(cancelAgentApproval).mockResolvedValue({ ...waiting, status: 'cancelled', can_cancel: false })
    render(<AgentApprovalsSection />)

    await userEvent.selectOptions(await screen.findByLabelText('Assistant approvals view'), 'mine')
    await waitFor(() => expect(getAgentApprovals).toHaveBeenLastCalledWith('mine', 'pending'))

    expect(await screen.findByRole('button', { name: 'Confirm and run' })).toBeInTheDocument()
    expect(screen.getByText('Waiting for HR / Operations, an admin, or your manager.')).toBeInTheDocument()

    const cancelButtons = screen.getAllByRole('button', { name: 'Cancel request' })
    await userEvent.click(cancelButtons[cancelButtons.length - 1])
    expect(cancelAgentApproval).toHaveBeenCalledWith('approval-3')
    expect(await screen.findByText('Cancelled. Nothing was run.')).toBeInTheDocument()
  })
})
