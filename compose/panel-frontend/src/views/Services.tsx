import { useState } from 'react'
import { api } from '../api'
import { usePoll } from '../usePoll'
import type { ActionResult, ContainerInfo, StackService } from '../types'

// Grouping key: the panel.category compose label (RUNBOOK → "add an app").
// Order: pinned list first, then unknown categories alphabetically, then
// uncategorized last. One line to tweak as the group set grows.
const CATEGORY_ORDER = ['LLM', 'Panel', 'Web']
const OTHER = 'Other'

// Collapse state is a per-browser VIEW preference (the grouping itself is
// data, from the compose labels), so localStorage keyed by category name.
const COLLAPSED_KEY = 'panel.svc.collapsed'

function categoryOf(x: { category?: string | null }): string {
  return x.category && x.category.trim() ? x.category.trim() : OTHER
}

function orderedCats(cats: string[]): string[] {
  const pinned = CATEGORY_ORDER.filter((c) => cats.includes(c))
  const rest = cats
    .filter((c) => !CATEGORY_ORDER.includes(c) && c !== OTHER)
    .sort()
  return [...pinned, ...rest, ...(cats.includes(OTHER) ? [OTHER] : [])]
}

function loadCollapsed(): Record<string, boolean> {
  try {
    const v = JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? '{}')
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, boolean>) : {}
  } catch {
    return {} // corrupt or first visit → everything open
  }
}

function stateClass(state: string): string {
  if (state === 'running') return 'badge ok'
  if (state === 'paused') return 'badge warn'
  return 'badge err'
}

