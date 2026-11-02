import { useEffect, useState } from 'react'
import {
  getMeetingWorkflow,
  streamMeetingWorkflow,
  type WorkflowRun,
} from '../api/workflows'

const WORKFLOW_STATUS_POLL_MS = 2000

function isFinished(workflow: WorkflowRun | null): boolean {
  return (
    workflow?.status === 'completed' ||
    workflow?.status === 'failed' ||
    workflow?.status === 'cancelled'
  )
}

export function useMeetingWorkflow(meetingId: string) {
  const [workflow, setWorkflow] = useState<WorkflowRun | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  useEffect(() => {
    if (!meetingId) {
      setWorkflow(null)
      setLoading(false)
      return
    }

    let active = true
    setLoading(true)
    setError('')
    let fallbackTimer: ReturnType<typeof setInterval> | undefined
    const controller = new AbortController()
    const update = (result: { workflow: WorkflowRun | null }) => {
      if (!active) return
      setWorkflow(result.workflow)
      setError('')
      setLoading(false)
    }
    const load = async () => {
      try {
        update(await getMeetingWorkflow(meetingId))
      } catch (reason) {
        if (active) {
          setError(reason instanceof Error ? reason.message : 'Workflow status could not be loaded.')
          setLoading(false)
        }
      }
    }
    const connect = async () => {
      await load()
      if (!active) return
      try {
        await streamMeetingWorkflow(meetingId, update, controller.signal)
      } catch (reason) {
        if (!active || controller.signal.aborted) return
        setError(reason instanceof Error ? reason.message : 'Live workflow updates are unavailable.')
        fallbackTimer = setInterval(() => void load(), WORKFLOW_STATUS_POLL_MS)
      }
    }

    void connect()
    return () => {
      active = false
      controller.abort()
      if (fallbackTimer !== undefined) clearInterval(fallbackTimer)
    }
  }, [meetingId])

  return { workflow, loading, error, isFinished: isFinished(workflow) }
}
