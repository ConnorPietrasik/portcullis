import { useCallback, useEffect, useRef, useState } from 'react'
import { NotModified } from './api'

/**
 * Poll `fn` every `intervalMs`; keeps `fn` fresh via a ref (no stale closures).
 *
 * Pauses while the tab is hidden (phone lock, another tab, a minimized
 * window) — the data-budget control: nothing polls while it can't be seen.
 * Becoming visible triggers an immediate refresh, so the view is current the
 * moment you return: "once per load" becomes "once per (re)appearance".
 */
export function usePoll<T>(fn: () => Promise<T>, intervalMs: number) {
  const fnRef = useRef(fn)
  fnRef.current = fn

  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [last, setLast] = useState<number | null>(null)

  const refresh = useCallback(async () => {
    try {
      const d = await fnRef.current()
      setData(d)
      setError(null)
      setLast(Date.now())
    } catch (e) {
      // The poll's 304 (the server's ETag says the body is unchanged — the
      // idle ticks): the browser usually re-serves its cached body instead,
      // but if the 304 reaches the app it is a SUCCESS — the data stands (it
      // is current: the server says the body is unchanged), a stale error
      // clears, and only the timestamp moves.
      if (e instanceof NotModified) {
        setError(null)
        setLast(Date.now())
        return
      }
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  /**
   * Adopt a value fetched outside the poll cycle (e.g. a mutation's response
   * carrying the fresh server state) — same effect as a successful refresh,
   * so the view updates immediately instead of waiting for the next tick.
   */
  const adopt = useCallback((d: T) => {
    setData(d)
    setError(null)
    setLast(Date.now())
  }, [])

  useEffect(() => {
    let t: number | undefined
    const stop = () => {
      if (t !== undefined) {
        window.clearInterval(t)
        t = undefined
      }
    }
    const start = () => {
      stop() // guard against double-start (visible→visible events)
      void refresh()
      t = window.setInterval(() => void refresh(), Math.max(250, intervalMs))
    }
    // A tab restored already-hidden (mobile tab restore) polls nothing until
    // it is actually shown — its first refresh comes from the visibility event.
    if (!document.hidden) start()
    const onVis = () => (document.hidden ? stop() : start())
    document.addEventListener('visibilitychange', onVis)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [intervalMs, refresh])

  return { data, error, last, refresh, adopt }
}
