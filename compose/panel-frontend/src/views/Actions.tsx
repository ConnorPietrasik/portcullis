// Actions — the panel-do registry, as a workbench instead of a wall of cards.
//
// Left: every action, grouped, searchable, with its args next to its name and
// a run button that fits the arg (a container picker for a 12-hex id, the
// compose services for a service name, the fleet for a gpu-* instance). Right:
// the console — the last run's output, pretty-printed, and this session's run
// history. The registry stays the only whitelist: this view renders what
// `panel-do --list` reports and posts to `/api/action/<name>`, nothing else.
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { usePoll } from '../usePoll'
import { ConfirmModal } from '../ui'
import { CopyBtn } from '../ui'
import { clockOf } from '../fmt'
import type { ActionInfo, ActionResult, ContainerInfo, StackService } from '../types'

const GROUPS = ['gpu', 'containers', 'services', 'updates', 'servitor']
const COLLAPSE_KEY = 'panel.actions.collapsed'

/** The registry's group, with a name-based fallback for a panel-do binary
 *  older than the field (the group is display metadata — never a gate). */
function groupOf(a: ActionInfo): string {
  if (a.group) return a.group
  if (a.name.startsWith('gpu-')) return 'gpu'
  if (a.name.startsWith('update') || a.name.startsWith('autoupdate')) return 'updates'
  if (a.name.startsWith('servitor')) return 'servitor'
  if (a.name.includes('container') || a.name === 'prune-docker' || a.name === 'edge-ips') return 'containers'
  if (a.name.includes('service')) return 'services'
  return 'other'
}

/** Which control one arg deserves, read off the registry's own pattern. */
function argKind(pattern: string | undefined): 'container' | 'gpu' | 'service' | 'onoff' | 'text' {
  const p = pattern ?? ''
  if (p.includes('0-9a-f]{12}')) return 'container'
  if (p.startsWith('^gpu-')) return 'gpu'
  if (p === '^(on|off)$') return 'onoff'
  if (p.includes('gpu-') || p.includes('0-9a-f')) return 'text'
  // a .service suffix is a systemd unit name (restart-service) — free text,
  // not a compose service the picker could offer
  if (p.includes('\\.service')) return 'text'
  // a bare token is a compose service name (start-service); an empty pattern
  // has no arg at all
  return p ? 'service' : 'text'
}

/** stdout as a human reads it: JSON (or JSON Lines) gets structure, anything
 *  else is already text. */
function pretty(out: string): string {
  const t = out.trim()
  if (!t) return ''
  const one = tryJson(t)
  if (one != null) return JSON.stringify(one, null, 2)
  const lines = t.split('\n')
  if (lines.length > 1) {
    const each = lines.map(l => tryJson(l.trim()))
    if (each.every(v => v != null)) return each.map(v => JSON.stringify(v)).join('\n')
  }
  return out
}

