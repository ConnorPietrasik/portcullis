// ─────────────────────────── visual primitives ───────────────────────────
// Two indicator kinds, deliberately:
//   • bars (Meter / Stack) read LEVEL against a scale, with hairline ticks
//     marking where warn/crit sit;
//   • charts (Chart) read TREND over the poll history.
// Colour is never a scale painted across a bar — a bar carries ONE colour and
// that colour means "this needs you" (amber/red) or "just a reading"
// (accent/cyan). Anything else is a rainbow you have to decode.

import type { ReactNode } from 'react'
import { axis, clamp01 } from './fmt'
import type { Tone } from './fmt'
import type { Num } from './history'

export const TONE_COLOR: Record<Tone, string> = {
  ok: 'var(--green)',
  warn: 'var(--amber)',
  crit: 'var(--red)',
  load: 'var(--accent)',
  flow: 'var(--cyan)',
  muted: 'var(--dim)',
  na: 'var(--dim)',
}

/** Fill width as a % of the bar. */
export function Meter({
  pct,
  tone = 'load',
  ticks = [],
  bands,
  title,
}: {
  pct: Num
  tone?: Tone
  /** hairline marks at fractions with no zone shading (e.g. "1.0 per thread") */
  ticks?: number[]
  /** [warn, crit] fractions — paints the bar's own track: room · watch · danger */
  bands?: [number, number]
  title?: string
}) {
  // Zones show even with no reading: the scale is a fact about the sensor, and
  // an iGPU row with nothing to report should still line up with its siblings.
  const zones = bands
    ? ({ '--warn': `${clamp01(bands[0]) * 100}%`, '--crit': `${clamp01(bands[1]) * 100}%` } as React.CSSProperties)
    : undefined
  return (
    <span
      className={`meter t-${tone}${pct == null ? ' na' : ''}${bands ? ' zones' : ''}`}
      style={zones}
      title={title}
      aria-hidden
    >
      <i style={{ width: `${(pct == null ? 0 : clamp01(pct)) * 100}%` }} />
      {!bands && ticks.map((t, i) => <b key={i} style={{ left: `${clamp01(t) * 100}%` }} />)}
    </span>
  )
}

/** Segmented bar: the parts sum to what they say they do (RAM, VRAM, disks). */
export function Stack({ parts, title }: { parts: { pct: number; cls: string; label: string }[]; title?: string }) {
  return (
    <span className="stack" title={title} aria-hidden>
      {parts.map((p, i) => (
        <i key={i} className={p.cls} style={{ width: `${clamp01(p.pct) * 100}%` }} title={p.label} />
      ))}
    </span>
  )
}

export interface Series {
  values: Num[]
  tone: Tone
  fill?: boolean
  dashed?: boolean
}

/**
 * A series that always spans whatever width it is given: x is drawn in
 * percentages, y in pixels (the height is fixed), and the svg is width:100%
 * with no viewBox. Nothing is measured, so nothing can feed back into the
 * parent's content-based sizing — a measured, pixel-sized <svg> used to be
 * the engine of a runaway: chart measured container, svg rendered that wide,
 * the grid track grew to the svg's intrinsic width, the chart re-measured
 * wider, and the card ended up ~1800 px wide inside a 696 px slot.
 * No scaling means strokes stay 1.6 px and the "now" dot stays a circle.
 * Nulls are gaps — a missing reading never becomes a dramatic drop to zero.
 */
