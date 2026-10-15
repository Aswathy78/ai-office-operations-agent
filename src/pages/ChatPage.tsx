import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Bot, MessageSquare, SendHorizontal } from 'lucide-react'
import {
  createEmailDraft,
  getDirectoryGroups,
  getMyEmailDrafts,
  type DirectoryGroup,
  type EmailDraft,
} from '../api/emailDrafts'
import { EmailDraftPreview, EmailDraftStatusPill } from '../components/email/EmailDraftPreview'
import { useAuth } from '../contexts/AuthContext'

type ChatMessage =
  | { id: string; role: 'user'; text: string }
  | { id: string; role: 'assistant'; text: string; draftId?: string; tone?: 'error' }

const SUGGESTIONS = [
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
        'Hi! Tell me what e-mail you need and who it is for — a person, an address, or a team such as ' +
        '"the development team". I will prepare a draft for you to review; nothing is sent until you confirm.',
    },
  ])
  const [drafts, setDrafts] = useState<Record<string, EmailDraft>>({})
  const [recent, setRecent] = useState<EmailDraft[]>([])
  const [groups, setGroups] = useState<DirectoryGroup[]>([])
  const [input, setInput] = useState('')
  const [pending, setPending] = useState(false)
  const endRef = useRef<HTMLLIElement>(null)

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
      return draft.status === 'discarded' ? others : [draft, ...others]
    })
  }

  async function submit(text: string) {
    const prompt = text.trim()
    if (!prompt || !user || !actor || pending) return
    setInput('')
    setPending(true)
    setMessages((current) => [...current, { id: nextId(), role: 'user', text: prompt }])
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
            <h2>Draft an e-mail</h2>
            <p>The assistant prepares the e-mail. You review, edit, and decide whether it is sent.</p>
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
                </div>
              </li>
            )
          })}
          {pending && (
            <li className="chat-message assistant">
              <span className="chat-avatar" aria-hidden="true"><Bot size={16} /></span>
              <div className="chat-bubble" role="status"><p>Drafting your e-mail…</p></div>
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
            placeholder="e.g. Email the development team about Friday's release"
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
          <ul>
            {recent.map((draft) => (
              <li key={draft.id}>
                <button type="button" onClick={() => openDraft(draft)}>
                  <strong>{draft.subject || '(no subject)'}</strong>
                  <span>{draft.recipient_count} recipient{draft.recipient_count === 1 ? '' : 's'}</span>
                  <EmailDraftStatusPill status={draft.status} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>
    </div>
  )
}
