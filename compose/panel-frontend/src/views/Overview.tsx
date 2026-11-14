// ─────────────────────────────── Overview ───────────────────────────────
// The one page that answers "is the box okay?" without scrolling through
// tables. Reading order is the order you'd ask the questions:
//   host strip  → is anything on fire right now
//   GPU cards   → are the cards working, full, hot. Condensed by default —
//                 one at-a-glance row per card (load with clocks and fan,
//                 VRAM, junction with mem temp, power), the whole pool on
//                 screen — every sensor's 5-min trace expands under the
//                 header on demand, remembered per card.
//   storage     → will I run out of room
//   fleet       → is what should be running running (and which model is serving)
// Indicators are bars (level) and traces (trend) — see ui.tsx for the colour
// rules. The API is polled every intervalMs and the samples are kept (see
// history.ts) so every panel here has a past, not just a number.

import { useEffect, useState } from 'react'
import { api } from '../api'
import { usePoll } from '../usePoll'
import { useHistory, seriesOf, tail } from '../history'
import type { Sample } from '../history'
import type { ActionResult, ContainerInfo, Disk, Gpu, LLMSettings, Metrics, Sensor, UpdatePreview, UpdateReport } from '../types'
import {
  axis,
  bitsPerSec,
  bytesPerSec,
  clockOf,
  lastOf,
  mem,
  num,
  relTime,
  spread,
  tone as toneOf,
  trend,
  updateTs,
  uptime,
} from '../fmt'
import type { Tone } from '../fmt'
import { Big, Chart, Chip, Meter, Pill, Stack, Tile, TrendMark } from '../ui'

// Where amber starts and red starts, in one table, as fractions of each
// sensor's own scale. VRAM and power are deliberately absent: an LLM server
// preallocates all of the VRAM, and the cards are power-limited, so a full
// bar means the box is at its set point — working as intended, not a
// warning — and colouring it red every time would train you to ignore red.
const T = {
  cpuUse: { warn: 0.85, crit: 0.95 },
  cpuTemp: { max: 100, warn: 0.78, crit: 0.92 },
  edge: { max: 110, warn: 0.72, crit: 0.86 },
  junction: { max: 120, warn: 0.75, crit: 0.88 },
  memTemp: { max: 110, warn: 0.82, crit: 0.91 },
  ram: { warn: 0.8, crit: 0.92 },
  swap: { warn: 0.55, crit: 0.85 },
  disk: { warn: 0.8, crit: 0.9 },
  loadPerCore: { warn: 0.85, crit: 1.15 },
}

// Physical ceilings for the fan and clock traces. axis() scales to the data
// up to the ceiling, so these only stop the scale clipping a real reading:
// the ASRock R9700 blower tops out at ~3725 rpm sustained (the ASUS board's
// hits 3967) and NAVI 48 boost is 2920 MHz.
const FAN_CEIL_RPM = 4000
const CLOCK_CEIL_MHZ = 3000

const shortCpu = (m: string) =>
  m
    .replace(/\b(advanced micro devices|amd|intel\(r\)|intel|genuineintel)\b/gi, ' ')
    .replace(/\((?:r|c|tm)\)/gi, ' ')
    .replace(/\b\d+-core\b/gi, ' ')
    .replace(/\bprocessor\b/gi, ' ')
    .replace(/\s+/g, ' ')
    .trim()

/** Re-render on a clock so "updated 3 s ago" is true, not true-at-fetch-time. */
function useNow(ms = 1000): number {
  const [n, setN] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setN(Date.now()), ms)
    return () => window.clearInterval(t)
  }, [ms])
  return n
}

function gpuState(util: number | null, junction: number | null): { label: string; tone: Tone; title: string } {
  if (junction != null && junction >= T.junction.max * T.junction.crit)
    return { label: 'THROTTLE RISK', tone: 'crit', title: `junction ${junction.toFixed(0)} °C` }
  if (junction != null && junction >= T.junction.max * T.junction.warn)
    return { label: 'HOT', tone: 'warn', title: `junction ${junction.toFixed(0)} °C` }
  if (util == null) return { label: 'no readings', tone: 'na', title: 'sysfs gave nothing for this card' }
  if (util >= 90) return { label: 'SATURATED', tone: 'load', title: 'GPU busy ≥ 90 % — this is the card working, not a fault' }
  if (util >= 25) return { label: 'WORKING', tone: 'load', title: `GPU busy ${util.toFixed(0)} %` }
  if (util > 3) return { label: 'LIGHT', tone: 'muted', title: `GPU busy ${util.toFixed(0)} %` }
  return { label: 'IDLE', tone: 'muted', title: 'nothing is using this card' }
}

/**
 * Condensed by default so the whole pool reads at a glance; this remembers,
 * per card, whether the user opened the full detail. Keyed by the physical
 * card — the same identity React keys the <GpuCard> by — so the remembered
 * state follows the device even if its ROCR index shifts between polls.
 * localStorage — survives tab switches and reloads, and a card the user
 * never expanded stays condensed.
 */
function useGpuOpen(card: string): [boolean, () => void] {
  const key = `panel.ov.gpu.${card}`
  const [open, set] = useState(() => {
    try {
      return localStorage.getItem(key) === '1'
    } catch {
      return false
    }
  })
  // The write is a side effect of `open`, so it lives in an effect — not in
  // the setState updater, which React may invoke twice under StrictMode.
  useEffect(() => {
    try {
      localStorage.setItem(key, open ? '1' : '0')
    } catch {
      /* storage unavailable — the card simply won't remember */
    }
  }, [key, open])
  const toggle = () => set((o) => !o)
  return [open, toggle]
}

