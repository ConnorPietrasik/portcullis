// ─────────────────────────── visual primitives ───────────────────────────
// Two indicator kinds, deliberately:
//   • bars (Meter / Stack) read LEVEL against a scale, with hairline ticks
//     marking where warn/crit sit;
//   • charts (Chart) read TREND over the poll history.
// Colour is never a scale painted across a bar — a bar carries ONE colour and
// that colour means "this needs you" (amber/red) or "just a reading"
// (accent/cyan). Anything else is a rainbow you have to decode.

import type { ReactNode } from 'react'
import { clamp01 } from './fmt'
import type { Tone } from './fmt'
import type { Num } from './history'

const TONE_COLOR: Record<Tone, string> = {
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
}

/**
 * A series that always spans whatever width it is given: x is drawn in a
 * fixed 0–100 user-unit space that the viewBox stretches to the svg's
 * 100% width, y in pixels (the height is fixed), and the svg is width:100%
 * with preserveAspectRatio="none". Nothing is measured, so nothing can feed
 * back into the parent's content-based sizing — a measured, pixel-sized
 * <svg> used to be the engine of a runaway: chart measured container, svg
 * rendered that wide, the grid track grew to the svg's intrinsic width, the
 * chart re-measured wider, and the card ended up ~1800 px wide inside a
 * 696 px slot. (Path data can't use % — only numbers in user units — so the
 * % idea lives in the viewBox, not in `d`.)
 * vector-effect="non-scaling-stroke" keeps strokes 1.6 px despite the x
 * stretch. The "now" dot and the "no data" line are HTML overlays: under
 * the non-uniform scaling a <circle> would render as an ellipse and
 * stroke-dasharray would stretch with the width, while both are
 * fixed-geometry, so neither needs measuring.
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
  // W is the viewBox's user-unit width; x() values are numbers for `d` (which
  // rejects %), and the same numbers read as % of the rendered width for the
  // HTML dot overlay.
  const W = 100
  const x = (i: number) => (i / (n - 1)) * W
  const y = (v: number) => padTop + usable - ((Math.max(min, Math.min(max, v)) - min) / span) * usable

  const paths = series.map((s) => {
    let d = ''
    let open = false
    s.values.forEach((v, i) => {
      if (v == null) {
        open = false
        return
      }
      d += `${open ? 'L' : 'M'}${x(i).toFixed(3)} ${y(v).toFixed(2)}`
      open = true
    })
    const first = s.values.findIndex((v) => v != null)
    const lastIdx = lastIndexOf(s.values)
    const area =
      s.fill && first >= 0 && lastIdx > first
        ? `${d}L${x(lastIdx).toFixed(3)} ${h}L${x(first).toFixed(3)} ${h}Z`
        : ''
    let dotAt: { xPct: number; y: number } | null = null
    if (lastIdx >= 0 && s.values[lastIdx] != null) dotAt = { xPct: x(lastIdx), y: y(s.values[lastIdx] as number) }
    return { d, area, dotAt, s }
  })

  return (
    <div className="chart" style={{ height: h }}>
      <svg width="100%" height={h} viewBox={`0 0 ${W} ${h}`} preserveAspectRatio="none" role="img">
        {grid.filter((g) => g > min && g < max).map((g) => (
          <line key={g} x1={0} x2={W} y1={y(g)} y2={y(g)} className="grid" />
        ))}
        {paths.map(({ d, area, s }, i) => (
          <g key={i}>
            {area && <path d={area} fill={TONE_COLOR[s.tone]} fillOpacity={0.12} stroke="none" />}
            <path
              d={d}
              fill="none"
              stroke={TONE_COLOR[s.tone]}
              strokeWidth={1.6}
              strokeLinejoin="round"
              strokeLinecap="round"
              vectorEffect="non-scaling-stroke"
            />
          </g>
        ))}
      </svg>
      {/* "now" markers live outside the svg: the x stretch is non-uniform, so a
          <circle> would render as an ellipse. y is 1:1 (viewBox height ==
          element height); --x maps 0–100 user units onto 0–100% of the width,
          and .chart-now clamps it so the dot can't spill into the tile
          padding at the chart's edges. */}
      {paths.map(({ dotAt, s }, i) =>
        dot && dotAt ? (
          <span
            key={i}
            className="chart-now"
            style={{ '--x': `${dotAt.xPct}%`, top: dotAt.y, background: TONE_COLOR[s.tone] } as React.CSSProperties}
          />
        ) : null,
      )}
      {/* "no data" placeholder: HTML for the same reason as the dots — an SVG
          <line>'s dash lengths sit in user space and stretch with the x-scale
          (wider tile, coarser dashes). The line is fixed-geometry (full width,
          mid height), so a fixed-px 2/4 pattern needs no measurement. */}
      {paths.every((p) => !p.d) && <span className="chart-empty" style={{ top: h / 2 - 0.5 }} />}
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
