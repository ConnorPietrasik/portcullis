// ─────────────────────────── visual primitives ───────────────────────────
// Two indicator kinds, deliberately:
//   • bars (Meter / Stack) read LEVEL against a scale, with hairline ticks
//     marking where warn/crit sit;
//   • charts (Chart) read TREND over the poll history.
// Colour is never a scale painted across a bar — a bar carries ONE colour and
// that colour means "this needs you" (amber/red) or "just a reading"
// (accent/cyan). Anything else is a rainbow you have to decode.

import { useEffect, useId, useRef, useState } from 'react'
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

// ─────────────────────────── form bits ─────────────────────────────

/** A labelled field: label above the control. Every form in the panel uses it
 *  so label placement never changes from control to control. A div, NOT a
 *  label: the aspect field's control is a group of buttons, and a label
 *  activates its first labelable descendant — clicking the caption would
 *  fire the 1:1 button and retune the size behind the user's back. */
export function Field({
  label,
  children,
  className,
  title,
}: {
  label: ReactNode
  children: ReactNode
  className?: string
  title?: string
}) {
  return (
    <div className={`fld${className ? ` ${className}` : ''}`} title={title}>
      <span>{label}</span>
      {children}
    </div>
  )
}

/** A settings row: label in its own column, control after it. One rhythm for
 *  the whole form, so a stack of options reads as a table, not a scatter. */
export function FRow({ label, children, hint }: { label: ReactNode; children: ReactNode; hint?: ReactNode }) {
  return (
    <div className="frow">
      <span className="fl">{label}</span>
      <div className="fc">
        {children}
        {hint && <span className="fh">{hint}</span>}
      </div>
    </div>
  )
}

/** Pill switch — a two-state choice that needs no words next to it. */
export function Toggle({
  label,
  checked,
  onChange,
  disabled,
  title,
}: {
  label: string
  checked: boolean
  onChange: (on: boolean) => void
  disabled?: boolean
  title?: string
}) {
  return (
    <label className={`tgl${checked ? ' on' : ''}`} title={title}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={e => onChange(e.target.checked)} />
      <i />
      <span>{label}</span>
    </label>
  )
}

/** A small group of mutually exclusive choices, rendered as one control. */
export function Segmented<T extends string>({
  value,
  onChange,
  options,
}: {
  value: T
  onChange: (v: T) => void
  options: { value: T; label: ReactNode; title?: string; count?: number }[]
}) {
  return (
    <div className="seg">
      {options.map(o => (
        <button
          key={o.value}
          type="button"
          className={o.value === value ? 'on' : ''}
          title={o.title}
          onClick={() => onChange(o.value)}
        >
          {o.label}
          {o.count != null && <em>{o.count}</em>}
        </button>
      ))}
    </div>
  )
}

/** Copy-to-clipboard button that says ✓ when it worked (no dialog, no prose). */
export function CopyBtn({ text, title = 'copy' }: { text: string; title?: string }) {
  const [done, setDone] = useState(false)
  const copy = async () => {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text)
      } else {
        // plain-http LAN fallback: the async clipboard API is secure-context only
        const ta = document.createElement('textarea')
        ta.value = text
        ta.style.position = 'fixed'
        ta.style.opacity = '0'
        document.body.appendChild(ta)
        ta.select()
        document.execCommand('copy')
        ta.remove()
      }
      setDone(true)
      window.setTimeout(() => setDone(false), 1200)
    } catch {
      /* clipboard refused — the text is still on screen */
    }
  }
  return (
    <button type="button" className="mini" title={title} onClick={() => void copy()}>
      {done ? '✓' : '⧉'}
    </button>
  )
}

export function Pill({ tone = 'muted', children, title }: { tone?: Tone; children: ReactNode; title?: string }) {
  return (
    <span className={`pill t-${tone}`} title={title}>
      {children}
    </span>
  )
}

/** Tiny monospace tag: a PCI id, a mount, a device name. Reads as metadata. */
export function Chip({
  children,
  title,
  className,
}: {
  children: ReactNode
  title?: string
  className?: string
}) {
  return (
    <span className={`chip${className ? ` ${className}` : ''}`} title={title}>
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

// ─────────────────────────── dialog ──────────────────────────────────────

/**
 * A confirmation that is part of the page instead of the browser's — used for
 * anything destructive (deleting jobs, running a privileged action). The
 * subject goes in `body` so the dialog can name what is about to happen.
 */
export function ConfirmModal({
  title,
  body,
  confirm = 'confirm',
  danger,
  busy,
  onConfirm,
  onClose,
}: {
  title: ReactNode
  body: ReactNode
  confirm?: string
  danger?: boolean
  busy?: boolean
  onConfirm: () => void
  onClose: () => void
}) {
  return (
    <Modal
      title={title}
      onClose={onClose}
      width={460}
      footer={
        <>
          <button onClick={onClose}>cancel</button>
          <button
            className={danger ? 'danger' : 'warn'}
            disabled={busy}
            onClick={() => void onConfirm()}
          >
            {busy ? '…' : confirm}
          </button>
        </>
      }
    >
      {body}
    </Modal>
  )
}

/**
 * A popup editor: a backdrop + a centred card over the page. Closes on
 * Escape, the ×, the caller's own cancel, or a mousedown that STARTS on the
 * backdrop (checked on mousedown, not click — a click that only ENDS on the
 * backdrop after dragging text out of the modal would otherwise close it
 * and lose the selection). Body scroll is locked while open. On open, focus
 * moves into the dialog (so Escape and Tab start there); on close it is
 * restored to whatever was focused before. Deliberately no focus trap:
 * this app's inputs are simple and tab order stays sane.
 */
export function Modal({
  title,
  onClose,
  children,
  footer,
  width = 640,
}: {
  title: ReactNode
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  /** max width in px */
  width?: number
}) {
  // Callers pass inline arrows (new identity every render); read the latest
  // through a ref so the Escape listener below subscribes ONCE instead of
  // re-subscribing on every caller re-render (i.e. every keystroke).
  const closeRef = useRef(onClose)
  useEffect(() => {
    closeRef.current = onClose
  }, [onClose])

  const boxRef = useRef<HTMLDivElement>(null)
  const titleId = useId()
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') closeRef.current()
    }
    window.addEventListener('keydown', onKey)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    const prevFocus = document.activeElement
    boxRef.current?.focus()
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = prevOverflow
      if (prevFocus instanceof HTMLElement) prevFocus.focus()
    }
  }, [])

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div
        ref={boxRef}
        className="modal"
        style={{ maxWidth: width }}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <header>
          <h3 id={titleId}>{title}</h3>
          <button className="modal-x" title="close" onClick={onClose}>
            ×
          </button>
        </header>
        <div className="modal-body">{children}</div>
        {footer && <div className="modal-footer btns">{footer}</div>}
      </div>
    </div>
  )
}