export default function Overview({ intervalMs }: { intervalMs: number }) {
  const { data, error, last } = usePoll(() => api<Metrics>('/metrics'), intervalMs)
  const hist = useHistory(data)
  const now = useNow(500)
  // Auto-update: the report is the control-plane poll (state file + live
  // images — cheap, and the in-progress banner tails the run log through it),
  // the preview is what the NEXT update would apply (local git + file facts).
  // Neither is essential — a failure costs a banner/card, never the metrics.
  // Respect the user's data budget (Settings → refresh interval): the update
  // polls never run FASTER than the chosen interval. The 10 s / 30 s floors
  // only apply at the fast desktop default — on a phone set to minutes- or
  // hours (metered data) the update views refresh at that cadence too, i.e.
  // effectively on load; the in-progress log tail then shows a snapshot, not
  // a live feed. Button actions refresh immediately regardless (below).
  const upd = usePoll(() => api<UpdateReport>('/update'), Math.max(10_000, intervalMs))
  const prev = usePoll(() => api<UpdatePreview>('/update/preview'), Math.max(30_000, intervalMs))
  const [updBusy, setUpdBusy] = useState(false)

  const runUpdate = async (force: boolean) => {
    const majors = (prev.data?.changes ?? []).filter((c) => c.change === 'major')
    const dirty = (prev.data?.dirty ?? []).length > 0
    const gitError = prev.data?.git_error ?? null
    const msg =
      'run the stack update now?\n\n' +
      (prev.data == null || prev.error != null
        ? // The preview poll failed (or never succeeded) — don't claim a
          // clean preview the API could not confirm; the action re-checks
          // the clone itself (dirty skips, unreadable status refuses).
          'the pending-changes preview could not be read just now — the update still re-checks the clone (a dirty one skips, an unreadable status refuses)\n\n'
        : majors.length > 0
          ? `MAJOR changes pending:\n${majors.map((m) => `  ${m.kind} ${m.name}: ${m.old} → ${m.new}`).join('\n')}\n\n`
          : 'no MAJOR line changes detected\n') +
      (force && dirty ? 'the server clone has uncommitted changes — they will be applied too.\n\n' : '') +
      (force && gitError != null
        ? 'the server clone’s git status is UNREADABLE — the auto-update refuses it; this applies the working tree as-is.\n\n'
        : '') +
      'The update is gated (health + doctor) and auto-rolls-back on failure.'
    if (!window.confirm(msg)) return
    setUpdBusy(true)
    try {
      const r = await api<ActionResult>('/update', { method: 'POST', body: JSON.stringify({ force }) })
      if (!r.ok) window.alert(r.stderr || r.stdout)
      await upd.refresh()
    } catch (e) {
      window.alert(String(e))
    } finally {
      setUpdBusy(false)
    }
  }

  const rollbackUpdate = async () => {
    if (!window.confirm('roll the stack back to the pre-update snapshot?')) return
    setUpdBusy(true)
    try {
      const r = await api<ActionResult>('/update/rollback', { method: 'POST' })
      if (!r.ok) window.alert(r.stderr || r.stdout)
      await upd.refresh()
    } catch (e) {
      window.alert(String(e))
    } finally {
      setUpdBusy(false)
    }
  }

  const windowN = Math.max(30, Math.min(300, Math.round(300_000 / intervalMs)))
  const intervalS = intervalMs / 1000
  const spanLabel = `${Math.max(1, Math.round((windowN * intervalS) / 60))}m`
  const stale = data != null && last != null && now - last > intervalMs * 3

  if (error && !data) return <div className="banner err">metrics: {error}</div>

  const cpu = data?.cpu
  const ram = data?.ram ?? {}
  const swap = data?.swap ?? {}
  const net = data?.net ?? {}
  const disks = Array.isArray(data?.disks) ? data!.disks : []
  const gpus = Array.isArray(data?.gpus) ? data!.gpus : []
  const load = Array.isArray(data?.load) ? data!.load : []
  const cores = cpu?.cores || 0

  return (
    <div className={`ov${stale ? ' stale' : ''}`}>
      {error && <div className="banner err">metrics: {error} — showing the last good sample</div>}
      {!error && data == null && <div className="banner">reading /sys and /proc…</div>}
      <UpdateBanners report={upd.data} preview={prev.data} busy={updBusy} onRun={runUpdate} onRollback={rollbackUpdate} />

      <div className="ov-bar">
        <h2 className="host">{data?.host?.hostname ?? 'host'}</h2>
        <span className="sub">{cpu?.model ? shortCpu(cpu.model) : '—'}</span>
        {cores > 0 && <Chip title="logical CPUs — load average is per-core against this">{cores} threads</Chip>}
        {data?.host?.kernel && <Chip title="kernel">{data.host.kernel}</Chip>}
        <span className="right">
          <span className={`live ${error ? 'err' : stale ? 'stale' : 'ok'}`}>
            <i />
            {error ? 'API error' : stale ? 'stale' : 'live'}
          </span>
          <span className="ago">{relTime(now, last, intervalMs * 1.5)}</span>
          <span className="ago">up {uptime(data?.uptime_s)}</span>
        </span>
      </div>

      <div className="strip">
        <TileUse cpu={cpu} hist={hist} windowN={windowN} intervalS={intervalS} />
        <TileCpuTemp hist={hist} windowN={windowN} intervalS={intervalS} />
        <TileLoad
          load={load}
          cores={cores}
          hist={hist}
          windowN={windowN}
          intervalS={intervalS}
          blocked={cpu?.procs_blocked ?? null}
          running={cpu?.procs_running ?? null}
        />
        <TileRam ram={ram} hist={hist} windowN={windowN} intervalS={intervalS} />
        {(swap.total_mib ?? 0) > 0 && <TileSwap swap={swap} hist={hist} windowN={windowN} intervalS={intervalS} />}
        <TileNet net={net} hist={hist} windowN={windowN} spanLabel={spanLabel} />
      </div>

      {data != null && gpus.length === 0 && (
        <div className="panelish empty">
          <b>No AMD GPUs in sysfs.</b> Nothing exposing <code>/sys/class/drm/card*/device/gpu_busy_percent</code>{' '}
          was found — a card that is there but not bound to <code>amdgpu</code> looks exactly like a card that is
          gone.
        </div>
      )}

      {gpus.length > 0 && (
        <div className="gpus">
          {gpus.map((g) => (
            <GpuCard key={g.card} gpu={g} hist={hist} windowN={windowN} intervalS={intervalS} spanLabel={spanLabel} />
          ))}
        </div>
      )}

      <div className="lower">
        <StorageCard
          disks={disks}
          sensors={Array.isArray(data?.sensors) ? data!.sensors : []}
          hist={hist}
          windowN={windowN}
          spanLabel={spanLabel}
        />
        <FleetCard intervalMs={intervalMs} />
        <UpdatesCard report={upd.data} busy={updBusy} onRun={runUpdate} onRollback={rollbackUpdate} />
      </div>
    </div>
  )
}

