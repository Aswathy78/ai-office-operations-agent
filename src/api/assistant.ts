/**
 * AI Assistant streaming API client (Epic 6.1 / FR-30, Story 6.4 Plan & Retrieve).
 *
 * Connects to the backend streaming endpoint (/api/v1/assistant/chat/stream)
 * via Server-Sent Events / chunked HTTP streams. Handles streaming tokens,
 * network errors, and service availability gracefully without faking AI responses.
 *
 * Story 6.4 additions:
 *  - Exposes new SSE event types: plan, step_start, retrieval, step_done,
 *    step_error, halted.
 *  - All new callbacks are optional so existing callers are not broken.
 */

import { API_BASE_URL, apiFetch } from './client'

export interface AssistantChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: string
  isStreaming?: boolean
  error?: boolean
}

// ---------------------------------------------------------------------------
// Story 6.4 — Plan & Retrieve types
// ---------------------------------------------------------------------------

export type StepStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

export interface PlanStep {
  index: number
  description: string
  action_type: string
  status: StepStatus
  result_summary: string | null
  retrieval_query: string | null
  retrieval_snippet: string | null
  estimated_tokens: number
}

export interface RetrievalEvent {
  query: string
  snippet: string
}

export interface StepStartEvent {
  index: number
  label: string
}

export interface StepDoneEvent {
  index: number
  summary: string
}

export interface StepErrorEvent {
  index: number
  message: string
}

// ---------------------------------------------------------------------------
// Streaming options
// ---------------------------------------------------------------------------

export interface StreamAssistantChatOptions {
  message: string
  conversationId?: string | null
  history?: AssistantChatMessage[]
  signal?: AbortSignal
  onChunk: (chunk: string) => void
  onConversationId?: (id: string) => void
  onComplete?: (fullContent: string) => void
  onError?: (error: Error) => void
  /** Called once with the full ordered plan before execution starts. */
  onPlan?: (steps: PlanStep[]) => void
  /** Called when a step begins executing. */
  onStepStart?: (event: StepStartEvent) => void
  /** Called when a step successfully retrieves company knowledge. */
  onRetrieval?: (event: RetrievalEvent) => void
  /** Called when a step completes successfully. */
  onStepDone?: (event: StepDoneEvent) => void
  /** Called when a step fails (earlier checkpoints are preserved). */
  onStepError?: (event: StepErrorEvent) => void
  /** Called when execution is halted due to step-limit or cost-cap. */
  onHalted?: (reason: string) => void
}

export interface QuickAction {
  id: string
  label: string
  prompt: string
  description?: string
}

export const ASSISTANT_QUICK_ACTIONS: QuickAction[] = [
  {
    id: 'schedule-meeting',
    label: 'Schedule a meeting',
    prompt: 'Schedule a meeting with the development team tomorrow at 10 AM',
    description: 'Book a new calendar slot with team members',
  },
  {
    id: 'create-task',
    label: 'Create a task',
    prompt: 'Create a task: Review sprint demo deliverables by Friday',
    description: 'Add a new action item to the task list',
  },
  {
    id: 'summarize-meeting',
    label: 'Summarize a meeting',
    prompt: 'Summarize the latest completed meeting and list its key decisions',
    description: 'Retrieve and review meeting notes & decisions',
  },
  {
    id: 'pending-tasks',
    label: 'Show my pending tasks',
    prompt: 'Show my pending tasks and upcoming deadlines',
    description: 'Check your assigned deliverables',
  },
]

/**
 * Initiates a streaming chat request to the assistant endpoint.
 *
 * Reads streaming SSE/text chunks progressively and invokes `onChunk` for each token.
 * Story 6.4: also dispatches plan, step, retrieval, and halt events via optional callbacks.
 */