export default function Services({ intervalMs }: { intervalMs: number }) {
  const { data, error, refresh } = usePoll(
    () => api<{ containers: ContainerInfo[] }>('/containers'),
    intervalMs,
  )
  // The defined set only changes via `make update` — poll slower than the
  // container list, and never faster than the user's interval (same data-
  // budget rule as the update views: a metered phone sees this effectively on
  // load; action buttons refresh it immediately anyway). With a panel-do
  // binary that predates list-services this 500s; the section simply stays
  // hidden instead of erroring the whole view.
  const svcs = usePoll(() => api<{ services: StackService[] }>('/stack-services'), Math.max(15_000, intervalMs))

  const [collapsed, setCollapsed] = useState<Record<string, boolean>>(loadCollapsed)
  function setCatOpen(cat: string, open: boolean) {
    const next = { ...collapsed }
    if (open) delete next[cat]
    else next[cat] = true
    setCollapsed(next)
    try {
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify(next))
    } catch {
      // private mode etc. — collapse state just won't persist
    }
  }

  async function act(c: ContainerInfo, action: string) {
    if (action !== 'start' && !window.confirm(`${action} ${c.name}?`)) return
    try {
      if (action === 'start' && c.service?.startsWith('llm-')) {
        // GPU-aware start: the saved card set when free, else the lowest
        // free set — a raw `docker start` would reuse the create-time env
        // and could resurrect cards another instance now owns (GPU view).
        await api('/llm/gpus/start', { method: 'POST', body: JSON.stringify({ service: c.service }) })
      } else {
        await api(`/containers/${c.id}/${action}`, { method: 'POST' })
      }
      await refresh()
    } catch (e) {
      window.alert(String(e))
    }
  }

  async function bringUp(s: StackService) {
    if (!window.confirm(`create + start ${s.display} (${s.name})?`)) return
    try {
      // The generic action endpoint answers 200 + {ok:false, stderr} on a
      // panel-do failure (unlike the container endpoint, which raises 502)
      // — surface that instead of silently refreshing.
      if (s.name.startsWith('llm-')) {
        // GPU-aware create+start: picks the instance's saved cards (or the
        // lowest free set) and writes the assignment — see the GPU view.
        await api('/llm/gpus/start', { method: 'POST', body: JSON.stringify({ service: s.name }) })
      } else {
        const res = await api<ActionResult>('/action/start-service', {
          method: 'POST',
          body: JSON.stringify({ args: [s.name] }),
        })
        if (!res.ok)
          throw new Error(`start-service failed (exit ${res.exit_code}): ${res.stderr || res.stdout}`)
      }
      await refresh()
      await svcs.refresh()
    } catch (e) {
      window.alert(String(e))
    }
  }

  // Defined services whose container no longer exists (pruned, or never
  // created — e.g. a fleet instance `make update` deliberately left down).
  const have = new Set((data?.containers ?? []).map((c) => c.service).filter(Boolean))
  const missing = (svcs.data?.services ?? []).filter((s) => !have.has(s.name))

  if (error) return <div className="banner err">containers: {error}</div>
  if (!data) return <div className="banner">loading…</div>

  // Both tables group by category (uncategorized → "Other"). The collapse
  // state is shared, so a closed "LLM" closes in both sections at once.
  const groups = new Map<string, ContainerInfo[]>()
  for (const c of data.containers) {
    const cat = categoryOf(c)
    const list = groups.get(cat)
    if (list) list.push(c)
    else groups.set(cat, [c])
  }
  const cats = orderedCats([...groups.keys()])

  const missGroups = new Map<string, StackService[]>()
  for (const s of missing) {
    const cat = categoryOf(s)
    const list = missGroups.get(cat)
    if (list) list.push(s)
    else missGroups.set(cat, [s])
  }
  const missCats = orderedCats([...missGroups.keys()])

  return (
    <>
      {data.containers.length === 0 ? (
        <div className="banner">
          no containers carry the <code>panel.manage</code> label — add it to services you want to
          control here (see docs/RUNBOOK.md → “add an app”)
        </div>
      ) : (
        cats.map((cat) => {
          const rows = groups.get(cat)!
          const up = rows.filter((c) => c.state === 'running').length
          return (
            <details
              key={cat}
              className="svc-cat"
              open={!collapsed[cat]}
              onToggle={(e) => setCatOpen(cat, (e.currentTarget as HTMLDetailsElement).open)}
            >
              <summary>
                <span className="svc-cat-name">{cat}</span>
                <span className="muted">
                  {up}/{rows.length} up
                </span>
              </summary>
              <table>
                <thead>
                  <tr>
                    <th>name</th>
                    <th>image</th>
                    <th>state</th>
                    <th>status</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c) => (
                    <tr key={c.id}>
                      <td>{c.name}</td>
                      <td className="muted">{c.image}</td>
                      <td>
                        <span className={stateClass(c.state)}>{c.state}</span>
                      </td>
                      <td className="muted">{c.status}</td>
                      <td className="btns">
                        {/* running only — while the instance is down the /open bounce lands on a 502 */}
                        {c.state === 'running' && c.service?.startsWith('llm-') && (
                          <a
                            href={`https://${c.service.slice(4)}.pietrasik.top/open`}
                            target="_blank"
                            rel="noreferrer"
                            title="radiance dashboard — Authelia, then the API key is injected for this origin (panel-api /open)"
                          >
                            dashboard ↗
                          </a>
                        )}
                        <button onClick={() => act(c, 'start')}>start</button>
                        <button className="warn" onClick={() => act(c, 'restart')}>
                          restart
                        </button>
                        <button className="danger" onClick={() => act(c, 'stop')}>
                          stop
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          )
        })
      )}
      {missing.length > 0 && (
        <>
          <h2 className="svc-missing">defined, no container</h2>
          {missCats.map((cat) => (
            <details
              key={cat}
              className="svc-cat"
              open={!collapsed[cat]}
              onToggle={(e) => setCatOpen(cat, (e.currentTarget as HTMLDetailsElement).open)}
            >
              <summary>
                <span className="svc-cat-name">{cat}</span>
                <span className="muted">{missGroups.get(cat)!.length} defined</span>
              </summary>
              <table>
                <thead>
                  <tr>
                    <th>service</th>
                    <th>image</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {missGroups.get(cat)!.map((s) => (
                    <tr key={s.name}>
                      <td>
                        {s.display} <span className="muted">{s.name}</span>
                      </td>
                      <td className="muted">{s.image}</td>
                      <td className="btns">
                        <button onClick={() => bringUp(s)}>create + start</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          ))}
        </>
      )}
    </>
  )
}
