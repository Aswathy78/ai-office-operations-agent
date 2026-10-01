import type { User } from '../types/user'

/**
 * Temporary authentication seam for Story 1.1.
 * TODO: Replace this source with the real authenticated user/context.
 */
export const mockUser: User = {
  id: 'user-001',
  name: 'Rahul',
  email: 'rahul@example.com',
  role: 'employee',
  department: 'Engineering',
}
