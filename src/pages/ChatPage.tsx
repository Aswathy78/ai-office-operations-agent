import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Bot, MessageSquare, SendHorizontal } from 'lucide-react'
import {
  EMAIL_STATUS_POLL_MS,
  createEmailDraft,
  getDirectoryGroups,
  getMyEmailDrafts,
  isInFlight,
  type DirectoryGroup,
  type EmailDraft,
} from '../api/emailDrafts'
import { confirmMeetingSlot, scheduleMeetingNL } from '../api/meetings'
import { EmailDraftPreview, EmailDraftStatusPill } from '../components/email/EmailDraftPreview'
import { ScheduledMeetingCard } from '../components/meetings/ScheduledMeetingCard'
import { useAuth } from '../contexts/AuthContext'
import { usePolling } from '../hooks/usePolling'
import type { ScheduleResult } from '../types/meeting'

export function isMeetingSchedulingIntent(prompt: string): boolean {
  const p = prompt.trim()
  if (!p) return false

  // If prompt explicitly starts with an email action, prioritize email drafting
  if (/^(email|send\s+(an?\s+)?email|draft\s+(an?\s+)?email|write\s+(an?\s+)?email)\b/i.test(p)) {
    return false
  }

  // Common scheduling verbs paired with meeting nouns or time/participants
  const schedulingVerb = /\b(schedule|reschedule|book|set\s+up|arrange|organize)\b/i
  const meetingNoun = /\b(meeting|sync|call|discussion|demo|session|catchup|catch-up|1-on-1|one-on-one)\b/i

  if (schedulingVerb.test(p) && meetingNoun.test(p)) {
    return true
  }

  // Starts with "schedule ", "reschedule ", or "book "
  if (/^(schedule|reschedule|book)\b/i.test(p)) {
    return true
  }

  return false
}

type ChatMessage =
  | { id: string; role: 'user'; text: string }
  | {
      id: string
      role: 'assistant'
      text: string
      draftId?: string
      meetingResult?: ScheduleResult
      meetingPrompt?: string
      tone?: 'error' | 'warning'
    }

const SUGGESTIONS = [
  'Schedule a project meeting tomorrow at 10 AM with the development team',
  "Email the development team a reminder about tomorrow's sprint demo at 10am",
  'Let the finance team know I will be on leave next Monday and Tuesday',
  'Email Ava about the deadline for the Q3 report on Friday',
]

let messageCounter = 0
const nextId = () => `msg-${Date.now()}-${messageCounter++}`

function describeDraft(draft: EmailDraft): string {
  const parts = [
    `I drafted an e-mail to ${draft.recipient_count} recipient${draft.recipient_count === 1 ? '' : 's'}` +
      (draft.recipient_groups.length
        ? ` (${draft.recipient_groups.map((g) => `${g.name}: ${g.count}`).join(', ')})`
        : '') +
      '. Nothing has been sent — review and edit it below.',
  ]
  if (draft.recipient_count === 0) parts.push('I could not find any recipients in your request, so please add them.')
  if (draft.approval_required) parts.push('This e-mail needs approval before it can be sent.')
  if (draft.sensitive_findings.length) {
    parts.push(`The sensitive-data check flagged ${draft.sensitive_findings.length} item(s).`)
  }
  return parts.join(' ')
}

