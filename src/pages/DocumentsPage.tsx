import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Download,
  FileCheck,
  FileEdit,
  FileImage,
  FileSpreadsheet,
  FileText,
  Lock,
  Presentation,
  RefreshCw,
  RotateCcw,
  Search,
  Shield,
  Trash2,
  UploadCloud,
  Users,
  X,
} from 'lucide-react'
import {
  deleteDocument,
  downloadDocument,
  listDocuments,
  replaceDocument,
  resetAllDocuments,
  uploadDocument,
  type DocumentItem,
  type DocumentStatus,
} from '../api/documents'
import { AccessDenied } from '../components/common/AccessDenied'
import { useAuth } from '../contexts/AuthContext'

function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function formatDate(iso: string): string {
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(new Date(iso))
  } catch {
    return iso
  }
}

function getFileIcon(mime: string) {
  if (mime.includes('pdf')) return <FileText className="text-red-500" size={20} />
  if (mime.includes('word') || mime.includes('document'))
    return <FileText className="text-blue-500" size={20} />
  if (mime.includes('presentation'))
    return <Presentation className="text-orange-500" size={20} />
  if (mime.includes('spreadsheet') || mime.includes('excel'))
    return <FileSpreadsheet className="text-emerald-500" size={20} />
  if (mime.startsWith('image/'))
    return <FileImage className="text-purple-500" size={20} />
  return <FileText size={20} />
}

