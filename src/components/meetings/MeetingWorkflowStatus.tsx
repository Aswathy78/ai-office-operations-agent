import { useMeetingWorkflow } from '../../hooks/useMeetingWorkflow'

const STEP_LABELS: Record<string, string> = {
  transcription: 'Transcription',
  summary: 'Meeting summary',
  action_items: 'Action items',
  task_creation: 'Confirmed task creation',
}

function label(status: string): string {
  return status.charAt(0).toUpperCase() + status.slice(1)
}

export function MeetingWorkflowStatus({ meetingId }: { meetingId: string }) {
  const { workflow, loading, error } = useMeetingWorkflow(meetingId)

  return (
    <section className="task-workspace-card" aria-label="Meeting follow-up workflow">
      <div>
        <p className="eyebrow">Automatic follow-up</p>
        <h3>Meeting workflow</h3>
        {loading ? (
          <p role="status">Loading workflow status…</p>
        ) : error ? (
          <p className="task-manage-feedback error" role="alert">{error}</p>
        ) : !workflow ? (
          <p role="status">The follow-up workflow will appear after the recording is completed.</p>
        ) : (
          <>
            <p>
              <span className={`status-pill ${workflow.status === 'failed' ? 'danger' : workflow.status === 'completed' || workflow.status === 'cancelled' ? 'muted' : 'warning'}`}>
                {label(workflow.status)}
              </span>
              {workflow.status === 'retrying' && workflow.max_attempts ? (
                <small> Attempt {workflow.attempt_count} of {workflow.max_attempts} failed; retrying automatically.</small>
              ) : null}
            </p>
            {workflow.status === 'failed' && (
              <p className="email-status-error" role="status">
                This workflow failed after {workflow.attempt_count}
                {workflow.max_attempts ? ` of ${workflow.max_attempts}` : ''} attempts. It was moved to the
                dead-letter queue and an administrator has been notified.
              </p>
            )}
            {workflow.status === 'cancelled' && (
              <p role="status">An administrator cancelled this workflow.</p>
            )}
            {workflow.last_error && workflow.status !== 'completed' && (
              <p className="email-status-error">{workflow.last_error}</p>
            )}
            <ol className="workflow-step-list">
              {workflow.steps.map((step) => (
                <li key={step.id}>
                  <span>{STEP_LABELS[step.step_key] ?? step.step_key.replaceAll('_', ' ')}</span>
                  <span className={`status-pill ${step.status === 'failed' ? 'danger' : step.status === 'completed' ? 'muted' : 'warning'}`}>
                    {label(step.status)}
                  </span>
                  {step.last_error && <small className="email-status-error">{step.last_error}</small>}
                </li>
              ))}
            </ol>
          </>
        )}
      </div>
    </section>
  )
}
