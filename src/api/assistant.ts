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
 *
 * Story 6.5 additions:
 *  - Plan steps name the tool they run; write steps may wait for the user to
 *    reply "confirm" (awaiting_confirmation event) before they execute.
 *
 * Story 6.6 additions:
 *  - Every write is permission-, conflict- and risk-checked. Medium risk waits
 *    for "confirm" (awaiting_confirmation); high risk waits for another approver
 *    (approval_required event) and is decided on the Approvals page.
 */

import { API_BASE_URL, apiFetch } from './client'

/**
 * Story 7.5: one source an answer relies on. `[id]` in the answer text points here.
 * `page` is a page, slide or sheet; `section` the heading it sits under; `quote`
 * the passage that supports the claim. Either `page` or `section` is usually set;
 * `label` always names where to look.
 */
export interface Citation {
  id: number
  document_id: string | null
  document_version: number | null
  source: string
  page: string | null
  section: string | null
  label: string
  quote: string
}

export interface AssistantChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: string
  isStreaming?: boolean
  error?: boolean
  /** Story 7.5: the sources cited in `content`, verified by the server. */
  citations?: Citation[]
}

// ---------------------------------------------------------------------------
// Story 6.4 — Plan & Retrieve types
// ---------------------------------------------------------------------------

export type StepStatus =
  | 'pending'
  | 'running'
  | 'done'
  | 'failed'
  | 'skipped'
  | 'awaiting_confirmation'
  | 'approval_required'

export interface PlanStep {
  index: number
  description: string
  action_type: string
  status: StepStatus
  result_summary: string | null
  retrieval_query: string | null
  retrieval_snippet: string | null
  estimated_tokens: number
  /** Story 6.5: the tool this step runs and whether it waits for confirmation. */
  tool_name?: string | null
  tool_arguments?: Record<string, unknown> | null
  requires_confirmation?: boolean
  /** Story 6.6: the approval request when the write guard paused the step. */
  approval_id?: string | null
  risk_level?: 'low' | 'medium' | 'high' | null
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

export interface AwaitingConfirmationEvent {
  index: number
  label: string
  tool_name: string | null
  arguments: Record<string, unknown> | null
  /** Story 6.6 fields. */
  approval_id?: string
  risk_level?: 'low' | 'medium' | 'high'
  risk_reasons?: string[]
  approver_kind?: 'requester' | 'approver'
  summary?: string
}

/** Story 6.6: a high-risk write waiting for HR / Operations, an admin or a manager. */
export type ApprovalRequiredEvent = AwaitingConfirmationEvent

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
  /** Story 7.5: called with the verified citations of a document-based answer. */
  onCitations?: (citations: Citation[]) => void
  /** Called when a step completes successfully. */
  onStepDone?: (event: StepDoneEvent) => void
  /** Called when a step fails (earlier checkpoints are preserved). */
  onStepError?: (event: StepErrorEvent) => void
  /** Called when execution is halted due to step-limit or cost-cap. */
  onHalted?: (reason: string) => void
  /** Story 6.5: a write step is waiting for the user to reply "confirm". */
  onAwaitingConfirmation?: (event: AwaitingConfirmationEvent) => void
  /** Story 6.6: a high-risk write is waiting for another person's approval. */
  onApprovalRequired?: (event: ApprovalRequiredEvent) => void
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
    prompt: 'I want to schedule a meeting',
    description: 'Book a new calendar slot with team members',
  },
  {
    id: 'create-task',
    label: 'Create a task',
    prompt: 'I want to create a task',
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
    onCitations,
    onStepDone,
    onStepError,
    onHalted,
    onAwaitingConfirmation,
    onApprovalRequired,
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

                // ---- Story 7.5: Citations ----
                if (Array.isArray(parsed.citations)) {
                  onCitations?.(parsed.citations as Citation[])
                }

                // ---- Story 6.4: Step done ----
                if (parsed.step_done && typeof parsed.step_done === 'object') {
                  onStepDone?.(parsed.step_done as StepDoneEvent)
                }

                // ---- Story 6.5: Write step waiting for confirmation ----
                if (
                  parsed.awaiting_confirmation &&
                  typeof parsed.awaiting_confirmation === 'object'
                ) {
                  onAwaitingConfirmation?.(parsed.awaiting_confirmation as AwaitingConfirmationEvent)
                }

                // ---- Story 6.6: High-risk write waiting for an approver ----
                if (parsed.approval_required && typeof parsed.approval_required === 'object') {
                  onApprovalRequired?.(parsed.approval_required as ApprovalRequiredEvent)
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
