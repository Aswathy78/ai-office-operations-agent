/**
 * AI Assistant Floating Chat Widget.
 *
 * Stories:
 *  6.1 — Floating animated robot, chat panel, streaming tokens, quick actions,
 *         conversation history, minimize / close.
 *  6.4 — Execution plan panel, per-step status, retrieval indicator, halt banner.
 *
 * The plan panel is rendered as a collapsible section inside the chat panel.
 * It is shown as soon as the backend emits the `plan` SSE event — before any
 * step executes — so the user always sees the ordered plan first.
 */

import { type FormEvent, useEffect, useRef, useState } from 'react'
import {
  AlertTriangle,
  BookOpen,
  CheckCircle,
  ChevronDown,
  ChevronUp,
  CircleDashed,
  Loader,
  Minus,
  RotateCcw,
  SendHorizontal,
  Sparkles,
  X,
  XCircle,
} from 'lucide-react'
import { ASSISTANT_QUICK_ACTIONS, type PlanStep } from '../../api/assistant'
import { useAssistant } from '../../contexts/AssistantContext'

// ---------------------------------------------------------------------------
// Robot mascot (unchanged from 6.1)
// ---------------------------------------------------------------------------

export function RobotMascot({ size = 38 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 48 48"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="assistant-robot-svg"
      aria-hidden="true"
    >
      {/* Antenna */}
      <circle cx="24" cy="6" r="3.5" fill="#38bdf8" />
      <path
        d="M24 9.5V14"
        stroke="#93c5fd"
        strokeWidth="2.5"
        strokeLinecap="round"
      />

      {/* Head Outer */}
      <rect
        x="8"
        y="14"
        width="32"
        height="26"
        rx="8"
        fill="url(#robot-gradient)"
        stroke="#60a5fa"
        strokeWidth="1.5"
      />

      {/* Ears / Headphone nubs */}
      <rect x="4" y="21" width="4" height="12" rx="2" fill="#2563eb" />
      <rect x="40" y="21" width="4" height="12" rx="2" fill="#2563eb" />

      {/* Face Visor Screen */}
      <rect
        x="13"
        y="19"
        width="22"
        height="14"
        rx="4"
        fill="#0b1e36"
      />

      {/* Glowing Eyes */}
      <circle cx="19" cy="25" r="2.5" fill="#38bdf8" />
      <circle cx="29" cy="25" r="2.5" fill="#38bdf8" />
      {/* Eye glint */}
      <circle cx="18" cy="24" r="0.8" fill="#ffffff" />
      <circle cx="28" cy="24" r="0.8" fill="#ffffff" />

      {/* Friendly Smile */}
      <path
        d="M21 29C22.2 30.2 25.8 30.2 27 29"
        stroke="#38bdf8"
        strokeWidth="1.5"
        strokeLinecap="round"
      />

      {/* Gradients */}
      <defs>
        <linearGradient
          id="robot-gradient"
          x1="8"
          y1="14"
          x2="40"
          y2="40"
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor="#1e40af" />
          <stop offset="1" stopColor="#0f2d52" />
        </linearGradient>
      </defs>
    </svg>
  )
}

// ---------------------------------------------------------------------------
// Step status icon (Story 6.4)
// ---------------------------------------------------------------------------

function StepStatusIcon({ status }: { status: PlanStep['status'] }) {
  switch (status) {
    case 'done':
      return <CheckCircle size={14} className="assistant-step-icon done" aria-label="Done" />
    case 'failed':
      return <XCircle size={14} className="assistant-step-icon failed" aria-label="Failed" />
    case 'running':
      return (
        <Loader
          size={14}
          className="assistant-step-icon running assistant-step-spin"
          aria-label="Running"
        />
      )
    case 'skipped':
      return <CircleDashed size={14} className="assistant-step-icon skipped" aria-label="Skipped" />
    case 'awaiting_confirmation':
      return (
        <CircleDashed
          size={14}
          className="assistant-step-icon awaiting"
          aria-label="Waiting for your confirmation"
        />
      )
    default:
      return <CircleDashed size={14} className="assistant-step-icon pending" aria-label="Pending" />
  }
}

// ---------------------------------------------------------------------------
// Execution Plan Panel (Story 6.4)
// ---------------------------------------------------------------------------

interface PlanPanelProps {
  plan: PlanStep[]
  activeStepIndex: number
  haltedReason: string | null
  hasRetrieval: boolean
}

