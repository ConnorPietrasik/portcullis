// ─────────────────────────────── Overview ───────────────────────────────
// The one page that answers "is the box okay?" without scrolling through
// tables. Reading order is the order you'd ask the questions:
//   host strip  → is anything on fire right now
//   GPU cards   → are the cards working, full, hot
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
import type { ContainerInfo, Disk, Gpu, LLMSettings, Metrics, Sensor } from '../types'
import {
  avg,
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
  uptime,
} from '../fmt'
import type { Tone } from '../fmt'
import { Big, Chart, Chip, Meter, MetricRow, Pill, Stack, Tile, TrendMark, TONE_COLOR } from '../ui'

// Where amber starts and red starts, in one table, as fractions of each
// sensor's own scale. VRAM is deliberately absent: an LLM server
// preallocates all of it, so a full VRAM bar is a working box, not a
// warning — colouring it red every time would train you to ignore red.
const T = {
  cpuUse: { warn: 0.85, crit: 0.95 },
  cpuTemp: { max: 100, warn: 0.78, crit: 0.92 },
  edge: { max: 110, warn: 0.72, crit: 0.86 },
  junction: { max: 120, warn: 0.75, crit: 0.88 },
  memTemp: { max: 110, warn: 0.82, crit: 0.91 },
  ram: { warn: 0.8, crit: 0.92 },
  swap: { warn: 0.55, crit: 0.85 },
  disk: { warn: 0.8, crit: 0.9 },
  power: { warn: 0.85, crit: 0.98 },
  loadPerCore: { warn: 0.85, crit: 1.15 },
}

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

