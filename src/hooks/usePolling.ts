import { useEffect, useRef } from 'react'

/**
 * Calls `callback` every `intervalMs` while `enabled` is true. Waits for each call
 * to finish before scheduling the next one, so slow responses never overlap.
 */
export function usePolling(callback: () => Promise<unknown> | void, intervalMs: number, enabled: boolean) {
  const latest = useRef(callback)
  latest.current = callback

  useEffect(() => {
    if (!enabled) return
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const tick = async () => {
      try {
        await latest.current()
      } catch {
        // Callers surface their own errors; keep polling.
      }
      if (!cancelled) timer = setTimeout(() => void tick(), intervalMs)
    }
    timer = setTimeout(() => void tick(), intervalMs)

    return () => {
      cancelled = true
      if (timer !== undefined) clearTimeout(timer)
    }
  }, [enabled, intervalMs])
}
