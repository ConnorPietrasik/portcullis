import { useEffect, useState } from 'react'
import { api } from '../api'
import { usePoll } from '../usePoll'
import type { ActionResult, LLMSettings, UpdateReport } from '../types'

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
      <AutoUpdateCard intervalMs={intervalMs} />
    </div>
  )
}

function AutoUpdateCard({ intervalMs }: { intervalMs: number }) {
  // timer_enabled comes from the update-report poll (panel-do asks
  // systemctl): null when the unit isn't installed (`make autoupdate`).
  // Same data-budget rule as Overview: never faster than the user's interval.
  const { data, refresh } = usePoll(() => api<UpdateReport>('/update'), Math.max(30_000, intervalMs))
  const [busy, setBusy] = useState(false)
  const enabled = data?.timer_enabled ?? null

  const toggle = async (on: boolean) => {
    // The registry's Confirm:true is honored by the Actions view only — this
    // card POSTs directly, so it confirms itself. Enable gets the extra line:
    // systemd elapses a past-due OnBootSec immediately on activation, so on a
    // box already up the first tick (a gated update-run) starts at once.
    if (
      !window.confirm(
        on
          ? 'enable the daily auto-update timer?\n\nit fires 2 min after boot + daily — on a box that is already up, the first tick fires immediately (the same gated, auto-rolling-back make update; a dirty clone skips)'
          : 'disable the daily auto-update timer?',
      )
    )
      return
    setBusy(true)
    try {
      const r = await api<ActionResult>('/action/autoupdate-toggle', {
        method: 'POST',
        body: JSON.stringify({ args: [on ? 'on' : 'off'] }),
      })
      if (!r.ok) window.alert(r.stderr || r.stdout)
      await refresh()
    } catch (e) {
      window.alert(String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="card">
      <h3>auto-update</h3>
      <p className="muted">
        Re-runs <code>make update</code> daily (and after boot): floating container images, panel-web's build
        bases, npm/pip within their declared ranges — with the same doctor precondition, snapshot, health gate and
        auto-rollback as a manual run. It applies only what the server clone holds — no git pull; a dirty clone is
        skipped, and majors only land when you commit them.
      </p>
      <p className="muted">
        {enabled == null ? (
          'timer not installed — run <code>make autoupdate</code> on the server'
        ) : enabled ? (
          <b className="t-ok">enabled</b>
        ) : (
          <b className="t-warn">disabled</b>
        )}
        {data?.state?.status === 'in-progress' && <span className="t-warn"> · update in progress</span>}
      </p>
      <div className="btns">
        <button onClick={() => toggle(false)} disabled={busy || enabled == null}>
          disable
        </button>
        <button onClick={() => toggle(true)} disabled={busy || enabled == null}>
          enable
        </button>
      </div>
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