export default function Overview({ intervalMs }: { intervalMs: number }) {
  const { data, error, last } = usePoll(() => api<Metrics>('/metrics'), intervalMs)
  const hist = useHistory(data)
  const now = useNow(500)

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
      </div>
    </div>
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
  const w = tail(hist, windowN)
  // Series are read out of the shared history buffer; a card that appeared
  // after the buffer started reads as a run of nulls (a gap), never zeros.
  const col = (pick: (g: NonNullable<Sample['gpus'][string]>) => number | null) => seriesOf(w, (s) => pick(s.gpus[gpu.card] ?? emptyGpu))

  const util = gpu.util_pct ?? null
  const junction = gpu.temp_junction_c ?? null
  const edge = gpu.temp_edge_c ?? null
  const state = gpuState(util, junction)

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

  return (
    <article className={`gpu${state.tone === 'crit' ? ' crit' : ''}`}>
      <header>
        <h3>{gpu.name}</h3>
        {gpu.name !== gpu.card && <Chip title="drm card">{gpu.card}</Chip>}
        {gpu.pci_id && <Chip title={`driver: ${gpu.driver || '?'}`}>{gpu.pci_id}</Chip>}
        {gpu.pci_slot && <Chip title="PCI slot">{gpu.pci_slot.replace(/^0000:/, '')}</Chip>}
        <Pill tone={state.tone} title={state.title}>
          {state.label}
        </Pill>
      </header>

      <div className="heroes">
        <div className="hero">
          <h5>gpu load</h5>
          <Big value={num(util)} unit="%" tone="load" />
          <Chart series={[{ values: utilS, tone: 'load', fill: true }]} min={0} max={100} height={34} grid={[50]} />
          <div className="foot">
            <TrendMark {...trend(utilS, intervalS, 0, '%')} />
            <span className="f">
              avg {num(avg(utilS))}% · max {num(spread(utilS).max)}%
            </span>
          </div>
        </div>

        <div className="hero">
          <h5>vram</h5>
          <Big value={mem(gpu.vram_used_mib)} unit={gpu.vram_total_mib ? `/ ${mem(gpu.vram_total_mib, 0)}` : undefined} tone="load" />
          <Meter pct={vramFrac} tone="load" />
          <div
            className="foot"
            title="No warn zone on purpose: an LLM server reserves all the VRAM it is given, so a full bar means a model is loaded — not that you are out of room."
          >
            <span className="f">
              {num((vramFrac ?? 0) * 100, 0)}% · headroom{' '}
              {mem(Math.max(0, (gpu.vram_total_mib ?? 0) - (gpu.vram_used_mib ?? 0)))}
            </span>
          </div>
        </div>

        <div className="hero">
          <h5>{junction != null ? 'junction' : 'edge'}</h5>
          <Big value={num(therm, 0)} unit="°C" tone={thermTone} />
          <Chart series={[{ values: thermS, tone: thermTone, fill: true }]} min={thermAxis.min} max={thermAxis.max} height={34} grid={[thermMax * thermWarn]} />
          <div className="foot">
            <span className="f">
              max {spanLabel} {num(spread(thermS).max, 0)}° · hot above {Math.round(thermMax * thermWarn)}°
            </span>
          </div>
        </div>
      </div>

      <div className="gpu-trend">
        <div className="trendhead">
          <span className="k">{spanLabel} trend</span>
          <span className="legend">
            <span className="li">
              <i style={{ background: TONE_COLOR.load }} /> util {num(lastOf(utilS))}%
            </span>
            <span className="li">
              <i style={{ background: TONE_COLOR.flow }} /> vram {num(lastOf(vramS))}%
            </span>
          </span>
          <span className="axis">
            {w.length > 1 ? `${clockOf(w[0].t)} → now` : 'gathering…'}
          </span>
        </div>
        <Chart series={[{ values: utilS, tone: 'load', fill: true }, { values: vramS, tone: 'flow' }]} min={0} max={100} height={64} grid={[25, 50, 75]} />
      </div>

      <div className="rows">
        <MetricRow
          label="edge"
          value={num(edge, 0)}
          unit="°C"
          tone={toneOf(edge == null ? null : edge / T.edge.max, T.edge.warn, T.edge.crit)}
          pct={edge == null ? null : edge / T.edge.max}
          bands={[T.edge.warn, T.edge.crit]}
          spark={edgeS}
          title="hwmon temp1 — the cooler's own sensor"
        />
        <MetricRow
          label="junction"
          value={num(junction, 0)}
          unit="°C"
          tone={toneOf(junction == null ? null : junction / T.junction.max, T.junction.warn, T.junction.crit)}
          pct={junction == null ? null : junction / T.junction.max}
          bands={[T.junction.warn, T.junction.crit]}
          spark={junctionS}
          title="hwmon temp2 — the silicon hot-spot; this is what throttles first"
        />
        <MetricRow
          label="mem temp"
          value={num(gpu.temp_mem_c, 0)}
          unit="°C"
          tone={toneOf(gpu.temp_mem_c == null ? null : gpu.temp_mem_c / T.memTemp.max, T.memTemp.warn, T.memTemp.crit)}
          pct={gpu.temp_mem_c == null ? null : gpu.temp_mem_c / T.memTemp.max}
          bands={[T.memTemp.warn, T.memTemp.crit]}
          spark={memS}
          title="hwmon temp3 — HBM/GDDR temperature"
        />
        <MetricRow
          label="power"
          value={num(gpu.power_w, 1)}
          unit="W"
          tone={toneOf(powerFrac, T.power.warn, T.power.crit)}
          pct={powerFrac}
          bands={[T.power.warn, T.power.crit]}
          spark={powerS}
          title={gpu.power_cap_w ? `${num(gpu.power_w, 1)} W of a ${gpu.power_cap_w} W limit` : 'no power sensor on this card'}
        />
        <MetricRow
          label="fan"
          value={gpu.fan_rpm == null ? '—' : gpu.fan_rpm === 0 ? 'passive' : num(gpu.fan_rpm)}
          unit={gpu.fan_rpm ? 'rpm' : undefined}
          tone="muted"
          pct={undefined}
          spark={fanS}
          title="fan1_input — 0 rpm means the fan is stopped (below the fan curve's floor), not broken"
        />
        <MetricRow
          label="clock"
          value={num(gpu.clock_mhz)}
          unit="MHz"
          tone="muted"
          pct={undefined}
          spark={clockS}
          title={gpu.mem_clock_mhz ? `graphics clock; memory clock ${gpu.mem_clock_mhz} MHz` : 'graphics clock (sclk)'}
        />
      </div>
    </article>
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