function tryJson(s: string): unknown {
  if (!s || !/^[[{]/.test(s)) return null
  try {
    return JSON.parse(s)
  } catch {
    return null
  }
}

interface Run {
  key: string
  name: string
  args: string[]
  at: number
  ms: number
  exit: number | null
  ok: boolean
  stdout: string
  stderr: string
  pending?: boolean
}

export default function Actions({ intervalMs }: { intervalMs: number }) {
  // The registry only changes when the panel-do binary is reinstalled.
  const reg = usePoll(() => api<{ actions: ActionInfo[] }>('/actions'), Math.max(60_000, intervalMs))
  // The arg pickers: live containers (12-hex ids) and the deployed compose
  // services — both cheap (panel-api caches them), both slow-refresh.
  const conts = usePoll(() => api<{ containers: ContainerInfo[] }>('/containers'), Math.max(15_000, intervalMs))
  const svcs = usePoll(() => api<{ services: StackService[] }>('/stack-services'), Math.max(60_000, intervalMs))

  const [q, setQ] = useState('')
  const [args, setArgs] = useState<Record<string, string[]>>({})
  const [runs, setRuns] = useState<Run[]>([])
  const [shown, setShown] = useState<string | null>(null)
  const [ask, setAsk] = useState<{ a: ActionInfo; args: string[] } | null>(null)
  const [collapsed, setCollapsed] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem(COLLAPSE_KEY) ?? '[]') as string[]
    } catch {
      return []
    }
  })
  const [, tick] = useState(0)

  useEffect(() => localStorage.setItem(COLLAPSE_KEY, JSON.stringify(collapsed)), [collapsed])
  // the elapsed counter of a pending run (a 10-minute servitor-sync is a lot
  // more patient when it visibly counts)
  useEffect(() => {
    if (!runs.some(r => r.pending)) return
    const t = window.setInterval(() => tick(n => n + 1), 1000)
    return () => window.clearInterval(t)
  }, [runs])

  const actions = reg.data?.actions ?? []
  const byName = useMemo(() => Object.fromEntries(actions.map(a => [a.name, a])) as Record<string, ActionInfo>, [actions])

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    if (!needle) return actions
    return actions.filter(a => `${a.name} ${a.summary} ${groupOf(a)}`.toLowerCase().includes(needle))
  }, [actions, q])

  const grouped = useMemo(() => {
    const m = new Map<string, ActionInfo[]>()
    for (const a of filtered) {
      const g = groupOf(a)
      m.set(g, [...(m.get(g) ?? []), a])
    }
    return [...m.entries()].sort((x, y) => {
      const [i, j] = [GROUPS.indexOf(x[0]), GROUPS.indexOf(y[0])]
      return (i < 0 ? 99 : i) - (j < 0 ? 99 : j) || x[0].localeCompare(y[0])
    })
  }, [filtered])

  const argList = (kind: ReturnType<typeof argKind>): { value: string; label: string }[] => {
    if (kind === 'container')
      return (conts.data?.containers ?? []).map(c => ({ value: c.id, label: `${c.name} · ${c.id}${c.state === 'running' ? '' : ' (down)'}` }))
    const all = svcs.data?.services ?? []
    if (kind === 'gpu') return all.filter(s => s.name.startsWith('gpu-')).map(s => ({ value: s.name, label: `${s.display} · ${s.name}` }))
    if (kind === 'service') return all.map(s => ({ value: s.name, label: `${s.display} · ${s.name}` }))
    if (kind === 'onoff')
      return [
        { value: 'on', label: 'on' },
        { value: 'off', label: 'off' },
      ]
    return []
  }

  async function run(a: ActionInfo, argv: string[]) {
    const key = `${a.name}-${Date.now()}`
    const start = Date.now()
    setRuns(rs => [{ key, name: a.name, args: argv, at: start, ms: 0, exit: null, ok: false, stdout: '', stderr: '', pending: true }, ...rs].slice(0, 25))
    setShown(key)
    let res: ActionResult
    try {
      res = await api<ActionResult>(`/action/${a.name}`, { method: 'POST', body: JSON.stringify({ args: argv }) })
    } catch (e) {
      res = { ok: false, exit_code: -1, stdout: '', stderr: String(e) }
    }
    setRuns(rs =>
      rs.map(r => (r.key === key ? { ...r, ...res, exit: res.exit_code ?? null, pending: false, ms: Date.now() - start } : r)),
    )
    // the fleet view is the usual reason to run one of these — refresh the
    // caches the pickers read so the next pick sees what this run changed
    void conts.refresh()
  }

  const current = runs.find(r => r.key === shown) ?? runs[0] ?? null

  if (reg.error) return <div className="banner err">actions: {reg.error}</div>
  if (!reg.data) return <div className="banner">loading…</div>

  return (
    <div className="acts">
      <section className="card alist">
        <div className="alist-bar">
          <input className="search" placeholder="filter" value={q} onChange={e => setQ(e.target.value)} autoFocus />
          <span className="sp" />
          <span className="muted">{filtered.length}</span>
        </div>
        {grouped.map(([group, list]) => (
          <details
            key={group}
            className="agroup"
            open={!collapsed.includes(group)}
            onToggle={e => {
              const open = (e.target as HTMLDetailsElement).open
              setCollapsed(c => (open ? c.filter(g => g !== group) : [...new Set([...c, group])]))
            }}
          >
            <summary>
              <span className="gn">{group}</span>
              <span className="gc">{list.length}</span>
            </summary>
            <div className="agroup-rows">
              {list.map(a => {
                const cur = args[a.name] ?? Array(a.arity).fill('')
                const missing = cur.some(v => v.trim() === '')
                const pending = runs.some(r => r.pending && r.name === a.name)
                return (
                  <div key={a.name} className={`arow${a.confirm ? ' warn' : ''}`}>
                    <code className="an" title={`${a.summary} · ${a.timeout_sec}s timeout`}>
                      {a.name}
                    </code>
                    <span className="as" title={a.summary}>
                      {a.summary}
                    </span>
                    <span className="actrls">
                      {Array.from({ length: a.arity }, (_, i) => {
                        const kind = argKind(a.patterns[i])
                        const opts = argList(kind)
                        return kind !== 'text' && opts.length > 0 ? (
                          <select
                            key={i}
                            className="aarg"
                            value={opts.some(o => o.value === cur[i]) ? cur[i] : ''}
                            onChange={e => {
                              const next = [...cur]
                              next[i] = e.target.value
                              setArgs({ ...args, [a.name]: next })
                            }}
                          >
                            <option value="" disabled>
                              {kind === 'onoff' ? 'on / off' : kind === 'gpu' ? 'gpu instance' : kind}
                            </option>
                            {opts.map(o => (
                              <option key={o.value} value={o.value}>
                                {o.label}
                              </option>
                            ))}
                          </select>
                        ) : (
                          <input
                            key={i}
                            className="aarg"
                            placeholder={a.patterns[i]?.includes('.service') ? 'unit.service' : 'arg'}
                            value={cur[i] ?? ''}
                            onChange={e => {
                              const next = [...cur]
                              next[i] = e.target.value
                              setArgs({ ...args, [a.name]: next })
                            }}
                          />
                        )
                      })}
                      <button
                        className={a.confirm ? 'warn' : ''}
                        disabled={pending || missing}
                        title={missing ? 'fill every arg first' : undefined}
                        onClick={() => {
                          if (a.confirm) setAsk({ a, args: cur.map(v => v.trim()) })
                          else void run(a, cur.map(v => v.trim()))
                        }}
                      >
                        {pending ? '…' : 'run'}
                      </button>
                    </span>
                  </div>
                )
              })}
            </div>
          </details>
        ))}
        {filtered.length === 0 && <div className="empty">nothing matches “{q}”</div>}
      </section>

      <section className="card console">
        {current ? (
          <>
            <div className="con-bar">
              <code className="an">
                {current.name}
                {current.args.filter(Boolean).map(a => ` ${a}`)}
              </code>
              <span className={`badge ${current.pending ? 'warn' : current.ok ? 'ok' : 'err'}`}>
                {current.pending ? `running ${Math.round((Date.now() - current.at) / 1000)}s` : current.exit === -1 ? 'failed' : `exit ${current.exit} · ${(current.ms / 1000).toFixed(1)}s`}
              </span>
              <span className="sp" />
              <span className="muted">{clockOf(current.at)}</span>
              <CopyBtn text={`${current.stdout}${current.stderr ? `\n${current.stderr}` : ''}`} title="copy output" />
            </div>
            <div className="con-body">
              {current.stdout && <pre className={`out ${current.ok ? 'ok' : 'err'}`}>{pretty(current.stdout)}</pre>}
              {current.stderr && <pre className="out err">{current.stderr}</pre>}
              {current.pending && !current.stdout && <pre className="out">running — {byName[current.name]?.summary ?? ''}</pre>}
            </div>
            {runs.length > 1 && (
              <div className="con-hist">
                {runs.map(r => (
                  <button key={r.key} className={r.key === current.key ? 'on' : ''} onClick={() => setShown(r.key)} title={`${r.name} ${r.args.join(' ')}`}>
                    <i className={r.pending ? 'p' : r.ok ? 'ok' : 'bad'} />
                    {clockOf(r.at)} <span>{r.name}</span>
                  </button>
                ))}
              </div>
            )}
          </>
        ) : (
          <div className="empty">nothing run yet</div>
        )}
      </section>

      {ask && (
        <ConfirmModal
          title={`run ${ask.a.name}?`}
          body={
            <div className="asklist">
              <p>{ask.a.summary}</p>
              {ask.args.filter(Boolean).length > 0 && (
                <p className="mono">
                  {ask.a.name} {ask.args.join(' ')}
                </p>
              )}
              <p className="muted">
                privileged · {ask.a.timeout_sec}s timeout
              </p>
            </div>
          }
          confirm="run"
          danger
          onClose={() => setAsk(null)}
          onConfirm={() => {
            const { a, args: argv } = ask
            setAsk(null)
            void run(a, argv)
          }}
        />
      )}
    </div>
  )
}
