import type { ReactNode } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/layout/AppShell'
import { AccessDenied, NotFound } from './components/common/AccessDenied'
import { routePermissions } from './config/navigation'
import { AssistantProvider } from './contexts/AssistantContext'
import { useAuth } from './contexts/AuthContext'
import { CreateMeetingPlaceholder } from './pages/CreateMeetingPlaceholder'
import { GuestMeetingRoomPlaceholder } from './pages/GuestMeetingRoomPlaceholder'
import { GuestJoinPage } from './pages/GuestJoinPage'
import { HomePage } from './pages/HomePage'
import { LoginPage } from './pages/LoginPage'
import { MeetingEntryPage } from './pages/MeetingEntryPage'
import { MeetingReportPage } from './pages/MeetingReportPage'
import { MeetingRoomPage } from './pages/MeetingRoomPage'
import { MeetingsPage } from './pages/MeetingsPage'
import { AuditLogsPage } from './pages/AuditLogsPage'
import { ApprovalsPage } from './pages/ApprovalsPage'
import { ChatPage } from './pages/ChatPage'
import { DocumentsPage } from './pages/DocumentsPage'
import { TaskWorkspacePage } from './pages/TaskWorkspacePage'
import { NotificationsPage } from './pages/NotificationsPage'
import { NotificationPreferencesPage } from './pages/NotificationPreferencesPage'
import { MyApprovalsPage } from './pages/MyApprovalsPage'
import { WorkflowQueuePage } from './pages/WorkflowQueuePage'
import { AdminDashboardPage } from './pages/AdminDashboardPage'
import { ManagerDashboard } from './pages/ManagerDashboard'
import { ManagerMonitoringPage } from './pages/ManagerMonitoringPage'
import { AdminAssistantRunsPage } from './pages/AdminAssistantRunsPage'

function ProtectedRoute({ path, children }: { path: string; children: ReactNode }) {
  const { user, isAuthenticated } = useAuth()
  const allowedRoles = routePermissions[path]

  if (!isAuthenticated || !user) return <Navigate to="/login" replace />
  if (allowedRoles && !allowedRoles.includes(user.role)) return <AccessDenied />
  return <>{children}</>
}

function ProtectedAppPage({ path, children }: { path: string; children: ReactNode }) {
  return (
    <ProtectedRoute path={path}>
      <AppShell>{children}</AppShell>
    </ProtectedRoute>
  )
}

/** Administrators land on the system dashboard (users, departments, integrations, health),
 *  with the task dashboard they already had underneath it. Everyone else sees the task dashboard. */
function DashboardPage() {
  const { user } = useAuth()
  if (user?.role !== 'admin') return <TaskWorkspacePage view="dashboard" />
  return (
    <>
      <AdminDashboardPage />
      <TaskWorkspacePage view="dashboard" />
    </>
  )
}

function AppRoutes() {
  return (
    <Routes>
      {/* Public routes - no auth required */}
      <Route path="/login" element={<LoginPage />} />

      {/* Guest-only sub-routes (unauthenticated flow) */}
      <Route path="/meetings/:meetingId/guest" element={<GuestJoinPage />} />
      <Route path="/meetings/:meetingId/guest/room" element={<GuestMeetingRoomPlaceholder />} />

      {/* Full-screen authenticated meeting room — outside AppShell */}
      <Route
        path="/app/meetings/:meetingId"
        element={<ProtectedRoute path="/meetings"><MeetingRoomPage /></ProtectedRoute>}
      />

      {/* Authenticated routes inside AppShell */}
      <Route path="/" element={<Navigate to="/home" replace />} />
      <Route path="/home" element={<ProtectedAppPage path="/home"><HomePage /></ProtectedAppPage>} />

      {/* Explicit meeting routes inside AppShell */}
      <Route path="/meetings" element={<ProtectedAppPage path="/meetings"><MeetingsPage /></ProtectedAppPage>} />
      <Route path="/meetings/create" element={<ProtectedAppPage path="/meetings"><CreateMeetingPlaceholder /></ProtectedAppPage>} />
      <Route path="/meetings/:meetingId/report" element={<ProtectedAppPage path="/meetings"><MeetingReportPage /></ProtectedAppPage>} />

      {/* Other authenticated routes inside AppShell */}
      <Route path="/chat" element={<ProtectedAppPage path="/chat"><ChatPage /></ProtectedAppPage>} />
      <Route path="/tasks" element={<ProtectedAppPage path="/tasks"><TaskWorkspacePage view="mine" /></ProtectedAppPage>} />
      <Route path="/calendar" element={<ProtectedAppPage path="/calendar"><TaskWorkspacePage view="calendar" /></ProtectedAppPage>} />
      <Route path="/dashboard" element={<ProtectedAppPage path="/dashboard"><DashboardPage /></ProtectedAppPage>} />
      <Route path="/team-dashboard" element={<ProtectedAppPage path="/team-dashboard"><ManagerDashboard /></ProtectedAppPage>} />
      <Route path="/monitoring" element={<ProtectedAppPage path="/monitoring"><ManagerMonitoringPage /></ProtectedAppPage>} />
      <Route path="/documents" element={<ProtectedAppPage path="/documents"><DocumentsPage /></ProtectedAppPage>} />
      <Route path="/approvals" element={<ProtectedAppPage path="/approvals"><ApprovalsPage /></ProtectedAppPage>} />
      <Route path="/my-approvals" element={<ProtectedAppPage path="/my-approvals"><MyApprovalsPage /></ProtectedAppPage>} />
      <Route path="/notifications" element={<ProtectedAppPage path="/notifications"><NotificationsPage /></ProtectedAppPage>} />
      <Route path="/settings/notifications" element={<ProtectedAppPage path="/settings/notifications"><NotificationPreferencesPage /></ProtectedAppPage>} />
      <Route path="/admin/dashboard" element={<ProtectedAppPage path="/admin/dashboard"><AdminDashboardPage /></ProtectedAppPage>} />
      <Route path="/admin/users" element={<ProtectedAppPage path="/admin/users"><AdminDashboardPage view="users" /></ProtectedAppPage>} />
      <Route path="/admin/workflows" element={<ProtectedAppPage path="/admin/workflows"><WorkflowQueuePage /></ProtectedAppPage>} />
      <Route path="/admin/agent-monitor" element={<ProtectedAppPage path="/admin/agent-monitor"><AdminDashboardPage view="health" /></ProtectedAppPage>} />
      <Route path="/admin/assistant-runs" element={<ProtectedAppPage path="/admin/assistant-runs"><AdminAssistantRunsPage /></ProtectedAppPage>} />
      <Route path="/admin/audit-logs" element={<ProtectedAppPage path="/admin/audit-logs"><AuditLogsPage /></ProtectedAppPage>} />

      {/* Smart meeting entry — dynamic route for /meetings/:meetingId (only hits if not /meetings/create or /meetings) */}
      <Route path="/meetings/:meetingId" element={<MeetingEntryPage />} />

      {/* Fallback 404 */}
      <Route path="*" element={<ProtectedAppPage path="/home"><NotFound /></ProtectedAppPage>} />
    </Routes>
  )
}

export default function App() {
  return (
    <AssistantProvider>
      <AppRoutes />
    </AssistantProvider>
  )
}
