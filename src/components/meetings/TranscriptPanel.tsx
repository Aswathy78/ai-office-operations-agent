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
  isOrganizer: boolean
  isOpen: boolean
  onClose: () => void
}

function formatTimestamp(seconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(safeSeconds / 3600)
  const minutes = Math.floor((safeSeconds % 3600) / 60)
  const remainingSeconds = safeSeconds % 60

  if (hours > 0) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(remainingSeconds).padStart(2, '0')}`
  }
  return `${minutes}:${String(remainingSeconds).padStart(2, '0')}`
}

function formatCorrectionTimestamp(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'Time unavailable'
  return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(date)
}

function safeErrorMessage(error: unknown): string {
  if (error instanceof MeetingApiError && error.status === 404) {
    return 'Transcript is not available yet.'
  }
  return 'Unable to load the transcript. Check your connection and try again.'
}

function TranscriptSegmentRow({
  segment,
  index,
  isOrganizer,
  saving,
  onSave,
}: {
  segment: TranscriptSegment
  index: number
  isOrganizer: boolean
  saving: boolean
  onSave: (index: number, text: string, speaker: string) => Promise<boolean>
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
    <article className="meet-transcript-segment" aria-label={`Transcript segment ${index + 1}`}>
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

export function TranscriptPanel({
  meetingId,
  organizerId,
  isOrganizer,
  isOpen,
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

  const loadTranscript = useCallback(async () => {
    setLoading(true)
    setLoadError('')
    try {
      const result = await getMeetingTranscript(meetingId, organizerId)
      setTranscript(result)
    } catch (error) {
      setTranscript(null)
      setLoadError(safeErrorMessage(error))
    } finally {
      setLoading(false)
    }
  }, [meetingId, organizerId])

  useEffect(() => {
    if (!isOpen || !meetingId || !organizerId) return
    let active = true
    setTranscript(null)
    setLoading(true)
    setLoadError('')
    void getMeetingTranscript(meetingId, organizerId)
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
  }, [isOpen, meetingId, organizerId])

  useEffect(() => {
    if (!isOpen || !isOrganizer || !showHistory) return
    let active = true
    setHistoryLoading(true)
    setHistoryError('')
    setHistoryLoaded(false)
    setCorrections([])
    void getMeetingTranscriptCorrections(meetingId, organizerId)
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

  const status = transcript?.status.toLowerCase()
  const isCompleted = status === 'completed'
  const isProcessing = status === 'processing' || status === 'pending'
  const isFailed = status === 'failed'

  async function saveSegment(index: number, text: string, speaker: string): Promise<boolean> {
    if (!transcript) return false
    const nextSegments = transcript.segments.map((segment, segmentIndex) => ({
      text: segmentIndex === index ? text : segment.text,
      speaker: segmentIndex === index ? speaker : segment.speaker,
    }))

    setSaving(true)
    setSaveError('')
    try {
      const updated = await updateMeetingTranscript(meetingId, organizerId, nextSegments)
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
    <aside className="meet-side-panel meet-transcript-panel" aria-label="Meeting transcript">
      <div className="meet-side-panel-header">
        <div className="meet-transcript-panel-title">
          <FileText size={17} aria-hidden="true" />
          <h2>Transcript</h2>
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
      ) : transcript && isProcessing ? (
        <p className="meet-transcript-state" role="status">
          Transcription is in progress. Refresh the transcript to check for updates.
          <button className="meet-transcript-action" type="button" onClick={() => void loadTranscript()}>
            <RefreshCw size={13} aria-hidden="true" /> Refresh
          </button>
        </p>
      ) : transcript && isFailed ? (
        <p className="meet-transcript-state" role="alert">
          Transcription failed. The transcript is unavailable.
          <button className="meet-transcript-action" type="button" onClick={() => void loadTranscript()}>
            <RefreshCw size={13} aria-hidden="true" /> Retry
          </button>
        </p>
      ) : transcript && isCompleted ? (
        <>
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

          {saveError && <p className="meet-transcript-error" role="alert">{saveError}</p>}

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
                      segment={segment}
                      index={index}
                      isOrganizer={isOrganizer && isCompleted}
                      saving={saving}
                      onSave={saveSegment}
                    />
                  ))}
                </div>
              )}
            </div>
          )}
        </>
      ) : (
        <p className="meet-transcript-state" role="status">Transcript is unavailable.</p>
      )}
    </aside>
  )
}
