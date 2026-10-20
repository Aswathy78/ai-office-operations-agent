/**
 * AI Assistant streaming API client (Epic 6.1 / FR-30).
 *
 * Connects to the backend streaming endpoint (/api/v1/assistant/chat/stream)
 * via Server-Sent Events / chunked HTTP streams. Handles streaming tokens,
 * network errors, and service availability gracefully without faking AI responses.
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

export interface StreamAssistantChatOptions {
  message: string
  conversationId?: string | null
  history?: AssistantChatMessage[]
  signal?: AbortSignal
  onChunk: (chunk: string) => void
  onConversationId?: (id: string) => void
  onComplete?: (fullContent: string) => void
  onError?: (error: Error) => void
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
 * If the backend streaming endpoint is not yet mounted (e.g., HTTP 404/501),
 * reports an honest service-unavailable error so that missing backend work
 * is transparently surfaced rather than simulated.
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
                if (parsed.error && typeof parsed.error === 'string') {
                  const sseError = new Error(parsed.error)
                  onError?.(sseError)
                  throw sseError
                }
                if (parsed.conversation_id && typeof parsed.conversation_id === 'string') {
                  onConversationId?.(parsed.conversation_id)
                }
              }
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