// ─────────────────────────────── updates ─────────────────────────────
// The auto-update's notification surface:
//   banner (transient) — in progress · update available (MAJOR highlighted) ·
//                        failed + rolled back
//   UpdatesCard (always) — the last update's before→after with badges
// All state comes from /api/update + /api/update/preview (panel-do actions).
// The long work is detached inside the update script — its step 6 restarts
// panel-api, so the banner survives the blip and re-polls on the other side.

const changeBadge: Record<string, string> = {
  major: 'badge err',
  removed: 'badge warn',
  changed: 'badge warn',
  minor: 'badge',
  moved: 'badge',
  rebuilt: 'badge',
  unchanged: 'badge',
}

function UpdateBanners({
  report,
  preview,
  busy,
  onRun,
  onRollback,
}: {
  report: UpdateReport | null
  preview: UpdatePreview | null
  busy: boolean
  onRun: (force: boolean) => void
  onRollback: () => void
}) {
  const status = report?.state?.status
  if (status === 'in-progress') {
    return (
      <div className="banner">
        <b>update in progress</b> — started {updateTs(report?.state?.snapshot)} · gated (health +
        doctor), auto-rolls-back on failure · the API may blip while panel-api itself updates
        {report?.log_tail && <pre className="out">{report.log_tail}</pre>}
      </div>
    )
  }
  if (status === 'rolled-back' || status === 'partially-rolled-back') {
    return (
      <div className="banner err">
        <b>
          the last update {status === 'rolled-back' ? 'failed and was rolled back' : 'failed — rollback was only PARTIAL'}
        </b>{' '}
        {updateTs(report?.state?.snapshot)}{' '}
        <button onClick={onRollback} disabled={busy}>
          re-run rollback
        </button>{' '}
        <button onClick={() => onRun(true)} disabled={busy}>
          re-run update
        </button>
        {report?.log_tail && <pre className="out">{report.log_tail}</pre>}
      </div>
    )
  }
  const commits = preview?.commits ?? []
  const lineChanges = (preview?.changes ?? []).filter((c) => c.change !== 'unchanged')
  const dirty = (preview?.dirty ?? []).length > 0
  const gitError = preview?.git_error ?? null
  if (preview && (commits.length > 0 || lineChanges.length > 0 || dirty || gitError != null)) {
    const majors = lineChanges.filter((c) => c.change === 'major')
    // commit_count is the FULL pending count — the commits list is capped at
    // the last 30 for display.
    const commitCount = Math.max(preview.commit_count ?? 0, commits.length)
    const hasChanges = commits.length > 0 || lineChanges.length > 0 || dirty
    return (
      <div className="banner warn">
        <b>{hasChanges ? 'update available' : 'clone problem'}</b>
        {commits.length > 0 && (
          <span>
            {' '}— {commitCount} pending commit{commitCount > 1 ? 's' : ''} on the server clone
          </span>
        )}
        {lineChanges.length > 0 && (
          <span> · {lineChanges.map((c) => `${c.name} ${c.old} → ${c.new}`).join(', ')}</span>
        )}
        {dirty && <span> · uncommitted changes in the clone</span>}
        {gitError != null && (
          <span title={gitError}>
            {' '}· <b className="t-crit">clone git status unreadable</b> — the auto-update refuses an
            unverifiable clone
          </span>
        )}
        {majors.length > 0 && (
          <span className="badge err" title={majors.map((m) => `${m.kind} ${m.name}: ${m.old} → ${m.new}`).join('\n')}>
            MAJOR ×{majors.length}
          </span>
        )}
        {' '}
        <button
          onClick={() => onRun(false)}
          disabled={busy || dirty || gitError != null}
          title={
            dirty
              ? 'the clone has uncommitted changes — commit them, or use “run anyway”'
              : gitError != null
                ? 'git status is unreadable — the action will refuse; “run anyway” applies the working tree as-is, deliberately'
                : undefined
          }
        >
          {dirty ? 'run (clone dirty)' : gitError != null ? 'run (status unreadable)' : 'run now'}
        </button>
        {(dirty || gitError != null) && (
          <button
            onClick={() => onRun(true)}
            disabled={busy}
            title="applies the working tree as-is (the daily auto-update skips a dirty clone and refuses an unreadable one)"
          >
            run anyway
          </button>
        )}
      </div>
    )
  }
  return null
}

