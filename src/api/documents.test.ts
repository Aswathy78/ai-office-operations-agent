import { afterEach, describe, expect, it, vi } from 'vitest'
import { API_BASE_URL } from './client'
import { searchDocuments, type SemanticSearchResult } from './documents'

const result: SemanticSearchResult = {
  document_id: 'doc-1',
  filename: 'handbook.pdf',
  heading: 'Employee onboarding',
  content: 'HR coordinates onboarding.',
  score: 0.9,
  document_version: 1,
}

function jsonResponse(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('semantic document search API', () => {
  it('parses successful semantic search results and forwards the query', async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({ results: [result] }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(searchDocuments('employee onboarding process')).resolves.toEqual([result])

    const url = new URL(fetchMock.mock.calls[0][0] as string)
    expect(url.pathname).toBe('/api/v1/documents/search')
    expect(url.searchParams.get('q')).toBe('employee onboarding process')
    expect(url.searchParams.get('limit')).toBe('10')
    expect(url.origin).toBe(API_BASE_URL)
  })

  it('returns an empty list when the API finds no matching content', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ results: [] })),
    )

    await expect(searchDocuments('unrelated query')).resolves.toEqual([])
  })

  it('surfaces API errors instead of treating them as empty results', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(
        jsonResponse({ detail: 'Search service unavailable.' }, 503),
      ),
    )

    await expect(searchDocuments('employee onboarding')).rejects.toThrow(
      'Search service unavailable.',
    )
  })

  it('rejects a malformed success response', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({ results: [{ filename: 'bad' }] })),
    )

    await expect(searchDocuments('employee onboarding')).rejects.toThrow(
      'Semantic search returned an invalid response',
    )
  })
})
