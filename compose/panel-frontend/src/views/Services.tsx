import { api } from '../api'
import { usePoll } from '../usePoll'
import type { ActionResult, ContainerInfo, StackService } from '../types'

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
  // container list. With a panel-do binary that predates list-services this
  // 500s; the section simply stays hidden instead of erroring the whole view.
  const svcs = usePoll(() => api<{ services: StackService[] }>('/stack-services'), 15_000)

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

  return (
    <>
      {data.containers.length === 0 ? (
        <div className="banner">
          no containers carry the <code>panel.manage</code> label — add it to services you want to
          control here (see docs/RUNBOOK.md → “add an app”)
        </div>
      ) : (
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
            {data.containers.map((c) => (
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
      )}
      {missing.length > 0 && (
        <>
          <h2 className="svc-missing">defined, no container</h2>
          <table>
            <thead>
              <tr>
                <th>service</th>
                <th>image</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {missing.map((s) => (
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
        </>
      )}
    </>
  )
}
