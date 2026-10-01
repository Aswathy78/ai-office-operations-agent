import type { LucideIcon } from 'lucide-react'
import type { UserRole } from './user'

export interface NavigationItem {
  id: string
  label: string
  route: string
  icon: LucideIcon
  allowedRoles: UserRole[]
}
