// ────────────────────────── formatting + thresholds ──────────────────────────
// One place decides how a number looks and what colour it deserves, so every
// tile on the Overview agrees: GiB above a GiB, bits for links, bytes for
// disks, and colour ONLY where a human should consider acting.

export type Tone = 'ok' | 'warn' | 'crit' | 'na' | 'load' | 'flow' | 'muted'

/**
 * What a level inside its safe band looks like. One word, two flavours:
 *   'load' — accent blue: the page stays quiet and only problems are coloured.
 *   'ok'   — green: the whole strip reads "all good" from across the room.
 * The danger zones are painted on the bar's track either way (Meter's `bands`),
 * so the colour always says something — never a full-spectrum arc sitting
 * behind a colourless value, which is the thing that made the old gauges lie.
 */
export const IN_RANGE: Tone = 'load'

/** Severity of a 0…1 ratio (used/total, temp/max, …) against its own scale. */
export function tone(frac: number | null | undefined, warn = 0.8, crit = 0.92): Tone {
  if (frac == null || !Number.isFinite(frac)) return 'na'
  if (frac >= crit) return 'crit'
  if (frac >= warn) return 'warn'
  return IN_RANGE
}

export const clamp01 = (v: number) => Math.max(0, Math.min(1, v))

/** Fixed-decimal number, or an em dash for "not readable". */
export function num(v: number | null | undefined, digits = 0): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return v.toFixed(digits)
}

/** Memory in MiB → the unit a human actually reads it in. */
export function mem(mibv: number | null | undefined, digits?: number): string {
  if (mibv == null || !Number.isFinite(mibv)) return '—'
  const d = digits ?? (Math.abs(mibv) >= 10240 ? 0 : 1)
  if (Math.abs(mibv) >= 1024 * 1024) return `${(mibv / 1024 / 1024).toFixed(d)} TiB`
  if (Math.abs(mibv) >= 1024) return `${(mibv / 1024).toFixed(d)} GiB`
  return `${Math.round(mibv)} MiB`
}

/** Bytes/s (disk throughput). */
export function bytesPerSec(b: number | null | undefined): string {
  if (b == null || !Number.isFinite(b)) return '—'
  if (b >= 1e9) return `${(b / 1e9).toFixed(2)} GB/s`
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} MB/s`
  if (b >= 1e3) return `${Math.round(b / 1e3)} kB/s`
  return `${Math.round(b)} B/s`
}

/** Bits/s (network — links are always quoted in bits). */
export function bitsPerSec(b: number | null | undefined): string {
  if (b == null || !Number.isFinite(b)) return '—'
  if (b >= 1e9) return `${(b / 1e9).toFixed(2)} Gb/s`
  if (b >= 1e6) return `${(b / 1e6).toFixed(1)} Mb/s`
  if (b >= 1e3) return `${Math.round(b / 1e3)} kb/s`
  return `${Math.round(b)} b/s`
}

export function uptime(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s)) return '—'
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d > 0) return `${d}d ${h}h`
  if (h > 0) return `${h}h ${m}m`
  return `${m}m`
}

/**
 * "2s ago" style freshness readout. `freshMs` is how recent a sample still
 * counts as "now" — the poll interval plus headroom, so a healthy box reads
 * "now" instead of flapping "now / 2s ago" on every poll. Seconds then count
 * up by whole seconds (floor, so the text changes once per second, never
 * twice) once the data is actually late.
 */
export function relTime(now: number, then: number | null, freshMs = 2000): string {
  if (then == null) return 'never'
  const ms = now - then
  if (ms <= freshMs) return 'now'
  // ≥ 1: a late sample that is under a second old reads "1s ago", never "0s ago"
  const s = Math.max(1, Math.floor(ms / 1000))
  if (s < 60) return `${s}s ago`
  return `${Math.floor(s / 60)}m ago`
}

/** hh:mm, for chart spans. */
export function clockOf(t: number): string {
  return new Date(t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

export interface Trend {
  arrow: '' | '▲' | '▼'
  text: string
  tone: Tone | 'load'
}

/**
 * Change over the tail of a series, as change-per-minute — "▲ 4°C/min" is
 * actionable, "0.13 since the window started" is not. Kept deliberately grey:
 * the number next to it already carries the alarm colour, and a rising trace is
 * not automatically bad (VRAM climbing just means a model loaded).
 */
export function trend(values: (number | null)[], intervalS: number, digits = 1, unit = ''): Trend {
  const pts = values.filter((v): v is number => v != null)
  if (pts.length < 4) return { arrow: '', text: '', tone: 'muted' }
  const spanS = Math.max(1, (pts.length - 1) * intervalS)
  const first = pts[0]
  const last = pts[pts.length - 1]
  const perMin = ((last - first) / spanS) * 60
  const shown = Math.abs(perMin).toFixed(digits)
  if (Math.abs(perMin) < 0.05 || Number(shown) === 0) return { arrow: '', text: 'steady', tone: 'muted' }
  return {
    arrow: perMin > 0 ? '▲' : '▼',
    text: `${perMin > 0 ? '+' : '−'}${shown}${unit}/min`,
    tone: 'muted',
  }
}

/** min/max of a series — "5m: 44 / 61" tells you whether it spiked and came down. */
export function spread(values: (number | null)[]): { min: number | null; max: number | null } {
  const pts = values.filter((v): v is number => v != null)
  if (pts.length === 0) return { min: null, max: null }
  return { min: Math.min(...pts), max: Math.max(...pts) }
}

/**
 * Chart axis for a series: its own range, padded, clamped to sane bounds.
 * Autoscaling is what makes a 2 °C drift visible; the absolute number is in the
 * label next to the chart, so the axis does not have to carry it.
 */
export function axis(
  values: (number | null)[],
  opts: { floorAt?: number; ceilAt?: number } = {},
): { min: number; max: number } {
  const { min, max } = spread(values)
  const floor = opts.floorAt ?? 0
  const ceil = opts.ceilAt ?? 100
  if (min == null || max == null) return { min: floor, max: ceil }
  const pad = Math.max((max - min) * 0.25, (ceil - floor) * 0.04, 0.4)
  const lo = Math.max(floor, min - pad)
  const hi = Math.min(ceil, max + pad)
  return hi - lo < 1 ? { min: lo, max: lo + 1 } : { min: lo, max: hi }
}

export const lastOf = (values: (number | null)[]): number | null => {
  for (let i = values.length - 1; i >= 0; i--) if (values[i] != null) return values[i]
  return null
}
