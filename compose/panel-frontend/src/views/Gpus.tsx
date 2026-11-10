// GPU pool: which cards each LLM instance runs on, auto-grab from the free
// set, and the boot-autostart list. The server is the arbiter — this view
// only renders and asks; every mutation re-derives the whole view from the
// response (assigned = the ledger's intent, holding = the live truth).
import { Fragment, useState } from 'react'
import { api } from '../api'
import { usePoll } from '../usePoll'
import { Meter } from '../ui'
import { mem, num } from '../fmt'
import type { Gpu, GpuPool, LlmGpuInstance, Metrics } from '../types'

function stateClass(state: string): string {
  if (state === 'running') return 'badge ok'
  if (state === 'absent') return 'badge'
  return 'badge warn'
}

function Cards({ xs, tone }: { xs: number[] | null; tone?: 'bad' }) {
  if (!xs || xs.length === 0) return <span className="muted">—</span>
  return (
    <span className="cardset">
      {xs.map((c) => (
        <span key={c} className={`cardchip${tone === 'bad' ? ' bad' : ''}`}>
          {c}
        </span>
      ))}
    </span>
  )
}

function GpuTile({ index, holder, gpu }: { index: number; holder: string | null; gpu: Gpu | undefined }) {
  return (
    <div className={`gpu-tile${holder ? ' held' : ' free'}`}>
      <div className="gpu-tile-top">
        <span className="gpu-idx">{index}</span>
        <span className={holder ? 'badge ok' : 'badge'}>{holder ? holder : 'free'}</span>
      </div>
      <div className="gpu-tile-row">
        {gpu ? (
          <>
            <span className="muted" title="edge temperature">
              {num(gpu.temp_edge_c)}°C
            </span>
            <span className="muted" title="VRAM used / total">
              {mem(gpu.vram_used_mib)}/{mem(gpu.vram_total_mib)}
            </span>
          </>
        ) : (
          <span className="muted">—</span>
        )}
      </div>
      {gpu && gpu.util_pct != null && (
        <div className="gpu-tile-row" title={`utilisation ${gpu.util_pct}%`}>
          <span className="b">
            <Meter pct={gpu.util_pct / 100} tone={gpu.util_pct >= 90 ? 'warn' : 'load'} />
          </span>
        </div>
      )}
    </div>
  )
}

