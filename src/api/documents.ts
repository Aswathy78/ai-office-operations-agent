import { apiFetch, API_BASE_URL } from './client'

export interface DocumentACLEntry {
  id: string
  employee_id: string
  created_at: string
}

export type DocumentStatus = 'uploaded' | 'processing' | 'ready' | 'failed'
export type DocumentIndexingStatus = 'pending' | 'processing' | 'ready' | 'failed'

export interface DocumentItem {
  id: string
  original_filename: string
  file_size: number
  mime_type: string
  owner_id: string
  status: DocumentStatus
  failure_reason: string | null
  indexing_status: DocumentIndexingStatus
  indexing_failure_reason: string | null
  version: number
  page_count: number | null
  processing_started_at: string | null
  processing_completed_at: string | null
  created_at: string
  updated_at: string
  acl: DocumentACLEntry[]
  /** Story 7.3: every active employee may read it and ask the assistant about it. */
  shared_with_all_employees?: boolean
}

export interface DocumentUploadResponse {
  document: DocumentItem
  warnings: string[]
  message: string
}

export interface DocumentListResponse {
  documents: DocumentItem[]
  total: number
}

export interface SemanticSearchResult {
  document_id: string
  filename: string
  heading: string | null
  content: string
  score: number
  document_version: number
  page_label: string | null
}

const MAX_SEMANTIC_SEARCH_RESULTS = 5

function isSemanticSearchResult(value: unknown): value is SemanticSearchResult {
  if (typeof value !== 'object' || value === null) return false
  if (
    !('document_id' in value)
    || !('filename' in value)
    || !('heading' in value)
    || !('content' in value)
    || !('score' in value)
    || !('document_version' in value)
    || !('page_label' in value)
  ) {
    return false
  }
  return (
    typeof value.document_id === 'string'
    && typeof value.filename === 'string'
    && (typeof value.heading === 'string' || value.heading === null)
    && typeof value.content === 'string'
    && typeof value.score === 'number'
    && typeof value.document_version === 'number'
    && (typeof value.page_label === 'string' || value.page_label === null)
  )
}

export async function searchDocuments(
  query: string,
  limit: number = MAX_SEMANTIC_SEARCH_RESULTS,
  signal?: AbortSignal,
): Promise<SemanticSearchResult[]> {
  const boundedLimit = Math.min(Math.max(limit, 1), MAX_SEMANTIC_SEARCH_RESULTS)
  const params = new URLSearchParams({ q: query, limit: String(boundedLimit) })
  const response = await apiFetch(`${API_BASE_URL}/api/v1/documents/search?${params}`, {
    signal,
  })
  if (!response.ok) {
    let errorDetail = 'Semantic search failed'
    try {
      const errorJson = await response.json()
      errorDetail = errorJson.detail || errorDetail
    } catch {
      errorDetail = response.statusText || errorDetail
    }
    throw new Error(errorDetail)
  }
  const result: unknown = await response.json()
  if (
    typeof result !== 'object'
    || result === null
    || !('results' in result)
    || !Array.isArray(result.results)
    || !result.results.every(isSemanticSearchResult)
  ) {
    throw new Error('Semantic search returned an invalid response')
  }
  return result.results
}

export async function retryDocumentIndexing(id: string): Promise<DocumentItem> {
  const response = await apiFetch(`${API_BASE_URL}/api/v1/documents/${id}/index`, {
    method: 'POST',
  })
  if (!response.ok) {
    let errorDetail = 'Could not retry document indexing'
    try {
      const errorJson = await response.json()
      errorDetail = errorJson.detail || errorDetail
    } catch {
      errorDetail = response.statusText || errorDetail
    }
    throw new Error(errorDetail)
  }
  return response.json()
}

export async function reindexDocument(id: string): Promise<DocumentItem> {
  const response = await apiFetch(`${API_BASE_URL}/api/v1/documents/${id}/reindex`, {
    method: 'POST',
  })
  if (!response.ok) {
    let errorDetail = 'Could not queue document reindexing'
    try {
      const errorJson = await response.json()
      errorDetail = errorJson.detail || errorDetail
    } catch {
      errorDetail = response.statusText || errorDetail
    }
    throw new Error(errorDetail)
  }
  return response.json()
}

/**
 * Upload a document (PDF, Word, PowerPoint, Excel, or Image) with optional ACL access list.
 */
