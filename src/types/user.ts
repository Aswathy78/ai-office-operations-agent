export type UserRole = 'employee' | 'manager' | 'hr_operations' | 'admin'

export interface User {
  id: string
  name: string
  email: string
  role: UserRole
  department: string
}

export const ROLE_LABELS: Record<UserRole, string> = {
  employee: 'Employee',
  manager: 'Manager',
  hr_operations: 'HR / Operations',
  admin: 'Admin',
}
