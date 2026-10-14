import { useState } from 'react'

export default function Settings({
  intervalMs,
  onChange,
}: {
  intervalMs: number
  onChange: (ms: number) => void
}) {
  const [v, setV] = useState(String(intervalMs))

  const save = () => {
    const n = Math.max(500, Math.round(Number(v)) || 2000)
    onChange(n)
    setV(String(n))
  }

  return (
    <div className="card settings">
      <h3>Settings</h3>
      <label>
        poll interval (ms, min 500)
        <input value={v} inputMode="numeric" onChange={(e) => setV(e.target.value)} />
      </label>
      <button onClick={save}>save</button>
      <p className="muted">
        Stored in this browser's localStorage. 2s is a good default; the API is rate-limited to
        10 req/s, so don't go below ~1.5s with all views open.
      </p>
    </div>
  )
}