export async function uploadDocument(
  file: File,
  aclEmployeeIds: string[] = [],
  sharedWithAllEmployees = false,
): Promise<DocumentUploadResponse> {
  const formData = new FormData()
  formData.append('file', file)
  if (aclEmployeeIds.length > 0) {
    formData.append('acl_employee_ids', JSON.stringify(aclEmployeeIds))
  }
  if (sharedWithAllEmployees) {
    formData.append('shared_with_all_employees', 'true')
  }

  const response = await apiFetch(`${API_BASE_URL}/api/v1/documents/upload`, {
    method: 'POST',
    body: formData,
  })

  if (!response.ok) {
    let errorDetail = 'Upload failed'
    try {
      const errorJson = await response.json()
      errorDetail = errorJson.detail || errorDetail
    } catch {
      errorDetail = response.statusText || errorDetail
    }
    throw new Error(errorDetail)
  }

  return response.json()
}

/**
 * Fetch all documents accessible to the authenticated user.
 */
export async function listDocuments(
  limit: number = 50,
  offset: number = 0,
): Promise<DocumentListResponse> {
  const response = await apiFetch(
    `${API_BASE_URL}/api/v1/documents/?limit=${limit}&offset=${offset}`,
  )

  if (!response.ok) {
    let errorDetail = 'Could not load documents'
    try {
      const errorJson = await response.json()
      errorDetail = errorJson.detail || errorDetail
    } catch {
      errorDetail = response.statusText || errorDetail
    }
    throw new Error(errorDetail)
  }

  return response.json()
}

/**
 * Fetch a single document's metadata and processing status.
 */
export async function getDocument(id: string): Promise<DocumentItem> {
  const response = await apiFetch(`${API_BASE_URL}/api/v1/documents/${id}`)

  if (!response.ok) {
    let errorDetail = 'Could not load document'
    try {
      const errorJson = await response.json()
      errorDetail = errorJson.detail || errorDetail
    } catch {
      errorDetail = response.statusText || errorDetail
    }
    throw new Error(errorDetail)
  }

  return response.json()
}

/**
 * Download a document file and trigger browser download.
 */
export async function downloadDocument(id: string, filename: string): Promise<void> {
  const response = await apiFetch(`${API_BASE_URL}/api/v1/documents/${id}/download`)

  if (!response.ok) {
    let errorDetail = 'Download failed'
    try {
      const errorJson = await response.json()
      errorDetail = errorJson.detail || errorDetail
    } catch {
      errorDetail = response.statusText || errorDetail
    }
    throw new Error(errorDetail)
  }

  const blob = await response.blob()
  const blobUrl = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = blobUrl
  anchor.download = filename || 'download'
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(blobUrl)
}

/**
 * Replace an existing document file and/or change who can access it.
 *
 * With a file, the document is re-extracted and re-indexed. With `file` null,
 * only access changes: the file and its index are left as they are. An empty
 * `aclEmployeeIds` list clears the access list (the owner always keeps access).
 */
export async function replaceDocument(
  id: string,
  file: File | null,
  aclEmployeeIds?: string[],
  sharedWithAllEmployees?: boolean,
): Promise<DocumentUploadResponse> {
  const formData = new FormData()
  if (file) {
    formData.append('file', file)
  }
  if (aclEmployeeIds) {
    formData.append('acl_employee_ids', JSON.stringify(aclEmployeeIds))
  }
  if (sharedWithAllEmployees !== undefined) {
    formData.append('shared_with_all_employees', String(sharedWithAllEmployees))
  }

  const response = await apiFetch(`${API_BASE_URL}/api/v1/documents/${id}`, {
    method: 'PUT',
    body: formData,
  })

  if (!response.ok) {
    let errorDetail = 'File update failed'
    try {
      const errorJson = await response.json()
      errorDetail = errorJson.detail || errorDetail
    } catch {
      errorDetail = response.statusText || errorDetail
    }
    throw new Error(errorDetail)
  }

  return response.json()
}

/**
 * Delete a document permanently.
 */
export async function deleteDocument(id: string): Promise<void> {
  const response = await apiFetch(`${API_BASE_URL}/api/v1/documents/${id}`, {
    method: 'DELETE',
  })

  if (!response.ok) {
    let errorDetail = 'Delete failed'
    try {
      const errorJson = await response.json()
      errorDetail = errorJson.detail || errorDetail
    } catch {
      errorDetail = response.statusText || errorDetail
    }
    throw new Error(errorDetail)
  }
}

/**
 * Start over: reset and remove all uploaded documents from database and storage.
 */
export async function resetAllDocuments(): Promise<{ message: string; removed_count: number }> {
  const response = await apiFetch(`${API_BASE_URL}/api/v1/documents/reset`, {
    method: 'POST',
  })

  if (!response.ok) {
    let errorDetail = 'Reset failed'
    try {
      const errorJson = await response.json()
      errorDetail = errorJson.detail || errorDetail
    } catch {
      errorDetail = response.statusText || errorDetail
    }
    throw new Error(errorDetail)
  }

  return response.json()
}