export async function streamAssistantChat(options: StreamAssistantChatOptions): Promise<void> {
  const {
    message,
    conversationId,
    history = [],
    signal,
    onChunk,
    onConversationId,
    onComplete,
    onError,
    onPlan,
    onStepStart,
    onRetrieval,
    onStepDone,
    onStepError,
    onHalted,
  } = options

  const payload = {
    message,
    conversation_id: conversationId || undefined,
    history: history.map((msg) => ({
      role: msg.role,
      content: msg.content,
    })),
  }

  let response: Response
  try {
    response = await apiFetch(`${API_BASE_URL}/api/v1/assistant/chat/stream`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'text/event-stream, text/plain, */*',
      },
      body: JSON.stringify(payload),
      signal,
    })
  } catch (fetchError) {
    const error =
      fetchError instanceof Error
        ? fetchError
        : new Error('Failed to connect to the AI Assistant service.')
    onError?.(error)
    throw error
  }

  if (!response.ok) {
    let detail = ''
    try {
      const errJson = await response.json()
      detail = errJson.detail || errJson.message || ''
    } catch {
      // response might not be JSON
    }

    const errorMsg =
      response.status === 404
        ? 'AI Assistant streaming service is not reachable (HTTP 404). Backend streaming support at /api/v1/assistant/chat/stream is required for live AI responses.'
        : `AI Assistant service error (HTTP ${response.status}${detail ? `: ${detail}` : ''}).`

    const error = new Error(errorMsg)
    onError?.(error)
    throw error
  }

  if (!response.body) {
    const error = new Error('AI Assistant response contained no readable stream.')
    onError?.(error)
    throw error
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let accumulated = ''

  try {
    let buffer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      const chunkText = decoder.decode(value, { stream: true })
      buffer += chunkText

      // Check for SSE formatting (lines starting with "data: ")
      if (buffer.includes('data:')) {
        const lines = buffer.split('\n')
        buffer = lines.pop() ?? '' // keep incomplete last line in buffer

        for (const line of lines) {
          const trimmed = line.trim()
          if (!trimmed || trimmed.startsWith(':')) continue // empty line or comment
          if (trimmed.startsWith('data:')) {
            const dataStr = trimmed.slice(5).trim()
            if (dataStr === '[DONE]') continue
            try {
              const parsed = JSON.parse(dataStr)
              if (parsed && typeof parsed === 'object') {
                // ---- Error event ----
                if (parsed.error && typeof parsed.error === 'string') {
                  const sseError = new Error(parsed.error)
                  onError?.(sseError)
                  throw sseError
                }

                // ---- Conversation ID (init payload) ----
                if (parsed.conversation_id && typeof parsed.conversation_id === 'string') {
                  onConversationId?.(parsed.conversation_id)
                }

                // ---- Story 6.4: Plan ----
                if (Array.isArray(parsed.plan)) {
                  onPlan?.(parsed.plan as PlanStep[])
                }

                // ---- Story 6.4: Step start ----
                if (parsed.step_start && typeof parsed.step_start === 'object') {
                  onStepStart?.(parsed.step_start as StepStartEvent)
                }

                // ---- Story 6.4: Retrieval ----
                if (parsed.retrieval && typeof parsed.retrieval === 'object') {
                  onRetrieval?.(parsed.retrieval as RetrievalEvent)
                }

                // ---- Story 6.4: Step done ----
                if (parsed.step_done && typeof parsed.step_done === 'object') {
                  onStepDone?.(parsed.step_done as StepDoneEvent)
                }

                // ---- Story 6.4: Step error ----
                if (parsed.step_error && typeof parsed.step_error === 'object') {
                  onStepError?.(parsed.step_error as StepErrorEvent)
                }

                // ---- Story 6.4: Halted ----
                if (parsed.halted && typeof parsed.halted === 'string') {
                  onHalted?.(parsed.halted)
                }
              }

              // ---- Token chunks (text) ----
              const token =
                typeof parsed === 'string'
                  ? parsed
                  : parsed.token || parsed.chunk || parsed.content || parsed.text || ''
              if (token) {
                accumulated += token
                onChunk(token)
              }
            } catch {
              // Not JSON, emit raw data string
              accumulated += dataStr
              onChunk(dataStr)
            }
          }
        }
      } else {
        // Raw plain-text stream
        accumulated += chunkText
        onChunk(chunkText)
        buffer = ''
      }
    }

    // Flush any remaining buffer text
    if (buffer.trim()) {
      if (buffer.startsWith('data:')) {
        const dataStr = buffer.slice(5).trim()
        if (dataStr && dataStr !== '[DONE]') {
          try {
            const parsed = JSON.parse(dataStr)
            const token =
              typeof parsed === 'string'
                ? parsed
                : parsed.token || parsed.chunk || parsed.content || parsed.text || ''
            if (token) {
              accumulated += token
              onChunk(token)
            }
          } catch {
            accumulated += dataStr
            onChunk(dataStr)
          }
        }
      } else {
        accumulated += buffer
        onChunk(buffer)
      }
    }

    onComplete?.(accumulated)
  } catch (streamError) {
    if (signal?.aborted) {
      return
    }
    const error =
      streamError instanceof Error
        ? streamError
        : new Error('Error while reading streaming response.')
    onError?.(error)
    throw error
  }
}
