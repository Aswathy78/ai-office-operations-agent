import type { ReactNode } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/layout/AppShell'
import { AccessDenied, NotFound } from './components/common/AccessDenied'
import { routePermissions } from './config/navigation'
import { useAuth } from './contexts/AuthContext'
import { CreateMeetingPlaceholder } from './pages/CreateMeetingPlaceholder'
import { HomePage } from './pages/HomePage'
import { MeetingRoomPlaceholder } from './pages/MeetingRoomPlaceholder'
import { PlaceholderPage } from './pages/PlaceholderPage'
import { LoginPage } from './pages/LoginPage'

function ProtectedRoute({ path, children }: { path: string; children: ReactNode }) {
  const { user, isAuthenticated } = useAuth()
  const allowedRoles = routePermissions[path]

  if (!isAuthenticated || !user) return <Navigate to="/login" replace />
  if (allowedRoles && !allowedRoles.includes(user.role)) return <AccessDenied />
  return <>{children}</>
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="*" element={<AuthenticatedRoutes />} />
    </Routes>
  )
}

function AuthenticatedRoutes() {
  const { isAuthenticated } = useAuth()
  if (!isAuthenticated) return <Navigate to="/login" replace />

  return (
    <AppShell>
      <Routes>
        <Route path="/" element={<Navigate to="/home" replace />} />
        <Route path="/home" element={<ProtectedRoute path="/home"><HomePage /></ProtectedRoute>} />
        <Route path="/meetings" element={<ProtectedRoute path="/meetings"><PlaceholderPage /></ProtectedRoute>} />
        <Route path="/meetings/create" element={<ProtectedRoute path="/meetings"><CreateMeetingPlaceholder /></ProtectedRoute>} />
        <Route path="/meetings/:meetingId" element={<ProtectedRoute path="/meetings"><MeetingRoomPlaceholder /></ProtectedRoute>} />
        <Route path="/chat" element={<ProtectedRoute path="/chat"><PlaceholderPage /></ProtectedRoute>} />
        <Route path="/tasks" element={<ProtectedRoute path="/tasks"><PlaceholderPage /></ProtectedRoute>} />
        <Route path="/calendar" element={<ProtectedRoute path="/calendar"><PlaceholderPage /></ProtectedRoute>} />
        <Route path="/dashboard" element={<ProtectedRoute path="/dashboard"><PlaceholderPage /></ProtectedRoute>} />
        <Route path="/documents" element={<ProtectedRoute path="/documents"><PlaceholderPage /></ProtectedRoute>} />
        <Route path="/approvals" element={<ProtectedRoute path="/approvals"><PlaceholderPage /></ProtectedRoute>} />
        <Route path="/admin/users" element={<ProtectedRoute path="/admin/users"><PlaceholderPage /></ProtectedRoute>} />
        <Route path="/admin/agent-monitor" element={<ProtectedRoute path="/admin/agent-monitor"><PlaceholderPage /></ProtectedRoute>} />
        <Route path="/admin/audit-logs" element={<ProtectedRoute path="/admin/audit-logs"><PlaceholderPage /></ProtectedRoute>} />
        <Route path="*" element={<NotFound />} />
      </Routes>
    </AppShell>
  )
}

export default function App() {
  return <AppRoutes />
}
