// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { DocumentItem } from '../api/documents'
import { DocumentsPage } from './DocumentsPage'

const { api, getActiveEmployees } = vi.hoisted(() => ({
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
  getActiveEmployees: vi.fn(),
}))

vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: { id: 'manager-1', name: 'Manager', role: 'manager', team: 'Operations', email: 'm@example.test' },
  }),
}))
vi.mock('../api/documents', () => api)
vi.mock('../api/meetings', () => ({ getActiveEmployees }))

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
  acl: [{ id: 'a1', employee_id: 'emp-2', created_at: '2026-10-28T10:00:00Z' }],
}

beforeEach(() => {
  vi.clearAllMocks()
  api.listDocuments.mockResolvedValue({ documents: [documentItem], total: 1 })
  getActiveEmployees.mockResolvedValue([
    { id: 'emp-2', name: 'Noah Patel', email: 'noah@example.test', team: 'Finance' },
    { id: 'emp-3', name: 'Ava Brooks', email: 'ava@example.test', team: 'Engineering' },
  ])
  api.replaceDocument.mockResolvedValue({ document: documentItem, warnings: [] })
})

afterEach(cleanup)

/** The Manage dialog's picker (the upload form has one with the same names). */
async function openManageDialog() {
  render(<DocumentsPage />)
  fireEvent.click(await screen.findByRole('button', { name: /^Manage$/ }))
  const search = await screen.findAllByPlaceholderText(/Search employees/)
  const dialog = document.getElementById('manage-acl-input')!.parentElement!
  expect(search.length).toBe(2)
  await within(dialog).findByText('Ava Brooks')
  return dialog
}

describe('Editing document access', () => {
  it('saves an access change without a replacement file', async () => {
    const dialog = await openManageDialog()

    fireEvent.click(within(dialog).getByRole('checkbox', { name: /Ava Brooks/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Save Access Changes' }))

    await waitFor(() => expect(api.replaceDocument).toHaveBeenCalledOnce())
    expect(api.replaceDocument).toHaveBeenCalledWith('doc-1', null, ['emp-2', 'emp-3'], false)
    expect(await screen.findByText(/Access to "handbook.pdf" was updated/)).toBeInTheDocument()
  })

  it('can clear the whole access list', async () => {
    const dialog = await openManageDialog()

    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove Noah Patel' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save Access Changes' }))

    await waitFor(() => expect(api.replaceDocument).toHaveBeenCalledWith('doc-1', null, [], false))
  })
})