function ExecutionPlanPanel({
  plan,
  activeStepIndex,
  haltedReason,
  hasRetrieval,
}: PlanPanelProps) {
  const [collapsed, setCollapsed] = useState(false)

  const completedCount = plan.filter((s) => s.status === 'done').length
  const failedCount = plan.filter((s) => s.status === 'failed').length

  return (
    <div
      className="assistant-plan-panel"
      role="region"
      aria-label="Execution plan"
      data-testid="assistant-plan-panel"
    >
      {/* Plan header */}
      <button
        type="button"
        className="assistant-plan-header"
        onClick={() => setCollapsed((c) => !c)}
        aria-expanded={!collapsed}
        aria-controls="assistant-plan-steps"
      >
        <span className="assistant-plan-title">
          <Sparkles size={13} aria-hidden="true" />
          Execution Plan
          <span className="assistant-plan-badge">
            {completedCount}/{plan.length} done
          </span>
          {failedCount > 0 && (
            <span className="assistant-plan-badge failed">{failedCount} failed</span>
          )}
        </span>
        {collapsed ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
      </button>

      {/* Halt banner */}
      {haltedReason && (
        <div
          className="assistant-plan-halt"
          role="alert"
          data-testid="assistant-plan-halt"
        >
          <AlertTriangle size={13} aria-hidden="true" />
          {haltedReason === 'step_limit'
            ? 'Execution stopped: maximum step limit reached.'
            : haltedReason === 'cost_cap'
              ? 'Execution stopped: token budget exhausted.'
              : `Execution stopped: ${haltedReason}.`}
        </div>
      )}

      {/* Retrieval indicator */}
      {hasRetrieval && !collapsed && (
        <div
          className="assistant-plan-retrieval-badge"
          data-testid="assistant-plan-retrieval-badge"
        >
          <BookOpen size={12} aria-hidden="true" />
          Company policy retrieved
        </div>
      )}

      {/* Step list */}
      {!collapsed && (
        <ol
          id="assistant-plan-steps"
          className="assistant-plan-steps"
          data-testid="assistant-plan-steps"
        >
          {plan.map((step) => {
            const isActive = step.index === activeStepIndex && step.status === 'running'
            return (
              <li
                key={step.index}
                className={`assistant-plan-step ${step.status}${isActive ? ' active' : ''}`}
                data-testid={`assistant-plan-step-${step.index}`}
              >
                <StepStatusIcon status={step.status} />
                <div className="assistant-step-body">
                  <span className="assistant-step-desc">{step.description}</span>
                  {step.result_summary && step.status !== 'pending' && (
                    <span className="assistant-step-result">{step.result_summary}</span>
                  )}
                </div>
              </li>
            )
          })}
        </ol>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main widget
// ---------------------------------------------------------------------------

export function AssistantWidget() {
  const {
    messages,
    isOpen,
    isMinimized,
    isStreaming,
    openWidget,
    closeWidget,
    minimizeWidget,
    sendMessage,
    clearHistory,
    // Story 6.4
    currentPlan,
    activeStepIndex,
    lastRetrieval,
    haltedReason,
  } = useAssistant()

  const [input, setInput] = useState('')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  // Auto-scroll to bottom of conversation
  useEffect(() => {
    if (isOpen && !isMinimized) {
      messagesEndRef.current?.scrollIntoView?.({ behavior: 'smooth' })
    }
  }, [messages, currentPlan, isOpen, isMinimized])

  // Focus input on open
  useEffect(() => {
    if (isOpen && !isMinimized) {
      inputRef.current?.focus()
    }
  }, [isOpen, isMinimized])

  const handleSubmit = async (e?: FormEvent) => {
    e?.preventDefault()
    const prompt = input.trim()
    if (!prompt || isStreaming) return
    setInput('')
    await sendMessage(prompt)
  }

  const handleQuickAction = async (prompt: string) => {
    if (isStreaming) return
    setInput('')
    await sendMessage(prompt)
  }

  return (
    <div
      className="assistant-widget-container"
      data-testid="assistant-widget-container"
    >
      {/* Floating Chat Panel */}
      {isOpen && !isMinimized && (
        <section
          className="assistant-panel"
          role="dialog"
          aria-label="AI Assistant Command Center"
          data-testid="assistant-chat-panel"
        >
          {/* Header */}
          <header className="assistant-panel-header">
            <div className="assistant-header-title">
              <div className="assistant-header-avatar" aria-hidden="true">
                <RobotMascot size={28} />
              </div>
              <div>
                <div className="assistant-heading-row">
                  <h3>AI Assistant</h3>
                  <span className="assistant-status-badge" aria-label="Status: Online">
                    <span className="assistant-status-dot" aria-hidden="true" />
                    Online
                  </span>
                </div>
                <p className="assistant-subtitle">Epic 6 Agentic Command Center</p>
              </div>
            </div>

            <div className="assistant-header-actions">
              <button
                type="button"
                className="assistant-tool-btn"
                onClick={clearHistory}
                title="Clear conversation"
                aria-label="Clear conversation history"
              >
                <RotateCcw size={16} />
              </button>
              <button
                type="button"
                className="assistant-tool-btn"
                onClick={minimizeWidget}
                title="Minimize chat"
                aria-label="Minimize AI Assistant"
              >
                <Minus size={17} />
              </button>
              <button
                type="button"
                className="assistant-tool-btn"
                onClick={closeWidget}
                title="Close chat"
                aria-label="Close AI Assistant"
              >
                <X size={17} />
              </button>
            </div>
          </header>

          {/* Messages Body */}
          <div
            className="assistant-messages-body"
            role="log"
            aria-live="polite"
            aria-label="Conversation messages"
          >
            {messages.map((msg) => {
              const isUser = msg.role === 'user'
              return (
                <div
                  key={msg.id}
                  className={`assistant-msg-row ${isUser ? 'user' : 'assistant'} ${
                    msg.error ? 'has-error' : ''
                  }`}
                  data-testid={`assistant-message-${msg.role}`}
                >
                  {!isUser && (
                    <div className="assistant-msg-avatar" aria-hidden="true">
                      <RobotMascot size={24} />
                    </div>
                  )}
                  <div className="assistant-msg-bubble">
                    <p className="assistant-msg-text">
                      {msg.content}
                      {msg.isStreaming && (
                        <span
                          className="assistant-streaming-cursor"
                          aria-hidden="true"
                        />
                      )}
                    </p>
                    {msg.isStreaming && !msg.content && (
                      <div
                        className="assistant-typing-indicator"
                        aria-label="Assistant is thinking"
                        data-testid="assistant-typing-indicator"
                      >
                        <span className="dot" />
                        <span className="dot" />
                        <span className="dot" />
                      </div>
                    )}
                  </div>
                </div>
              )
            })}

            {/* Story 6.4: Execution plan panel — rendered inside the message feed
                so the user sees it in context, between their request and the
                streaming response.  It appears as soon as the `plan` SSE event
                arrives, guaranteeing it is visible before any step executes. */}
            {currentPlan && currentPlan.length > 0 && (
              <div className="assistant-msg-row assistant" data-testid="assistant-plan-row">
                <div className="assistant-msg-avatar" aria-hidden="true">
                  <RobotMascot size={24} />
                </div>
                <div className="assistant-msg-bubble assistant-plan-bubble">
                  <ExecutionPlanPanel
                    plan={currentPlan}
                    activeStepIndex={activeStepIndex}
                    haltedReason={haltedReason}
                    hasRetrieval={lastRetrieval !== null}
                  />
                </div>
              </div>
            )}

            <div ref={messagesEndRef} className="assistant-scroll-anchor" />
          </div>

          {/* Quick-action suggestions */}
          <div
            className="assistant-quick-actions"
            aria-label="Quick action suggestions"
            data-testid="assistant-quick-actions"
          >
            <div className="assistant-quick-label">
              <Sparkles size={13} aria-hidden="true" />
              <span>Suggested commands</span>
            </div>
            <div className="assistant-chips-row">
              {ASSISTANT_QUICK_ACTIONS.map((action) => (
                <button
                  key={action.id}
                  type="button"
                  className="assistant-chip-btn"
                  onClick={() => handleQuickAction(action.prompt)}
                  disabled={isStreaming}
                  title={action.description || action.prompt}
                >
                  {action.label}
                </button>
              ))}
            </div>
          </div>

          {/* Footer Input */}
          <form
            className="assistant-panel-footer"
            onSubmit={handleSubmit}
            aria-label="Send message to assistant"
          >
            <input
              ref={inputRef}
              type="text"
              className="assistant-input"
              placeholder="Ask the assistant or give a command..."
              value={input}
              onChange={(e) => setInput(e.target.value)}
              disabled={isStreaming}
              aria-label="Message AI Assistant"
            />
            <button
              type="submit"
              className="assistant-send-btn"
              disabled={!input.trim() || isStreaming}
              aria-label="Send message"
            >
              <SendHorizontal size={18} />
            </button>
          </form>
        </section>
      )}

      {/* Floating Robot Launcher Button */}
      {(!isOpen || isMinimized) && (
        <div className="assistant-launcher-wrapper">
          <button
            type="button"
            className="assistant-launcher-btn"
            onClick={openWidget}
            aria-label="Open AI Assistant"
            aria-expanded={isOpen && !isMinimized}
            aria-haspopup="dialog"
            data-testid="assistant-launcher-btn"
          >
            <span className="assistant-robot-shadow" aria-hidden="true" />
            <RobotMascot size={42} />
            <span className="assistant-launcher-label">AI Assistant</span>
          </button>
        </div>
      )}
    </div>
  )
}