function UpdatesCard({
  report,
  busy,
  onRun,
  onRollback,
}: {
  report: UpdateReport | null
  busy: boolean
  onRun: (force: boolean) => void
  onRollback: () => void
}) {
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem('panel.ov.updates') === '1'
    } catch {
      return false
    }
  })
  useEffect(() => {
    try {
      localStorage.setItem('panel.ov.updates', open ? '1' : '0')
    } catch {
      /* storage unavailable */
    }
  }, [open])

  const status = report?.state?.status
  const rows = report?.services ?? []
  const changed = rows.filter((r) => r.change !== 'unchanged')
  const majors = rows.filter((r) => r.change === 'major')
  const inProgress = status === 'in-progress'

  return (
    <section className="panelish">
      <header>
        <h3 onClick={() => setOpen(!open)} style={{ cursor: 'pointer' }} title="last update's before→after — click to expand">
          updates {open ? '▾' : '▸'}
        </h3>
        <span className="note">
          {status == null && 'no update run yet'}
          {inProgress && <b className="t-warn">in progress…</b>}
          {status === 'updated' && <b className="t-ok">{updateTs(report?.state?.snapshot)}</b>}
          {(status === 'rolled-back' || status === 'partially-rolled-back') && (
            <b className="t-crit">{status}</b>
          )}
          {majors.length > 0 && <span className="badge err">MAJOR ×{majors.length}</span>}
        </span>
      </header>
      {open && (
        <div className="sub">
          <p className="note">
            {report?.state?.repo_root ? (
              <>
                deployed {report.state.repo_head ? <code>{report.state.repo_head.slice(0, 7)}</code> : 'from the server clone'} ·{' '}
                <code>{report.state.repo_root}</code> — the auto-update re-runs <code>make update</code> from it (gated,
                auto-rollback; it never git-pulls)
              </>
            ) : (
              <>
                no update state yet — run <code>make update</code> once; the auto-update (daily timer + the button
                below) then re-runs it, gated, with auto-rollback
              </>
            )}
          </p>
          {rows.length > 0 && (
            <table>
              <thead>
                <tr>
                  <th>service</th>
                  <th>before</th>
                  <th>after</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {changed.map((r) => (
                  <tr key={r.service}>
                    <td>{r.service}</td>
                    <td className="dim">{r.pre_version || r.pre_tags.split(' ')[0] || '—'}</td>
                    <td className="dim">{r.post_version || (r.post_image ? 'new image' : 'no container')}</td>
                    <td>
                      <span className={changeBadge[r.change] ?? 'badge'}>{r.change === 'major' ? 'MAJOR' : r.change}</span>
                    </td>
                  </tr>
                ))}
                {changed.length === 0 && (
                  <tr>
                    <td colSpan={4} className="dim">
                      nothing changed in the last update
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          )}
          {report?.log_tail && (
            <details>
              <summary className="note">run log</summary>
              <pre className="out">{report.log_tail}</pre>
            </details>
          )}
          <div className="btns">
            {!inProgress && (
              <button onClick={() => onRun(false)} disabled={busy}>
                run update
              </button>
            )}
            <button onClick={onRollback} disabled={busy || status == null}>
              rollback last
            </button>
          </div>
        </div>
      )}
    </section>
  )
}

// ────────────────────────────── host strip ──────────────────────────────

function TileUse({ cpu, hist, windowN, intervalS }: { cpu: Metrics['cpu'] | undefined; hist: Sample[]; windowN: number; intervalS: number }) {
  // Utilisation is the one number here that is a rate, so it is null on the very
  // first poll — show "—" rather than a reassuring 0 %.
  const use = cpu?.usage_pct ?? null
  const s = seriesOf(tail(hist, windowN), (x) => x.cpuPct)
  const t = trend(s, intervalS, 0, '%')
  return (
    <Tile label="cpu use" title="instantaneous utilisation of all cores (Δ of /proc/stat)"> 
      <Big value={num(use, use != null && use < 10 ? 1 : 0)} unit="%" tone={toneOf(use == null ? null : use / 100, T.cpuUse.warn, T.cpuUse.crit)} />
      <Chart series={[{ values: s, tone: 'load', fill: true }]} min={0} max={100} height={26} grid={[50]} />
      <div className="foot">
        <TrendMark {...t} />
        <span className="f">{cpu?.freq_mhz ? `${(cpu.freq_mhz / 1000).toFixed(2)} GHz` : 'clock —'}</span>
      </div>
    </Tile>
  )
}

function TileCpuTemp({ hist, windowN, intervalS }: { hist: Sample[]; windowN: number; intervalS: number }) {
  const s = seriesOf(tail(hist, windowN), (x) => x.cpuTemp)
  const temp = lastOf(s)
  const { min, max } = spread(s)
  const a = axis(s, { floorAt: 20, ceilAt: T.cpuTemp.max })
  const tn = toneOf(temp == null ? null : temp / T.cpuTemp.max, T.cpuTemp.warn, T.cpuTemp.crit)
  return (
    <Tile label="cpu temp" title="k10temp Tctl — on Ryzen this tracks the hottest core, offset and all" right={<TrendMark {...trend(s, intervalS, 1, '°')} />}>
      <Big value={num(temp, 0)} unit="°C" tone={tn} />
      <Chart series={[{ values: s, tone: tn, fill: true }]} min={a.min} max={a.max} height={26} grid={[T.cpuTemp.max * T.cpuTemp.warn]} />
      <div className="foot">
        <span className="f">
          {span(min, max, 0, '°')} · warn {Math.round(T.cpuTemp.max * T.cpuTemp.warn)}°
        </span>
      </div>
    </Tile>
  )
}

function TileLoad({
  load,
  cores,
  hist,
  windowN,
  intervalS,
  blocked,
  running,
}: {
  load: number[]
  cores: number
  hist: Sample[]
  windowN: number
  intervalS: number
  blocked: number | null
  running: number | null
}) {
  const s = seriesOf(tail(hist, windowN), (x) => x.load1)
  const per = cores > 0 && load[0] != null ? load[0] / cores : null
  return (
    <Tile
      label="load"
      title={
        cores > 0
          ? `runnable tasks, 1/5/15 min — ${cores} threads means ${cores}.0 is a fully busy box`
          : 'runnable tasks, 1/5/15 min'
      }
      right={
        blocked ? (
          <Pill tone="crit" title="procs_blocked — tasks stuck in uninterruptible IO; this is the 'why is everything slow' counter">
            {blocked} blocked
          </Pill>
        ) : (
          <TrendMark {...trend(s, intervalS, 2, '')} />
        )
      }
    >
      <Big value={num(load[0], 2)} unit={cores > 0 ? `/ ${cores}` : '1m'} tone={toneOf(per, T.loadPerCore.warn, T.loadPerCore.crit)} />
      <div className="bars">
        {(
          [
            ['1', load[0]],
            ['5', load[1]],
            ['15', load[2]],
          ] as [string, number | undefined][]
        ).map(([k, v]) => {
          // A load of N on N threads is a fully busy box — hence "of cores", and
          // the tick at 1.0 shows where that line is instead of pretending.
          const frac = cores > 0 && v != null ? v / cores : null
          return (
            <span className="bar" key={k} title={`load ${k}: ${num(v, 2)}${cores ? ` = ${num(frac, 2)} per thread` : ''}`}>
              <em>{k}</em>
              <Meter pct={frac} tone={toneOf(frac, T.loadPerCore.warn, T.loadPerCore.crit)} ticks={[1]} />
            </span>
          )
        })}
      </div>
      <div className="foot">
        <span className="f">
          {per == null ? '—' : `${per.toFixed(2)} per thread`} · {num(running)} runnable
          {blocked ? <b className="t-crit"> · {blocked} on IO</b> : ''}
        </span>
      </div>
    </Tile>
  )
}

function TileRam({ ram, hist, windowN, intervalS }: { ram: NonNullable<Metrics['ram']>; hist: Sample[]; windowN: number; intervalS: number }) {
  const total = ram.total_mib ?? 0
  const used = ram.used_mib ?? null
  const cache = ram.cache_mib ?? 0
  const free = ram.free_mib ?? 0
  const frac = total > 0 && used != null ? used / total : null
  const s = seriesOf(tail(hist, windowN), (x) => (x.ramTotal ? (x.ramUsed ?? 0) / x.ramTotal : null))
  const pctOf = (v: number) => (total > 0 ? v / total : 0)
  return (
    <Tile
      label="memory"
      title={`used ${mem(used)} — memory nothing can reclaim · cache ${mem(cache)} — pages the kernel drops the moment you need them · free ${mem(free)} · alarm at ${Math.round(
        T.ram.warn * 100
      )}% / ${Math.round(T.ram.crit * 100)}%`}
      right={<TrendMark {...trend(s, intervalS, 1, '%')} />}
    >
      <Big value={mem(used)} unit={total ? `/ ${mem(total, 0)}` : undefined} tone={toneOf(frac, T.ram.warn, T.ram.crit)} />
      <Stack
        parts={[
          { pct: pctOf(used ?? 0), cls: 'used', label: `used ${mem(used)}` },
          { pct: pctOf(cache), cls: 'cache', label: `cache ${mem(cache)}` },
          { pct: pctOf(free), cls: 'free', label: `free ${mem(free)}` },
        ]}
      />
      <div className="foot">
        <span className="f">
          {num((frac ?? 0) * 100, 0)}% used · {num(pctOf(cache) * 100, 0)}% cache · {num(pctOf(free) * 100, 0)}% free
        </span>
      </div>
    </Tile>
  )
}

function TileSwap({ swap, hist, windowN, intervalS }: { swap: NonNullable<Metrics['swap']>; hist: Sample[]; windowN: number; intervalS: number }) {
  const total = swap.total_mib ?? 0
  const used = swap.used_mib ?? 0
  const frac = total > 0 ? used / total : null
  const s = seriesOf(tail(hist, windowN), (x) => (x.swapTotal ? (x.swapUsed ?? 0) / x.swapTotal : null))
  return (
    <Tile
      label="swap"
      title="swapped out — RAM is fine until this moves, then something is out of memory"
      right={<TrendMark {...trend(s, intervalS, 1, '%')} />}
    >
      <Big value={mem(used)} unit={total ? `/ ${mem(total, 0)}` : undefined} tone={toneOf(frac, T.swap.warn, T.swap.crit)} />
      <Meter pct={frac} tone={toneOf(frac, T.swap.warn, T.swap.crit)} bands={[T.swap.warn, T.swap.crit]} />
      <div className="foot">
        <span className="f">{num((frac ?? 0) * 100, 1)}% of the swap area in use</span>
      </div>
    </Tile>
  )
}

function TileNet({ net, hist, windowN, spanLabel }: { net: NonNullable<Metrics['net']>; hist: Sample[]; windowN: number; spanLabel: string }) {
  const rx = seriesOf(tail(hist, windowN), (x) => x.rx)
  const tx = seriesOf(tail(hist, windowN), (x) => x.tx)
  const top = Math.max(1, ...rx.map((v, i) => Math.max(v ?? 0, tx[i] ?? 0)))
  const ifaces = net.ifaces?.length ? net.ifaces.join(' ') : 'no physical interface'
  return (
    <Tile label="network" title={`${ifaces} — physical links only, docker/veth/bridge/tunnel traffic is already counted there`}>
      <div className="dual">
        <span className="dl">
          <em>↓</em> {bitsPerSec(net.rx_bits_s)}
        </span>
        <span className="dl">
          <em>↑</em> {bitsPerSec(net.tx_bits_s)}
        </span>
      </div>
      <Chart
        series={[
          { values: rx, tone: 'flow', fill: true },
          { values: tx, tone: 'load' },
        ]}
        min={0}
        max={top * 1.15}
        height={26}
      />
      {/* the chart has no axis, so its peak is the only scale it carries — it
          leads the line, and the (long) interface names ellipsize after it */}
      <div className="foot" title={`${ifaces} — ${spanLabel} peak ${bitsPerSec(top)}`}>
        <span className="f">
          {spanLabel} peak {bitsPerSec(top)} · {(net.ifaces ?? []).slice(0, 2).join(' ') || '—'}
        </span>
      </div>
    </Tile>
  )
}

// ─────────────────────────────── GPUs ───────────────────────────────

function GpuCard({ gpu, hist, windowN, intervalS, spanLabel }: { gpu: Gpu; hist: Sample[]; windowN: number; intervalS: number; spanLabel: string }) {
  const [open, setOpen] = useGpuOpen(gpu.card)
  const w = tail(hist, windowN)
  // Series are read out of the shared history buffer; a card that appeared
  // after the buffer started reads as a run of nulls (a gap), never zeros.
  const col = (pick: (g: NonNullable<Sample['gpus'][string]>) => number | null) => seriesOf(w, (s) => pick(s.gpus[gpu.card] ?? emptyGpu))

  const util = gpu.util_pct ?? null
  const junction = gpu.temp_junction_c ?? null
  const edge = gpu.temp_edge_c ?? null
  const memC = gpu.temp_mem_c
  const state = gpuState(util, junction)
  const fanText = gpu.fan_rpm == null ? '—' : `${num(gpu.fan_rpm)} rpm`

  const utilS = col((g) => g.util)
  const vramS = col((g) => g.vramPct)
  const edgeS = col((g) => g.edge)
  const junctionS = col((g) => g.junction)
  const memS = col((g) => g.mem)
  const powerS = col((g) => g.power)
  const fanS = col((g) => g.fan)
  const clockS = col((g) => g.clock)

  const vramFrac = gpu.vram_total_mib && gpu.vram_total_mib > 0 && gpu.vram_used_mib != null ? gpu.vram_used_mib / gpu.vram_total_mib : null
  const powerFrac = gpu.power_cap_w && gpu.power_w != null ? gpu.power_w / gpu.power_cap_w : null
  const therm = junction ?? edge
  const thermS = junction != null ? junctionS : edgeS
  const thermMax = junction != null ? T.junction.max : T.edge.max
  const thermWarn = junction != null ? T.junction.warn : T.edge.warn
  const thermCrit = junction != null ? T.junction.crit : T.edge.crit
  const thermTone = toneOf(therm == null ? null : therm / thermMax, thermWarn, thermCrit)
  const thermAxis = axis(thermS, { floorAt: 20, ceilAt: thermMax })

  // Scales for the expanded trace grid: each sensor gets its own autoscale
  // so a 2 °C drift is visible, clamped under the max its colour rules are
  // quoted against. Power is the exception — fixed 0 → cap — so the trace's
  // height *is* the level against the limit; near the cap that hides small
  // wiggles, and that's fine, the range row carries the numbers.
  const junctionAxis = axis(junctionS, { floorAt: 20, ceilAt: T.junction.max })
  const memAxis = axis(memS, { floorAt: 20, ceilAt: T.memTemp.max })
  const edgeAxis = axis(edgeS, { floorAt: 20, ceilAt: T.edge.max })
  const fanAxis = axis(fanS, { floorAt: 0, ceilAt: FAN_CEIL_RPM })
  const clockAxis = axis(clockS, { floorAt: 0, ceilAt: CLOCK_CEIL_MHZ })
  const powerMax = gpu.power_cap_w ?? Math.max(10, (spread(powerS).max ?? 10) * 1.2)

  // "GPU-N" — N is the 0-based ROCR index, the same number the GPU pool's tiles use.
  return (
    <article className={`gpu${state.tone === 'crit' ? ' crit' : ''}${open ? ' open' : ''}`}>
      <header
        role="button"
        tabIndex={0}
        aria-expanded={open}
        title={open ? 'click to condense' : 'click for the full card'}
        onClick={setOpen}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault()
            setOpen()
          }
        }}
      >
        <h3>GPU-{gpu.rocr_index}</h3>
        {gpu.pci_id && <Chip title={`driver: ${gpu.driver || '?'}`}>{gpu.pci_id}</Chip>}
        {gpu.pci_slot && <Chip title="PCI slot">{gpu.pci_slot.replace(/^0000:/, '')}</Chip>}
        <Pill tone={state.tone} title={state.title}>
          {state.label}
        </Pill>
        <span className="gchev" aria-hidden>
          ▾
        </span>
      </header>

      <div className="gsum">
        <div className="gstat">
          <h5>load</h5>
          <Big value={num(util)} unit="%" tone="load" />
          <span
            className="gsub"
            title={
              gpu.mem_clock_mhz
                ? `graphics clock (memory runs at ${gpu.mem_clock_mhz} MHz) · fan`
                : 'graphics clock (sclk) · fan'
            }
          >
            {num(gpu.clock_mhz)} MHz · {fanText}
          </span>
          <Chart series={[{ values: utilS, tone: 'load', fill: true }]} min={0} max={100} height={20} dot={false} />
        </div>
        <div
          className="gstat"
          title="No warn zone on purpose: an LLM server reserves all the VRAM it is given, so a full bar means a model is loaded — not that you are out of room."
        >
          <h5>vram</h5>
          <span className="gv t-load">
            {mem(gpu.vram_used_mib)}
            {gpu.vram_total_mib != null && <em> / {mem(gpu.vram_total_mib, 0)}</em>}
          </span>
          <Meter pct={vramFrac} tone="load" />
        </div>
        <div
          className="gstat"
          title={junction != null ? 'junction — the silicon hot-spot; this is what throttles first' : 'edge — no junction sensor on this card'}
        >
          <h5>{junction != null ? 'junction' : 'edge'}</h5>
          <span className={`gv t-${thermTone}`}>
            {num(therm, 0)}
            <em>°C</em>
          </span>
          <span className="gsub" title="HBM/GDDR memory temperature — the third sensor; hot VRAM throttles the memory clock">
            mem {num(memC, 0)}°
          </span>
          <Chart series={[{ values: thermS, tone: thermTone, fill: true }]} min={thermAxis.min} max={thermAxis.max} height={20} dot={false} />
        </div>
        <div
          className="gstat"
          title={gpu.power_cap_w ? `${num(gpu.power_w, 1)} W of a ${gpu.power_cap_w} W limit — a full bar is the card working at its limit, not a warning` : 'no power sensor on this card'}
        >
          <h5>power</h5>
          <span className="gv t-load">
            {num(gpu.power_w, 1)}
            <em>
              W{gpu.power_cap_w != null ? ` / ${gpu.power_cap_w}W` : ''}
            </em>
          </span>
          <Meter pct={powerFrac} tone="load" />
        </div>
      </div>

      {open && (
        <div className="gdetail">
          <div className="trendhead">
            <span className="k">{spanLabel} trace</span>
            <span className="axis">
              {w.length > 1 ? `${clockOf(w[0].t)} → now` : 'gathering…'}
            </span>
          </div>

          {/* the condensed row says what is true now — so this section is the
              history: every sensor on its own scale, with the window's range
              and rate under each trace. Nothing up top is repeated here. */}
          <div className="gtrace">
            <GpuTrace
              label="util"
              values={utilS}
              intervalS={intervalS}
              min={0}
              max={100}
              grid={[50]}
              tone="load"
              unit="%"
              trendUnit="%"
              title="GPU busy — the work the card is doing"
            />
            <GpuTrace
              label="vram"
              values={vramS}
              intervalS={intervalS}
              min={0}
              max={100}
              grid={[50]}
              tone="flow"
              unit="%"
              trendUnit="%"
              title="VRAM as a % of total — flat at 100 % is a loaded model, not a leak"
            />
            <GpuTrace
              label="junction"
              values={junctionS}
              intervalS={intervalS}
              min={junctionAxis.min}
              max={junctionAxis.max}
              grid={[T.junction.max * T.junction.warn]}
              tone={toneOf(junction == null ? null : junction / T.junction.max, T.junction.warn, T.junction.crit)}
              unit="°C"
              trendUnit="°"
              title="hwmon temp2 — the silicon hot-spot; this is what throttles first"
            />
            <GpuTrace
              label="edge"
              values={edgeS}
              intervalS={intervalS}
              min={edgeAxis.min}
              max={edgeAxis.max}
              grid={[T.edge.max * T.edge.warn]}
              tone={toneOf(edge == null ? null : edge / T.edge.max, T.edge.warn, T.edge.crit)}
              unit="°C"
              trendUnit="°"
              title="hwmon temp1 — the cooler's own sensor"
            />
            <GpuTrace
              label="mem temp"
              values={memS}
              intervalS={intervalS}
              min={memAxis.min}
              max={memAxis.max}
              grid={[T.memTemp.max * T.memTemp.warn]}
              tone={toneOf(memC == null ? null : memC / T.memTemp.max, T.memTemp.warn, T.memTemp.crit)}
              unit="°C"
              trendUnit="°"
              title="hwmon temp3 — HBM/GDDR temperature; hot VRAM throttles the memory clock"
            />
            <GpuTrace
              label="power"
              values={powerS}
              intervalS={intervalS}
              min={0}
              max={powerMax}
              tone="load"
              unit="W"
              trendUnit=" W"
              title={
                gpu.power_cap_w
                  ? `drawn against the ${gpu.power_cap_w} W limit — a full trace is the card at its limit, not a warning`
                  : 'no power sensor on this card'
              }
            />
            <GpuTrace
              label="fan"
              values={fanS}
              intervalS={intervalS}
              min={fanAxis.min}
              max={fanAxis.max}
              tone="muted"
              unit="rpm"
              trendUnit=" rpm"
              title="fan1"
            />
            <GpuTrace
              label="clock"
              values={clockS}
              intervalS={intervalS}
              min={clockAxis.min}
              max={clockAxis.max}
              tone="muted"
              unit="MHz"
              trendUnit=" MHz"
              title={gpu.mem_clock_mhz ? `graphics clock (sclk); memory runs at ${gpu.mem_clock_mhz} MHz` : 'graphics clock (sclk)'}
            />
          </div>
        </div>
      )}
    </article>
  )
}

