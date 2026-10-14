import { api } from '../api'
import { usePoll } from '../usePoll'
import type { ContainerInfo } from '../types'

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

  async function act(c: ContainerInfo, action: string) {
    if (action !== 'start' && !window.confirm(`${action} ${c.name}?`)) return
    try {
      await api(`/containers/${c.id}/${action}`, { method: 'POST' })
      await refresh()
    } catch (e) {
      window.alert(String(e))
    }
  }

  if (error) return <div className="banner err">containers: {error}</div>
  if (!data) return <div className="banner">loading…</div>
  if (data.containers.length === 0)
    return (
      <div className="banner">
        no containers carry the <code>panel.manage</code> label — add it to services you want to
        control here (see docs/RUNBOOK.md → “add an app”)
      </div>
    )

  return (
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
  )
}
