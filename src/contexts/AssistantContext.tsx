/**
 * AssistantContext — Shared conversation state and widget visibility.
 *
 * Stories covered:
 *  6.1 — Persistent conversation history, progressive streaming, session storage.
 *  6.4 — Execution plan state, step status, retrieval indicators, halt reasons.
 *
 * All Story 6.1–6.3 behaviour is preserved unchanged.
 * Story 6.4 adds optional plan/step state that is threaded through context
 * and cleared at the start of every new message.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import type { ReactNode } from 'react'
import {
  streamAssistantChat,
  type AssistantChatMessage,
  type PlanStep,
  type RetrievalEvent,
  type StepDoneEvent,
  type StepErrorEvent,
  type StepStartEvent,
} from '../api/assistant'
import { useAuth } from './AuthContext'

// ---------------------------------------------------------------------------
// Context value shape
// ---------------------------------------------------------------------------

interface AssistantContextValue {
  // Conversation
  messages: AssistantChatMessage[]
  isOpen: boolean
  isMinimized: boolean
  isStreaming: boolean
  openWidget: () => void
  closeWidget: () => void
  minimizeWidget: () => void
  toggleWidget: () => void
  sendMessage: (text: string) => Promise<void>
  clearHistory: () => void

  // Story 6.4: Plan & Retrieve state
  /** Full ordered plan for the current request, or null if no plan was produced. */
  currentPlan: PlanStep[] | null
  /** Index of the step currently running (-1 = not started). */
  activeStepIndex: number
  /** Most recent retrieval event, or null. */
  lastRetrieval: RetrievalEvent | null
  /** Reason execution was halted, or null. */
  haltedReason: string | null
}

const AssistantContext = createContext<AssistantContextValue | null>(null)

// ---------------------------------------------------------------------------
// Storage helpers (unchanged from 6.1)
// ---------------------------------------------------------------------------

const INITIAL_WELCOME_MESSAGE: AssistantChatMessage = {
  id: 'welcome-msg',
  role: 'assistant',
  content:
    'Hi! I am your AI Office Operations Assistant. I can help you schedule meetings, manage tasks, draft emails, and summarize discussions. Ask a question or click a quick action below to get started!',
  timestamp: new Date().toISOString(),
}

const STORAGE_KEY_PREFIX = 'ao_assistant_chat_'

function getSessionStorageKey(userId?: string | null): string {
  return `${STORAGE_KEY_PREFIX}${userId || 'anonymous'}`
}

function getConversationStorageKey(userId?: string | null): string {
  return `${getSessionStorageKey(userId)}_conversation`
}

function loadPersistedConversationId(userId?: string | null): string | null {
  try {
    if (typeof window === 'undefined' || !window.sessionStorage) return null
    return window.sessionStorage.getItem(getConversationStorageKey(userId))
  } catch {
    return null
  }
}

function loadPersistedMessages(userId?: string | null): AssistantChatMessage[] {
  try {
    if (typeof window === 'undefined' || !window.sessionStorage) {
      return [INITIAL_WELCOME_MESSAGE]
    }
    const raw = window.sessionStorage.getItem(getSessionStorageKey(userId))
    if (!raw) return [INITIAL_WELCOME_MESSAGE]
    const parsed = JSON.parse(raw) as AssistantChatMessage[]
    return Array.isArray(parsed) && parsed.length > 0 ? parsed : [INITIAL_WELCOME_MESSAGE]
  } catch {
    return [INITIAL_WELCOME_MESSAGE]
  }
}

function savePersistedMessages(userId: string | null | undefined, messages: AssistantChatMessage[]): void {
  try {
    if (typeof window === 'undefined' || !window.sessionStorage) return
    window.sessionStorage.setItem(getSessionStorageKey(userId), JSON.stringify(messages))
  } catch {
    // SessionStorage may be restricted in private mode or quota exceeded
  }
}

function savePersistedConversationId(
  userId: string | null | undefined,
  conversationId: string | null,
): void {
  try {
    if (typeof window === 'undefined' || !window.sessionStorage) return
    const key = getConversationStorageKey(userId)
    if (conversationId) {
      window.sessionStorage.setItem(key, conversationId)
    } else {
      window.sessionStorage.removeItem(key)
    }
  } catch {
    // SessionStorage may be restricted in private mode or quota exceeded
  }
}

// ---------------------------------------------------------------------------
// Provider
// ---------------------------------------------------------------------------

