import { useEffect, useState } from 'react'
import { api } from '../api'
import { usePoll } from '../usePoll'
import type { LLMSettings } from '../types'

function stateClass(state: 'up' | 'down' | 'unknown' | string): string {
  if (state === 'up') return 'badge ok'
  if (state === 'down') return 'badge err'
  return 'badge warn'
}

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
    <div className="settings">
      <div className="card">
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
      <LLMRouterCard intervalMs={intervalMs} />
    </div>
  )
}

function LLMRouterCard({ intervalMs }: { intervalMs: number }) {
  const { data, error, refresh } = usePoll(
    () => api<LLMSettings>('/settings/llm'),
    intervalMs,
  )
  const [draft, setDraft] = useState<string[] | null>(null)
  const [saving, setSaving] = useState(false)

  // follow the server order until the user edits it
  useEffect(() => {
    if (data && draft === null) setDraft(data.order)
  }, [data, draft])

  const order = draft ?? data?.order ?? []
  const dirty = draft !== null && data !== null && JSON.stringify(draft) !== JSON.stringify(data.order)

  const move = (i: number, d: number) =>
    setDraft(o => {
      if (o === null) return o
      const n = [...o]
      const j = i + d
      if (j < 0 || j >= n.length) return o
      ;[n[i], n[j]] = [n[j], n[i]]
      return n
    })

  const remove = (i: number) => setDraft(o => (o === null ? o : o.filter((_, k) => k !== i)))

  const add = (name: string) => {
    if (name) setDraft(o => (o === null || o.includes(name) ? o : [...o, name]))
  }

  const save = async () => {
    if (!dirty) return
    setSaving(true)
    try {
      await api('/settings/llm', { method: 'PUT', body: JSON.stringify({ order }) })
      await refresh()
    } catch (e) {
      window.alert(String(e))
    }
    setSaving(false)
  }

  if (error)
    return (
      <div className="card llm-card">
        <h3>
          llm.pietrasik.top <span className="muted">— ordered fallback</span>
        </h3>
        <div className="banner err">{error}</div>
      </div>
    )
  if (!data)
    return (
      <div className="card llm-card">
        <h3>
          llm.pietrasik.top <span className="muted">— ordered fallback</span>
        </h3>
        <p className="muted">loading…</p>
      </div>
    )

  const available = data.fleet.filter(f => f.name && !order.includes(f.name))

  return (
    <div className="card llm-card">
      <h3>
        llm.pietrasik.top <span className="muted">— ordered fallback</span>
      </h3>
      <p className="muted">
        One base URL for clients that just want the best model currently serving:{' '}
        <code>base_url = https://llm.pietrasik.top/v1</code> with your LLM_API_KEY. Requests go to
        the FIRST instance below that's up — the order is priority, traffic is never split.
      </p>
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>instance</th>
            <th>state</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {order.map((name, i) => {
            const f = data.fleet.find(x => x.name === name)
            const st = data.status[name] ?? 'unknown'
            const active = data.active === name
            return (
              <tr key={name}>
                <td className="muted">{i + 1}</td>
                <td>
                  {f?.display ?? name} <span className="muted">{name}</span>{' '}
                  {active && <span className="badge ok">active</span>}
                </td>
                <td>
                  <span className={stateClass(st)}>{st}</span>
                  {f && f.state !== 'running' && <span className="muted"> ({f.state})</span>}
                </td>
                <td className="btns">
                  <button disabled={i === 0} onClick={() => move(i, -1)}>
                    ↑
                  </button>
                  <button disabled={i === order.length - 1} onClick={() => move(i, 1)}>
                    ↓
                  </button>
                  <button className="danger" onClick={() => remove(i)}>
                    ×
                  </button>
                </td>
              </tr>
            )
          })}
          {order.length === 0 && (
            <tr>
              <td colSpan={4} className="muted">
                order is empty — every request gets 503 until you add an instance
              </td>
            </tr>
          )}
        </tbody>
      </table>
      <div className="llm-add">
        <select value="" onChange={e => add(e.target.value)}>
          <option value="" disabled>
            add instance…
          </option>
          {available.map(f => (
            <option key={f.name} value={f.name}>
              {f.display} ({f.name})
            </option>
          ))}
        </select>
        <button onClick={save} disabled={saving || !dirty}>
          save order
        </button>
        {dirty && <span className="muted">unsaved changes</span>}
      </div>
    </div>
  )
}