export function Chart({
  series,
  min,
  max,
  height = 56,
  grid = [],
  dot = true,
  padTop = 3,
}: {
  series: Series[]
  min: number
  max: number
  height?: number
  grid?: number[]
  dot?: boolean
  padTop?: number
}) {
  const h = height
  const usable = Math.max(1, h - padTop * 2)
  const span = max - min || 1
  // ≥ 2 so x() never divides by zero (single reading → one point at the left edge)
  const n = Math.max(...series.map((s) => s.values.length), 2)
  const x = (i: number) => (i / (n - 1)) * 100
  const y = (v: number) => padTop + usable - ((Math.max(min, Math.min(max, v)) - min) / span) * usable

  const paths = series.map((s) => {
    let d = ''
    let open = false
    s.values.forEach((v, i) => {
      if (v == null) {
        open = false
        return
      }
      d += `${open ? 'L' : 'M'}${x(i).toFixed(3)}% ${y(v).toFixed(2)}`
      open = true
    })
    const first = s.values.findIndex((v) => v != null)
    const lastIdx = lastIndexOf(s.values)
    const area =
      s.fill && first >= 0 && lastIdx > first
        ? `${d}L${x(lastIdx).toFixed(3)}% ${h}L${x(first).toFixed(3)}% ${h}Z`
        : ''
    let dotAt: [string, string] | null = null
    if (lastIdx >= 0 && s.values[lastIdx] != null) dotAt = [`${x(lastIdx).toFixed(3)}%`, y(s.values[lastIdx] as number).toFixed(2)]
    return { d, area, dotAt, s }
  })

  return (
    <div className="chart" style={{ height: h }}>
      <svg width="100%" height={h} role="img">
        {grid.filter((g) => g > min && g < max).map((g) => (
          <line key={g} x1="0" x2="100%" y1={y(g)} y2={y(g)} className="grid" />
        ))}
        {paths.map(({ d, area, dotAt, s }, i) => (
          <g key={i}>
            {area && <path d={area} fill={TONE_COLOR[s.tone]} fillOpacity={0.12} stroke="none" />}
            <path
              d={d}
              fill="none"
              stroke={TONE_COLOR[s.tone]}
              strokeWidth={1.6}
              strokeLinejoin="round"
              strokeLinecap="round"
              strokeDasharray={s.dashed ? '3 3' : undefined}
            />
            {dot && dotAt && <circle cx={dotAt[0]} cy={dotAt[1]} r={2.4} fill={TONE_COLOR[s.tone]} />}
          </g>
        ))}
        {paths.every((p) => !p.d) && <line x1="0" x2="100%" y1={h / 2} y2={h / 2} className="grid" strokeDasharray="2 4" />}
      </svg>
    </div>
  )
}

function lastIndexOf(values: Num[]): number {
  for (let i = values.length - 1; i >= 0; i--) if (values[i] != null) return i
  return -1
}

export function Pill({ tone = 'muted', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`pill t-${tone}`} title={title}>
      {children}
    </span>
  )
}

/** Tiny monospace tag: a PCI id, a mount, a device name. Reads as metadata. */
export function Chip({ children, title }: { children: ReactNode; title?: string }) {
  return (
    <span className="chip" title={title}>
      {children}
    </span>
  )
}

/** One labelled reading: name · [history] · [bar] · value. */
export function MetricRow({
  label,
  value,
  unit,
  tone = 'load',
  pct,
  ticks = [],
  bands,
  spark,
  title,
}: {
  label: string
  value: string
  unit?: string
  tone?: Tone
  /** null = no reading (empty track); undefined = this metric has no scale to draw. */
  pct?: Num
  ticks?: number[]
  bands?: [number, number]
  spark?: Num[]
  title?: string
}) {
  return (
    <div className="mrow" title={title}>
      <span className="k">{label}</span>
      {spark && (
        <span className="s">
          <Chart series={[{ values: spark, tone, fill: true }]} min={axis(spark, { floorAt: 0 }).min} max={axis(spark, { floorAt: 0 }).max} height={16} dot={false} padTop={2} />
        </span>
      )}
      {/* the slot is always there: without a scale the value must still line up
          with the values in the rows above and below it */}
      <span className="b">{pct !== undefined && <Meter pct={pct} tone={tone} ticks={ticks} bands={bands} />}</span>
      <span className={`v t-${tone}`}>
        {value}
        {unit && <em>{unit}</em>}
      </span>
    </div>
  )
}

/** A big number with its unit, the thing you read from across the room. */
export function Big({ value, unit, tone = 'load' }: { value: string; unit?: string; tone?: Tone }) {
  return (
    <div className={`big t-${tone}`}>
      {value}
      {unit && <em>{unit}</em>}
    </div>
  )
}

/** A labelled cell of the host strip. */
export function Tile({ label, title, right, children }: { label: string; title?: string; right?: ReactNode; children: ReactNode }) {
  return (
    <section className="tile" title={title}>
      <h4>
        {label}
        {right && <span className="r">{right}</span>}
      </h4>
      {children}
    </section>
  )
}

/** "▲ 4.1/min" style delta — the one place a trend arrow appears. */
export function TrendMark({ arrow, text, tone = 'muted' }: { arrow: string; text: string; tone?: Tone }) {
  if (!text) return null
  return (
    <span className={`trend t-${tone}`}>
      {arrow && <b>{arrow}</b>}
      {text}
    </span>
  )
}
