import { useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight, X } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { TaskRecord } from '../../api/tasks'
import type { MeetingCalendarEvent } from '../../types/meeting'

interface TaskCalendarMonthProps {
  tasks: TaskRecord[]
  meetings?: MeetingCalendarEvent[]
  timeZone?: string
  meetingsLoading?: boolean
  meetingsError?: string
  onVisibleMonthChange?: (year: number, month: number) => void
}

interface CalendarDay {
  date: Date
  inCurrentMonth: boolean
}

type CalendarEntry =
  | { kind: 'task'; task: TaskRecord; startAt: Date }
  | {
      kind: 'meeting'
      meeting: MeetingCalendarEvent
      startAt: Date
      endAt: Date
    }

const WEEKDAYS = [
  'Sunday',
  'Monday',
  'Tuesday',
  'Wednesday',
  'Thursday',
  'Friday',
  'Saturday',
]

function startOfMonth(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), 1)
}

function dateKey(date: Date): string {
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

function dateKeyInTimeZone(date: Date, timeZone: string): string {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(date)
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]))
  return `${values.year}-${values.month}-${values.day}`
}

function parseDueDate(value: unknown): Date | null {
  if (typeof value !== 'string' || value.trim() === '') return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

function buildCalendarDays(month: Date): CalendarDay[] {
  const firstDay = startOfMonth(month)
  const gridStart = new Date(
    firstDay.getFullYear(),
    firstDay.getMonth(),
    1 - firstDay.getDay(),
  )
  const daysInMonth = new Date(
    firstDay.getFullYear(),
    firstDay.getMonth() + 1,
    0,
  ).getDate()
  const cellCount = Math.ceil((firstDay.getDay() + daysInMonth) / 7) * 7

  return Array.from({ length: cellCount }, (_, index) => {
    const date = new Date(gridStart)
    date.setDate(gridStart.getDate() + index)
    return { date, inCurrentMonth: date.getMonth() === firstDay.getMonth() }
  })
}

function formatMonth(date: Date): string {
  return new Intl.DateTimeFormat('en-US', {
    month: 'long',
    year: 'numeric',
  }).format(date)
}

function formatDayLabel(date: Date): string {
  return new Intl.DateTimeFormat('en-US', {
    weekday: 'long',
    month: 'long',
    day: 'numeric',
    year: 'numeric',
  }).format(date)
}

function formatDueTime(date: Date, timeZone: string): string {
  return new Intl.DateTimeFormat('en-US', {
    hour: 'numeric',
    minute: '2-digit',
    timeZone,
  }).format(date)
}

function formatDueDateTime(date: Date, timeZone: string): string {
  return new Intl.DateTimeFormat('en-US', {
    dateStyle: 'full',
    timeStyle: 'short',
    timeZone,
  }).format(date)
}

function formatSourceTimestamp(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const remainingSeconds = seconds - minutes * 60
  return `${minutes}:${remainingSeconds.toFixed(2).padStart(5, '0')}`
}

export function TaskCalendarMonth({
  tasks,
  meetings = [],
  timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  meetingsLoading = false,
  meetingsError = '',
  onVisibleMonthChange,
}: TaskCalendarMonthProps) {
  const [visibleMonth, setVisibleMonth] = useState(() => startOfMonth(new Date()))
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [selectedMeetingId, setSelectedMeetingId] = useState<string | null>(null)
  const todayKey = dateKeyInTimeZone(new Date(), timeZone)

  const calendarDays = useMemo(
    () => buildCalendarDays(visibleMonth),
    [visibleMonth],
  )
  const calendarEntries = useMemo(() => {
    const grouped = new Map<string, CalendarEntry[]>()
    const undated: TaskRecord[] = []

    for (const task of tasks) {
      const startAt = parseDueDate(task.due_at)
      if (!startAt) {
        undated.push(task)
        continue
      }
      const key = dateKeyInTimeZone(startAt, timeZone)
      const entries = grouped.get(key) ?? []
      entries.push({ kind: 'task', task, startAt })
      grouped.set(key, entries)
    }

    const uniqueMeetings = new Map(meetings.map((meeting) => [meeting.id, meeting]))
    for (const meeting of uniqueMeetings.values()) {
      const startAt = parseDueDate(meeting.start_at)
      const endAt = parseDueDate(meeting.end_at)
      if (!startAt || !endAt) continue
      const key = dateKeyInTimeZone(startAt, timeZone)
      const entries = grouped.get(key) ?? []
      entries.push({ kind: 'meeting', meeting, startAt, endAt })
      grouped.set(key, entries)
    }

    for (const entries of grouped.values()) {
      entries.sort((left, right) => left.startAt.getTime() - right.startAt.getTime())
    }

    return { grouped, undated, uniqueMeetings: [...uniqueMeetings.values()] }
  }, [meetings, tasks, timeZone])

  const selectedTask = tasks.find((task) => task.id === selectedTaskId)
  const selectedMeeting = calendarEntries.uniqueMeetings.find(
    (meeting) => meeting.id === selectedMeetingId,
  )
  const selectedDueAt = selectedTask ? parseDueDate(selectedTask.due_at) : null
  const monthLabel = formatMonth(visibleMonth)

  function changeMonth(amount: number) {
    const nextMonth = new Date(
      visibleMonth.getFullYear(),
      visibleMonth.getMonth() + amount,
      1,
    )
    setVisibleMonth(nextMonth)
    setSelectedTaskId(null)
    setSelectedMeetingId(null)
    onVisibleMonthChange?.(nextMonth.getFullYear(), nextMonth.getMonth() + 1)
  }

  function showToday() {
    const currentMonth = startOfMonth(new Date())
    setVisibleMonth(currentMonth)
    setSelectedTaskId(null)
    setSelectedMeetingId(null)
    onVisibleMonthChange?.(
      currentMonth.getFullYear(),
      currentMonth.getMonth() + 1,
    )
  }

  function selectTask(taskId: string) {
    setSelectedMeetingId(null)
    setSelectedTaskId(taskId)
  }

  function selectMeeting(meetingId: string) {
    setSelectedTaskId(null)
    setSelectedMeetingId(meetingId)
  }

  return (
    <section className="task-calendar" aria-label="Monthly task and meeting calendar">
      <div className="task-calendar-toolbar">
        <h3 aria-live="polite">{monthLabel}</h3>
        <nav className="task-calendar-navigation" aria-label="Calendar month controls">
          <button
            className="button secondary"
            type="button"
            aria-label="Previous Month"
            onClick={() => changeMonth(-1)}
          >
            <ChevronLeft size={16} aria-hidden="true" />
            Previous Month
          </button>
          <button className="button secondary" type="button" onClick={showToday}>
            Today
          </button>
          <button
            className="button secondary"
            type="button"
            aria-label="Next Month"
            onClick={() => changeMonth(1)}
          >
            Next Month
            <ChevronRight size={16} aria-hidden="true" />
          </button>
        </nav>
      </div>
      <p className="task-calendar-timezone">
        Times are shown in {timeZone}.
      </p>
      {meetingsLoading && (
        <p className="task-calendar-empty" role="status">Loading scheduled meetings…</p>
      )}
      {meetingsError && (
        <p className="task-calendar-error" role="alert">
          Scheduled meetings could not be loaded: {meetingsError}
        </p>
      )}
      {tasks.length === 0 &&
        calendarEntries.uniqueMeetings.length === 0 &&
        !meetingsLoading && (
          <p className="task-calendar-empty">
            No tasks or meetings scheduled for this calendar.
          </p>
      )}

      <div className="task-calendar-grid-wrap">
        <table className="task-calendar-grid">
          <caption className="visually-hidden">{monthLabel} task calendar with meetings</caption>
          <thead>
            <tr>
              {WEEKDAYS.map((weekday) => (
                <th scope="col" key={weekday}>{weekday}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {Array.from(
              { length: calendarDays.length / 7 },
              (_, weekIndex) => (
                <tr key={`${monthLabel}-week-${weekIndex}`}>
                  {calendarDays
                    .slice(weekIndex * 7, weekIndex * 7 + 7)
                    .map(({ date, inCurrentMonth }) => {
                      const key = dateKey(date)
                      const entries = calendarEntries.grouped.get(key) ?? []
                      const isToday = key === todayKey

                      return (
                        <td
                          key={key}
                          aria-label={formatDayLabel(date)}
                          className={[
                            'task-calendar-day',
                            inCurrentMonth ? '' : 'outside-month',
                            isToday ? 'today' : '',
                          ].filter(Boolean).join(' ')}
                        >
                          <time
                            className="task-calendar-day-number"
                            dateTime={key}
                            aria-current={isToday ? 'date' : undefined}
                          >
                            {date.getDate()}
                          </time>
                          <ul className="task-calendar-day-tasks">
                            {entries.map((entry) => entry.kind === 'task' ? (
                              <li key={`task-${entry.task.id}`}>
                                <button
                                  type="button"
                                  className="task-calendar-task"
                                  aria-label={`${entry.task.title}, ${formatDueTime(entry.startAt, timeZone)}, ${entry.task.priority} priority, ${entry.task.status}`}
                                  onClick={() => selectTask(entry.task.id)}
                                >
                                  <span className="task-calendar-task-title">{entry.task.title}</span>
                                  <span className="task-calendar-task-meta">
                                    {formatDueTime(entry.startAt, timeZone)} · {entry.task.priority} · {entry.task.status}
                                  </span>
                                  <span className="task-calendar-task-assignee">
                                    {entry.task.assignee_name}
                                  </span>
                                </button>
                              </li>
                            ) : (
                              <li key={`meeting-${entry.meeting.id}`}>
                                <button
                                  type="button"
                                  className="task-calendar-task task-calendar-meeting"
                                  aria-label={`Meeting: ${entry.meeting.title}, ${formatDueTime(entry.startAt, timeZone)} to ${formatDueTime(entry.endAt, timeZone)}`}
                                  onClick={() => selectMeeting(entry.meeting.id)}
                                >
                                  <span className="task-calendar-meeting-kind">Meeting</span>
                                  <span className="task-calendar-task-title">{entry.meeting.title}</span>
                                  <span className="task-calendar-task-meta">
                                    {formatDueTime(entry.startAt, timeZone)}–{formatDueTime(entry.endAt, timeZone)}
                                  </span>
                                  <span className="task-calendar-task-assignee">
                                    {entry.meeting.attendees.map((attendee) => attendee.name).join(', ')}
                                  </span>
                                </button>
                              </li>
                            ))}
                          </ul>
                        </td>
                      )
                    })}
                </tr>
              ),
            )}
          </tbody>
        </table>
      </div>

      {selectedMeeting && (
        <section
          className="task-calendar-details task-calendar-meeting-details"
          aria-labelledby="task-calendar-meeting-details-title"
          aria-live="polite"
        >
          <div className="task-calendar-details-heading">
            <h3 id="task-calendar-meeting-details-title">{selectedMeeting.title}</h3>
            <button
              className="task-calendar-close"
              type="button"
              aria-label="Close meeting details"
              onClick={() => setSelectedMeetingId(null)}
            >
              <X size={18} aria-hidden="true" />
            </button>
          </div>
          <dl className="task-calendar-details-list">
            <div>
              <dt>Date and time</dt>
              <dd>
                {formatDueDateTime(parseDueDate(selectedMeeting.start_at)!, timeZone)}
                {' – '}
                {formatDueTime(parseDueDate(selectedMeeting.end_at)!, timeZone)}
              </dd>
            </div>
            <div><dt>Status</dt><dd>{selectedMeeting.status}</dd></div>
            <div>
              <dt>Attendees</dt>
              <dd>
                {selectedMeeting.attendees.map((attendee) => attendee.name).join(', ') || 'None listed'}
              </dd>
            </div>
          </dl>
          {selectedMeeting.join_url && (
            <a className="task-calendar-meeting-join" href={selectedMeeting.join_url}>
              Join Meeting
            </a>
          )}
        </section>
      )}

      {selectedTask && (
        <section
          className="task-calendar-details"
          aria-labelledby="task-calendar-details-title"
          aria-live="polite"
        >
          <div className="task-calendar-details-heading">
            <h3 id="task-calendar-details-title">{selectedTask.title}</h3>
            <button
              className="task-calendar-close"
              type="button"
              aria-label="Close task details"
              onClick={() => setSelectedTaskId(null)}
            >
              <X size={18} aria-hidden="true" />
            </button>
          </div>
          <dl className="task-calendar-details-list">
            <div>
              <dt>Due</dt>
              <dd>
                {selectedDueAt ? formatDueDateTime(selectedDueAt, timeZone) : 'No due date'}
              </dd>
            </div>
            <div><dt>Status</dt><dd>{selectedTask.status}</dd></div>
            <div><dt>Priority</dt><dd>{selectedTask.priority}</dd></div>
            <div>
              <dt>Assignee</dt>
              <dd>
                {selectedTask.assignee_name}
                {selectedTask.assignee_team ? ` (${selectedTask.assignee_team})` : ''}
              </dd>
            </div>
            <div><dt>Meeting</dt><dd>{selectedTask.meeting_title ?? 'None (AI assistant)'}</dd></div>
            <div><dt>Creator</dt><dd>{selectedTask.creator_id}</dd></div>
            <div><dt>Source</dt><dd>{selectedTask.source}</dd></div>
            <div>
              <dt>Source action item</dt>
              <dd>{selectedTask.source_action_item_id}</dd>
            </div>
            <div><dt>Transcript</dt><dd>{selectedTask.transcript_id}</dd></div>
            <div>
              <dt>Transcript segment</dt>
              <dd>
                {selectedTask.source_segment_index === null
                  ? 'None'
                  : `${selectedTask.source_segment_index} at ${formatSourceTimestamp(selectedTask.source_timestamp_seconds ?? 0)}`}
              </dd>
            </div>
            <div>
              <dt>Assignment email</dt>
              <dd>{selectedTask.assignment_email_status}</dd>
            </div>
          </dl>
          {selectedTask.pending_reassignment && (
            <p className="task-calendar-pending">
              Reassignment awaiting approval to {selectedTask.pending_reassignment.to_assignee_name}.
            </p>
          )}
          {selectedTask.meeting_id && selectedTask.transcript_id && (
            <Link
              className="task-calendar-source-link"
              to={`/meetings/${encodeURIComponent(selectedTask.meeting_id)}/report?transcript_id=${encodeURIComponent(selectedTask.transcript_id)}&segment=${selectedTask.source_segment_index}`}
            >
              Open source meeting: {selectedTask.meeting_title} -{' '}
              {formatSourceTimestamp(selectedTask.source_timestamp_seconds ?? 0)}
            </Link>
          )}
        </section>
      )}

      {calendarEntries.undated.length > 0 && (
        <section className="task-calendar-undated" aria-label="Tasks without due dates">
          <h3>Tasks without a due date</h3>
          <ul>
            {calendarEntries.undated.map((task) => (
              <li key={task.id}>
                <button
                  type="button"
                  className="task-calendar-task"
                  aria-label={`${task.title}, no due date`}
                  onClick={() => selectTask(task.id)}
                >
                  <span className="task-calendar-task-title">{task.title}</span>
                  <span className="task-calendar-task-meta">
                    {task.priority} · {task.status}
                  </span>
                  <span className="task-calendar-task-assignee">{task.assignee_name}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </section>
  )
}