export function DocumentsPage() {
  const { user } = useAuth()

  // Epic 7 access control: Manager / HR only
  const isAuthorized = user && ['manager', 'hr_operations', 'admin'].includes(user.role)

  const [documents, setDocuments] = useState<DocumentItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [searchQuery, setSearchQuery] = useState('')
  const [statusFilter, setStatusFilter] = useState<'all' | DocumentStatus>('all')

  // Upload state
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [aclInput, setAclInput] = useState('')
  const [uploading, setUploading] = useState(false)
  const [uploadFeedback, setUploadFeedback] = useState<{
    type: 'success' | 'error' | 'warning'
    message: string
    warnings?: string[]
  } | null>(null)

  const [downloadingId, setDownloadingId] = useState<string | null>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)

  // Start Over modal state
  const [showResetModal, setShowResetModal] = useState(false)
  const [isResetting, setIsResetting] = useState(false)

  // Manage document modal state
  const [managingDoc, setManagingDoc] = useState<DocumentItem | null>(null)
  const [replacementFile, setReplacementFile] = useState<File | null>(null)
  const [replacementAcl, setReplacementAcl] = useState('')
  const [isReplacing, setIsReplacing] = useState(false)
  const [isDeleting, setIsDeleting] = useState(false)
  const [manageError, setManageError] = useState('')
  const manageFileInputRef = useRef<HTMLInputElement>(null)

  const loadDocuments = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    try {
      const res = await listDocuments(100, 0)
      setDocuments(res.documents)
      setError('')
    } catch (err) {
      if (!silent) {
        setError(err instanceof Error ? err.message : 'Failed to load documents')
      }
    } finally {
      if (!silent) setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (isAuthorized) {
      void loadDocuments()
    }
  }, [loadDocuments, isAuthorized])

  // Real-time polling when documents are processing
  const hasProcessingDocs = useMemo(
    () =>
      documents.some(
        (d) => d.status === 'uploaded' || d.status === 'processing',
      ),
    [documents],
  )

  useEffect(() => {
    if (!hasProcessingDocs || !isAuthorized) return
    const interval = setInterval(() => {
      void loadDocuments(true)
    }, 3000)
    return () => clearInterval(interval)
  }, [hasProcessingDocs, loadDocuments, isAuthorized])

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files && e.target.files[0]) {
      setSelectedFile(e.target.files[0])
      setUploadFeedback(null)
    }
  }

  const handleDrop = (e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault()
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      setSelectedFile(e.dataTransfer.files[0])
      setUploadFeedback(null)
    }
  }

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!selectedFile) return

    setUploading(true)
    setUploadFeedback(null)

    // Parse ACL employee IDs from comma/space separated input
    const aclIds = aclInput
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean)

    try {
      const res = await uploadDocument(selectedFile, aclIds)
      setUploadFeedback({
        type: res.warnings && res.warnings.length > 0 ? 'warning' : 'success',
        message: `"${res.document.original_filename}" uploaded successfully and queued for extraction.`,
        warnings: res.warnings,
      })
      setSelectedFile(null)
      setAclInput('')
      if (fileInputRef.current) fileInputRef.current.value = ''
      await loadDocuments(true)
    } catch (err) {
      setUploadFeedback({
        type: 'error',
        message: err instanceof Error ? err.message : 'Upload failed',
      })
    } finally {
      setUploading(false)
    }
  }

  const handleDownload = async (doc: DocumentItem) => {
    setDownloadingId(doc.id)
    try {
      await downloadDocument(doc.id, doc.original_filename)
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Download failed')
    } finally {
      setDownloadingId(null)
    }
  }

  // Handle Start Over (Reset all uploaded documents)
  const handleStartOver = async () => {
    setIsResetting(true)
    try {
      const res = await resetAllDocuments()
      setShowResetModal(false)
      setUploadFeedback({
        type: 'success',
        message: `Start Over complete: ${res.removed_count} document(s) removed. You have a fresh slate.`,
      })
      await loadDocuments(false)
    } catch (err) {
      alert(err instanceof Error ? err.message : 'Failed to reset documents')
    } finally {
      setIsResetting(false)
    }
  }

  // Handle document replacement
  const handleReplaceDocument = async () => {
    if (!managingDoc || !replacementFile) return
    setIsReplacing(true)
    setManageError('')

    const aclIds = replacementAcl
      .split(/[\s,]+/)
      .map((s) => s.trim())
      .filter(Boolean)

    try {
      const res = await replaceDocument(managingDoc.id, replacementFile, aclIds)
      setUploadFeedback({
        type: res.warnings && res.warnings.length > 0 ? 'warning' : 'success',
        message: `"${res.document.original_filename}" replaced successfully and re-queued for extraction.`,
        warnings: res.warnings,
      })
      setManagingDoc(null)
      setReplacementFile(null)
      await loadDocuments(true)
    } catch (err) {
      setManageError(err instanceof Error ? err.message : 'Replacement failed')
    } finally {
      setIsReplacing(false)
    }
  }

  // Handle document deletion
  const handleDeleteDoc = async () => {
    if (!managingDoc) return
    if (!window.confirm(`Are you sure you want to permanently delete "${managingDoc.original_filename}"?`)) {
      return
    }
    setIsDeleting(true)
    setManageError('')
    try {
      await deleteDocument(managingDoc.id)
      setUploadFeedback({
        type: 'success',
        message: `"${managingDoc.original_filename}" has been deleted.`,
      })
      setManagingDoc(null)
      await loadDocuments(true)
    } catch (err) {
      setManageError(err instanceof Error ? err.message : 'Delete failed')
    } finally {
      setIsDeleting(false)
    }
  }

  const filteredDocuments = useMemo(() => {
    return documents.filter((doc) => {
      const matchesSearch =
        searchQuery === '' ||
        doc.original_filename.toLowerCase().includes(searchQuery.toLowerCase())
      const matchesStatus =
        statusFilter === 'all' || doc.status === statusFilter
      return matchesSearch && matchesStatus
    })
  }, [documents, searchQuery, statusFilter])

  const counts = useMemo(() => {
    return {
      total: documents.length,
      ready: documents.filter((d) => d.status === 'ready').length,
      processing: documents.filter(
        (d) => d.status === 'uploaded' || d.status === 'processing',
      ).length,
      failed: documents.filter((d) => d.status === 'failed').length,
    }
  }, [documents])

  if (user && !isAuthorized) {
    return <AccessDenied />
  }

  return (
    <div className="page-stack">
      {/* Hero Card */}
      <section className="hero-card" aria-labelledby="documents-title">
        <div>
          <p className="eyebrow light">Epic 7 &bull; HR & Manager Portal</p>
          <h2 id="documents-title">Company Files & Knowledge Ingestion (RAG)</h2>
          <p>
            Upload company policies, handbooks, SOPs, spreadsheets, and scanned documents.
            Files are automatically parsed, OCR-processed, and studied by the AI Assistant
            and Corporate Chatbox to answer team questions with citations.
          </p>
        </div>
        <div className="hero-actions">
          <button
            type="button"
            className="button"
            style={{
              background: '#fff',
              border: '1px solid #f87171',
              color: '#b91c1c',
              display: 'inline-flex',
              alignItems: 'center',
              gap: '6px',
              fontWeight: 600,
            }}
            onClick={() => setShowResetModal(true)}
            disabled={loading || isResetting}
            title="Start fresh: remove all uploaded documents"
          >
            <RotateCcw size={16} className={isResetting ? 'animate-spin' : ''} />
            Start Over
          </button>
        </div>
      </section>

      {/* Metrics Row */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '16px' }}>
        <div style={{ background: '#fff', border: '1px solid var(--border)', borderRadius: '12px', padding: '16px' }}>
          <div className="eyebrow">Total Documents</div>
          <div style={{ fontSize: '26px', fontWeight: '800', marginTop: '6px' }}>{counts.total}</div>
        </div>
        <div style={{ background: '#fff', border: '1px solid var(--border)', borderRadius: '12px', padding: '16px' }}>
          <div className="eyebrow" style={{ color: '#0c754b' }}>Ready for RAG</div>
          <div style={{ fontSize: '26px', fontWeight: '800', marginTop: '6px', color: '#0c754b' }}>{counts.ready}</div>
        </div>
        <div style={{ background: '#fff', border: '1px solid var(--border)', borderRadius: '12px', padding: '16px' }}>
          <div className="eyebrow" style={{ color: '#2167d5' }}>Processing / Queued</div>
          <div style={{ fontSize: '26px', fontWeight: '800', marginTop: '6px', color: '#2167d5' }}>{counts.processing}</div>
        </div>
        <div style={{ background: '#fff', border: '1px solid var(--border)', borderRadius: '12px', padding: '16px' }}>
          <div className="eyebrow" style={{ color: '#a83232' }}>Processing Failed</div>
          <div style={{ fontSize: '26px', fontWeight: '800', marginTop: '6px', color: '#a83232' }}>{counts.failed}</div>
        </div>
      </div>

      {/* Document Upload Card */}
      <section
        style={{
          background: '#fff',
          border: '1px solid var(--border)',
          borderRadius: '12px',
          padding: '24px',
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '16px' }}>
          <UploadCloud className="text-blue-600" size={24} />
          <div>
            <h3 style={{ margin: 0, fontSize: '18px', fontWeight: '700' }}>Upload New Knowledge Document</h3>
            <p style={{ margin: '2px 0 0', color: 'var(--muted)', fontSize: '13px' }}>
              Supported: PDF, DOCX, PPTX, XLSX, PNG, JPG, JPEG, TIFF, BMP (Max 50 MB)
            </p>
          </div>
        </div>

        <form onSubmit={handleUpload}>
          {/* Dropzone */}
          <div
            onDragOver={(e) => e.preventDefault()}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
            style={{
              border: '2px dashed #bfd0e5',
              borderRadius: '10px',
              padding: '32px 20px',
              textAlign: 'center',
              cursor: 'pointer',
              background: '#f8fafd',
              transition: 'border-color 0.2s',
            }}
          >
            <input
              ref={fileInputRef}
              type="file"
              onChange={handleFileSelect}
              style={{ display: 'none' }}
              accept=".pdf,.docx,.pptx,.xlsx,.png,.jpg,.jpeg,.tiff,.tif,.bmp,.gif"
            />
            <UploadCloud size={40} style={{ margin: '0 auto 10px', color: '#3875c5' }} />
            {selectedFile ? (
              <div>
                <strong style={{ fontSize: '15px', color: '#0f2d52' }}>{selectedFile.name}</strong>
                <p style={{ margin: '4px 0 0', color: 'var(--muted)', fontSize: '13px' }}>
                  {formatFileSize(selectedFile.size)} &bull; Ready to upload
                </p>
              </div>
            ) : (
              <div>
                <p style={{ margin: 0, fontWeight: '600', color: '#0f2d52' }}>
                  Click to select or drag and drop your document here
                </p>
                <p style={{ margin: '4px 0 0', color: 'var(--muted)', fontSize: '12px' }}>
                  PDF documents, Office files, or scanned images for OCR
                </p>
              </div>
            )}
          </div>

          {/* ACL input */}
          <div style={{ marginTop: '18px' }}>
            <label
              htmlFor="acl-input"
              style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', fontWeight: '600', marginBottom: '6px' }}
            >
              <Shield size={15} />
              Access Control List (Optional)
            </label>
            <input
              id="acl-input"
              type="text"
              placeholder="e.g. emp-102, emp-105 (comma-separated employee IDs)"
              value={aclInput}
              onChange={(e) => setAclInput(e.target.value)}
              style={{
                width: '100%',
                padding: '9px 12px',
                border: '1px solid var(--border)',
                borderRadius: '8px',
                fontSize: '13px',
              }}
            />
            <small style={{ color: 'var(--muted)', fontSize: '12px', display: 'block', marginTop: '4px' }}>
              You ({user?.name || 'Current User'}) are automatically the owner. Add other employee IDs to grant them view and download permissions.
            </small>
          </div>

          {/* Upload Button */}
          <div style={{ marginTop: '18px', display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
            {selectedFile && (
              <button
                type="button"
                className="button secondary"
                onClick={() => {
                  setSelectedFile(null)
                  if (fileInputRef.current) fileInputRef.current.value = ''
                }}
              >
                Cancel
              </button>
            )}
            <button
              type="submit"
              className="button primary"
              disabled={!selectedFile || uploading}
              style={{ minWidth: '140px' }}
            >
              {uploading ? (
                <>
                  <RefreshCw size={15} className="animate-spin" />
                  Uploading...
                </>
              ) : (
                'Upload & Ingest'
              )}
            </button>
          </div>
        </form>

        {/* Feedback Alert */}
        {uploadFeedback && (
          <div
            style={{
              marginTop: '16px',
              padding: '12px 16px',
              borderRadius: '8px',
              background:
                uploadFeedback.type === 'error'
                  ? '#fde8e8'
                  : uploadFeedback.type === 'warning'
                  ? '#fef6e7'
                  : '#eaf8f1',
              color:
                uploadFeedback.type === 'error'
                  ? '#9b1c1c'
                  : uploadFeedback.type === 'warning'
                  ? '#8f5200'
                  : '#0e6245',
              border: `1px solid ${
                uploadFeedback.type === 'error'
                  ? '#f8b4b4'
                  : uploadFeedback.type === 'warning'
                  ? '#f5d59f'
                  : '#a3e0c4'
              }`,
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                {uploadFeedback.type === 'error' ? (
                  <AlertCircle size={18} />
                ) : uploadFeedback.type === 'warning' ? (
                  <AlertTriangle size={18} />
                ) : (
                  <CheckCircle2 size={18} />
                )}
                <span style={{ fontWeight: '600', fontSize: '13px' }}>{uploadFeedback.message}</span>
              </div>
              <button
                type="button"
                onClick={() => setUploadFeedback(null)}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'inherit' }}
              >
                <X size={16} />
              </button>
            </div>
            {uploadFeedback.warnings && uploadFeedback.warnings.length > 0 && (
              <ul style={{ margin: '6px 0 0 24px', padding: 0, fontSize: '12px' }}>
                {uploadFeedback.warnings.map((w, i) => (
                  <li key={i}>{w}</li>
                ))}
              </ul>
            )}
          </div>
        )}
      </section>

      {/* Filter and Search Bar */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '12px' }}>
        <div style={{ display: 'flex', gap: '8px' }}>
          {(['all', 'ready', 'processing', 'failed'] as const).map((status) => (
            <button
              key={status}
              type="button"
              className={`button ${statusFilter === status ? 'primary' : 'secondary'}`}
              style={{ textTransform: 'capitalize', minHeight: '34px', fontSize: '12px', padding: '0 12px' }}
              onClick={() => setStatusFilter(status)}
            >
              {status === 'all' ? `All (${counts.total})` : status === 'ready' ? `Ready (${counts.ready})` : status === 'processing' ? `Processing (${counts.processing})` : `Failed (${counts.failed})`}
            </button>
          ))}
        </div>

        <div style={{ position: 'relative', minWidth: '260px' }}>
          <Search size={16} style={{ position: 'absolute', left: '10px', top: '10px', color: 'var(--muted)' }} />
          <input
            type="text"
            placeholder="Search documents by name..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              width: '100%',
              padding: '7px 10px 7px 32px',
              border: '1px solid var(--border)',
              borderRadius: '8px',
              fontSize: '13px',
            }}
          />
        </div>
      </div>

      {/* Document List Table */}
      <section style={{ background: '#fff', border: '1px solid var(--border)', borderRadius: '12px', overflow: 'hidden' }}>
        {loading ? (
          <div style={{ padding: '40px', textAlign: 'center', color: 'var(--muted)' }}>
            <RefreshCw size={24} className="animate-spin" style={{ margin: '0 auto 8px' }} />
            Loading documents...
          </div>
        ) : error ? (
          <div style={{ padding: '30px', textAlign: 'center', color: '#9b1c1c' }}>
            <AlertCircle size={24} style={{ margin: '0 auto 8px' }} />
            {error}
          </div>
        ) : filteredDocuments.length === 0 ? (
          <div style={{ padding: '50px 20px', textAlign: 'center', color: 'var(--muted)' }}>
            <FileText size={36} style={{ margin: '0 auto 12px', opacity: 0.5 }} />
            <h4 style={{ margin: 0, fontSize: '16px', color: '#0f2d52' }}>No documents found</h4>
            <p style={{ margin: '4px 0 0', fontSize: '13px' }}>
              {searchQuery || statusFilter !== 'all'
                ? 'Try adjusting your search query or status filter.'
                : 'Upload your first company knowledge document above.'}
            </p>
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '13px' }}>
              <thead>
                <tr style={{ background: '#f8fafd', borderBottom: '1px solid var(--border)', color: 'var(--muted)', fontSize: '11px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
                  <th style={{ padding: '14px 20px' }}>Document</th>
                  <th style={{ padding: '14px 16px' }}>Size</th>
                  <th style={{ padding: '14px 16px' }}>Uploaded</th>
                  <th style={{ padding: '14px 16px' }}>Pages / Structure</th>
                  <th style={{ padding: '14px 16px' }}>Access Control</th>
                  <th style={{ padding: '14px 16px' }}>Status</th>
                  <th style={{ padding: '14px 20px', textAlign: 'right' }}>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredDocuments.map((doc) => {
                  const isOwner = user?.id === doc.owner_id
                  return (
                    <tr
                      key={doc.id}
                      style={{
                        borderBottom: '1px solid var(--border)',
                        transition: 'background 0.15s',
                      }}
                    >
                      {/* Name & MIME */}
                      <td style={{ padding: '14px 20px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                          <div style={{ padding: '6px', borderRadius: '8px', background: '#f1f5fa' }}>
                            {getFileIcon(doc.mime_type)}
                          </div>
                          <div>
                            <div style={{ fontWeight: '600', color: '#0f2d52' }}>
                              {doc.original_filename}
                            </div>
                            <div style={{ color: 'var(--muted)', fontSize: '11px', marginTop: '2px' }}>
                              {doc.mime_type}
                            </div>
                          </div>
                        </div>
                      </td>

                      {/* Size */}
                      <td style={{ padding: '14px 16px', color: '#334155' }}>
                        {formatFileSize(doc.file_size)}
                      </td>

                      {/* Uploaded At */}
                      <td style={{ padding: '14px 16px', color: 'var(--muted)', fontSize: '12px' }}>
                        {formatDate(doc.created_at)}
                      </td>

                      {/* Page Count */}
                      <td style={{ padding: '14px 16px', color: '#334155' }}>
                        {doc.page_count !== null && doc.page_count !== undefined ? (
                          <span>{doc.page_count} {doc.mime_type.includes('presentation') ? 'slides' : doc.mime_type.includes('spreadsheet') ? 'sheets' : 'pages'}</span>
                        ) : (
                          <span style={{ color: 'var(--muted)' }}>—</span>
                        )}
                      </td>

                      {/* Access Control */}
                      <td style={{ padding: '14px 16px' }}>
                        {doc.acl && doc.acl.length > 0 ? (
                          <span
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '5px',
                              padding: '3px 8px',
                              borderRadius: '6px',
                              background: '#eaf2ff',
                              color: '#1a56a7',
                              fontSize: '11px',
                              fontWeight: '600',
                            }}
                          >
                            <Users size={12} />
                            Shared ({doc.acl.length})
                          </span>
                        ) : (
                          <span
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '5px',
                              padding: '3px 8px',
                              borderRadius: '6px',
                              background: '#f1f5f9',
                              color: '#475569',
                              fontSize: '11px',
                              fontWeight: '600',
                            }}
                          >
                            <Lock size={12} />
                            Private
                          </span>
                        )}
                      </td>

                      {/* Status */}
                      <td style={{ padding: '14px 16px' }}>
                        {doc.status === 'ready' ? (
                          <span
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '5px',
                              padding: '3px 8px',
                              borderRadius: '6px',
                              background: '#def7ec',
                              color: '#03543f',
                              fontSize: '11px',
                              fontWeight: '700',
                            }}
                          >
                            <CheckCircle2 size={12} />
                            Ready
                          </span>
                        ) : doc.status === 'processing' || doc.status === 'uploaded' ? (
                          <span
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '5px',
                              padding: '3px 8px',
                              borderRadius: '6px',
                              background: '#e1effe',
                              color: '#1e429f',
                              fontSize: '11px',
                              fontWeight: '700',
                            }}
                          >
                            <RefreshCw size={12} className="animate-spin" />
                            {doc.status === 'processing' ? 'Extracting' : 'Queued'}
                          </span>
                        ) : (
                          <div>
                            <span
                              style={{
                                display: 'inline-flex',
                                alignItems: 'center',
                                gap: '5px',
                                padding: '3px 8px',
                                borderRadius: '6px',
                                background: '#fde8e8',
                                color: '#9b1c1c',
                                fontSize: '11px',
                                fontWeight: '700',
                              }}
                              title={doc.failure_reason || 'Processing failed'}
                            >
                              <AlertCircle size={12} />
                              Failed
                            </span>
                            {doc.failure_reason && (
                              <div
                                style={{
                                  fontSize: '11px',
                                  color: '#a83232',
                                  marginTop: '4px',
                                  maxWidth: '220px',
                                  overflow: 'hidden',
                                  textOverflow: 'ellipsis',
                                  whiteSpace: 'nowrap',
                                }}
                                title={doc.failure_reason}
                              >
                                {doc.failure_reason}
                              </div>
                            )}
                          </div>
                        )}
                      </td>

                      {/* Actions */}
                      <td style={{ padding: '14px 20px', textAlign: 'right' }}>
                        <div style={{ display: 'inline-flex', alignItems: 'center', gap: '8px', justifyContent: 'flex-end' }}>
                          <button
                            type="button"
                            className="button secondary"
                            style={{ minHeight: '32px', padding: '0 10px', fontSize: '12px' }}
                            onClick={() => handleDownload(doc)}
                            disabled={downloadingId === doc.id}
                            title="Download original file"
                          >
                            {downloadingId === doc.id ? (
                              <RefreshCw size={14} className="animate-spin" />
                            ) : (
                              <Download size={14} />
                            )}
                            Download
                          </button>
                          <button
                            type="button"
                            className="button primary"
                            style={{
                              minHeight: '32px',
                              padding: '0 12px',
                              fontSize: '12px',
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '6px',
                            }}
                            onClick={() => {
                              setManagingDoc(doc)
                              setReplacementFile(null)
                              setReplacementAcl(doc.acl ? doc.acl.map((a) => a.employee_id).join(', ') : '')
                              setManageError('')
                            }}
                            title="Manage document (edit/replace file or delete)"
                          >
                            <FileEdit size={14} />
                            Manage
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {/* Start Over Confirmation Modal */}
      {showResetModal && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(15, 23, 42, 0.6)',
            backdropFilter: 'blur(3px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1100,
            padding: '16px',
          }}
          role="dialog"
          aria-modal="true"
          aria-labelledby="reset-modal-title"
        >
          <div
            style={{
              background: '#ffffff',
              borderRadius: '16px',
              maxWidth: '460px',
              width: '100%',
              boxShadow: '0 20px 40px rgba(0,0,0,0.25)',
              border: '1px solid var(--border)',
              padding: '24px',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '14px', color: '#c81e1e' }}>
              <AlertTriangle size={24} />
              <h3 id="reset-modal-title" style={{ margin: 0, fontSize: '18px', fontWeight: 700 }}>
                Start Over & Reset All Documents?
              </h3>
            </div>
            <p style={{ margin: '0 0 16px', fontSize: '13px', color: '#334155', lineHeight: 1.5 }}>
              Are you sure you want to start over? All uploaded knowledge documents will be permanently removed from disk and database so you can start completely fresh.
            </p>
            <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '10px' }}>
              <button
                type="button"
                className="button secondary"
                onClick={() => setShowResetModal(false)}
                disabled={isResetting}
              >
                Cancel
              </button>
              <button
                type="button"
                className="button"
                style={{
                  background: '#c81e1e',
                  color: '#fff',
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: '6px',
                  fontWeight: 600,
                }}
                onClick={handleStartOver}
                disabled={isResetting}
              >
                <RotateCcw size={15} className={isResetting ? 'animate-spin' : ''} />
                {isResetting ? 'Removing...' : 'Yes, Start Over'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Manage Document Modal */}
      {managingDoc && (
        <div
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(15, 23, 42, 0.6)',
            backdropFilter: 'blur(3px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1100,
            padding: '16px',
          }}
          role="dialog"
          aria-modal="true"
          aria-labelledby="manage-modal-title"
          onClick={() => !isReplacing && !isDeleting && setManagingDoc(null)}
        >
          <div
            style={{
              background: '#ffffff',
              borderRadius: '16px',
              maxWidth: '560px',
              width: '100%',
              boxShadow: '0 20px 40px rgba(0,0,0,0.25)',
              border: '1px solid var(--border)',
              overflow: 'hidden',
            }}
            onClick={(e) => e.stopPropagation()}
          >
            {/* Modal Header */}
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                alignItems: 'center',
                padding: '18px 24px',
                borderBottom: '1px solid var(--border)',
                background: '#f8fafd',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
                <FileEdit className="text-blue-600" size={20} />
                <h3 id="manage-modal-title" style={{ margin: 0, fontSize: '17px', fontWeight: 700, color: 'var(--navy)' }}>
                  Manage Document
                </h3>
              </div>
              <button
                type="button"
                onClick={() => setManagingDoc(null)}
                disabled={isReplacing || isDeleting}
                style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--muted)' }}
                aria-label="Close"
              >
                <X size={20} />
              </button>
            </div>

            {/* Modal Body */}
            <div style={{ padding: '24px', maxHeight: 'calc(90vh - 120px)', overflowY: 'auto' }}>
              {/* Document summary info */}
              <div
                style={{
                  background: '#f1f5fa',
                  borderRadius: '10px',
                  padding: '14px 16px',
                  marginBottom: '20px',
                  fontSize: '13px',
                }}
              >
                <div style={{ fontWeight: 700, color: '#0f2d52', fontSize: '14px', wordBreak: 'break-all' }}>
                  {managingDoc.original_filename}
                </div>
                <div style={{ color: 'var(--muted)', marginTop: '4px', display: 'flex', gap: '16px', flexWrap: 'wrap' }}>
                  <span>Size: {formatFileSize(managingDoc.file_size)}</span>
                  <span>Uploaded: {formatDate(managingDoc.created_at)}</span>
                  <span>Status: <strong>{managingDoc.status}</strong></span>
                </div>
              </div>

              {manageError && (
                <div
                  style={{
                    padding: '10px 14px',
                    borderRadius: '8px',
                    background: '#fde8e8',
                    color: '#9b1c1c',
                    fontSize: '13px',
                    marginBottom: '16px',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px',
                  }}
                >
                  <AlertCircle size={16} />
                  {manageError}
                </div>
              )}

              {/* Section: Edit / Replace Document */}
              <div style={{ marginBottom: '24px' }}>
                <h4 style={{ margin: '0 0 6px', fontSize: '14px', fontWeight: 700, color: '#0f2d52' }}>
                  Upload Replacement File & Re-process
                </h4>
                <p style={{ margin: '0 0 12px', fontSize: '12px', color: 'var(--muted)' }}>
                  Uploaded the wrong file? Select the correct document below. It will replace the existing file and automatically re-run parsing and OCR for the AI Assistant and Corporate Chatbox.
                </p>

                <div
                  onClick={() => manageFileInputRef.current?.click()}
                  style={{
                    border: '2px dashed #bfd0e5',
                    borderRadius: '8px',
                    padding: '20px',
                    textAlign: 'center',
                    cursor: 'pointer',
                    background: '#fcfdfd',
                  }}
                >
                  <input
                    ref={manageFileInputRef}
                    type="file"
                    onChange={(e) => {
                      if (e.target.files && e.target.files[0]) {
                        setReplacementFile(e.target.files[0])
                        setManageError('')
                      }
                    }}
                    style={{ display: 'none' }}
                    accept=".pdf,.docx,.pptx,.xlsx,.png,.jpg,.jpeg,.tiff,.tif,.bmp,.gif"
                  />
                  <UploadCloud size={28} style={{ margin: '0 auto 6px', color: '#3875c5' }} />
                  {replacementFile ? (
                    <div>
                      <strong style={{ fontSize: '13px', color: '#0f2d52' }}>{replacementFile.name}</strong>
                      <p style={{ margin: '2px 0 0', color: 'var(--muted)', fontSize: '12px' }}>
                        {formatFileSize(replacementFile.size)} &bull; Click to change
                      </p>
                    </div>
                  ) : (
                    <div>
                      <span style={{ fontSize: '13px', fontWeight: 600, color: '#0f2d52' }}>
                        Click to choose new replacement file
                      </span>
                      <p style={{ margin: '2px 0 0', color: 'var(--muted)', fontSize: '11px' }}>
                        PDF, Word, Excel, PowerPoint, or scanned images
                      </p>
                    </div>
                  )}
                </div>

                {/* ACL input */}
                <div style={{ marginTop: '12px' }}>
                  <label
                    htmlFor="manage-acl-input"
                    style={{ display: 'block', fontSize: '12px', fontWeight: 600, marginBottom: '4px' }}
                  >
                    Access Control List (Optional)
                  </label>
                  <input
                    id="manage-acl-input"
                    type="text"
                    placeholder="e.g. emp-102, emp-105"
                    value={replacementAcl}
                    onChange={(e) => setReplacementAcl(e.target.value)}
                    style={{
                      width: '100%',
                      padding: '8px 10px',
                      border: '1px solid var(--border)',
                      borderRadius: '6px',
                      fontSize: '13px',
                    }}
                  />
                </div>

                <button
                  type="button"
                  className="button primary"
                  style={{ marginTop: '14px', width: '100%', justifyContent: 'center' }}
                  disabled={!replacementFile || isReplacing || isDeleting}
                  onClick={handleReplaceDocument}
                >
                  {isReplacing ? (
                    <>
                      <RefreshCw size={16} className="animate-spin" />
                      Uploading & Processing...
                    </>
                  ) : (
                    'Save & Process Replacement'
                  )}
                </button>
              </div>

              {/* Section: Danger Zone (Delete) */}
              <div style={{ borderTop: '1px solid var(--border)', paddingTop: '18px' }}>
                <h4 style={{ margin: '0 0 4px', fontSize: '13px', fontWeight: 700, color: '#c81e1e' }}>
                  Danger Zone
                </h4>
                <p style={{ margin: '0 0 10px', fontSize: '12px', color: 'var(--muted)' }}>
                  Remove this file permanently from company knowledge.
                </p>
                <button
                  type="button"
                  className="button"
                  style={{
                    background: '#fde8e8',
                    border: '1px solid #f8b4b4',
                    color: '#9b1c1c',
                    fontSize: '12px',
                    display: 'inline-flex',
                    alignItems: 'center',
                    gap: '6px',
                  }}
                  disabled={isReplacing || isDeleting}
                  onClick={handleDeleteDoc}
                >
                  <Trash2 size={14} />
                  {isDeleting ? 'Deleting...' : 'Delete Document'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