/**
 * One sensor's history over the window: its own scale, the window's min–max
 * and rate under the trace. The current value stays out on purpose — the
 * condensed row already says what is true now, so the expanded card is the
 * history, not a repeat of it.
 */
function GpuTrace({
  label,
  values,
  intervalS,
  min,
  max,
  grid = [],
  tone,
  unit,
  trendUnit,
  title,
}: {
  label: string
  values: (number | null)[]
  intervalS: number
  min: number
  max: number
  grid?: number[]
  tone: Tone
  /** unit shown after the window's min–max */
  unit: string
  /** unit inside the rate, e.g. "°" → "▲ +2°/min" */
  trendUnit: string
  title?: string
}) {
  const { min: lo, max: hi } = spread(values)
  return (
    <div className="gtr" title={title}>
      <h5>
        {label}
        <span className="gt">
          <TrendMark {...trend(values, intervalS, 0, trendUnit)} />
        </span>
      </h5>
      <Chart series={[{ values, tone, fill: true }]} min={min} max={max} height={24} grid={grid} dot={false} />
      <span className="gr">{lo == null ? 'no readings' : `${num(lo)}–${num(hi)} ${unit}`}</span>
    </div>
  )
}

const emptyGpu: Sample['gpus'][string] = {
  util: null,
  edge: null,
  junction: null,
  mem: null,
  vramPct: null,
  vramUsed: null,
  power: null,
  fan: null,
  clock: null,
}

