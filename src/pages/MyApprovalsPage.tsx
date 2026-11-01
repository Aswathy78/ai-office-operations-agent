import { Bot } from 'lucide-react'
import { AgentApprovalsSection } from '../components/assistant/AgentApprovalsSection'

export function MyApprovalsPage() {
  return (
    <div className="page-stack">
      <section className="task-workspace-card notification-page-heading">
        <div>
          <p className="eyebrow">My requests</p>
          <h2>My assistant approvals</h2>
          <p>Review and confirm assistant actions you requested.</p>
        </div>
        <Bot size={24} aria-hidden="true" />
      </section>
      <AgentApprovalsSection initialScope="mine" />
    </div>
  )
}
