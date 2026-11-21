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

function GpuTile({
  index,
  holder,
  reserved = null,
  reservedDisplay = null,
  gpu,
}: {
  index: number
  holder: string | null
  // optional — only the dedicated group below passes these; the main pool
  // grid renders unreserved cards only (its filter)
  reserved?: { for: string; note: string } | null
  reservedDisplay?: string | null
  gpu: Gpu | undefined
}) {
  return (
    <div className={`gpu-tile${holder ? ' held' : reserved ? ' reserved' : ' free'}`}>
      <div className="gpu-tile-top">
        <span className="gpu-idx">{index}</span>
        {holder ? (
          <span className="badge ok">{holder}</span>
        ) : reserved ? (
          <span
            className="badge"
            title={`reserved for ${reserved.for} — auto-grab never hands this card to another instance`}
          >
            {reserved.note || 'reserved'}
          </span>
        ) : (
          <span className="badge">free</span>
        )}
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
      {reserved && (
        <div
          className="gpu-tile-row"
          title={`reserved for ${reserved.for}${reserved.note ? ` — ${reserved.note}` : ''}: the auto-grab never hands it to another instance, and this instance's own grab lands on it first`}
        >
          <span className="muted">→ {reservedDisplay ?? reserved.for}</span>
        </div>
      )}
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
  const pool = usePoll(() => api<GpuPool>('/gpus'), intervalMs)
  // hardware readings (temp/VRAM/util) ride the metrics poll at the same
  // cadence, joined on pool index = ROCR index (the space
  // ROCR_VISIBLE_DEVICES counts in). The API supplies it as rocr_index (the
  // card's position in the KFD topology order — node id, host CPU node
  // skipped; the same files the ROCm runtime reads) — the sysfs card NAME is
  // not the ROCR index (on this host a non-GPU drm device takes card0, so
  // the GPUs are card1..card7, and joining on the name was off by one: tile
  // 0 had no readings, tiles 1+ showed the previous card's).
  const metrics = usePoll(() => api<Metrics>('/metrics'), intervalMs)

  const [busy, setBusy] = useState<string | null>(null)
  const [pickerFor, setPickerFor] = useState<string | null>(null)
  const [picked, setPicked] = useState<number[]>([])
  // the reserve-a-card form (the card dedication — e.g. the x8-linked card)
  const [reserving, setReserving] = useState(false)
  const [resCard, setResCard] = useState<number | null>(null)
  const [resFor, setResFor] = useState('')
  const [resNote, setResNote] = useState('')

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
  const reservedCards = data.pool.filter((p) => p.reserved != null)
  // "free" = open to the auto-grab: unheld AND unreserved (a reserved card
  // is free only for its owner — it is its dedication)
  const freeCount = data.pool.filter((p) => p.holder == null && p.reserved == null).length

  return (
    <div className="gpus-view">
      <div className="card">
        <h3>
          gpu pool{' '}
          <span className="muted">
            — {data.cards} cards · {freeCount} free
            {reservedCards.length ? ` · ${reservedCards.length} reserved` : ''}
          </span>
        </h3>
        <p className="muted">
          Cards are assigned per instance below: <code>auto</code> grabs the lowest free set (sized
          by the instance's <code>gpu.cards</code> label — a card reserved for that instance comes
          first), <code>pick</code> names exact cards. A card can be <b>reserved</b> for one
          instance — the physically-odd card (e.g. the x8-linked one) that must never be mixed into
          the others' pool: the auto-grab then never hands it to anyone else, another instance's pick
          of it is refused, and the reserving instance's own grab lands on it first. The pin is{' '}
          <code>ROCR_VISIBLE_DEVICES</code> at container start — an assignment (re)creates the
          container, and the set sticks across stops, updates and reboots.
        </p>
        <div className="gpu-pool">
          {data.pool
            .filter((p) => p.reserved == null) // reserved cards render in the dedicated group below
            .map((p) => (
              <GpuTile
                key={p.index}
                index={p.index}
                holder={holderDisplay(p.holder)}
                gpu={gpusByIndex.get(p.index)}
              />
            ))}
        </div>

        {reservedCards.length > 0 && (
          <div className="gpu-pool-dedicated">
            <div className="gpu-pool-caption">dedicated — outside the auto-grab pool</div>
            <div className="gpu-pool dedicated">
              {reservedCards.map((p) => (
                <GpuTile
                  key={p.index}
                  index={p.index}
                  holder={holderDisplay(p.holder)}
                  reserved={p.reserved}
                  reservedDisplay={instance(p.reserved!.for)?.display ?? p.reserved!.for}
                  gpu={gpusByIndex.get(p.index)}
                />
              ))}
            </div>
          </div>
        )}

        {(reservedCards.length > 0 || reserving) && (
          <div className="reservations">
            {reservedCards.map((p) => {
              const r = p.reserved!
              const inst = instance(r.for)
              return (
                <div key={p.index} className="reservation">
                  <span className="cardchip">{p.index}</span>
                  {r.note && <span className="muted">{r.note}</span>}
                  <span className="muted">→</span>
                  <span>
                    {inst?.display ?? r.for}
                    {inst && <span className="muted"> {r.for}</span>}
                  </span>
                  {p.holder && <span className="badge ok">held by {holderDisplay(p.holder)}</span>}
                  <button
                    className="danger"
                    disabled={busy !== null}
                    title="unreserve — the card rejoins the auto-grab pool for everyone"
                    onClick={() =>
                      act(`unreserve:${p.index}`, () =>
                        api<GpuPool>('/gpus/unreserve', {
                          method: 'POST',
                          body: JSON.stringify({ card: p.index }),
                        })
                      )
                    }
                  >
                    unreserve
                  </button>
                </div>
              )
            })}
            {!reserving ? (
              <button
                disabled={busy !== null || data.instances.length === 0}
                onClick={() => {
                  setResCard(null)
                  setResFor(data.instances[0]?.name ?? '')
                  setResNote('')
                  setReserving(true)
                }}
              >
                reserve a card…
              </button>
            ) : (
              <div className="reservation form">
                <span className="muted">card</span>
                {data.pool
                  .filter((p) => p.reserved == null || p.reserved!.for === resFor)
                  .map((p) => {
                    const owner = p.reserved?.for ?? null
                    const reRecord = owner != null && owner === resFor
                    const title = owner
                      ? reRecord
                        ? `already reserved for ${holderDisplay(owner) ?? owner} — applying re-records its BDF`
                        : `reserved for ${holderDisplay(owner) ?? owner} — select that instance to re-record, or unreserve first`
                      : p.holder
                        ? `card ${p.index} — held by ${holderDisplay(p.holder)} (reserving it for that holder is allowed)`
                        : `card ${p.index}`
                    return (
                      <label key={p.index} className="pick" title={title}>
                        <input
                          type="radio"
                          name="reserve-card"
                          checked={resCard === p.index}
                          onChange={() => setResCard(p.index)}
                        />
                        <span className="cardchip">{p.index}</span>
                      </label>
                    )
                  })}
                <span className="muted">for</span>
                <select value={resFor} onChange={(e) => setResFor(e.target.value)}>
                  {data.instances.map((i) => (
                    <option key={i.name} value={i.name}>
                      {i.display} ({i.name})
                    </option>
                  ))}
                </select>
                <input
                  type="text"
                  value={resNote}
                  maxLength={200}
                  placeholder="why — e.g. PCIe5 x8"
                  onChange={(e) => setResNote(e.target.value)}
                />
                <button
                  disabled={busy !== null || resCard == null || !resFor}
                  onClick={() => {
                    const card = resCard!
                    const forSvc = resFor
                    const note = resNote
                    setReserving(false)
                    void act(`reserve:${card}`, () =>
                      api<GpuPool>('/gpus/reserve', {
                        method: 'POST',
                        body: JSON.stringify({ card, for: forSvc, note }),
                      })
                    )
                  }}
                >
                  apply
                </button>
                <button disabled={busy !== null} onClick={() => setReserving(false)}>
                  cancel
                </button>
              </div>
            )}
          </div>
        )}
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
          instances <span className="muted">— fleet (gpu-*) from the deployed compose</span>
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
                        title="start on boot (gpu-autostart.service, in list order)"
                        onChange={(e) =>
                          act(`autostart:${inst.name}`, () =>
                            api<GpuPool>('/gpus/autostart', {
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
                              return api<GpuPool>('/gpus/stop', {
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
                              api<GpuPool>('/gpus/start', {
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
                            ? 'move to the lowest free cards — its own reserved cards first (recreates — brief downtime)'
                            : 'grab the lowest free cards — its own reserved cards first + start'
                        }
                        disabled={busy !== null}
                        onClick={() =>
                          act(`auto:${inst.name}`, async () => {
                            if (
                              inst.running &&
                              !window.confirm(`move ${inst.display} to the lowest free cards? (the model reloads)`)
                            )
                              return
                            return api<GpuPool>('/gpus/assign', {
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
                          const reserved = data.pool[c]?.reserved ?? null
                          // fenced two ways: held by someone else, OR reserved for
                          // someone else — the reservation is the x8 card's fence
                          const blockedHolder = holder != null && holder !== inst.name
                          const blockedResv = reserved != null && reserved.for !== inst.name
                          const blocked = blockedHolder || blockedResv
                          const bits: string[] = []
                          if (blockedHolder && holder) bits.push(`held by ${holder}`)
                          if (blockedResv && reserved)
                            bits.push(`reserved for ${reserved.for}${reserved.note ? ` — ${reserved.note}` : ''}`)
                          return (
                            <label
                              key={c}
                              className={`pick${blocked ? ' blocked' : ''}`}
                              title={bits.length ? bits.join(' · ') : `card ${c}`}
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
                              api<GpuPool>('/gpus/assign', {
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
                  no gpu-* services in the deployed compose
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <p className="muted">
          <b>boot</b> = comes up automatically after a reboot (gpu-autostart.service, in list
          order; if two saved sets overlap, the later one is moved to other free cards — see the
          warnings above). <b>add a second instance of a model</b> (e.g. gpu-27b-1 on cards 2,3):
          copy the compose block, its radiance config, + the router pair in this repo (<code>make doctor</code> checks all six name references match), <code>make update</code>, then assign cards here — see docs/RUNBOOK.md → GPU fleet.
        </p>
      </div>
    </div>
  )
}
