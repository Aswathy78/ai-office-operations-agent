import { apiFetch, API_BASE_URL } from './client'

export interface DocumentACLEntry {
  id: string
  employee_id: string
  created_at: string
}

export type DocumentStatus = 'uploaded' | 'processing' | 'ready' | 'failed'

export interface DocumentItem {
  id: string
  original_filename: string
  file_size: number
  mime_type: string
  owner_id: string
  status: DocumentStatus
  failure_reason: string | null
  page_count: number | null
  processing_started_at: string | null
  processing_completed_at: string | null
  created_at: string
  updated_at: string
  acl: DocumentACLEntry[]
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

/**
 * Upload a document (PDF, Word, PowerPoint, Excel, or Image) with optional ACL access list.
 */
export async function uploadDocument(
  file: File,
  aclEmployeeIds: string[] = [],
): Promise<DocumentUploadResponse> {
  const formData = new FormData()
  formData.append('file', file)
  if (aclEmployeeIds.length > 0) {
    formData.append('acl_employee_ids', JSON.stringify(aclEmployeeIds))
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
 * Replace an existing document file with a new file and trigger re-extraction.
 */
export async function replaceDocument(
  id: string,
  file: File,
  aclEmployeeIds?: string[],
): Promise<DocumentUploadResponse> {
  const formData = new FormData()
  formData.append('file', file)
  if (aclEmployeeIds && aclEmployeeIds.length > 0) {
    formData.append('acl_employee_ids', JSON.stringify(aclEmployeeIds))
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