export function AssistantProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  const userId = user?.id

  const [isOpen, setIsOpen] = useState(false)
  const [isMinimized, setIsMinimized] = useState(false)
  const [isStreaming, setIsStreaming] = useState(false)
  const [conversationId, setConversationId] = useState<string | null>(() =>
    loadPersistedConversationId(userId),
  )
  const [messages, setMessages] = useState<AssistantChatMessage[]>(() => loadPersistedMessages(userId))

  // Story 6.4 plan state
  const [currentPlan, setCurrentPlan] = useState<PlanStep[] | null>(null)
  const [activeStepIndex, setActiveStepIndex] = useState<number>(-1)
  const [lastRetrieval, setLastRetrieval] = useState<RetrievalEvent | null>(null)
  const [haltedReason, setHaltedReason] = useState<string | null>(null)

  const activeAbortRef = useRef<AbortController | null>(null)
  const previousUserRef = useRef<string | undefined>(userId)

  // Reset or reload conversation when the authenticated user changes
  useEffect(() => {
    if (previousUserRef.current !== userId) {
      previousUserRef.current = userId
      setConversationId(loadPersistedConversationId(userId))
      setMessages(loadPersistedMessages(userId))
      // Clear plan state on user change
      setCurrentPlan(null)
      setActiveStepIndex(-1)
      setLastRetrieval(null)
      setHaltedReason(null)
    }
  }, [userId])

  // Persist conversation history to sessionStorage
  useEffect(() => {
    savePersistedMessages(userId, messages)
  }, [userId, messages])

  useEffect(() => {
    savePersistedConversationId(userId, conversationId)
  }, [userId, conversationId])

  const openWidget = useCallback(() => {
    setIsOpen(true)
    setIsMinimized(false)
  }, [])

  const closeWidget = useCallback(() => {
    setIsOpen(false)
    setIsMinimized(false)
  }, [])

  const minimizeWidget = useCallback(() => {
    setIsMinimized((prev) => !prev)
  }, [])

  const toggleWidget = useCallback(() => {
    setIsOpen((prev) => {
      if (!prev) setIsMinimized(false)
      return !prev
    })
  }, [])

  const clearHistory = useCallback(() => {
    const fresh = [INITIAL_WELCOME_MESSAGE]
    setConversationId(null)
    setMessages(fresh)
    setCurrentPlan(null)
    setActiveStepIndex(-1)
    setLastRetrieval(null)
    setHaltedReason(null)
    savePersistedConversationId(userId, null)
    savePersistedMessages(userId, fresh)
  }, [userId])

  const sendMessage = useCallback(
    async (text: string) => {
      const prompt = text.trim()
      if (!prompt || isStreaming) return

      // Clear plan state for the new request
      setCurrentPlan(null)
      setActiveStepIndex(-1)
      setLastRetrieval(null)
      setHaltedReason(null)

      const userMsgId = `user-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
      const assistantMsgId = `asst-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`

      const userMessage: AssistantChatMessage = {
        id: userMsgId,
        role: 'user',
        content: prompt,
        timestamp: new Date().toISOString(),
      }

      const initialAssistantMessage: AssistantChatMessage = {
        id: assistantMsgId,
        role: 'assistant',
        content: '',
        timestamp: new Date().toISOString(),
        isStreaming: true,
      }

      setMessages((prev) => [...prev, userMessage, initialAssistantMessage])
      setIsStreaming(true)

      const abortController = new AbortController()
      activeAbortRef.current = abortController

      let streamedText = ''

      try {
        await streamAssistantChat({
          message: prompt,
          conversationId,
          history: [...messages, userMessage],
          signal: abortController.signal,
          onConversationId: (id: string) => {
            setConversationId(id)
          },
          onChunk: (chunk: string) => {
            streamedText += chunk
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === assistantMsgId
                  ? { ...msg, content: streamedText, isStreaming: true }
                  : msg,
              ),
            )
          },
          onComplete: (finalContent: string) => {
            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === assistantMsgId
                  ? { ...msg, content: finalContent || streamedText, isStreaming: false }
                  : msg,
              ),
            )
          },
          onError: (error: Error) => {
            const errorNotice =
              streamedText.trim().length > 0
                ? `${streamedText}\n\n[Stream interrupted: ${error.message}]`
                : `Sorry, I encountered an issue: ${error.message}`

            setMessages((prev) =>
              prev.map((msg) =>
                msg.id === assistantMsgId
                  ? {
                      ...msg,
                      content: errorNotice,
                      isStreaming: false,
                      error: true,
                    }
                  : msg,
              ),
            )
          },

          // ---- Story 6.4 callbacks ----
          onPlan: (steps) => {
            setCurrentPlan(steps)
          },
          onStepStart: (event: StepStartEvent) => {
            setActiveStepIndex(event.index)
            // Update the plan step status to "running"
            setCurrentPlan((prev) =>
              prev
                ? prev.map((s) =>
                    s.index === event.index ? { ...s, status: 'running' as const } : s,
                  )
                : prev,
            )
          },
          onRetrieval: (event: RetrievalEvent) => {
            setLastRetrieval(event)
          },
          onStepDone: (event: StepDoneEvent) => {
            setCurrentPlan((prev) =>
              prev
                ? prev.map((s) =>
                    s.index === event.index
                      ? { ...s, status: 'done' as const, result_summary: event.summary }
                      : s,
                  )
                : prev,
            )
          },
          onStepError: (event: StepErrorEvent) => {
            setCurrentPlan((prev) =>
              prev
                ? prev.map((s) =>
                    s.index === event.index
                      ? { ...s, status: 'failed' as const, result_summary: event.message }
                      : s,
                  )
                : prev,
            )
          },
          onHalted: (reason: string) => {
            setHaltedReason(reason)
          },
        })
      } catch {
        // streamAssistantChat already invoked onError callback
      } finally {
        setIsStreaming(false)
        activeAbortRef.current = null
      }
    },
    [conversationId, isStreaming, messages],
  )

  const value = useMemo(
    () => ({
      messages,
      isOpen,
      isMinimized,
      isStreaming,
      openWidget,
      closeWidget,
      minimizeWidget,
      toggleWidget,
      sendMessage,
      clearHistory,
      currentPlan,
      activeStepIndex,
      lastRetrieval,
      haltedReason,
    }),
    [
      messages,
      isOpen,
      isMinimized,
      isStreaming,
      openWidget,
      closeWidget,
      minimizeWidget,
      toggleWidget,
      sendMessage,
      clearHistory,
      currentPlan,
      activeStepIndex,
      lastRetrieval,
      haltedReason,
    ],
  )

  return <AssistantContext.Provider value={value}>{children}</AssistantContext.Provider>
}

export function useAssistant() {
  const context = useContext(AssistantContext)
  if (!context) {
    throw new Error('useAssistant must be used within an AssistantProvider')
  }
  return context
}
