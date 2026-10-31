import { useEffect, useMemo, useState } from 'react'
import { X } from 'lucide-react'
import { getActiveEmployees } from '../../api/meetings'
import type { Employee } from '../../types/meeting'

interface EmployeeAccessPickerProps {
  id: string
  selectedIds: string[]
  onChange: (ids: string[]) => void
  /** The owner already has access, so they are not offered in the list. */
  ownerId?: string
}

/** Pick employees by name or team instead of typing their ids. */
export function EmployeeAccessPicker({ id, selectedIds, onChange, ownerId }: EmployeeAccessPickerProps) {
  const [employees, setEmployees] = useState<Employee[]>([])
  const [loadFailed, setLoadFailed] = useState(false)
  const [query, setQuery] = useState('')

  useEffect(() => {
    let cancelled = false
    getActiveEmployees()
      .then((list) => {
        if (!cancelled) setEmployees(list)
      })
      .catch(() => {
        if (!cancelled) setLoadFailed(true)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const byId = useMemo(() => new Map(employees.map((e) => [e.id, e])), [employees])
  const needle = query.trim().toLowerCase()
  const visible = employees.filter(
    (e) =>
      e.id !== ownerId &&
      (!needle ||
        [e.name, e.team, e.email].some((value) => (value ?? '').toLowerCase().includes(needle))),
  )

  const toggle = (employeeId: string) =>
    onChange(
      selectedIds.includes(employeeId)
        ? selectedIds.filter((existing) => existing !== employeeId)
        : [...selectedIds, employeeId],
    )

  return (
    <div>
      {selectedIds.length > 0 && (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '6px', marginBottom: '8px' }}>
          {selectedIds.map((employeeId) => (
            <span
              key={employeeId}
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '4px',
                padding: '2px 8px',
                borderRadius: '999px',
                background: 'var(--surface-muted, #eef2ff)',
                fontSize: '12px',
              }}
            >
              {byId.get(employeeId)?.name ?? employeeId}
              <button
                type="button"
                aria-label={`Remove ${byId.get(employeeId)?.name ?? employeeId}`}
                onClick={() => toggle(employeeId)}
                style={{ border: 0, background: 'none', cursor: 'pointer', padding: 0, display: 'flex' }}
              >
                <X size={12} />
              </button>
            </span>
          ))}
        </div>
      )}
      <input
        id={id}
        type="search"
        placeholder="Search employees by name, team or email"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        style={{
          width: '100%',
          padding: '9px 12px',
          border: '1px solid var(--border)',
          borderRadius: '8px',
          fontSize: '13px',
        }}
      />
      <div
        role="group"
        aria-label="Employees who can access this document"
        style={{
          maxHeight: '170px',
          overflowY: 'auto',
          marginTop: '6px',
          border: '1px solid var(--border)',
          borderRadius: '8px',
        }}
      >
        {loadFailed && (
          <p style={{ margin: 0, padding: '8px 12px', fontSize: '12px', color: 'var(--muted)' }}>
            Could not load the employee list. Try again later.
          </p>
        )}
        {!loadFailed && visible.length === 0 && (
          <p style={{ margin: 0, padding: '8px 12px', fontSize: '12px', color: 'var(--muted)' }}>
            No employees found.
          </p>
        )}
        {visible.map((employee) => (
          <label
            key={employee.id}
            style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 12px', fontSize: '13px', cursor: 'pointer' }}
          >
            <input
              type="checkbox"
              checked={selectedIds.includes(employee.id)}
              onChange={() => toggle(employee.id)}
            />
            <span>
              {employee.name}
              <span style={{ color: 'var(--muted)', fontSize: '12px' }}>
                {' '}
                · {employee.team}
                {employee.email ? ` · ${employee.email}` : ''}
              </span>
            </span>
          </label>
        ))}
      </div>
    </div>
  )
}
