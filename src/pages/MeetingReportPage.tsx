import { useEffect, useState } from 'react'
import { useParams, Link } from 'react-router-dom'
import { ArrowLeft, Clock, Users, FileText } from 'lucide-react'
import { getMeetingReport } from '../api/meetings'
import { TranscriptPanel } from '../components/meetings/TranscriptPanel'
import { useAuth } from '../contexts/AuthContext'
import type { MeetingReport } from '../types/meeting'

export function MeetingReportPage() {
  const { meetingId = '' } = useParams()
  const { user } = useAuth()
  const [report, setReport] = useState<MeetingReport | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [transcriptOpen, setTranscriptOpen] = useState(false)

  useEffect(() => {
    let active = true
    if (!meetingId || !user) return

    setLoading(true)
    getMeetingReport(meetingId, user.id)
      .then((data) => {
        if (active) setReport(data)
      })
      .catch((err: unknown) => {
        if (active) setError(err instanceof Error ? err.message : 'Failed to load report')
      })
      .finally(() => {
        if (active) setLoading(false)
      })

    return () => { active = false }
  }, [meetingId, user])

  function formatTime(isoStr: string) {
    return new Intl.DateTimeFormat(undefined, { timeStyle: 'short' }).format(new Date(isoStr))
  }

  function formatDuration(joined: string, left: string | null) {
    if (!left) return 'Still in meeting'
    const diff = new Date(left).getTime() - new Date(joined).getTime()
    const minutes = Math.round(diff / 60000)
    if (minutes < 1) return '< 1 min'
    return `${minutes} min`
  }

  return (
    <div className="page-stack">
      <header className="page-header">
        <div>
          <Link to="/meetings" className="text-link" style={{ display: 'inline-flex', alignItems: 'center', gap: '0.25rem', marginBottom: '0.5rem' }}>
            <ArrowLeft size={14} /> Back to meetings
          </Link>
          <h2>Meeting Report</h2>
        </div>
      </header>

      {loading ? (
        <section className="state-card" role="status">Loading report…</section>
      ) : error ? (
        <section className="state-card" role="alert">
          <div className="state-icon danger">!</div>
          <p className="eyebrow">Access denied or unavailable</p>
          <h2>Report not found</h2>
          <p>{error}</p>
        </section>
      ) : report ? (
        <div className="report-content" style={{ display: 'flex', flexDirection: 'column', gap: '2rem' }}>
          <section className="report-summary" style={{ background: 'var(--surface)', padding: '1.5rem', borderRadius: '8px', border: '1px solid var(--border)' }}>
            <h3 style={{ marginTop: 0, marginBottom: '1rem', display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
              <FileText size={18} /> {report.title}
            </h3>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem', color: 'var(--text-muted)', fontSize: '0.9rem' }}>
              <div><strong>Start:</strong> {formatTime(report.start_at)}</div>
              <div><strong>End:</strong> {formatTime(report.end_at)}</div>
              <div><strong>Organizer:</strong> {report.organizer_id}</div>
              <div><strong>Participants:</strong> {report.attendance.length}</div>
            </div>
          </section>

          <section className="report-attendance">
            <h3 style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1rem' }}>
              <Users size={18} /> Attendance Log
            </h3>
            
            {report.attendance.length === 0 ? (
              <p>No participants joined this meeting.</p>
            ) : (
              <div style={{ overflowX: 'auto' }}>
                <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
                  <thead>
                    <tr style={{ borderBottom: '2px solid var(--border)' }}>
                      <th style={{ padding: '0.75rem 0.5rem' }}>Name</th>
                      <th style={{ padding: '0.75rem 0.5rem' }}>Email / Guest</th>
                      <th style={{ padding: '0.75rem 0.5rem' }}>Joined At</th>
                      <th style={{ padding: '0.75rem 0.5rem' }}>Left At</th>
                      <th style={{ padding: '0.75rem 0.5rem' }}>Duration</th>
                    </tr>
                  </thead>
                  <tbody>
                    {report.attendance.map((row, idx) => (
                      <tr key={idx} style={{ borderBottom: '1px solid var(--border)' }}>
                        <td style={{ padding: '0.75rem 0.5rem', fontWeight: 500 }}>{row.participant_name}</td>
                        <td style={{ padding: '0.75rem 0.5rem', color: 'var(--text-muted)', fontSize: '0.9rem' }}>
                          {row.participant_email || (row.is_external_guest ? 'External Guest' : 'Unknown')}
                        </td>
                        <td style={{ padding: '0.75rem 0.5rem', fontSize: '0.9rem' }}>{formatTime(row.joined_at)}</td>
                        <td style={{ padding: '0.75rem 0.5rem', fontSize: '0.9rem' }}>
                          {row.left_at ? formatTime(row.left_at) : <span style={{ color: 'var(--primary)' }}>Still in meeting</span>}
                        </td>
                        <td style={{ padding: '0.75rem 0.5rem', fontSize: '0.9rem' }}>
                          <Clock size={12} style={{ verticalAlign: 'text-bottom', marginRight: '4px' }} />
                          {formatDuration(row.joined_at, row.left_at)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <button
              className="button secondary"
              type="button"
              aria-expanded={transcriptOpen}
              aria-controls="meeting-transcript-panel"
              onClick={() => setTranscriptOpen((open) => !open)}
            >
              <FileText size={16} aria-hidden="true" />
              {transcriptOpen ? 'Hide Transcript' : 'View Transcript'}
            </button>
          </section>

          {transcriptOpen && (
            <TranscriptPanel
              meetingId={meetingId}
              organizerId={report.organizer_id}
              isOrganizer={user?.id === report.organizer_id}
              isOpen
              inline
              onClose={() => setTranscriptOpen(false)}
            />
          )}
        </div>
      ) : null}
    </div>
  )
}