// ────────────────────────────── storage ──────────────────────────────

function StorageCard({
  disks,
  sensors,
  hist,
  windowN,
  spanLabel,
}: {
  disks: Disk[]
  sensors: Sensor[]
  hist: Sample[]
  windowN: number
  spanLabel: string
}) {
  const w = tail(hist, windowN)
  return (
    <section className="panelish">
      <header>
        <h3>storage</h3>
        <span className="note">{disks.length} mount{disks.length === 1 ? '' : 's'} — PANEL_DISK_MOUNTS</span>
      </header>
      {disks.length === 0 && <p className="note">nothing configured — set PANEL_DISK_MOUNTS in /opt/panel/.env</p>}
      {disks.map((d) => {
        const frac = d.total_mib && d.total_mib > 0 && d.used_mib != null ? d.used_mib / d.total_mib : null
        const tn = toneOf(frac, T.disk.warn, T.disk.crit)
        const rd = seriesOf(w, (s) => s.disks[d.mount]?.read ?? null)
        const wr = seriesOf(w, (s) => s.disks[d.mount]?.write ?? null)
        const top = Math.max(1, ...rd.map((v, i) => Math.max(v ?? 0, wr[i] ?? 0)))
        const id = [d.device, d.fstype].filter(Boolean).join(' · ')
        if (d.error)
          return (
            <div className="disk bad" key={d.mount}>
              <div className="line">
                <span className="m">{d.mount}</span>
                <span className="id">{id || '—'}</span>
                <Pill tone="crit">broken</Pill>
              </div>
              <div className="line sub">
                <span className="badmsg">⚠ {d.error}</span>
                <span className="io">statvfs reports the containing filesystem here — trust nothing about this mount</span>
              </div>
            </div>
          )
        return (
          <div className="disk" key={d.mount}>
            <div className="line">
              <span className="m">{d.mount}</span>
              <span className="id">{id || 'device —'}</span>
              {d.temp_c != null && (
                <span className={`id t-${dtone(d.temp_c)}`} title="drive temperature — NVMe throttles around 70-80 °C and dies young when it lives there">
                  {num(d.temp_c, 0)}°
                </span>
              )}
              <span className="spacer" />
              <span className={`lvl t-${tn}`} title={`${mem(d.used_mib)} written of ${mem(d.total_mib, 0)}`}>
                {num((frac ?? 0) * 100, 0)}% <em>used</em>
              </span>
              <span className="of">{mem(d.used_mib)} / {mem(d.total_mib, 0)}</span>
              <span className="free" title="space a non-root user can actually write (reserved blocks excluded)">
                {mem(d.avail_mib)} free
              </span>
            </div>
            <div className="line">
              <Meter pct={frac} tone={tn} bands={[T.disk.warn, T.disk.crit]} title={`${mem(d.used_mib)} of ${mem(d.total_mib, 0)} used`} />
              <Chart
                series={[
                  { values: rd, tone: 'flow', fill: true },
                  { values: wr, tone: 'load' },
                ]}
                min={0}
                max={top * 1.15}
                height={22}
              />
              <span className="io">
                <em>↓</em> {bytesPerSec(d.read_bytes_s)} <em>↑</em> {bytesPerSec(d.write_bytes_s)}{' '}
                <span className="dim">peak {bytesPerSec(top)} / {spanLabel}</span>
              </span>
            </div>
          </div>
        )
      })}
      <BoardSensors sensors={sensors} />
    </section>
  )
}

