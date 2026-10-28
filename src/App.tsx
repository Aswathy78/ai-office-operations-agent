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
import { PlaceholderPage } from './pages/PlaceholderPage'
import { ApprovalsPage } from './pages/ApprovalsPage'
import { ChatPage } from './pages/ChatPage'
import { DocumentsPage } from './pages/DocumentsPage'
import { TaskWorkspacePage } from './pages/TaskWorkspacePage'

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
      <Route path="/dashboard" element={<ProtectedAppPage path="/dashboard"><TaskWorkspacePage view="dashboard" /></ProtectedAppPage>} />
      <Route path="/documents" element={<ProtectedAppPage path="/documents"><DocumentsPage /></ProtectedAppPage>} />
      <Route path="/approvals" element={<ProtectedAppPage path="/approvals"><ApprovalsPage /></ProtectedAppPage>} />
      <Route path="/admin/users" element={<ProtectedAppPage path="/admin/users"><PlaceholderPage /></ProtectedAppPage>} />
      <Route path="/admin/agent-monitor" element={<ProtectedAppPage path="/admin/agent-monitor"><PlaceholderPage /></ProtectedAppPage>} />
      <Route path="/admin/audit-logs" element={<ProtectedAppPage path="/admin/audit-logs"><PlaceholderPage /></ProtectedAppPage>} />

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
