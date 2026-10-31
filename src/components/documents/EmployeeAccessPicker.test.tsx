// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { EmployeeAccessPicker } from './EmployeeAccessPicker'

const { getActiveEmployees } = vi.hoisted(() => ({ getActiveEmployees: vi.fn() }))
vi.mock('../../api/meetings', () => ({ getActiveEmployees }))

const employees = [
  { id: 'emp-1', name: 'Maya Chen', email: 'maya@example.test', team: 'Engineering' },
  { id: 'emp-2', name: 'Noah Patel', email: 'noah@example.test', team: 'Finance' },
  { id: 'emp-3', name: 'Ava Brooks', email: 'ava@example.test', team: 'Engineering' },
]

afterEach(() => {
  cleanup()
  getActiveEmployees.mockReset()
})

describe('EmployeeAccessPicker', () => {
  it('lets the user tick names instead of typing ids, and hides the owner', async () => {
    getActiveEmployees.mockResolvedValue(employees)
    const onChange = vi.fn()
    render(<EmployeeAccessPicker id="acl" selectedIds={[]} onChange={onChange} ownerId="emp-1" />)

    expect(await screen.findByText('Noah Patel')).toBeInTheDocument()
    expect(screen.queryByText('Maya Chen')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: /Noah Patel/ }))
    expect(onChange).toHaveBeenCalledWith(['emp-2'])
  })

  it('filters by team and removes a selected employee', async () => {
    getActiveEmployees.mockResolvedValue(employees)
    const onChange = vi.fn()
    render(<EmployeeAccessPicker id="acl" selectedIds={['emp-2']} onChange={onChange} />)

    await screen.findByText('Ava Brooks')
    fireEvent.change(screen.getByPlaceholderText(/Search employees/), { target: { value: 'engineering' } })
    expect(screen.queryByRole('checkbox', { name: /Noah Patel/ })).not.toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: /Ava Brooks/ })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Remove Noah Patel' }))
    expect(onChange).toHaveBeenCalledWith([])
  })

  it('shows a message when the employee list cannot be loaded', async () => {
    getActiveEmployees.mockRejectedValue(new Error('offline'))
    render(<EmployeeAccessPicker id="acl" selectedIds={[]} onChange={vi.fn()} />)
    expect(await screen.findByText(/Could not load the employee list/)).toBeInTheDocument()
  })
})