/** Chips the kernel reports that the CPU/GPU panels don't already show. */
const BOARD_SKIP = new Set(['k10temp', 'zenpower', 'coretemp', 'cpu_thermal', 'amdgpu', 'i915', 'nouveau', 'nvme', 'acpitz', 'soc_dts'])

/** Kernel chip name → the word a human searches for. */
function boardLabel(chip: string): string {
  if (/^(spd5118|spd5408|jc45|tsens)$/.test(chip)) return 'dimm'
  if (/^(r8169|igc|r8168|mlx|e1000|i40e|txgbe|mvneta|ax88|en88)/.test(chip)) return 'nic'
  if (/xhci|^prom\d/.test(chip)) return 'usb'
  if (/^(it8[0-9]{2}|it87|nct67|f7188|f8|w83)/.test(chip)) return 'super-i/o'
  if (/^(asus|wmi|acpi)/.test(chip)) return 'wmi'
  return chip
}

const dtone = (c: number): Tone => toneOf(c / 100, 0.7, 0.8)

/**
 * The rest of the board's thermometry — DIMMs, NIC, USB controller — as one
 * grey strip. Same colour rules as everything else: it only goes amber when a
 * chip is genuinely cooking, so a wall of numbers stays quiet.
 */
function BoardSensors({ sensors }: { sensors: Sensor[] }) {
  const groups = new Map<string, Sensor[]>()
  for (const s of sensors) {
    if (BOARD_SKIP.has(s.chip)) continue
    const l = boardLabel(s.chip)
    groups.set(l, [...(groups.get(l) ?? []), s])
  }
  const chips = [...groups.entries()]
    .map(([label, list]) => {
      const hot = list.reduce((a, b) => (b.temp_c > a.temp_c ? b : a))
      return {
        label,
        temp_c: hot.temp_c,
        tone: dtone(hot.temp_c),
        detail: list.map((s) => `${s.device}${s.label ? ` ${s.label}` : ''}: ${s.temp_c.toFixed(1)} °C`).join('\n'),
      }
    })
    .sort((a, b) => b.temp_c - a.temp_c)
  if (chips.length === 0) return null
  return (
    <div className="board">
      <span className="bk" title="hottest reading per chip, from /sys/class/hwmon — drives are shown on their mount instead">
        board
      </span>
      {chips.map((c) => (
        <span className={`bchip t-${c.tone}`} key={c.label} title={c.detail}>
          {c.label} <b>{num(c.temp_c, 0)}°</b>
        </span>
      ))}
    </div>
  )
}

