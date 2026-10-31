import { afterEach, describe, expect, it, vi } from 'vitest'
import { streamAssistantChat, type Citation } from './assistant'

const citation: Citation = {
  id: 1,
  document_id: 'doc-1',
  document_version: 1,
  source: 'Remote.pdf',
  page: 'Page 3',
  section: 'Remote Work',
  label: 'Remote.pdf, Page 3, section "Remote Work"',
  quote: 'Up to 15 days per year.',
}

function sse(...events: unknown[]): Response {
  const body = events.map((event) => `data: ${JSON.stringify(event)}\n\n`).join('') + 'data: [DONE]\n\n'
  return new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('streamAssistantChat citations (Story 7.5)', () => {
  it('delivers the citations event before the answer text', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(
        sse(
          { conversation_id: 'c1' },
          { retrieval: { query: 'q', snippet: 's', source: 'Remote.pdf' } },
          { citations: [citation] },
          { token: 'Up to 15 days per year. [1]' },
        ),
      ),
    )
    const order: string[] = []
    let received: Citation[] = []
    let text = ''

    await streamAssistantChat({
      message: 'How many remote days?',
      onCitations: (citations) => {
        order.push('citations')
        received = citations
      },
      onChunk: (chunk) => {
        order.push('chunk')
        text += chunk
      },
    })

    expect(received).toEqual([citation])
    expect(order[0]).toBe('citations')
    expect(text).toBe('Up to 15 days per year. [1]')
  })

  it('works when the server sends no citations', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(sse({ conversation_id: 'c1' }, { token: 'Not found.' })),
    )
    let called = false

    await streamAssistantChat({
      message: 'q',
      onCitations: () => {
        called = true
      },
      onChunk: () => undefined,
    })

    expect(called).toBe(false)
  })
})