export function ChatPage() {
  const { user } = useAuth()
  const actor = useMemo(
    () => (user ? { id: user.id, role: user.role, team: user.department || null } : null),
    [user],
  )
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome',
      role: 'assistant',
      text:
        'Hi! Tell me what you need — draft an e-mail or schedule a meeting with a person or team such as ' +
        '"the development team". I will prepare everything for you to review; nothing is sent until you confirm.',
    },
  ])
  const [drafts, setDrafts] = useState<Record<string, EmailDraft>>({})
  const [recent, setRecent] = useState<EmailDraft[]>([])
  const [groups, setGroups] = useState<DirectoryGroup[]>([])
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)
  const endRef = useRef<HTMLLIElement>(null)
  const lastClarificationPromptRef = useRef<string | null>(null)

  const loadRecent = useCallback(async () => {
    if (!actor) return
    try {
      setRecent(await getMyEmailDrafts(actor))
    } catch {
      // The draft history is a convenience; the chat still works without it.
    }
  }, [actor])

  useEffect(() => {
    void loadRecent()
    getDirectoryGroups().then(setGroups).catch(() => setGroups([]))
  }, [loadRecent])

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ behavior: 'smooth', block: 'end' })
  }, [messages.length])

  function storeDraft(draft: EmailDraft) {
    setDrafts((current) => ({ ...current, [draft.id]: draft }))
    setRecent((current) => {
      const others = current.filter((item) => item.id !== draft.id)
      return draft.discarded_at ? others : [draft, ...others]
    })
  }

  // While any e-mail is in Approval, Queued, Sending or Retry, refresh every 2 seconds so
  // each status change (e.g. Queued -> Sending -> Sent) shows up within 5 seconds.
  const anyInFlight = recent.some(isInFlight) || Object.values(drafts).some(isInFlight)
  usePolling(
    async () => {
      if (!actor) return
      const latest = await getMyEmailDrafts(actor)
      setRecent(latest)
      setDrafts((current) => {
        const next = { ...current }
        for (const draft of latest) if (next[draft.id]) next[draft.id] = draft
        return next
      })
    },
    EMAIL_STATUS_POLL_MS,
    anyInFlight,
  )

  const needsAttention = recent.filter((draft) => draft.status === 'failed' || draft.status === 'retry')
  const others = recent.filter((draft) => draft.status !== 'failed' && draft.status !== 'retry')

  async function submit(text: string) {
    const prompt = text.trim()
    if (!prompt || !user || !actor || pending) return
    setInput('')
    setPending(true)
    setMessages((current) => [...current, { id: nextId(), role: 'user', text: prompt }])

    const isScheduling =
      isMeetingSchedulingIntent(prompt) ||
      (lastClarificationPromptRef.current !== null &&
        !/^(email|send\s+(an?\s+)?email|draft\s+(an?\s+)?email|write\s+(an?\s+)?email)\b/i.test(prompt))

    if (isScheduling) {
      const effectivePrompt = lastClarificationPromptRef.current
        ? `${lastClarificationPromptRef.current} ${prompt}`
        : prompt

      try {
        const result = await scheduleMeetingNL(effectivePrompt, actor)
        if (result.status === 'Completed') {
          lastClarificationPromptRef.current = null
          setMessages((current) => [
            ...current,
            {
              id: nextId(),
              role: 'assistant',
              text: result.message || `Scheduled "${result.meeting?.title}" successfully.`,
              meetingResult: result,
            },
          ])
        } else if (result.status === 'needs_clarification') {
          lastClarificationPromptRef.current = effectivePrompt
          setMessages((current) => [
            ...current,
            {
              id: nextId(),
              role: 'assistant',
              text: result.clarifying_question || 'Could you provide more details for this meeting?',
              meetingResult: result,
            },
          ])
        } else if (result.status === 'conflict') {
          lastClarificationPromptRef.current = null
          setMessages((current) => [
            ...current,
            {
              id: nextId(),
              role: 'assistant',
              text: result.message || 'There is a scheduling conflict with one or more attendees.',
              meetingResult: result,
              meetingPrompt: effectivePrompt,
              tone: 'warning',
            },
          ])
        } else {
          lastClarificationPromptRef.current = null
          setMessages((current) => [
            ...current,
            {
              id: nextId(),
              role: 'assistant',
              tone: 'error',
              text: result.message || 'Failed to schedule meeting.',
              meetingResult: result,
            },
          ])
        }
      } catch (reason) {
        lastClarificationPromptRef.current = null
        setMessages((current) => [
          ...current,
          {
            id: nextId(),
            role: 'assistant',
            tone: 'error',
            text: `I couldn't schedule that meeting: ${reason instanceof Error ? reason.message : 'unknown error'}`,
          },
        ])
      } finally {
        setPending(false)
      }
      return
    }

    lastClarificationPromptRef.current = null
    try {
      const draft = await createEmailDraft(prompt, { name: user.name, email: user.email }, actor)
      storeDraft(draft)
      setMessages((current) => [
        ...current,
        { id: nextId(), role: 'assistant', text: describeDraft(draft), draftId: draft.id },
      ])
    } catch (reason) {
      setMessages((current) => [
        ...current,
        {
          id: nextId(),
          role: 'assistant',
          tone: 'error',
          text: `I couldn't prepare that e-mail: ${reason instanceof Error ? reason.message : 'unknown error'}`,
        },
      ])
    } finally {
      setPending(false)
    }
  }

  function openDraft(draft: EmailDraft) {
    storeDraft(draft)
    setMessages((current) => [
      ...current,
      { id: nextId(), role: 'assistant', text: `Here is your e-mail “${draft.subject}”.`, draftId: draft.id },
    ])
  }

  async function chooseAlternative(prompt: string, startAt: string) {
    if (!actor || pending) return
    setPending(true)
    try {
      const result = await confirmMeetingSlot(prompt, startAt, actor)
      setMessages((current) => [
        ...current,
        {
          id: nextId(),
          role: 'assistant',
          text: result.message || (result.status === 'conflict'
            ? 'That time is no longer available. Here are fresh alternatives.'
            : 'Meeting scheduling updated.'),
          meetingResult: result,
          meetingPrompt: prompt,
          tone: result.status === 'conflict' ? 'warning' : undefined,
        },
      ])
    } catch (reason) {
      setMessages((current) => [
        ...current,
        {
          id: nextId(),
          role: 'assistant',
          tone: 'error',
          text: `I couldn't confirm that time: ${reason instanceof Error ? reason.message : 'unknown error'}`,
        },
      ])
    } finally {
      setPending(false)
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    void submit(input)
  }

  if (!user || !actor) return null

  return (
    <div className="chat-layout">
      <section className="chat-panel" aria-label="AI Assistant chat">
        <header className="chat-header">
          <div className="meeting-icon" aria-hidden="true"><MessageSquare size={21} /></div>
          <div>
            <p className="eyebrow">AI Assistant</p>
            <h2>AI Assistant</h2>
            <p>Draft e-mails or schedule meetings with colleagues using natural language.</p>
          </div>
        </header>

        <ol className="chat-messages" aria-live="polite">
          {messages.map((message) => {
            const draft = message.role === 'assistant' && message.draftId ? drafts[message.draftId] : undefined
            return (
              <li key={message.id} className={`chat-message ${message.role}${message.role === 'assistant' && message.tone ? ` ${message.tone}` : ''}`}>
                {message.role === 'assistant' && (
                  <span className="chat-avatar" aria-hidden="true"><Bot size={16} /></span>
                )}
                <div className="chat-bubble">
                  <p>{message.text}</p>
                  {draft && (
                    <EmailDraftPreview draft={draft} actor={actor} groups={groups} onChange={storeDraft} />
                  )}
                  {message.role === 'assistant' && message.meetingResult && (
                    <ScheduledMeetingCard
                      result={message.meetingResult}
                      confirming={pending}
                      onSelectAlternative={message.meetingPrompt
                        ? (startAt) => void chooseAlternative(message.meetingPrompt!, startAt)
                        : undefined}
                    />
                  )}
                </div>
              </li>
            )
          })}
          {pending && (
            <li className="chat-message assistant">
              <span className="chat-avatar" aria-hidden="true"><Bot size={16} /></span>
              <div className="chat-bubble" role="status"><p>Thinking…</p></div>
            </li>
          )}
          <li ref={endRef} aria-hidden="true" className="chat-scroll-anchor" />
        </ol>

        {messages.length === 1 && (
          <div className="chat-suggestions">
            {SUGGESTIONS.map((suggestion) => (
              <button key={suggestion} type="button" className="chat-suggestion" onClick={() => void submit(suggestion)}>
                {suggestion}
              </button>
            ))}
          </div>
        )}

        <form className="chat-input" onSubmit={onSubmit}>
          <textarea
            aria-label="Message the AI Assistant"
            placeholder="e.g. Schedule a meeting tomorrow at 10 AM with the development team, or draft an email..."
            value={input}
            rows={2}
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault()
                void submit(input)
              }
            }}
            disabled={pending}
          />
          <button className="button primary" type="submit" disabled={pending || !input.trim()} aria-label="Send message">
            <SendHorizontal size={16} aria-hidden="true" />
          </button>
        </form>
      </section>

      <aside className="chat-sidebar" aria-label="Your e-mail drafts">
        <h3>Your e-mails</h3>
        {recent.length === 0 ? (
          <p className="task-manage-hint">Drafts you create will appear here.</p>
        ) : (
          <>
            {needsAttention.length > 0 && (
              <section className="chat-sidebar-attention" aria-label="E-mails that need attention">
                <h4>Needs attention ({needsAttention.length})</h4>
                <ul>
                  {needsAttention.map((draft) => (
                    <li key={draft.id}>
                      <button type="button" onClick={() => openDraft(draft)}>
                        <strong>{draft.subject || '(no subject)'}</strong>
                        <span>
                          {draft.status === 'retry'
                            ? `Retrying automatically (attempt ${draft.attempt_count} of ${draft.max_attempts})`
                            : 'Not delivered to every recipient'}
                        </span>
                        <EmailDraftStatusPill status={draft.status} />
                      </button>
                    </li>
                  ))}
                </ul>
              </section>
            )}
            <ul>
              {others.map((draft) => (
                <li key={draft.id}>
                  <button type="button" onClick={() => openDraft(draft)}>
                    <strong>{draft.subject || '(no subject)'}</strong>
                    <span>{draft.recipient_count} recipient{draft.recipient_count === 1 ? '' : 's'}</span>
                    <EmailDraftStatusPill status={draft.status} />
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </aside>
    </div>
  )
}