// ─────────────────────────────── fleet ───────────────────────────────

function FleetCard({ intervalMs }: { intervalMs: number }) {
  // The overview only *reports*; starting/stopping lives in Services where the
  // buttons are. Both endpoints are cached server-side, and neither is
  // essential — a failure here costs a card, never the metrics.
  const containers = usePoll(() => api<{ containers: ContainerInfo[] }>('/containers'), Math.max(intervalMs, 4000))
  const llm = usePoll(() => api<LLMSettings>('/settings/llm'), Math.max(intervalMs, 3000))

  const cs = containers.data?.containers
  const byState = new Map<string, ContainerInfo[]>()
  for (const c of cs ?? []) byState.set(c.state, [...(byState.get(c.state) ?? []), c])
  const running = byState.get('running')?.length ?? 0
  const notRunning = [...byState.entries()].filter(([s]) => s !== 'running')

  const order = llm.data?.order ?? []
  const display = (name: string) => llm.data?.fleet.find((f) => f.name === name)?.display ?? name

  if (!cs && !llm.data && !containers.error && !llm.error) return null

  return (
    <section className="panelish">
      <header>
        <h3>fleet</h3>
        <span className="note">report only — control it in Services</span>
      </header>

      <div className="sub">
        <div className="subhead">
          containers
          <span className="counts">
            <b className="t-ok">{running}</b> running{cs ? ` / ${cs.length}` : ''}
            {containers.error && <span className="t-crit"> · {containers.error}</span>}
          </span>
        </div>
        {!cs && !containers.error && <p className="note">reading docker ps…</p>}
        {cs && cs.length === 0 && <p className="note">no container carries the panel.manage label</p>}
        {cs && cs.length > 0 && (
          <div className="chips">
            {[...byState.entries()].map(([state, list]) => (
              <span className={`cstate s-${state}`} key={state} title={list.map((c) => c.name).join(', ')}>
                <i />
                {state} <b>{list.length}</b>
              </span>
            ))}
          </div>
        )}
        {cs && notRunning.length > 0 && (
          <div className="chips names">
            {notRunning.map(([state, list]) =>
              list.map((c) => (
                <Chip key={c.id} title={`${c.image} — ${c.status}`}>
                  <span className={`t-${state === 'restarting' || state === 'dead' ? 'crit' : 'warn'}`}>{state}</span> {c.name}
                </Chip>
              )),
            )}
          </div>
        )}
      </div>

      <div className="sub">
        <div className="subhead">
          llm.pietrasik.top
          <span className="counts">
            {llm.error ? (
              <span className="t-crit">{llm.error}</span>
            ) : llm.data?.active ? (
              <>
                <b className="t-ok">●</b> {display(llm.data.active)} <span className="dim">is answering now</span>
              </>
            ) : (
              <span className="t-warn">no instance is active</span>
            )}
          </span>
        </div>
        {!llm.data && !llm.error && <p className="note">probing instances…</p>}
        {llm.data && order.length === 0 && <p className="note">priority order is empty — set it in Settings</p>}
        {order.length > 0 && (
          <ol className="order">
            {order.map((name, i) => {
              const st = llm.data?.status[name] ?? 'unknown'
              const fleet = llm.data?.fleet.find((f) => f.name === name)
              const tone: Tone = st === 'up' ? 'ok' : st === 'down' ? 'crit' : 'warn'
              return (
                <li key={name} className={llm.data?.active === name ? 'active' : ''}>
                  <span className="rank">{i + 1}</span>
                  <span className={`dot t-${tone}`} />
                  <span className="nm">{display(name)}</span>
                  <span className="dim">
                    {st === 'up' ? 'up' : st === 'down' ? 'down' : 'untested'}
                    {fleet && fleet.state !== 'running' ? ` · container ${fleet.state}` : ''}
                  </span>
                  {llm.data?.active === name && <Pill tone="ok">active</Pill>}
                </li>
              )
            })}
          </ol>
        )}
      </div>
    </section>
  )
}

// ────────────────────────────── small helpers ──────────────────────────────

/** "44 / 61" for a window's min/max, with the unit written once. */
function span(min: number | null, max: number | null, digits: number, unit: string) {
  if (min == null || max == null) return '— / —'
  return `${num(min, digits)} / ${num(max, digits)}${unit}`
}
