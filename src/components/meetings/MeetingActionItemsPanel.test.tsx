// @vitest-environment jsdom
import { cleanup, render, screen, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { MeetingActionItemsPanel } from './MeetingActionItemsPanel'
import type { Employee, MeetingActionItem, MeetingActionItems } from '../../types/meeting'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const employees: Employee[] = [
  { id: 'emp-1', name: 'Ava Patel', email: 'ava.patel@northstar.example' },
  { id: 'emp-2', name: 'Marcus Vance', email: 'marcus.vance@northstar.example' },
]

const sampleItems: MeetingActionItem[] = [
  {
    id: 'item-1',
    task_description: 'Finalize the project budget.',
    owner_name: 'Ava',
    proposed_employee_id: 'emp-1',
    confirmed_employee_id: null,
    employee_match_status: 'unique_match',
    ownership_status: 'proposed',
    review_status: 'approval_required',
    reviewed_by: null,
    reviewed_at: null,
    is_task_eligible: false,
    requires_approval: true,
    deadline_at: null,
    deadline_text: 'by Friday',
    priority: 'high',
    confidence: 0.72,
    meeting_id: 'meeting-1',
    transcript_id: 'transcript-1',
    source_segment_index: 0,
    source_start_seconds: 10,
    source_end_seconds: 15,
    source_quote_start_seconds: 11,
    source_quote_end_seconds: 14,
    source_quote: 'Ava should finalize the project budget by Friday.',
  },
  {
    id: 'item-2',
    task_description: 'Deploy the production release.',
    owner_name: 'Marcus',
    proposed_employee_id: 'emp-2',
    confirmed_employee_id: 'emp-2',
    employee_match_status: 'unique_match',
    ownership_status: 'confirmed',
    review_status: 'confirmed',
    reviewed_by: 'organizer-1',
    reviewed_at: '2026-10-12T10:00:00Z',
    is_task_eligible: true,
    requires_approval: false,
    deadline_at: '2026-10-16T18:00:00Z',
    deadline_text: 'by next week',
    priority: 'urgent',
    confidence: 0.95,
    meeting_id: 'meeting-1',
    transcript_id: 'transcript-1',
    source_segment_index: 1,
    source_start_seconds: 20,
    source_end_seconds: 25,
    source_quote_start_seconds: 21,
    source_quote_end_seconds: 24,
    source_quote: 'Marcus will deploy the production release.',
  },
  {
    id: 'item-3',
    task_description: 'Order more office supplies.',
    owner_name: null,
    proposed_employee_id: null,
    confirmed_employee_id: null,
    employee_match_status: 'missing_owner',
    ownership_status: 'unresolved',
    review_status: 'rejected',
    reviewed_by: 'organizer-1',
    reviewed_at: '2026-10-12T10:05:00Z',
    is_task_eligible: false,
    requires_approval: false,
    deadline_at: null,
    deadline_text: null,
    priority: 'low',
    confidence: 0.88,
    meeting_id: 'meeting-1',
    transcript_id: 'transcript-1',
    source_segment_index: 2,
    source_start_seconds: 30,
    source_end_seconds: 35,
    source_quote_start_seconds: null,
    source_quote_end_seconds: null,
    source_quote: 'We need more sticky notes.',
  },
]

const extraction: MeetingActionItems = {
  id: 'extraction-1',
  meeting_id: 'meeting-1',
  transcript_id: 'transcript-1',
  status: 'completed',
  created_at: '2026-10-12T09:00:00Z',
  updated_at: '2026-10-12T09:01:00Z',
  processing_started_at: null,
  completed_at: '2026-10-12T09:01:00Z',
  error_message: null,
  action_items: sampleItems,
}

describe('MeetingActionItemsPanel — Story 3.3 Action Item Review (FR-14)', () => {
  it('shows automatic no-match separately from organizer-confirmed ownership', () => {
    render(
      <MeetingActionItemsPanel
        extraction={{
          ...extraction,
          action_items: [
            {
              ...sampleItems[0],
              owner_name: 'Rahul',
              proposed_employee_id: null,
              confirmed_employee_id: 'emp-1',
              employee_match_status: 'no_match',
              ownership_status: 'confirmed',
              review_status: 'confirmed',
            },
          ],
        }}
        employees={employees}
        employeesLoading={false}
        isOrganizer={true}
        loading={false}
        triggering={false}
        confirmingActionItemId={null}
        ownerConfirmationFeedback={null}
        onExtract={vi.fn()}
        onConfirmOwner={vi.fn()}
        onOpenSource={vi.fn()}
      />,
    )

    const automaticMatchStatus = screen.getByText('Automatic match status').parentElement
    const ownershipStatus = screen.getByText('Ownership status').parentElement
    expect(automaticMatchStatus).toHaveTextContent('Automatic match statusNo match')
    expect(ownershipStatus).toHaveTextContent('Ownership statusConfirmed')
    expect(screen.getByText('Rahul')).toBeInTheDocument()
  })

  it('renders review status badges and approval required warning', () => {
    render(
      <MeetingActionItemsPanel
        extraction={extraction}
        employees={employees}
        employeesLoading={false}
        isOrganizer={true}
        loading={false}
        triggering={false}
        confirmingActionItemId={null}
        ownerConfirmationFeedback={null}
        onExtract={vi.fn()}
        onConfirmOwner={vi.fn()}
        onOpenSource={vi.fn()}
      />,
    )

    // Check badges
    expect(screen.getAllByText('Approval Required').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('Confirmed').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('Rejected').length).toBeGreaterThanOrEqual(1)

    // Check warning message for low confidence item
    expect(
      screen.getByText(/Confidence score is below the 0.85 threshold/),
    ).toBeInTheDocument()

    // Check rejected message
    expect(
      screen.getByText(/rejected and can never be converted into a task/),
    ).toBeInTheDocument()

    // Check durable decision metadata
    expect(screen.getAllByText('organizer-1').length).toBeGreaterThanOrEqual(1)
  })

  it('filters items by review status tabs', async () => {
    const user = userEvent.setup()

    render(
      <MeetingActionItemsPanel
        extraction={extraction}
        employees={employees}
        employeesLoading={false}
        isOrganizer={true}
        loading={false}
        triggering={false}
        confirmingActionItemId={null}
        ownerConfirmationFeedback={null}
        onExtract={vi.fn()}
        onConfirmOwner={vi.fn()}
        onOpenSource={vi.fn()}
      />,
    )

    // Filter to Confirmed
    await user.click(screen.getByRole('button', { name: /Confirmed \(1\)/ }))
    expect(screen.getByText('Deploy the production release.')).toBeInTheDocument()
    expect(screen.queryByText('Finalize the project budget.')).not.toBeInTheDocument()
    expect(screen.queryByText('Order more office supplies.')).not.toBeInTheDocument()

    // Filter to Rejected
    await user.click(screen.getByRole('button', { name: /Rejected \(1\)/ }))
    expect(screen.getByText('Order more office supplies.')).toBeInTheDocument()
    expect(screen.queryByText('Deploy the production release.')).not.toBeInTheDocument()

    // Filter to Needs Review
    await user.click(screen.getByRole('button', { name: /Needs Review \(1\)/ }))
    expect(screen.getByText('Finalize the project budget.')).toBeInTheDocument()
    expect(screen.queryByText('Order more office supplies.')).not.toBeInTheDocument()
  })

  it('triggers onReviewActionItem when organizer confirms an item', async () => {
    const user = userEvent.setup()
    const onReviewActionItem = vi.fn().mockResolvedValue({
      ...sampleItems[0],
      review_status: 'confirmed',
    })

    render(
      <MeetingActionItemsPanel
        extraction={extraction}
        employees={employees}
        employeesLoading={false}
        isOrganizer={true}
        loading={false}
        triggering={false}
        confirmingActionItemId={null}
        ownerConfirmationFeedback={null}
        onExtract={vi.fn()}
        onConfirmOwner={vi.fn()}
        onOpenSource={vi.fn()}
        onReviewActionItem={onReviewActionItem}
      />,
    )

    const confirmButtons = screen.getAllByRole('button', { name: /^Confirm$/ })
    await user.click(confirmButtons[0])

    expect(onReviewActionItem).toHaveBeenCalledWith(
      sampleItems[0],
      expect.objectContaining({ action: 'confirm' }),
    )
  })

  it('triggers onReviewActionItem when organizer rejects an item', async () => {
    const user = userEvent.setup()
    const onReviewActionItem = vi.fn().mockResolvedValue({
      ...sampleItems[0],
      review_status: 'rejected',
    })

    render(
      <MeetingActionItemsPanel
        extraction={extraction}
        employees={employees}
        employeesLoading={false}
        isOrganizer={true}
        loading={false}
        triggering={false}
        confirmingActionItemId={null}
        ownerConfirmationFeedback={null}
        onExtract={vi.fn()}
        onConfirmOwner={vi.fn()}
        onOpenSource={vi.fn()}
        onReviewActionItem={onReviewActionItem}
      />,
    )

    const rejectButtons = screen.getAllByRole('button', { name: /^Reject$/ })
    await user.click(rejectButtons[0])

    expect(onReviewActionItem).toHaveBeenCalledWith(
      sampleItems[0],
      expect.objectContaining({ action: 'reject' }),
    )
  })

  it('allows organizer to edit fields and save changes', async () => {
    const user = userEvent.setup()
    const onReviewActionItem = vi.fn().mockResolvedValue({
      ...sampleItems[0],
      task_description: 'Finalize the Q4 project budget immediately.',
    })

    render(
      <MeetingActionItemsPanel
        extraction={extraction}
        employees={employees}
        employeesLoading={false}
        isOrganizer={true}
        loading={false}
        triggering={false}
        confirmingActionItemId={null}
        ownerConfirmationFeedback={null}
        onExtract={vi.fn()}
        onConfirmOwner={vi.fn()}
        onOpenSource={vi.fn()}
        onReviewActionItem={onReviewActionItem}
      />,
    )

    const editButtons = screen.getAllByRole('button', { name: /^Edit$/ })
    await user.click(editButtons[0])

    const textarea = screen.getByLabelText('Task Description')
    await user.clear(textarea)
    await user.type(textarea, 'Finalize the Q4 project budget immediately.')

    await user.click(screen.getByRole('button', { name: 'Save Changes' }))

    expect(onReviewActionItem).toHaveBeenCalledWith(
      sampleItems[0],
      expect.objectContaining({
        action: 'edit',
        task_description: 'Finalize the Q4 project budget immediately.',
      }),
    )
  })

  it('saves a resolved due date and confirmed employee for task creation', async () => {
    const user = userEvent.setup()
    const onReviewActionItem = vi.fn().mockResolvedValue(sampleItems[0])

    render(
      <MeetingActionItemsPanel
        extraction={extraction}
        employees={employees}
        employeesLoading={false}
        isOrganizer={true}
        loading={false}
        triggering={false}
        confirmingActionItemId={null}
        ownerConfirmationFeedback={null}
        onExtract={vi.fn()}
        onConfirmOwner={vi.fn()}
        onOpenSource={vi.fn()}
        onReviewActionItem={onReviewActionItem}
      />,
    )

    await user.click(screen.getAllByRole('button', { name: /^Edit$/ })[0])
    await user.selectOptions(screen.getByLabelText('Linked Employee'), 'emp-1')
    await user.type(screen.getByLabelText('Due date and time'), '2026-10-16T18:00')
    await user.click(screen.getByRole('button', { name: 'Save Changes' }))

    expect(onReviewActionItem).toHaveBeenCalledWith(
      sampleItems[0],
      expect.objectContaining({
        action: 'edit',
        confirmed_employee_id: 'emp-1',
        deadline_at: expect.any(String),
      }),
    )
  })
})
