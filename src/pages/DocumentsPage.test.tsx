// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DocumentItem, SemanticSearchResult } from '../api/documents'
import { DocumentsPage } from './DocumentsPage'

const { authState, api } = vi.hoisted(() => ({
  authState: {
    user: {
      id: 'manager-1',
      name: 'Manager',
      role: 'manager',
      department: 'Operations',
      team: 'Operations',
      email: 'manager@example.test',
    },
  },
  api: {
    deleteDocument: vi.fn(),
    downloadDocument: vi.fn(),
    listDocuments: vi.fn(),
    reindexDocument: vi.fn(),
    replaceDocument: vi.fn(),
    retryDocumentIndexing: vi.fn(),
    resetAllDocuments: vi.fn(),
    searchDocuments: vi.fn(),
    uploadDocument: vi.fn(),
  },
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => authState,
}))

vi.mock('../api/documents', () => api)

const documentItem: DocumentItem = {
  id: 'doc-1',
  original_filename: 'handbook.pdf',
  file_size: 1024,
  mime_type: 'application/pdf',
  owner_id: 'manager-1',
  status: 'ready',
  failure_reason: null,
  indexing_status: 'ready',
  indexing_failure_reason: null,
  version: 1,
  page_count: 3,
  processing_started_at: null,
  processing_completed_at: null,
  created_at: '2026-10-28T10:00:00Z',
  updated_at: '2026-10-28T10:00:00Z',
  acl: [],
}

const searchResult: SemanticSearchResult = {
  document_id: 'doc-1',
  filename: 'handbook.pdf',
  heading: 'Employee onboarding',
  content: 'HR coordinates employee onboarding.',
  score: 0.9,
  document_version: 1,
  page_label: 'Page 1',
}

beforeEach(() => {
  vi.clearAllMocks()
  api.listDocuments.mockResolvedValue({ documents: [documentItem], total: 1 })
})

afterEach(() => {
  cleanup()
})

function beginMeaningSearch() {
  render(<DocumentsPage />)
  fireEvent.click(screen.getByRole('button', { name: 'Meaning' }))
  fireEvent.change(
    screen.getByPlaceholderText('Search document content by meaning...'),
    { target: { value: 'What is the employee onboarding process?' } },
  )
}

describe('Company Files processing status', () => {
  it('shows Uploaded, Processing, Ready and Failed through the indexing lifecycle', async () => {
    api.listDocuments.mockResolvedValue({
      documents: [
        { ...documentItem, id: 'doc-uploaded', status: 'uploaded', indexing_status: 'pending' },
        { ...documentItem, id: 'doc-processing', status: 'processing', indexing_status: 'pending' },
        { ...documentItem, id: 'doc-ready', status: 'ready', indexing_status: 'ready' },
        {
          ...documentItem,
          id: 'doc-failed',
          status: 'ready',
          indexing_status: 'failed',
          indexing_failure_reason: 'Vector indexing failed',
        },
      ],
      total: 4,
    })
    render(<DocumentsPage />)

    expect(await screen.findByText('Uploaded', { selector: 'span' })).toBeInTheDocument()
    expect(screen.getByText('Processing')).toBeInTheDocument()
    expect(screen.getByText('Ready')).toBeInTheDocument()
    expect(screen.getByText('Failed')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry index' })).toBeInTheDocument()
  })
})

describe('Company Files semantic search', () => {
  it('shows matching content and the matching document after a successful search', async () => {
    api.searchDocuments.mockResolvedValue([searchResult])
    beginMeaningSearch()

    await waitFor(() => expect(api.searchDocuments).toHaveBeenCalledOnce())
    expect(api.searchDocuments.mock.calls[0][1]).toBe(5)
    expect(await screen.findByText('HR coordinates employee onboarding.')).toBeInTheDocument()
    expect(screen.getByText('Employee onboarding')).toBeInTheDocument()
    expect(screen.getByText('Page 1 · Relevance 0.90')).toBeInTheDocument()
    expect(screen.getAllByText('handbook.pdf')).toHaveLength(2)
    expect(screen.queryByText('No documents found')).not.toBeInTheDocument()
  })

  it('shows at most five concise results with page and relevance details', async () => {
    api.searchDocuments.mockResolvedValue(
      Array.from({ length: 6 }, (_, index) => ({
        ...searchResult,
        document_id: `doc-${index}`,
        filename: `handbook-${index}.pdf`,
        content: `${'A concise relevant passage. '.repeat(20)}result-${index}`,
        score: 0.95 - index * 0.01,
        page_label: `Page ${index + 1}`,
      })),
    )
    beginMeaningSearch()

    await waitFor(() => expect(api.searchDocuments).toHaveBeenCalledOnce())
    expect(await screen.findByText('handbook-0.pdf')).toBeInTheDocument()
    expect(screen.getAllByRole('article')).toHaveLength(5)
    expect(screen.queryByText('handbook-5.pdf')).not.toBeInTheDocument()
    expect(screen.getByText('Page 1 · Relevance 0.95')).toBeInTheDocument()
    expect(screen.getAllByText(/A concise relevant passage.*…/)).toHaveLength(5)
    expect(screen.queryByText(/result-0$/)).not.toBeInTheDocument()
  })

  it('renders the answer passage and its heading context from search results', async () => {
    api.searchDocuments.mockResolvedValue([{
      ...searchResult,
      heading: 'ACL Access-Control Test Document — [Page 1]',
      page_label: '[Page 1]',
      content: 'Test question: What is the confidential test phrase?\nExpected answer: ORCHID-4829',
    }])
    beginMeaningSearch()

    expect(await screen.findByText(/ORCHID-4829/)).toBeInTheDocument()
    expect(screen.getByText('ACL Access-Control Test Document — [Page 1]')).toBeInTheDocument()
    expect(screen.getByText('Page 1 · Relevance 0.90')).toBeInTheDocument()
  })

  it('shows a completed empty state only after the API returns no results', async () => {
    api.searchDocuments.mockResolvedValue([])
    beginMeaningSearch()

    expect(await screen.findByText('No matching indexed content was found.')).toBeInTheDocument()
    expect(screen.getByText('No documents found')).toBeInTheDocument()
    expect(screen.queryByText('Searching indexed document content...')).not.toBeInTheDocument()
  })

  it('keeps the document list in a searching state while the request is pending', async () => {
    let resolveSearch!: (results: SemanticSearchResult[]) => void
    api.searchDocuments.mockReturnValue(
      new Promise<SemanticSearchResult[]>((resolve) => {
        resolveSearch = resolve
      }),
    )
    beginMeaningSearch()

    await waitFor(() => expect(api.searchDocuments).toHaveBeenCalledOnce())
    expect(screen.getAllByText('Searching indexed document content...')).toHaveLength(2)
    expect(screen.queryByText('No documents found')).not.toBeInTheDocument()

    await act(async () => resolveSearch([searchResult]))
    expect(await screen.findByText('HR coordinates employee onboarding.')).toBeInTheDocument()
  })

  it('shows the request error instead of presenting it as an empty search', async () => {
    api.searchDocuments.mockRejectedValue(new Error('Search service unavailable.'))
    beginMeaningSearch()

    expect(await screen.findByRole('alert')).toHaveTextContent('Search service unavailable.')
    expect(
      screen.getByText('Document list was not filtered because semantic search failed.'),
    ).toBeInTheDocument()
    expect(screen.queryByText('No documents found')).not.toBeInTheDocument()
  })
})
