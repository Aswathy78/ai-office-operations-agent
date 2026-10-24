import { useCallback, useEffect, useMemo, useState } from 'react'
import { Clock3, FileText, History, Pencil, RefreshCw, Save, Search, X } from 'lucide-react'
import {
  getMeetingTranscript,
  getMeetingTranscriptCorrections,
  MeetingApiError,
  updateMeetingTranscript,
} from '../../api/meetings'
import type {
  MeetingTranscript,
  TranscriptCorrection,
  TranscriptSegment,
} from '../../types/meeting'

interface TranscriptPanelProps {
  meetingId: string
  organizerId: string
  transcriptId?: string | null
  employeeEmail?: string
  isOrganizer: boolean
  isOpen: boolean
  inline?: boolean
  focusSegmentIndex?: number | null
  focusRequestId?: number
  onClose: () => void
}

function formatTimestamp(seconds: number): string {
  const centiseconds = Math.round(Math.max(0, seconds) * 100)
  const wholeSeconds = Math.floor(centiseconds / 100)
  const hours = Math.floor(wholeSeconds / 3600)
  const minutes = Math.floor((wholeSeconds % 3600) / 60)
  const remainingSeconds = wholeSeconds % 60
  const fractionalCentiseconds = centiseconds % 100
  const secondsLabel = `${String(remainingSeconds).padStart(2, '0')}${
    fractionalCentiseconds > 0
      ? `.${String(fractionalCentiseconds).padStart(2, '0')}`
      : ''
  }`

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${secondsLabel}`
  }
  return `${minutes}:${secondsLabel}`
}

function formatCorrectionTimestamp(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Time unavailable'
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function safeErrorMessage(error: unknown): string {
  if (error instanceof MeetingApiError && error.status === 404) {
    return 'Transcript not available.'
  }
  return 'Unable to load the transcript. Check your connection and try again.'
}

function TranscriptSegmentRow({
  id,
  segment,
  index,
  isOrganizer,
  saving,
  onSave,
  highlighted,
}: {
  id: string
  segment: TranscriptSegment
  index: number
  isOrganizer: boolean
  saving: boolean
  onSave: (index: number, text: string, speaker: string) => Promise<boolean>
  highlighted: boolean
}) {
  const [editing, setEditing] = useState(false)
  const [textDraft, setTextDraft] = useState(segment.text)
  const [speakerDraft, setSpeakerDraft] = useState(segment.speaker)
  const [savingThisSegment, setSavingThisSegment] = useState(false)

  useEffect(() => {
    if (!editing) {
      setTextDraft(segment.text)
      setSpeakerDraft(segment.speaker)
    }
  }, [editing, segment.speaker, segment.text])

  async function saveEdit() {
    setSavingThisSegment(true)
    try {
      if (await onSave(index, textDraft, speakerDraft)) setEditing(false)
    } finally {
      setSavingThisSegment(false)
    }
  }

  function cancelEdit() {
    setEditing(false)
    setTextDraft(segment.text)
    setSpeakerDraft(segment.speaker)
  }

  return (
    <article
      id={id}
      className={`meet-transcript-segment${highlighted ? ' is-source-highlighted' : ''}`}
      aria-label={`Transcript segment ${index + 1}`}
    >
      <div className="meet-transcript-segment-meta">
        <span className="meet-transcript-speaker">{segment.speaker || 'Unknown speaker'}</span>
        <span className="meet-transcript-time">
          <Clock3 size={12} aria-hidden="true" />
          <time>{formatTimestamp(segment.start)}–{formatTimestamp(segment.end)}</time>
        </span>
      </div>

      {editing ? (
        <div className="meet-transcript-edit-fields">
          <label>
            <span>Transcript text</span>
            <textarea
              aria-label={`Edit transcript text for segment ${index + 1}`}
              value={textDraft}
              onChange={(event) => setTextDraft(event.target.value)}
              rows={3}
              disabled={savingThisSegment || saving}
            />
          </label>
          <label>
            <span>Speaker</span>
            <input
              aria-label={`Edit speaker for segment ${index + 1}`}
              value={speakerDraft}
              onChange={(event) => setSpeakerDraft(event.target.value)}
              maxLength={255}
              disabled={savingThisSegment || saving}
            />
          </label>
          <div className="meet-transcript-edit-actions">
            <button
              className="meet-transcript-action meet-transcript-save"
              type="button"
              onClick={() => void saveEdit()}
              disabled={savingThisSegment || saving || !textDraft.trim() || !speakerDraft.trim()}
            >
              <Save size={14} aria-hidden="true" />
              {savingThisSegment ? 'Saving…' : 'Save'}
            </button>
            <button
              className="meet-transcript-action"
              type="button"
              onClick={cancelEdit}
              disabled={savingThisSegment || saving}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="meet-transcript-segment-content">
          <p>{segment.text}</p>
          {segment.flagged_for_correction && (
            <span className="meet-transcript-flag">Flagged for correction</span>
          )}
          {isOrganizer && (
            <button
              className="meet-transcript-action meet-transcript-edit"
              type="button"
              aria-label={`Edit segment ${index + 1}`}
              disabled={saving}
              onClick={() => {
                setTextDraft(segment.text)
                setSpeakerDraft(segment.speaker)
                setEditing(true)
              }}
            >
              <Pencil size={13} aria-hidden="true" /> Edit
            </button>
          )}
        </div>
      )}
    </article>
  )
}

// About two minutes of retries (one every 3 seconds) before giving up.
const MAX_POLL_FAILURES = 40

export function TranscriptPanel({
  meetingId,
  organizerId,
  transcriptId,
  employeeEmail,
  isOrganizer,
  isOpen,
  inline = false,
  focusSegmentIndex = null,
  focusRequestId = 0,
  onClose,
}: TranscriptPanelProps) {
  const [transcript, setTranscript] = useState<MeetingTranscript | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [search, setSearch] = useState('')
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState('')
  const [showHistory, setShowHistory] = useState(false)
  const [corrections, setCorrections] = useState<TranscriptCorrection[]>([])
  const [historyLoading, setHistoryLoading] = useState(false)
  const [historyError, setHistoryError] = useState('')
  const [historyLoaded, setHistoryLoaded] = useState(false)
  const [historyRequestVersion, setHistoryRequestVersion] = useState(0)
  // Consecutive failed polls; a failure retries instead of stopping polling for good.
  const [pollFailures, setPollFailures] = useState(0)
  const [focusedSegment, setFocusedSegment] = useState<{
    index: number
    requestId: number
  } | null>(null)

  const requestTranscript = useCallback(() => {
    if (employeeEmail) {
      return getMeetingTranscript(
        meetingId,
        organizerId,
        transcriptId ?? undefined,
        employeeEmail,
      )
    }
    return transcriptId
      ? getMeetingTranscript(meetingId, organizerId, transcriptId)
      : getMeetingTranscript(meetingId, organizerId)
  }, [employeeEmail, meetingId, organizerId, transcriptId])

  const loadTranscript = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      const result = await requestTranscript()
      setTranscript(result)
    } catch (error) {
      setTranscript(null)
      setLoadError(safeErrorMessage(error))
    } finally {
      setLoading(false)
    }
  }, [requestTranscript])

  useEffect(() => {
    if (!isOpen || !meetingId || !organizerId) return
    let active = true
    setTranscript(null)
    setLoading(true)
    setLoadError('')
    setPollFailures(0)
    const transcriptRequest = requestTranscript()
    void transcriptRequest
      .then((result) => {
        if (active) setTranscript(result)
      })
      .catch((error: unknown) => {
        if (active) {
          setTranscript(null)
          setLoadError(safeErrorMessage(error))
        }
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => {
      active = false
    }
  }, [isOpen, meetingId, organizerId, requestTranscript])

  useEffect(() => {
    if (!isOpen || !isOrganizer || !showHistory) return
    let active = true
    setHistoryLoading(true)
    setHistoryError('')
    setHistoryLoaded(false)
    setCorrections([])
    const correctionsRequest = transcriptId
      ? getMeetingTranscriptCorrections(meetingId, organizerId, transcriptId)
      : getMeetingTranscriptCorrections(meetingId, organizerId)
    void correctionsRequest
      .then((result) => {
        if (active) {
          setCorrections(result)
          setHistoryLoaded(true)
        }
      })
      .catch(() => {
        if (active) setHistoryError('Unable to load correction history. Check your connection and try again.')
      })
      .finally(() => {
        if (active) setHistoryLoading(false)
      })
    return () => {
      active = false
    }
  }, [historyRequestVersion, isOpen, isOrganizer, meetingId, organizerId, showHistory])

  const visibleSegments = useMemo(() => {
    const segments = transcript?.segments ?? []
    const normalizedSearch = search.trim().toLocaleLowerCase()
    if (!normalizedSearch) return segments.map((segment, index) => ({ segment, index }))
    return segments
      .map((segment, index) => ({ segment, index }))
      .filter(({ segment }) => segment.text.toLocaleLowerCase().includes(normalizedSearch))
  }, [search, transcript])

  useEffect(() => {
    if (focusSegmentIndex === null || !isOpen || !transcript) return
    if (focusSegmentIndex < 0 || focusSegmentIndex >= transcript.segments.length) {
      setFocusedSegment(null)
      setSaveError('The referenced transcript segment is not available.')
      return
    }
    setSearch('')
    setShowHistory(false)
    setFocusedSegment({ index: focusSegmentIndex, requestId: focusRequestId })
  }, [focusRequestId, focusSegmentIndex, isOpen, transcript])

  useEffect(() => {
    if (focusedSegment === null || !isOpen || !transcript) return
    requestAnimationFrame(() => {
      document
        .getElementById(`meeting-transcript-segment-${focusedSegment.index}`)
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }, [
    focusedSegment?.index,
    focusedSegment?.requestId,
    isOpen,
    transcript,
    visibleSegments,
  ])

  const status = transcript?.status.toLowerCase()
  const isCompleted = status === 'completed'
  const isPending = status === 'pending'
  const isProcessing = status === 'processing'
  const isFailed = status === 'failed'

  // Keep polling while the transcript is pending/processing, and for a while when
  // the first load failed (the transcript row appears only once the recording upload
  // finishes). A failed poll keeps the last transcript on screen and retries.
  const waitingForFirstLoad = transcript === null && Boolean(loadError) && pollFailures < MAX_POLL_FAILURES
  useEffect(() => {
    if (!isOpen || loading) return
    if (!isPending && !isProcessing && !waitingForFirstLoad) return
    if (pollFailures >= MAX_POLL_FAILURES) return
    let active = true
    const timer = setTimeout(() => {
      const transcriptRequest = requestTranscript()
      void transcriptRequest
        .then((result) => {
          if (active) {
            setTranscript(result)
            setLoadError('')
            setPollFailures(0)
          }
        })
        .catch((error: unknown) => {
          if (active) {
            if (transcript === null) setLoadError(safeErrorMessage(error))
            setPollFailures((count) => count + 1)
          }
        })
    }, 3000)

    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [isOpen, isPending, isProcessing, loading, pollFailures, requestTranscript, transcript, waitingForFirstLoad])

  async function saveSegment(index: number, text: string, speaker: string): Promise<boolean> {
    if (!transcript) return false
    const nextSegments = transcript.segments.map((segment, segmentIndex) => ({
      text: segmentIndex === index ? text : segment.text,
      speaker: segmentIndex === index ? speaker : segment.speaker,
    }))

    setSaving(true)
    setSaveError('')
    try {
      const updated = transcriptId
        ? await updateMeetingTranscript(meetingId, organizerId, nextSegments, transcriptId)
        : await updateMeetingTranscript(meetingId, organizerId, nextSegments)
      setTranscript(updated)
      if (historyLoaded) {
        setHistoryRequestVersion((version) => version + 1)
      }
      return true
    } catch {
      setSaveError('Unable to save transcript changes. Please try again.')
      return false
    } finally {
      setSaving(false)
    }
  }

  if (!isOpen) return null

  return (
    <aside
      id={inline ? 'meeting-transcript-panel' : undefined}
      className={`meet-side-panel meet-transcript-panel${inline ? ' is-inline' : ''}`}
      aria-label="Meeting transcript"
    >
      <div className="meet-side-panel-header">
        <div className="meet-transcript-panel-title">
          <FileText size={17} aria-hidden="true" />
          <h2>{inline ? 'Meeting Transcript' : 'Transcript'}</h2>
        </div>
        <button className="icon-button" onClick={onClose} aria-label="Close transcript panel">
          <X size={16} aria-hidden="true" />
        </button>
      </div>

      {loading ? (
        <p className="meet-transcript-state" role="status">Loading transcript…</p>
      ) : loadError ? (
        <div className="meet-transcript-state">
          <p role={loadError.startsWith('Transcript is not available') ? 'status' : 'alert'}>
            {loadError}
          </p>
          <button className="meet-transcript-action" type="button" onClick={() => void loadTranscript()}>
            <RefreshCw size={13} aria-hidden="true" /> Retry
          </button>
        </div>
      ) : transcript && isPending ? (
        <p className="meet-transcript-state" role="status">
          Transcript is pending.
          <button className="meet-transcript-action" type="button" onClick={() => void loadTranscript()}>
            <RefreshCw size={13} aria-hidden="true" /> Refresh
          </button>
        </p>
      ) : transcript && isProcessing ? (
        <p className="meet-transcript-state" role="status">
          Transcript is being processed.
          <button className="meet-transcript-action" type="button" onClick={() => void loadTranscript()}>
            <RefreshCw size={13} aria-hidden="true" /> Refresh
          </button>
        </p>
      ) : transcript && isFailed ? (
        <p className="meet-transcript-state" role="alert">
          Transcription failed. The transcript is unavailable.
          {transcript.error_message && <span>{transcript.error_message}</span>}
          <button className="meet-transcript-action" type="button" onClick={() => void loadTranscript()}>
            <RefreshCw size={13} aria-hidden="true" /> Retry
          </button>
        </p>
      ) : transcript && isCompleted ? (
        <>
          {saveError && <p className="meet-transcript-error" role="alert">{saveError}</p>}
          <div className="meet-transcript-toolbar">
            <label className="meet-transcript-search">
              <Search size={15} aria-hidden="true" />
              <input
                type="search"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                placeholder="Search transcript"
                aria-label="Search transcript"
              />
            </label>
            {isOrganizer && (
              <button
                className={`meet-transcript-action${showHistory ? ' is-active' : ''}`}
                type="button"
                onClick={() => {
                  setShowHistory((current) => !current)
                  setHistoryError('')
                }}
              >
                <History size={14} aria-hidden="true" />
                {showHistory ? 'Transcript' : 'Corrections'}
              </button>
            )}
          </div>

          {showHistory && isOrganizer ? (
            <section className="meet-transcript-scroll" aria-label="Transcript correction history">
              <h3 className="meet-transcript-subheading">Correction history</h3>
              {historyLoading ? (
                <p className="meet-transcript-state" role="status">Loading correction history…</p>
              ) : historyError ? (
                <div className="meet-transcript-state">
                  <p role="alert">{historyError}</p>
                  <button
                    className="meet-transcript-action"
                    type="button"
                    onClick={() => setHistoryRequestVersion((version) => version + 1)}
                  >
                    <RefreshCw size={13} aria-hidden="true" /> Retry
                  </button>
                </div>
              ) : corrections.length === 0 ? (
                <p className="meet-transcript-state">
                  {historyLoaded ? 'No transcript corrections yet.' : 'Loading correction history…'}
                </p>
              ) : (
                <div className="meet-transcript-history-list">
                  {corrections.map((correction) => (
                    <article className="meet-transcript-correction" key={correction.id}>
                      <p className="meet-transcript-correction-index">
                        Segment {correction.segment_index + 1}
                      </p>
                      <dl>
                        <div><dt>Original text</dt><dd>{correction.original_text}</dd></div>
                        <div><dt>Corrected text</dt><dd>{correction.corrected_text}</dd></div>
                        <div><dt>Original speaker</dt><dd>{correction.original_speaker}</dd></div>
                        <div><dt>Corrected speaker</dt><dd>{correction.corrected_speaker}</dd></div>
                        <div><dt>Editor</dt><dd>{correction.editor_id}</dd></div>
                        <div><dt>Edited</dt><dd>{formatCorrectionTimestamp(correction.created_at)}</dd></div>
                      </dl>
                    </article>
                  ))}
                </div>
              )}
            </section>
          ) : (
            <div className="meet-transcript-scroll">
              {visibleSegments.length === 0 ? (
                <p className="meet-transcript-state" role="status">
                  {search.trim() ? 'No transcript segments match your search.' : 'No transcript segments are available.'}
                </p>
              ) : (
                <div className="meet-transcript-list">
                  {visibleSegments.map(({ segment, index }) => (
                    <TranscriptSegmentRow
                      key={`${index}-${segment.start}`}
                      id={`meeting-transcript-segment-${index}`}
                      segment={segment}
                      index={index}
                      isOrganizer={isOrganizer && isCompleted}
                      saving={saving}
                      onSave={saveSegment}
                      highlighted={focusedSegment?.index === index}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </>
      ) : (
        <p className="meet-transcript-state" role="status">Transcript not available.</p>
      )}
    </aside>
  )
}
