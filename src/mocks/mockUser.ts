import type { User, UserRole } from '../types/user'

/**
 * Temporary authentication seam for Story 1.1.
 * TODO: Replace this source with the real authenticated user/context.
 */
export const demoUsers: Record<UserRole, User> = {
  employee: {
    id: 'user-001',
    name: 'Rahul',
    email: 'rahul@example.com',
    role: 'employee',
    department: 'Engineering',
  },
  manager: {
    id: 'user-002',
    name: 'Maya',
    email: 'manager@example.com',
    role: 'manager',
    department: 'Engineering',
  },
  hr_operations: {
    id: 'user-003',
    name: 'Priya',
    email: 'hr.operations@example.com',
    role: 'hr_operations',
    department: 'People Operations',
  },
  admin: {
    id: 'user-004',
    name: 'Alex',
    email: 'admin@example.com',
    role: 'admin',
    department: 'Administration',
  },
}

export const mockUser = demoUsers.employee
