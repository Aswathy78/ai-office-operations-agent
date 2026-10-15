// @vitest-environment jsdom
import { act, cleanup, render, screen, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getEmailDeliveryIssues, getMyEmailDrafts, type EmailDraft } from '../../api/emailDrafts'
import { demoUsers } from '../../mocks/mockUser'
import { ChatPage } from '../../pages/ChatPage'
import { EmailDeliveryPanel } from './EmailDeliveryPanel'
import { sampleDraft } from './emailTestFixtures'

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: demoUsers.employee }),
}))

vi.mock('../../api/emailDrafts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/emailDrafts')>()
  return {
    ...actual,
    createEmailDraft: vi.fn(),
    getDirectoryGroups: vi.fn().mockResolvedValue([]),
    getEmailDeliveryIssues: vi.fn(),
    getEmailDraftEvents: vi.fn().mockResolvedValue([]),
    getMyEmailDrafts: vi.fn(),
  }
})

const manager = { id: 'user-002', role: 'manager', team: 'Engineering' }

function withStatus(status: EmailDraft['status'], extra: Partial<EmailDraft> = {}): EmailDraft {
  return { ...sampleDraft, status, ...extra }
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

function sidebarStatuses(): string[] {
  const sidebar = screen.getByRole('complementary', { name: 'Your e-mail drafts' })
  return Array.from(sidebar.querySelectorAll('.email-status[data-status]')).map(
    (pill) => pill.getAttribute('data-status') ?? '',
  )
}

beforeEach(() => {
  vi.useFakeTimers()
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('E-mail status updates reach the UI within 5 seconds', () => {
  it('moves Queued -> Sending -> Sent in the chat page without a reload, then stops polling', async () => {
    vi.mocked(getMyEmailDrafts)
      .mockResolvedValueOnce([withStatus('queued')])
      .mockResolvedValueOnce([withStatus('sending')])
      .mockResolvedValue([withStatus('sent', { sent_at: '2026-10-16T10:00:00Z' })])

    render(<ChatPage />)
    await advance(0)
    expect(sidebarStatuses()).toEqual(['queued'])

    await advance(2000)
    expect(sidebarStatuses()).toEqual(['sending'])

    await advance(2000)
    expect(sidebarStatuses()).toEqual(['sent']) // 4s after queueing, inside the 5s budget

    const callsAfterSent = vi.mocked(getMyEmailDrafts).mock.calls.length
    await advance(10000)
    expect(vi.mocked(getMyEmailDrafts).mock.calls.length).toBe(callsAfterSent) // nothing left in flight
  })

  it('lists Failed and Retry e-mails under "Needs attention" for the employee', async () => {
    vi.mocked(getMyEmailDrafts).mockResolvedValue([
      withStatus('failed', { id: 'f1', subject: 'Contract' }),
      withStatus('retry', { id: 'r1', subject: 'Budget', attempt_count: 1 }),
      withStatus('sent', { id: 's1', subject: 'Lunch' }),
    ])

    render(<ChatPage />)
    await advance(0)

    const attention = screen.getByRole('region', { name: 'E-mails that need attention' })
    expect(within(attention).getByText('Needs attention (2)')).toBeInTheDocument()
    expect(within(attention).getByText('Contract')).toBeInTheDocument()
    expect(within(attention).getByText('Retrying automatically (attempt 1 of 3)')).toBeInTheDocument()
    expect(within(attention).queryByText('Lunch')).not.toBeInTheDocument()
  })
})

describe('Manager dashboard e-mail delivery panel', () => {
  it('shows Failed and Retry e-mails and picks up changes within 5 seconds', async () => {
    vi.mocked(getEmailDeliveryIssues)
      .mockResolvedValueOnce([
        withStatus('retry', { id: 'r1', subject: 'Budget', attempt_count: 1, last_error: 'timed out' }),
      ])
      .mockResolvedValue([
        withStatus('failed', { id: 'r1', subject: 'Budget', attempt_count: 3, last_error: 'timed out' }),
        withStatus('failed', { id: 'f2', subject: 'Contract', last_error: '550 No such user' }),
      ])

    render(<EmailDeliveryPanel actor={manager} />)
    await advance(0)
    expect(screen.getByText('0 failed · 1 retrying. Updates automatically.')).toBeInTheDocument()
    expect(screen.getByText('timed out')).toBeInTheDocument()

    await advance(2000)
    expect(screen.getByText('2 failed · 0 retrying. Updates automatically.')).toBeInTheDocument()
    expect(screen.getByText('550 No such user')).toBeInTheDocument()
    expect(getEmailDeliveryIssues).toHaveBeenCalledWith(manager)
  })

  it('says when nothing is failing', async () => {
    vi.mocked(getEmailDeliveryIssues).mockResolvedValue([])
    render(<EmailDeliveryPanel actor={manager} />)
    await advance(0)
    expect(screen.getByText('No e-mails are failing or waiting to retry.')).toBeInTheDocument()
  })
})
