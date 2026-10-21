import type { ReactNode } from 'react'
import { Header } from './Header'
import { Sidebar } from './Sidebar'
import { AssistantWidget } from '../assistant/AssistantWidget'

export function AppShell({ children }: { children: ReactNode }) {
  return (
    <div className="app-shell">
      <Sidebar />
      <div className="app-main">
        <Header />
        <main id="main-content" className="main-content" tabIndex={-1}>
          {children}
        </main>
        <AssistantWidget />
      </div>
    </div>
  )
}