export default function Gpus({ intervalMs }: { intervalMs: number }) {
  const pool = usePoll(() => api<GpuPool>('/llm/gpus'), intervalMs)
  // hardware readings (temp/VRAM/util) ride the metrics poll at the same
  // cadence, joined on pool index = ROCR index (the space
  // ROCR_VISIBLE_DEVICES counts in). The API supplies it as rocr_index (the
  // card's position in the KFD topology order — node id, host CPU node
  // skipped; the same files the ROCm runtime reads) — the sysfs card NAME is
  // not the ROCR index (on this host a non-GPU drm device takes card0, so
  // the GPUs are card1..card6, and joining on the name was off by one: tile
  // 0 had no readings, tiles 1+ showed the previous card's).
  const metrics = usePoll(() => api<Metrics>('/metrics'), intervalMs)

  const [busy, setBusy] = useState<string | null>(null)
  const [pickerFor, setPickerFor] = useState<string | null>(null)
  const [picked, setPicked] = useState<number[]>([])

  const data = pool.data
  const gpusByIndex = new Map<number, Gpu>()
  for (const g of metrics.data?.gpus ?? []) {
    gpusByIndex.set(g.rocr_index, g)
  }

  // Every mutation endpoint responds with the fresh pool view
  // (panel-api's _gpu_pool_now) — adopt it so the view updates
  // immediately instead of waiting for the next poll tick.
  async function act(key: string, fn: () => Promise<GpuPool | undefined>) {
    setBusy(key)
    try {
      const next = await fn()
      if (next) pool.adopt(next)
    } catch (e) {
      window.alert(String(e))
    }
    setBusy(null)
  }

  function openPicker(inst: LlmGpuInstance) {
    setPickerFor(inst.name)
    // seed from what it holds, else the ledger's set, else nothing
    setPicked(inst.holding ?? inst.assigned ?? [])
  }

  function toggleCard(c: number) {
    setPicked((p) => (p.includes(c) ? p.filter((x) => x !== c) : [...p, c]))
  }

  const instance = (name: string) => data?.instances.find((i) => i.name === name)

  if (pool.error) return <div className="banner err">gpu pool: {pool.error}</div>
  if (!data) return <div className="banner">loading…</div>

  const holderDisplay = (name: string | null) => (name ? instance(name)?.display ?? name : null)
  const freeCount = data.pool.filter((p) => p.holder == null).length

  return (
    <div className="gpus-view">
      <div className="card">
        <h3>
          gpu pool <span className="muted">— {data.cards} cards · {freeCount} free</span>
        </h3>
        <p className="muted">
          Cards are assigned per instance below: <code>auto</code> grabs the lowest free set (sized
          by the instance's <code>--tp</code>), <code>pick</code> names exact cards. The pin is{' '}
          <code>ROCR_VISIBLE_DEVICES</code> at container start — an assignment (re)creates the
          container, and the set sticks across stops, updates and reboots.
        </p>
        <div className="gpu-pool">
          {data.pool.map((p) => (
            <GpuTile key={p.index} index={p.index} holder={holderDisplay(p.holder)} gpu={gpusByIndex.get(p.index)} />
          ))}
        </div>
      </div>

      {data.warnings.length > 0 && (
        <div className="banner warn">
          {data.warnings.map((w, i) => (
            <div key={i}>{w}</div>
          ))}
        </div>
      )}

      <div className="card">
        <h3>
          instances <span className="muted">— fleet (llm-*) from the deployed compose</span>
        </h3>
        <table>
          <thead>
            <tr>
              <th>instance</th>
              <th>cards</th>
              <th>assigned</th>
              <th>state</th>
              <th>boot</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {data.instances.map((inst) => {
              const drift =
                inst.assigned && inst.holding && JSON.stringify(inst.assigned) !== JSON.stringify(inst.holding)
              return (
                <Fragment key={inst.name}>
                  <tr>
                    <td>
                      {inst.display} <span className="muted">{inst.name}</span>
                      {inst.autostart && <span className="badge">boot</span>}
                    </td>
                    <td className="muted">{inst.cards_needed}</td>
                    <td>
                      <Cards xs={inst.assigned} tone={drift ? 'bad' : undefined} />
                      {drift && inst.holding && (
                        <span
                          className="muted"
                          title="the live container pins different cards (a failed apply, or a hand-run compose)"
                        >
                          {' '}
                          → holds <Cards xs={inst.holding} tone="bad" />
                        </span>
                      )}
                    </td>
                    <td>
                      <span className={stateClass(inst.state)}>{inst.state}</span>
                    </td>
                    <td>
                      <input
                        type="checkbox"
                        checked={inst.autostart}
                        disabled={busy !== null}
                        title="start on boot (llm-autostart.service, in list order)"
                        onChange={(e) =>
                          act(`autostart:${inst.name}`, () =>
                            api<GpuPool>('/llm/gpus/autostart', {
                              method: 'POST',
                              body: JSON.stringify({ service: inst.name, enabled: e.target.checked }),
                            }))
                        }
                      />
                    </td>
                    <td className="btns">
                      {inst.running ? (
                        <button
                          className="danger"
                          disabled={busy !== null}
                          onClick={() =>
                            act(`stop:${inst.name}`, async () => {
                              if (!window.confirm(`stop ${inst.display}?`)) return
                              return api<GpuPool>('/llm/gpus/stop', {
                                method: 'POST',
                                body: JSON.stringify({ service: inst.name }),
                              })
                            })
                          }
                        >
                          stop
                        </button>
                      ) : (
                        <button
                          disabled={busy !== null}
                          onClick={() =>
                            act(`start:${inst.name}`, () =>
                              api<GpuPool>('/llm/gpus/start', {
                                method: 'POST',
                                body: JSON.stringify({ service: inst.name }),
                              }))
                          }
                        >
                          start
                        </button>
                      )}
                      <button
                        title={
                          inst.running
                            ? 'move to the lowest free cards (recreates — brief downtime)'
                            : 'grab the lowest free cards + start'
                        }
                        disabled={busy !== null}
                        onClick={() =>
                          act(`auto:${inst.name}`, async () => {
                            if (
                              inst.running &&
                              !window.confirm(`move ${inst.display} to the lowest free cards? (the model reloads)`)
                            )
                              return
                            return api<GpuPool>('/llm/gpus/assign', {
                              method: 'POST',
                              body: JSON.stringify({ service: inst.name, gpus: 'auto' }),
                            })
                          })
                        }
                      >
                        auto
                      </button>
                      <button
                        disabled={busy !== null}
                        onClick={() => (pickerFor === inst.name ? setPickerFor(null) : openPicker(inst))}
                      >
                        pick…
                      </button>
                    </td>
                  </tr>
                  {pickerFor === inst.name && (
                    <tr className="pick-row">
                      <td colSpan={6}>
                        <span className="muted">
                          {inst.name}: pick exactly {inst.cards_needed} card(s)
                        </span>{' '}
                        {Array.from({ length: data.cards }, (_, c) => {
                          const holder = data.pool[c]?.holder ?? null
                          const blocked = holder != null && holder !== inst.name
                          return (
                            <label
                              key={c}
                              className={`pick${blocked ? ' blocked' : ''}`}
                              title={blocked ? `held by ${holder}` : `card ${c}`}
                            >
                              <input
                                type="checkbox"
                                disabled={blocked}
                                checked={picked.includes(c)}
                                onChange={() => toggleCard(c)}
                              />
                              <span className="cardchip">{c}</span>
                            </label>
                          )
                        })}{' '}
                        <button
                          disabled={busy !== null || picked.length !== inst.cards_needed}
                          onClick={() => {
                            const gpus = picked.join(',')
                            const name = inst.name
                            setPickerFor(null)
                            void act(`pick:${name}`, () =>
                              api<GpuPool>('/llm/gpus/assign', {
                                method: 'POST',
                                body: JSON.stringify({ service: name, gpus }),
                              }))
                          }}
                        >
                          apply
                        </button>{' '}
                        <button disabled={busy !== null} onClick={() => setPickerFor(null)}>
                          cancel
                        </button>
                        {picked.length !== inst.cards_needed && (
                          <span className="muted">
                            {' '}
                            ({picked.length}/{inst.cards_needed} selected)
                          </span>
                        )}
                      </td>
                    </tr>
                  )}
                </Fragment>
              )
            })}
            {data.instances.length === 0 && (
              <tr>
                <td colSpan={6} className="muted">
                  no llm-* services in the deployed compose
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <p className="muted">
          <b>boot</b> = comes up automatically after a reboot (llm-autostart.service, in list
          order; if two saved sets overlap, the later one is moved to other free cards — see the
          warnings above). <b>add a second instance of a model</b> (e.g. llm-27b-1 on cards 2,3):
          copy the compose block + the router pair in this repo (<code>make doctor</code> checks all four name references match), <code>make update</code>, then assign cards here — see docs/RUNBOOK.md → LLM fleet.
        </p>
      </div>
    </div>
  )
}
