import { useCallback, useEffect, useRef, useState } from 'react'

/** Poll `fn` every `intervalMs`; keeps `fn` fresh via a ref (no stale closures). */
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
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    void refresh()
    const t = window.setInterval(() => void refresh(), Math.max(250, intervalMs))
    return () => window.clearInterval(t)
  }, [intervalMs, refresh])

  return { data, error, last, refresh }
}
