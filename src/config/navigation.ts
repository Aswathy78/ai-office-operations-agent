import {
  Activity,
  CalendarDays,
  CheckSquare,
  FileText,
  Gauge,
  LayoutDashboard,
  MessageSquare,
  Settings,
  ShieldCheck,
  Users,
  Video,
} from 'lucide-react'
import type { NavigationItem } from '../types/navigation'
import type { UserRole } from '../types/user'

export const navigationItems: NavigationItem[] = [
  { id: 'home', label: 'Home', route: '/home', icon: LayoutDashboard, allowedRoles: ['employee', 'manager', 'hr_operations', 'admin'] },
  { id: 'meetings', label: 'Meetings', route: '/meetings', icon: Video, allowedRoles: ['employee', 'manager', 'hr_operations', 'admin'] },
  { id: 'tasks', label: 'My Tasks', route: '/tasks', icon: CheckSquare, allowedRoles: ['employee', 'manager', 'admin'] },
  { id: 'calendar', label: 'Calendar', route: '/calendar', icon: CalendarDays, allowedRoles: ['employee', 'manager', 'hr_operations', 'admin'] },
  { id: 'dashboard', label: 'Dashboard', route: '/dashboard', icon: Gauge, allowedRoles: ['manager', 'hr_operations', 'admin'] },
  { id: 'documents', label: 'Knowledge', route: '/documents', icon: FileText, allowedRoles: ['manager', 'hr_operations', 'admin'] },
  { id: 'approvals', label: 'Approvals', route: '/approvals', icon: ShieldCheck, allowedRoles: ['manager', 'hr_operations', 'admin'] },
  { id: 'users', label: 'Users', route: '/admin/users', icon: Users, allowedRoles: ['admin'] },
  { id: 'agent-monitor', label: 'Agent Monitor', route: '/admin/agent-monitor', icon: Activity, allowedRoles: ['admin'] },
  { id: 'audit', label: 'Audit Logs', route: '/admin/audit-logs', icon: Settings, allowedRoles: ['admin'] },
  { id: 'chat', label: 'Corporate Chatbox', route: '/chat', icon: MessageSquare, allowedRoles: ['employee', 'manager', 'hr_operations', 'admin'] },
]

export const getNavigationForRole = (role: UserRole) =>
  navigationItems.filter((item) => item.allowedRoles.includes(role))

export const routePermissions: Record<string, UserRole[]> = Object.fromEntries(
  navigationItems.map((item) => [item.route, item.allowedRoles]),
)
