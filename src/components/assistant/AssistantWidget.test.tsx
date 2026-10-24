// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { AssistantWidget } from './AssistantWidget'
import { AssistantProvider } from '../../contexts/AssistantContext'
import * as assistantApi from '../../api/assistant'

const mockUser = {
  id: 'emp-101',
  name: 'Marcus Vance',
  email: 'marcus@example.com',
  role: 'employee',
}

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: mockUser,
    isAuthenticated: true,
    signIn: vi.fn(),
    signOut: vi.fn(),
  }),
}))

function renderWidget() {
  return render(
    <AssistantProvider>
      <AssistantWidget />
    </AssistantProvider>,
  )
}

describe('Epic 6.1 — AI Assistant Floating Chat Widget', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
    vi.restoreAllMocks()
  })

  afterEach(() => {
    cleanup()
    window.sessionStorage.clear()
  })

  it('renders the floating jumping robot launcher button with accessible label', () => {
    renderWidget()

    const launcher = screen.getByRole('button', { name: /open ai assistant/i })
    expect(launcher).toBeInTheDocument()
    expect(launcher).toHaveAttribute('aria-expanded', 'false')
    expect(launcher).toHaveClass('assistant-launcher-btn')
    expect(screen.getByText('AI Assistant')).toBeInTheDocument()
  })

  it('opens the chat panel when the robot launcher is clicked', async () => {
    const user = userEvent.setup()
    renderWidget()

    const launcher = screen.getByRole('button', { name: /open ai assistant/i })
    await user.click(launcher)

    const dialog = screen.getByRole('dialog', { name: /ai assistant command center/i })
    expect(dialog).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'AI Assistant', level: 3 })).toBeInTheDocument()
    expect(screen.getByText(/online/i)).toBeInTheDocument()
  })

  it('displays the initial welcome message and required quick action suggestions', async () => {
    const user = userEvent.setup()
    renderWidget()

    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))

    // Welcome message
    expect(
      screen.getByText(/hi! i am your ai office operations assistant/i),
    ).toBeInTheDocument()

    // Required quick actions from requirements
    expect(screen.getByRole('button', { name: /^schedule a meeting$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^create a task$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^summarize a meeting$/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^show my pending tasks$/i })).toBeInTheDocument()
  })

  it('minimizes and closes the panel when minimize and close controls are clicked', async () => {
    const user = userEvent.setup()
    renderWidget()

    // Open panel
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    // Click minimize
    const minimizeBtn = screen.getByRole('button', { name: /minimize ai assistant/i })
    await user.click(minimizeBtn)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /open ai assistant/i })).toBeInTheDocument()

    // Reopen and then click close
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    const closeBtn = screen.getByRole('button', { name: /close ai assistant/i })
    await user.click(closeBtn)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /open ai assistant/i })).toBeInTheDocument()
  })

  it('sends a user message, renders typing indicator, and appends streaming chunks progressively', async () => {
    const user = userEvent.setup()

    // Mock streaming implementation
    vi.spyOn(assistantApi, 'streamAssistantChat').mockImplementation(
      async (options: assistantApi.StreamAssistantChatOptions) => {
        // First token
        options.onChunk('I am analyzing ')
        await new Promise((r) => setTimeout(r, 10))
        // Second token
        options.onChunk('your request.')
        options.onComplete?.('I am analyzing your request.')
      },
    )

    renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))

    const input = screen.getByRole('textbox', { name: /message ai assistant/i })
    const sendBtn = screen.getByRole('button', { name: /send message/i })

    // Send button disabled when empty
    expect(sendBtn).toBeDisabled()

    await user.type(input, 'Check meeting schedule')
    expect(sendBtn).toBeEnabled()

    await user.click(sendBtn)

    // User message is rendered
    expect(screen.getByText('Check meeting schedule')).toBeInTheDocument()

    // Progressive chunks accumulate
    await waitFor(() => {
      expect(screen.getByText('I am analyzing your request.')).toBeInTheDocument()
    })
  })

  it('renders Markdown formatting and bullet lists in assistant messages', async () => {
    const user = userEvent.setup()
    const reply = [
      'The sky looks blue because of **Rayleigh scattering** and *selective light dispersion*.',
      '',
      '- Short wavelengths scatter more',
      '- Blue light reaches our eyes',
    ].join('\n')

    vi.spyOn(assistantApi, 'streamAssistantChat').mockImplementation(async (options) => {
      options.onChunk(reply)
      options.onComplete?.(reply)
    })

    renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))
    await user.type(
      screen.getByRole('textbox', { name: /message ai assistant/i }),
      'Why is the sky blue?',
    )
    await user.click(screen.getByRole('button', { name: /send message/i }))

    const bold = await screen.findByText('Rayleigh scattering')
    expect(bold.tagName).toBe('STRONG')
    expect(screen.getByText('selective light dispersion').tagName).toBe('EM')
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText('Short wavelengths scatter more')).toBeInTheDocument()
    expect(screen.getByText('Blue light reaches our eyes')).toBeInTheDocument()
    expect(screen.queryByText(/\*\*Rayleigh scattering\*\*/)).not.toBeInTheDocument()
  })

  it('renders Markdown tables and line breaks without enabling raw HTML', async () => {
    const user = userEvent.setup()
    const reply = [
      '| Item | Status |',
      '| --- | --- |',
      '| Task | Complete |',
      '',
      'First line<br>Second line',
    ].join('\n')

    vi.spyOn(assistantApi, 'streamAssistantChat').mockImplementation(async (options) => {
      options.onChunk(reply)
      options.onComplete?.(reply)
    })

    renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))
    await user.type(
      screen.getByRole('textbox', { name: /message ai assistant/i }),
      'Show results',
    )
    await user.click(screen.getByRole('button', { name: /send message/i }))

    const table = await screen.findByRole('table')
    expect(table.querySelectorAll('th')).toHaveLength(2)
    expect(table.querySelector('td')).toHaveTextContent('Task')
    expect(table.querySelectorAll('tr')).toHaveLength(2)

    const assistantMessage = screen.getAllByTestId('assistant-message-assistant').at(-1)
    const messageText = assistantMessage?.querySelector('.assistant-msg-text')
    expect(messageText).toHaveTextContent('First line')
    expect(messageText).toHaveTextContent('Second line')
    expect(messageText?.querySelector('br')).toBeInTheDocument()
    expect(screen.queryByText('<br>')).not.toBeInTheDocument()
  })

  it('reuses and persists the backend conversation ID for clarification replies', async () => {
    const user = userEvent.setup()
    const conversationIds: Array<string | null | undefined> = []

    vi.spyOn(assistantApi, 'streamAssistantChat').mockImplementation(async (options) => {
      conversationIds.push(options.conversationId)
      options.onConversationId?.('conversation-clarification')
      options.onChunk('Please clarify.')
      options.onComplete?.('Please clarify.')
    })

    const rendered = renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))

    const input = screen.getByRole('textbox', { name: /message ai assistant/i })
    await user.type(input, 'Schedule a meeting')
    await user.click(screen.getByRole('button', { name: /send message/i }))
    await waitFor(() => {
      expect(conversationIds).toHaveLength(1)
    })
    expect(window.sessionStorage.getItem('ao_assistant_chat_emp-101_conversation'))
      .toBe('conversation-clarification')

    rendered.unmount()
    renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))
    const replyInput = screen.getByRole('textbox', { name: /message ai assistant/i })
    await user.type(replyInput, 'Tomorrow at 3 PM')
    await user.click(screen.getByRole('button', { name: /send message/i }))

    await waitFor(() => {
      expect(conversationIds).toHaveLength(2)
    })
    expect(conversationIds).toEqual([null, 'conversation-clarification'])
  })

  it('clicking a quick action immediately submits the command without faking completed operations', async () => {
    const user = userEvent.setup()

    const streamSpy = vi
      .spyOn(assistantApi, 'streamAssistantChat')
      .mockImplementation(async (options) => {
        options.onChunk('Checking your pending task deliverables...')
        options.onComplete?.('Checking your pending task deliverables...')
      })

    renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))

    const pendingTasksChip = screen.getByRole('button', { name: /^show my pending tasks$/i })
    await user.click(pendingTasksChip)

    // Quick action prompt was dispatched
    expect(streamSpy).toHaveBeenCalledTimes(1)
    expect(streamSpy.mock.calls[0][0].message).toBe('Show my pending tasks and upcoming deadlines')

    expect(screen.getByText('Show my pending tasks and upcoming deadlines')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByText('Checking your pending task deliverables...')).toBeInTheDocument()
    })
  })

  it('preserves conversation history after closing and reopening the widget', async () => {
    const user = userEvent.setup()

    vi.spyOn(assistantApi, 'streamAssistantChat').mockImplementation(
      async (options) => {
        options.onChunk('Response before closing')
        options.onComplete?.('Response before closing')
      },
    )

    renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))

    const input = screen.getByRole('textbox', { name: /message ai assistant/i })
    await user.type(input, 'Persistent test query')
    await user.click(screen.getByRole('button', { name: /send message/i }))

    await waitFor(() => {
      expect(screen.getByText('Response before closing')).toBeInTheDocument()
    })

    // Close the widget
    await user.click(screen.getByRole('button', { name: /close ai assistant/i }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()

    // Reopen the widget
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()

    // Conversation history is still visible
    expect(screen.getByText('Persistent test query')).toBeInTheDocument()
    expect(screen.getByText('Response before closing')).toBeInTheDocument()
  })

  it('gracefully handles service errors when the backend streaming endpoint is unavailable', async () => {
    const user = userEvent.setup()

    vi.spyOn(assistantApi, 'streamAssistantChat').mockImplementation(
      async (options) => {
        const error = new Error('AI Assistant streaming service is not reachable (HTTP 404).')
        options.onError?.(error)
        throw error
      },
    )

    renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))

    const input = screen.getByRole('textbox', { name: /message ai assistant/i })
    await user.type(input, 'Test missing endpoint')
    await user.click(screen.getByRole('button', { name: /send message/i }))

    await waitFor(() => {
      expect(
        screen.getByText(/sorry, i encountered an issue: ai assistant streaming service is not reachable/i),
      ).toBeInTheDocument()
    })
  })

  it('allows clearing conversation history using the reset button', async () => {
    const user = userEvent.setup()

    vi.spyOn(assistantApi, 'streamAssistantChat').mockImplementation(async (options) => {
      options.onChunk('Some answer')
      options.onComplete?.('Some answer')
    })

    renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))

    const input = screen.getByRole('textbox', { name: /message ai assistant/i })
    await user.type(input, 'Message to clear')
    await user.click(screen.getByRole('button', { name: /send message/i }))

    await waitFor(() => {
      expect(screen.getByText('Some answer')).toBeInTheDocument()
    })

    const clearBtn = screen.getByRole('button', { name: /clear conversation history/i })
    await user.click(clearBtn)

    // User message and response are cleared, welcome message is restored
    expect(screen.queryByText('Message to clear')).not.toBeInTheDocument()
    expect(screen.queryByText('Some answer')).not.toBeInTheDocument()
    expect(screen.getByText(/hi! i am your ai office operations assistant/i)).toBeInTheDocument()
  })

  it('submits a message when pressing Enter in the input field', async () => {
    const user = userEvent.setup()

    const streamSpy = vi
      .spyOn(assistantApi, 'streamAssistantChat')
      .mockImplementation(async (options) => {
        options.onChunk('Submitted via Enter')
        options.onComplete?.('Submitted via Enter')
      })

    renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))

    const input = screen.getByRole('textbox', { name: /message ai assistant/i })
    await user.type(input, 'Enter key message{enter}')

    expect(streamSpy).toHaveBeenCalled()
    expect(screen.getByText('Enter key message')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByText('Submitted via Enter')).toBeInTheDocument()
    })
  })

  it('prevents repeated sends while streaming is active', async () => {
    const user = userEvent.setup()

    let completeStream: () => void = () => {}
    const streamSpy = vi
      .spyOn(assistantApi, 'streamAssistantChat')
      .mockImplementation((options) => {
        return new Promise<void>((resolve) => {
          options.onChunk('In-progress reply...')
          completeStream = () => {
            options.onComplete?.('In-progress reply...')
            resolve()
          }
        })
      })

    renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))

    const input = screen.getByRole('textbox', { name: /message ai assistant/i })
    const sendBtn = screen.getByRole('button', { name: /send message/i })

    await user.type(input, 'First prompt')
    await user.click(sendBtn)

    // Send button and input should now be disabled during stream
    expect(sendBtn).toBeDisabled()
    expect(input).toBeDisabled()

    // Rapid second click attempt
    await user.click(sendBtn)

    // Should only have initiated 1 request
    expect(streamSpy).toHaveBeenCalledTimes(1)

    // Complete the stream
    completeStream()
    await waitFor(() => {
      expect(sendBtn).toBeDisabled() // empty input after send
    })
  })

  it('restores conversation history from sessionStorage across remounts (simulating page reload)', async () => {
    const user = userEvent.setup()

    vi.spyOn(assistantApi, 'streamAssistantChat').mockImplementation(async (options) => {
      options.onChunk('Session reload reply')
      options.onComplete?.('Session reload reply')
    })

    const { unmount } = renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))

    const input = screen.getByRole('textbox', { name: /message ai assistant/i })
    await user.type(input, 'Persistent reload prompt')
    await user.click(screen.getByRole('button', { name: /send message/i }))

    await waitFor(() => {
      expect(screen.getByText('Session reload reply')).toBeInTheDocument()
    })

    // Unmount (simulates navigating away or page unmount)
    unmount()

    // Remount a fresh widget instance (simulates page refresh)
    renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))

    expect(screen.getByText('Persistent reload prompt')).toBeInTheDocument()
    expect(screen.getByText('Session reload reply')).toBeInTheDocument()
  })

  it('handles an interrupted stream and displays the partial content with an interruption notice', async () => {
    const user = userEvent.setup()

    vi.spyOn(assistantApi, 'streamAssistantChat').mockImplementation(async (options) => {
      options.onChunk('Partial text before disconnection')
      const interruptError = new Error('Connection reset by peer')
      options.onError?.(interruptError)
      throw interruptError
    })

    renderWidget()
    await user.click(screen.getByRole('button', { name: /open ai assistant/i }))

    const input = screen.getByRole('textbox', { name: /message ai assistant/i })
    await user.type(input, 'Will be interrupted')
    await user.click(screen.getByRole('button', { name: /send message/i }))

    await waitFor(() => {
      expect(screen.getByText('Partial text before disconnection')).toBeInTheDocument()
      expect(screen.getByText('[Stream interrupted: Connection reset by peer]')).toBeInTheDocument()
    })
  })
})
