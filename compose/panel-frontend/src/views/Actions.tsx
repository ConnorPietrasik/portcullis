import { useState } from 'react'
import { api } from '../api'
import { usePoll } from '../usePoll'
import type { ActionInfo, ActionResult } from '../types'

function ActionCard({ a }: { a: ActionInfo }) {
  const [args, setArgs] = useState<string[]>(() => Array(a.arity).fill(''))
  const [out, setOut] = useState<ActionResult | null>(null)
  const [busy, setBusy] = useState(false)

  const run = async () => {
    if (a.confirm && !window.confirm(`run ${a.name}?`)) return
    setBusy(true)
    try {
      setOut(await api<ActionResult>(`/action/${a.name}`, { method: 'POST', body: JSON.stringify({ args }) }))
    } catch (e) {
      setOut({ ok: false, exit_code: -1, stdout: '', stderr: String(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card action">
      <h3>
        {a.name} {a.confirm && <span className="badge warn">confirm</span>}
      </h3>
      <p className="muted">{a.summary}</p>
      {args.map((v, i) => (
        <input
          key={i}
          placeholder={a.patterns[i] ?? 'arg'}
          value={v}
          onChange={(e) => setArgs(args.map((x, j) => (j === i ? e.target.value : x)))}
        />
      ))}
      <button onClick={run} disabled={busy}>
        {busy ? 'running…' : 'run'}
      </button>
      {out && (
        <pre className={out.ok ? 'out ok' : 'out err'}>
          exit {out.exit_code}
          {'\n'}
          {out.stdout}
          {out.stderr}
        </pre>
      )}
    </div>
  )
}

export default function Actions() {
  // Registry changes rarely; poll slower than the metrics view.
  const { data, error } = usePoll(() => api<{ actions: ActionInfo[] }>('/actions'), 60_000)

  if (error) return <div className="banner err">actions: {error}</div>
  if (!data) return <div className="banner">loading…</div>

  return (
    <>
      <div className="banner">
        registry is read live from <code>panel-do --list</code> — the UI can never offer what the
        binary won't run
      </div>
      <div className="grid">
        {data.actions.map((a) => (
          <ActionCard key={a.name} a={a} />
        ))}
      </div>
    </>
  )
}
