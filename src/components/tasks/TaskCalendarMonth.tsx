import { useMemo, useState } from 'react'
import { ChevronLeft, ChevronRight, X } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { TaskRecord } from '../../api/tasks'

interface TaskCalendarMonthProps {
  tasks: TaskRecord[]
}

interface CalendarDay {
  date: Date
  inCurrentMonth: boolean
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

function formatDueTime(date: Date): string {
  return new Intl.DateTimeFormat('en-US', {
    hour: 'numeric',
    minute: '2-digit',
  }).format(date)
}

function formatDueDateTime(date: Date): string {
  return new Intl.DateTimeFormat('en-US', {
    dateStyle: 'full',
    timeStyle: 'short',
  }).format(date)
}

function formatSourceTimestamp(seconds: number): string {
  const minutes = Math.floor(seconds / 60)
  const remainingSeconds = seconds - minutes * 60
  return `${minutes}:${remainingSeconds.toFixed(2).padStart(5, '0')}`
}

export function TaskCalendarMonth({ tasks }: TaskCalendarMonthProps) {
  const [visibleMonth, setVisibleMonth] = useState(() => startOfMonth(new Date()))
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const today = new Date()
  const todayKey = dateKey(today)

  const calendarDays = useMemo(
    () => buildCalendarDays(visibleMonth),
    [visibleMonth],
  )
  const tasksByDay = useMemo(() => {
    const grouped = new Map<string, Array<{ task: TaskRecord; dueAt: Date }>>()
    const undated: TaskRecord[] = []

    for (const task of tasks) {
      const dueAt = parseDueDate(task.due_at)
      if (!dueAt) {
        undated.push(task)
        continue
      }
      const key = dateKey(dueAt)
      const dayTasks = grouped.get(key) ?? []
      dayTasks.push({ task, dueAt })
      grouped.set(key, dayTasks)
    }

    return { grouped, undated }
  }, [tasks])

  const selectedTask = tasks.find((task) => task.id === selectedTaskId)
  const selectedDueAt = selectedTask ? parseDueDate(selectedTask.due_at) : null
  const monthLabel = formatMonth(visibleMonth)
  const localTimeZone =
    Intl.DateTimeFormat().resolvedOptions().timeZone || 'your local time zone'

  function changeMonth(amount: number) {
    setVisibleMonth((current) =>
      new Date(current.getFullYear(), current.getMonth() + amount, 1),
    )
    setSelectedTaskId(null)
  }

  function showToday() {
    setVisibleMonth(startOfMonth(new Date()))
    setSelectedTaskId(null)
  }

  return (
    <section className="task-calendar" aria-label="Monthly task calendar">
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
        Due times are shown in {localTimeZone}.
      </p>
      {tasks.length === 0 && (
        <p className="task-calendar-empty">No tasks scheduled for this calendar.</p>
      )}

      <div className="task-calendar-grid-wrap">
        <table className="task-calendar-grid">
          <caption className="visually-hidden">{monthLabel} task calendar</caption>
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
                      const dayTasks = tasksByDay.grouped.get(key) ?? []
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
                            {dayTasks.map(({ task, dueAt }) => (
                              <li key={task.id}>
                                <button
                                  type="button"
                                  className="task-calendar-task"
                                  aria-label={`${task.title}, ${formatDueTime(dueAt)}, ${task.priority} priority, ${task.status}`}
                                  onClick={() => setSelectedTaskId(task.id)}
                                >
                                  <span className="task-calendar-task-title">{task.title}</span>
                                  <span className="task-calendar-task-meta">
                                    {formatDueTime(dueAt)} · {task.priority} · {task.status}
                                  </span>
                                  <span className="task-calendar-task-assignee">
                                    {task.assignee_name}
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
                {selectedDueAt ? formatDueDateTime(selectedDueAt) : 'No due date'}
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
            <div><dt>Meeting</dt><dd>{selectedTask.meeting_title}</dd></div>
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
                {selectedTask.source_segment_index} at{' '}
                {formatSourceTimestamp(selectedTask.source_timestamp_seconds)}
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
          <Link
            className="task-calendar-source-link"
            to={`/meetings/${encodeURIComponent(selectedTask.meeting_id)}/report?transcript_id=${encodeURIComponent(selectedTask.transcript_id)}&segment=${selectedTask.source_segment_index}`}
          >
            Open source meeting: {selectedTask.meeting_title} -{' '}
            {formatSourceTimestamp(selectedTask.source_timestamp_seconds)}
          </Link>
        </section>
      )}

      {tasksByDay.undated.length > 0 && (
        <section className="task-calendar-undated" aria-label="Tasks without due dates">
          <h3>Tasks without a due date</h3>
          <ul>
            {tasksByDay.undated.map((task) => (
              <li key={task.id}>
                <button
                  type="button"
                  className="task-calendar-task"
                  aria-label={`${task.title}, no due date`}
                  onClick={() => setSelectedTaskId(task.id)}
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
