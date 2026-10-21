/**
 * AssistantContext — Shared conversation state and widget visibility (Epic 6.1).
 *
 * Provides:
 * - Persistent conversation history across widget open/minimize/close and page navigation.
 * - Progressive streaming message accumulation.
 * - Scoped client-side session storage (cleared on logout for security).
 * - Transparent error handling without faking AI responses.
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
} from '../api/assistant'
import { useAuth } from './AuthContext'

interface AssistantContextValue {
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
}

const AssistantContext = createContext<AssistantContextValue | null>(null)

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

export function AssistantProvider({ children }: { children: ReactNode }) {
  const { user } = useAuth()
  const userId = user?.id

  const [isOpen, setIsOpen] = useState(false)
  const [isMinimized, setIsMinimized] = useState(false)
  const [isStreaming, setIsStreaming] = useState(false)
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [messages, setMessages] = useState<AssistantChatMessage[]>(() => loadPersistedMessages(userId))

  const activeAbortRef = useRef<AbortController | null>(null)
  const previousUserRef = useRef<string | undefined>(userId)

  // Reset or reload conversation when the authenticated user changes
  useEffect(() => {
    if (previousUserRef.current !== userId) {
      previousUserRef.current = userId
      setConversationId(null)
      setMessages(loadPersistedMessages(userId))
    }
  }, [userId])

  // Persist conversation history to sessionStorage
  useEffect(() => {
    savePersistedMessages(userId, messages)
  }, [userId, messages])

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
    savePersistedMessages(userId, fresh)
  }, [userId])

  const sendMessage = useCallback(
    async (text: string) => {
      const prompt = text.trim()
      if (!prompt || isStreaming) return

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
        })
      } catch {
        // streamAssistantChat already invoked onError callback
      } finally {
        setIsStreaming(false)
        activeAbortRef.current = null
      }
    },
    [isStreaming, messages],
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
