// Gen — the panel's image/video queue (panel-api's gen section).
//
// One composer, one queue, one gallery of what came out of it. The slots
// themselves live in the GPUs view (cards, starts, lane math) — this tab only
// shows what they are doing and links there.
//
// Two polls, both small (the tab may sit on a phone on a metered line — the
// whole 500-job store never crosses the wire):
//   /api/gen        counts + the in-flight set (bounded by the admission caps)
//   /api/gen/jobs   ONE page of history (newest first; cursor/q/filter)
// Both are ETag/304'd — an idle tick (nothing in flight, no card changing
// hands, no instance flapping) answers 304, headers only; the browser's
// cache re-serves its stored body for the app (the wire is what shrinks), and
// usePoll's NotModified path covers a 304 that reaches the app directly.
// The page's records are CARD projections: the human prompt clipped (the
// enhanced rewrite is dropped — it is machine text, not what was typed, and
// it is the modal's), no params. The in-flight set is FULL and is merged
// into whatever page is showing (so a long video keeps its progress bar on
// every page), and the modal + remix fetch the full record by id for a
// TERMINAL job. Every mutation just re-polls.
//
// The `model` field accepts a purpose key (image/video — mapped in the
// settings) or an exact model name; the server resolves it.
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Tab } from '../App'
import { api, download, NotFound } from '../api'
import { usePoll } from '../usePoll'
import { ConfirmModal, CopyBtn, Field, FRow, Meter, Modal, Segmented, Toggle } from '../ui'
import { clockOf, dur, relTime } from '../fmt'
import type {
  GenEditInput,
  GenRowModel,
  GenInstance,
  GenJob,
  GenJobCard,
  GenModelInfo,
  GenOverview,
  GenPage,
  GenSlot,
  GenState,
} from '../types'

const IN_FLIGHT: ReadonlySet<GenState> = new Set(['queued', 'starting', 'enhancing', 'loading', 'running'])
const isLive = (j: GenJob | GenJobCard) => IN_FLIGHT.has(j.state)

/** The record's params — the card projection drops the field (the modal and
 *  the remix hold the full record; the beat before the fetch lands, there
 *  are none). */
function paramsOf(j: GenJob | GenJobCard): Record<string, unknown> {
  return 'params' in j ? (j as GenJob).params : {}
}

/** The record's enhanced prompt — the card projection drops the field (the
 *  modal holds the full record; the beat before its fetch lands, there is
 *  none). */
function enhancedOf(j: GenJob | GenJobCard): string | null {
  return 'enhanced_prompt' in j ? ((j as GenJob).enhanced_prompt ?? null) : null
}

const STATE: Record<GenState, { cls: string; label: string }> = {
  queued: { cls: 'badge', label: 'queued' },
  starting: { cls: 'badge warn', label: 'starting' },
  enhancing: { cls: 'badge warn', label: 'enhancing' },
  loading: { cls: 'badge warn', label: 'loading' },
  running: { cls: 'badge ok', label: 'running' },
  completed: { cls: 'badge ok', label: 'done' },
  failed: { cls: 'badge err', label: 'failed' },
  canceled: { cls: 'badge dim', label: 'canceled' },
}

const shortId = (id: string) => id.replace('job-', '#')

/** A job's mode label: an edit READS PICTURES; its artifact class (`kind`)
 *  is still "image". */
const modeOf = (j: GenJob | GenJobCard) => (j.edit ? 'edit' : j.kind)

// What a deployed model can actually do, read off radiance's implementation
// (ref/radiance). A tooltip, not a second model registry: no discovery, no
// enforcement — radiance's submit-time 400 stays the authority, and unknown
// models get no note. (What a model is ALLOWED to do — generate, or also
// edit — is radiance's answer: `models[name].edits`.)
const MODEL_NOTES: Record<string, string> = {
  'Minimax-H3-T2V':
    '5.17–14.38 s (24 fps, request rounds up to a ≈0.71 s chunk) · with sound · sides ×32, ≥256 px, ≤1344×768 area, aspect ≤4:1 · prompt ≤8192 tokens',
}

// ── an edit's row budget (why the frames change with the model) ────────────
// max-model-len is the rows ONE pass gets: prompt + every reference picture +
// the frame. A reference resamples to the model's 1024x1024 reference area,
// which is what the 4600-row figure sizes (MULTIMODEL §6.3; the KV cost is
// 4096, GUIDE §5.6 — RUNBOOK → Editing pictures). PROMPT_ROWS is an
// allowance, not a measurement. Over budget is radiance's 400, which lands in
// the job's error column.
const ROW_PX = 16 // one latent row is a 16x16 block of pixels
const REF_ROWS = 4600 // MULTIMODEL §6.3: sizing a pass for one reference
const PROMPT_ROWS = 256 // allowance for the prompt's text rows (not measured)
// The fallback row model; the server's (`row_model`) arrives with the overview
// so the chips and the scheduler's routing price a pass the same way.
const RM0: GenRowModel = { row_px: ROW_PX, ref_rows: REF_ROWS, prompt_rows: PROMPT_ROWS }

/** What one pass costs: the frame + every reference picture + the prompt. */
function passRows(rm: GenRowModel, rows: number, refs: number): number {
  return rows + refs * rm.ref_rows + rm.prompt_rows
}

/** The slots that could take a `need`-row pass for `model`, cheapest seat
 *  first — the UI's echo of gen_jobs.job_candidates. An unbounded slot takes
 *  any pass, so it is the dearest seat, not the free one. */
function seatsFor(o: GenOverview, model: string | null, need: number | null): string[] {
  if (!model) return []
  const rank = (b: number) => (b > 0 ? b : Number.MAX_SAFE_INTEGER)
  return o.slots
    .filter(s => model in s.models)
    .filter(s => {
      const b = s.mlens[model] ?? 0
      return b <= 0 || need == null || need <= b
    })
    .sort((a, b) => rank(a.mlens[model] ?? 0) - rank(b.mlens[model] ?? 0))
    .map(s => s.display.replace(/^Gen /, ''))
}

/** WxH (or W*H) → the latent rows it costs, or null when it isn't two numbers
 *  (the row width comes from the server's row model, so the chips and the
 *  routing price a frame the same way). */
function latentRows(rm: GenRowModel, size: string): number | null {
  const m = /^\s*(\d+)\s*[x*]\s*(\d+)\s*$/i.exec(size)
  if (!m) return null
  const px = rm.row_px || ROW_PX
  return Math.floor(Number(m[1]) / px) * Math.floor(Number(m[2]) / px)
}

/** Rows a frame has LEFT on a slot bounding the model at `bound`: the bound
 *  minus the reference pictures and the prompt. 0 = that slot bounds nothing. */
function editBudget(rm: GenRowModel, bound: number, refs = 1): number {
  return bound > 0 ? Math.max(0, bound - refs * rm.ref_rows - rm.prompt_rows) : 0
}

// The frame ladder per aspect, largest first — the widest rung that fits the
// budget becomes the chip. Sides stay multiples of 32 (what these models take).
const EDIT_LADDER: { ratio: string; sizes: string[] }[] = [
  { ratio: '1:1', sizes: ['1024x1024', '960x960', '896x896', '768x768', '640x640'] },
  { ratio: '4:3', sizes: ['1152x864', '1024x768', '896x672', '768x576'] },
  { ratio: '3:2', sizes: ['1152x768', '1024x672', '896x576'] },
  { ratio: '16:9', sizes: ['1280x704', '1024x576', '896x512', '768x448'] },
  { ratio: '3:4', sizes: ['864x1152', '768x1024', '672x896', '576x768'] },
  { ratio: '2:3', sizes: ['768x1152', '672x1024', '576x896'] },
  { ratio: '9:16', sizes: ['704x1280', '576x1024', '512x896', '448x768'] },
]

/** The edit's frames: each aspect's largest rung the CHEAP seat can take; when
 *  it can take nothing, the largest rung the LONG-PASS bound covers (the chips
 *  stay dotted "far"). An unbounded model takes the top rung, and the smallest
 *  rung is the last resort when even the long-pass bound holds no rung. Sizing
 *  off the cheap seat keeps the chips on the frames the fleet serves routinely. */
function editFrames(rm: GenRowModel, bound: number, refs = 1, cheap = 0): { ratio: string; size: string }[] {
  const budget = editBudget(rm, bound, refs)
  const cheapBudget = cheap > 0 && cheap < bound ? editBudget(rm, cheap, refs) : budget
  return EDIT_LADDER.map(({ ratio, sizes }) => {
    const rung = (cap: number) => (cap > 0 ? sizes.find(x => (latentRows(rm, x) ?? 0) <= cap) : undefined)
    return {
      ratio,
      size: bound <= 0 ? sizes[0] : (rung(cheapBudget) ?? rung(budget) ?? sizes[sizes.length - 1]),
    }
  })
}

// One-click frames per class, square → landscape → portrait. Every entry
// satisfies the model's own grid: H3 wants sides ×32 within 1344×768 of area
// (1152×864 is the largest 4:3 that fits — smaller ones like 1024×768 would
// fit too, they just use less of the frame); Krea2 wants sides ×16 within
// 2048². 4:3 and 3:4 are the phone-camera frame — the one you actually shoot in.
const SIZES: Record<'image' | 'video', { ratio: string; size: string }[]> = {
  image: [
    { ratio: '1:1', size: '1024x1024' },
    { ratio: '4:3', size: '1536x1152' },
    { ratio: '3:2', size: '1536x1024' },
    { ratio: '16:9', size: '1344x768' },
    { ratio: '3:4', size: '1152x1536' },
    { ratio: '9:16', size: '768x1344' },
  ],
  video: [
    { ratio: '1:1', size: '960x960' },
    { ratio: '4:3', size: '1152x864' },
    { ratio: '16:9', size: '1344x768' },
    { ratio: '3:4', size: '864x1152' },
    { ratio: '9:16', size: '768x1344' },
  ],
}

/** WxH (or W*H) as an aspect, or null if it isn't two numbers. */
function aspect(size: string): number | null {
  const m = /^\s*(\d+)\s*[x*]\s*(\d+)\s*$/i.exec(size)
  return m ? Number(m[1]) / Number(m[2]) : null
}

/** The frame chips for one composer state: a video model's list, an edit's
 *  budget-sized ladder, or the generation list. Identity matters — a change
 *  of list is what retunes the size field (see refit). */
function framesFor(rm: GenRowModel, kind: 'image' | 'video' | null, editing: boolean, bound: number, refs = 1, cheap = 0): { ratio: string; size: string }[] {
  if (kind === 'video') return SIZES.video
  return editing ? editFrames(rm, bound, refs, cheap) : SIZES.image
}

/** A frame that fits one model/list is often illegal on another, so a change
 *  of kind or mode retunes the size: keep it if it is a frame of the new list,
 *  else take that list's rung for the same aspect, else its 16:9. Null = leave
 *  it alone. */
function refit(size: string, frames: { ratio: string; size: string }[]): string | null {
  if (frames.some(p => p.size === size.trim())) return null
  const a = aspect(size)
  const same =
    a == null
      ? undefined
      : frames.find(p => {
          const [w, h] = p.size.split('x').map(Number)
          return Math.abs(w / h - a) < 1e-6
        })
  return (same ?? frames.find(p => p.ratio === '16:9') ?? frames[0]).size
}

/** The composer's one-line note: the hardcoded per-model facts plus what the
 *  config and radiance say about the model in hand — including whether it can
 *  take pictures at all, and what an edit's frame fits. */
function noteFor(
  rm: GenRowModel,
  resolved: string,
  info: GenModelInfo | undefined,
  editing: boolean,
  refs: number,
  need: number | null,
  seats: string[],
): string {
  const parts: string[] = []
  if (MODEL_NOTES[resolved]) parts.push(MODEL_NOTES[resolved])
  if (info) {
    if (info.kind === 'image') {
      if (info.edits === false) parts.push('text only — radiance answers 501 on /v1/images/edits')
      else if (info.edits === null) parts.push('edits unverified — this slot has not answered /v1/models yet')
      if (info.mlen) {
        // WHERE it will draw: the same routing the scheduler will apply.
        const cost = `${refs} picture${refs > 1 ? 's' : ''} cost ${refs * rm.ref_rows}`
        const here = seats.join('/')
        if (need != null && info.mlen_min > 0 && need > info.mlen_min) {
          // here empty = every seat bounds the model and none covers the pass —
          // create refuses it, so say so instead of naming a seat that isn't one
          parts.push(
            here
              ? `${need} rows${editing ? ` (${cost})` : ''} — only ${here} takes that pass (${info.mlen_min} bounds the others)`
              : `${need} rows${editing ? ` (${cost})` : ''} — no slot takes that pass (the longest bound is ${info.mlen} rows)`,
          )
        } else {
          parts.push(
            `${need ?? info.mlen} rows${editing ? ` (${cost})` : ''} — ${here || 'any slot'} can draw it`,
          )
        }
      } else if (editing) {
        parts.push('edits')
      }
    }
  }
  return parts.join(' · ')
}

// The composer survives a reload (the prompt you were writing is the one
// thing worth keeping) and survives a submit (the same prompt twice is a
// perfectly valid thing to want).
interface Draft {
  model: string
  prompt: string
  size: string
  seed: string
  seconds: string
  enhance: boolean | null // null → the settings' default
}
const DRAFT_KEY = 'panel.gen.draft'
const BLANK: Draft = { model: '', prompt: '', size: '1344x768', seed: '-1', seconds: '5', enhance: null }

function loadDraft(): Draft {
  try {
    const raw = localStorage.getItem(DRAFT_KEY)
    return raw ? { ...BLANK, ...(JSON.parse(raw) as Partial<Draft>) } : BLANK
  } catch {
    return BLANK
  }
}

// ── an edit's source pictures (composer state, never the draft) ────────────
// `dataUrl` is what gets posted (a data: URL, the shape radiance's images API
// takes). Held in React state, NOT the persisted draft: base64 would eat the
// localStorage quota.
interface Source {
  key: string
  name: string
  size: number
  mime: string
  dataUrl: string
  /** the mask marks what may change (its alpha), on the FIRST picture */
  mask?: boolean
  /** remixed from this stored artifact */
  from?: string
  /** decoded pixel size (absent when the browser cannot draw it); the mask's
   *  door compares it against the first picture's */
  w?: number
  h?: number
}

// radiance's reference-picture cap and formats; the size cap mirrors
// gen_jobs.EDIT_INPUT_MAX_BYTES (a too-big upload is refused here so it never
// costs a slot start to fail).
const SRC_MAX = 10
const SRC_TYPES = ['image/png', 'image/jpeg', 'image/webp']
// a mask is the alpha-having subset (a jpeg mask decodes fully opaque and
// would repaint nothing — the server refuses it the same way)
const SRC_MASK_TYPES = ['image/png', 'image/webp']
const SRC_MAX_BYTES = 16 * 1024 * 1024
const SRC_ACCEPT = SRC_TYPES.join(',')
const SRC_MASK_ACCEPT = SRC_MASK_TYPES.join(',')

function readSource(file: File, mask: boolean): Promise<Source> {
  return new Promise((resolve, reject) => {
    if (!SRC_TYPES.includes(file.type)) {
      reject(new Error(`${file.name}: not a png, jpeg or webp picture`))
      return
    }
    if (file.size > SRC_MAX_BYTES) {
      reject(new Error(`${file.name}: ${(file.size / 1048576).toFixed(1)} MiB — a picture is at most 16 MiB`))
      return
    }
    const fr = new FileReader()
    fr.onload = () => {
      // the decoded pixel size rides along (the mask's door compares it to the
      // first picture's); a picture the browser cannot decode still attaches —
      // without w/h the check stands down.
      const base = { key: `${file.name}-${file.size}-${crypto.randomUUID?.() ?? Math.random()}`, name: file.name, size: file.size, mime: file.type, dataUrl: String(fr.result), mask }
      const img = new Image()
      img.onload = () => resolve({ ...base, w: img.naturalWidth, h: img.naturalHeight })
      img.onerror = () => resolve(base)
      img.src = String(fr.result)
    }
    fr.onerror = () => reject(new Error(`${file.name}: could not be read`))
    fr.readAsDataURL(file)
  })
}

/** A stored input (a remix's source) → the same shape, so one code path posts
 *  it. A dead URL (age GC, a deleted record) throws — the caller says so
 *  rather than queueing an edit with a missing picture. */
async function fetchSource(url: string, mask: boolean, mime?: string): Promise<Source> {
  const r = await fetch(url)
  if (!r.ok) throw new Error(`${url}: ${r.status}`)
  const blob = await r.blob()
  const name = url.split('/').pop() ?? 'picture'
  // the record's mime wins over the transport's: the panel stored exactly
  // these bytes under exactly that type
  const src = await readSource(new File([blob], name, { type: mime || blob.type || 'image/png' }), mask)
  return { ...src, from: url }
}

/** A chip's preview; a picture that will not decode collapses to a
 *  placeholder rather than a broken-image glyph. */
function SrcPreview({ src }: { src: Source }) {
  const [bad, setBad] = useState(false)
  return bad ? <span className="noimg">no preview</span> : <img src={src.dataUrl} alt="" onError={() => setBad(true)} />
}

const kibi = (n: number) => (n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KiB` : `${(n / 1048576).toFixed(1)} MiB`)

// ── status strip ────────────────────────────────────────────────────────────

function GenBar({ o, onSettings, onTab }: { o: GenOverview; onSettings: () => void; onTab?: (t: Tab) => void }) {
  const run = o.active.filter(j => j.state === 'running' || j.state === 'loading' || j.state === 'starting').length
  const enh = o.active.filter(j => j.state === 'enhancing').length
  const queue = o.active.filter(j => j.state === 'queued').length
  const et = o.enhance_target
  return (
    <div className="bar">
      <span className="grp">
        {run > 0 && (
          <span className="c ok">
            <i />
            {run} running
          </span>
        )}
        {enh > 0 && (
          <span className="c" title="the rewrite is running on the enhance target">
            <i />
            {enh} enhancing
          </span>
        )}
        {queue > 0 && (
          <span className="c">
            <i />
            {queue} queued
          </span>
        )}
        {run + queue + enh === 0 && <span className="c idle">queue empty</span>}
      </span>
      <span className="div" />
      <span className="grp">
        {o.slots.map((s: GenSlot) => (
          <span
            key={s.name}
            className={`slot s-${s.status.busy ? 'busy' : s.running ? 'up' : s.status.state === 'blocked' ? 'blocked' : 'down'}`}
            title={
              s.status.busy
                ? `${s.name} — a job is in flight`
                : s.running
                  ? `${s.name} — up, idle (model resident)`
                  // the idle-yield a start would perform (the old slots table's
                  // "down · yields N" — the bar chip is too small for the text)
                  : `${s.name} — down · ${
                      s.status.blockers.length
                        ? `blocked: ${s.status.blockers.join(' · ')}`
                        : s.status.stops.length
                          ? `a start would stop: ${s.status.stops.join(', ')} (idle)`
                          : 'cards free'
                    }`
            }
          >
            <i />
            {s.display.replace(/^Gen /, '')}
          </span>
        ))}
        {onTab && (
          <button className="mini" onClick={() => onTab('GPUs')} title="GPU pool — cards, starts, lanes">
            pool ↗
          </button>
        )}
      </span>
      <span className="sp" />
      <button
        className={`tgt${et.ok ? (et.running ? ' ok' : '') : ' err'}`}
        onClick={onSettings}
        title={et.ok ? `enhancement runs on ${et.instance} · ${et.model}` : et.reason || 'enhance target misconfigured'}
      >
        enhance {et.ok ? `${et.model}${et.running ? '' : ' (asleep)'}` : '· broken'}
      </button>
      <button onClick={onSettings}>settings</button>
    </div>
  )
}

// ── composer ────────────────────────────────────────────────────────────────

function Composer({
  o,
  draft,
  setDraft,
  srcs,
  setSrcs,
  running,
  onSubmit,
}: {
  o: GenOverview
  draft: Draft
  setDraft: (d: Draft) => void
  /** an edit's source pictures (composer state — see the Source note) */
  srcs: Source[]
  setSrcs: (s: Source[]) => void
  running: boolean
  onSubmit: () => Promise<string | null>
}) {
  const [err, setErr] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)
  const [drag, setDrag] = useState(false)
  const ta = useRef<HTMLTextAreaElement>(null)
  const fileIn = useRef<HTMLInputElement>(null)
  const maskIn = useRef<HTMLInputElement>(null)

  const { imageModels, videoModels, editModels, effective, kind, resolved, purpose } = resolveModel(o, draft.model)
  const info = resolved ? o.models[resolved] : undefined
  const refs = srcs.filter(s => !s.mask)
  const mask = srcs.find(s => s.mask)
  /** what the model radiance says about pictures: false = "text only", null
   *  = "never probed" — not "cannot", so the attach stays open */
  const canEdit = kind === 'image' && info?.edits !== false
  const editing = refs.length > 0 || purpose === 'edit'
  // One picture is assumed the moment the edit purpose is chosen with nothing
  // attached yet (submit stays disabled until one is). The model can be bound
  // differently per slot, so two lines matter: what the CHEAPEST seat takes and
  // what the LONGEST BOUNDED one takes (an unbounded seat takes any pass).
  const rm = o.row_model ?? RM0
  const big = info?.mlen ?? 0
  const cheap = info?.mlen_min ?? 0
  const nrefs = Math.max(1, refs.length)
  const frames = framesFor(rm, kind, editing, big, nrefs, cheap)
  const rows = latentRows(rm, draft.size)
  const need = rows == null ? null : passRows(rm, rows, editing ? nrefs : 0)
  const seats = seatsFor(o, resolved, need)
  const only = rows != null && cheap > 0 && need != null && need > cheap
  // create's routing check, mirrored: a pass is refused only when EVERY seat
  // bounds the model and none covers it (an unbounded seat takes any pass).
  const seatBounds = resolved ? o.slots.filter(s => resolved in s.models).map(s => s.mlens[resolved] ?? 0) : []
  const noSeat = (n: number) => seatBounds.length > 0 && seatBounds.every(b => b > 0 && n > b)
  /** no slot's pass covers it: create refuses this, and says it up front here */
  const over = rows != null && need != null && noSeat(need)
  const overLabel = `${draft.size}${refs.length ? ` + ${refs.length} picture${refs.length > 1 ? 's' : ''}` : ''} = ${need} rows > ${big} — no slot gives ${resolved} that pass`
  const note = noteFor(rm, resolved, info, editing, editing ? nrefs : 0, need, seats)
  const configured = !!(resolved && o.enhance.prompts[resolved])
  const enhanceOn = draft.enhance ?? o.enhance.enabled

  const patch = (p: Partial<Draft>) => setDraft({ ...draft, ...p })

  function say(msg: string | null) {
    setFlash(msg)
    if (msg) window.setTimeout(() => setFlash(null), 4000)
  }

  /** Attach pictures. The first one retunes the frame (an edit pays for its
   *  reference picture out of the frame's row budget). */
  async function addPics(files: File[]) {
    if (!files.length) return
    setErr(null)
    if (!canEdit) {
      setErr(`${resolved} reads no pictures — pick an edit-capable model`)
      return
    }
    const room = SRC_MAX - refs.length
    if (room <= 0) {
      setErr(`an edit takes ${SRC_MAX} pictures — remove one first`)
      return
    }
    const take = files.filter(f => !srcs.some(s => !s.mask && s.name === f.name && s.size === f.size)).slice(0, room)
    if (!take.length) return
    const first = refs.length === 0
    try {
      const got = await Promise.all(take.map(f => readSource(f, false)))
      setSrcs([...srcs, ...got])
      if (first) {
        const size = refit(draft.size, framesFor(rm, kind, true, big, refs.length + take.length, cheap))
        if (size) patch({ size })
      }
      if (take.length < files.length) setErr(`${files.length - take.length} picture(s) skipped (${SRC_MAX} a request, or already attached)`)
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e))
    }
  }

  /** One mask slot: it belongs to the FIRST picture, so a second one replaces
   *  the first rather than joining the list the model never reads. */
  async function addMask(files: File[]) {
    if (!files.length) return
    setErr(null)
    if (!refs.length) {
      setErr('a mask belongs to a picture — attach that picture first')
      return
    }
    const f = files[0]
    if (!SRC_MASK_TYPES.includes(f.type)) {
      setErr(`${f.name}: a mask is a png or webp (its alpha is what the model reads)`)
      return
    }
    try {
      const m = await readSource(files[0], true)
      // the door radiance enforces at submit, asked of the browser first — a
      // mismatch would cost a slot start to 400
      const first = refs[0]
      if (m.w != null && first.w != null && (m.w !== first.w || m.h !== first.h)) {
        setErr(`${m.name}: a mask must be the first picture's size (${m.w}x${m.h} ≠ ${first.w}x${first.h}) — radiance compares them, it does not resample`)
        return
      }
      setSrcs([...refs, m])
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e))
    }
  }

  function drop(src: Source) {
    let next = srcs.filter(s => s.key !== src.key)
    // a mask qualifies the FIRST picture — removing that one (or the last,
    // which re-anchors the list) orphans it, so it goes with its picture
    if (!src.mask && (refs.length === 1 || refs[0].key === src.key)) {
      next = next.filter(s => !s.mask)
    }
    setSrcs(next)
    // the last picture off ⇒ back to a generation: let the frame grow back
    if (src.mask) return
    if (next.some(s => !s.mask)) return
    const size = refit(draft.size, framesFor(rm, kind, false, big))
    if (size) patch({ size })
  }

  async function submit() {
    if (!effective || !draft.prompt.trim()) return
    setErr(null)
    setFlash(null)
    try {
      const id = await onSubmit()
      say(id ? `queued ${shortId(id)}` : 'queued')
      ta.current?.focus()
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e))
    }
  }

  const needPic = purpose === 'edit' && refs.length === 0
  const canSubmit = !!effective && draft.prompt.trim().length > 0 && !running && !needPic

  return (
    <div className="card gen-comp">
      <div className="rail">
        <Field label="model" className="w-model">
          <select
            value={effective}
            onChange={e => {
              const next = e.target.value
              const r = resolveModel(o, next)
              const nk = r.kind
              const ni = r.resolved ? o.models[r.resolved] : undefined
              // the frame follows the LIST the new state gets (a purpose key and
              // a model name can be the same class: only a real change retunes)
              const size =
                nk !== kind || (ni?.edits === false && refs.length > 0)
                  ? refit(draft.size, framesFor(rm, nk, nk === 'image' && ni?.edits !== false && refs.length > 0, ni?.mlen ?? 0, Math.max(1, refs.length), ni?.mlen_min ?? 0))
                  : null
              patch({ model: next, enhance: null, ...(size ? { size } : {}) })
              // a model the pictures cannot go to (a video, or a text-only
              // image — an unprobed one still takes them): drop the mask WITH
              // its pictures, or it would re-attach to a later edit.
              if (refs.length && (nk === 'video' || (nk === 'image' && ni?.edits === false))) {
                setSrcs([])
                const why =
                  nk === 'video'
                    ? `${r.resolved} is a video model (an edit's pictures go to an image model)`
                    : `${r.resolved} reads no pictures`
                say(`dropped ${refs.length} picture${refs.length === 1 ? '' : 's'}${mask ? ' + the mask' : ''} — ${why}`)
              }
            }}
            disabled={running}
            title={note}
          >
            {o.purposes.image && (
              <option value="image" disabled={imageModels.length === 0}>
                image → {o.purposes.image}
              </option>
            )}
            {o.purposes.video && (
              <option value="video" disabled={videoModels.length === 0}>
                video → {o.purposes.video}
              </option>
            )}
            {o.purposes.edit && (
              <option value="edit" disabled={editModels.length === 0}>
                edit → {o.purposes.edit}
              </option>
            )}
            {imageModels.length > 0 && (
              <optgroup label="image models">
                {imageModels.map(n => (
                  <option key={n} value={n}>
                    {n}
                    {o.models[n]?.edits === true ? ' · edits' : o.models[n]?.edits === null ? ' · edits?' : ''}
                  </option>
                ))}
              </optgroup>
            )}
            {videoModels.length > 0 && (
              <optgroup label="video models">
                {videoModels.map(n => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        </Field>

        <Field label="size" className="w-size" title="WxH — the chips are the frames this model and mode accept">
          <input value={draft.size} onChange={e => patch({ size: e.target.value })} placeholder="1344x768" disabled={running} />
        </Field>

        {/* its own field, not satellites of the input: the label row stays one
            line and a custom size simply leaves every chip off */}
        <Field label="aspect" className="w-aspect">
          <span className="ratios">
            {frames.map(p => {
              const r = latentRows(rm, p.size) ?? 0
              const pneed = passRows(rm, r, editing ? nrefs : 0)
              const bad = noSeat(pneed)
              // past the CHEAP SEAT'S BOUND (pneed already pays for the
              // references): only the long-pass slot takes this frame — worth a
              // dot. (An unbounded long-pass seat: "far" = past every BOUNDED
              // seat.)
              const far = !bad && cheap > 0 && pneed > cheap
              return (
                <button
                  key={p.size}
                  type="button"
                  className={`mini${draft.size === p.size ? ' on' : ''}${bad ? ' over' : far ? ' far' : ''}`}
                  title={
                    big
                      ? `${p.size} · ${pneed} rows${bad ? ` — no slot gives ${resolved} more than ${big}` : far ? ` — only ${seatsFor(o, resolved, pneed).join('/')} takes this; it parks what is on those cards` : ` · fits ${seatsFor(o, resolved, pneed).join('/') || 'any slot'}`}`
                      : p.size
                  }
                  onClick={() => patch({ size: p.size })}
                >
                  {p.ratio}
                </button>
              )
            })}
          </span>
        </Field>

        {kind === 'video' && (
          <Field label="seconds" className="w-num">
            <input type="number" step="0.5" min="1" value={draft.seconds} onChange={e => patch({ seconds: e.target.value })} disabled={running} />
          </Field>
        )}

        <Field label="seed" className="w-seed" title="-1 = a random seed each run">
          <span className="lin">
            <input type="number" value={draft.seed} onChange={e => patch({ seed: e.target.value })} disabled={running} />
            <button
              type="button"
              className="mini"
              title="random seed"
              onClick={() => patch({ seed: String(Math.floor(Math.random() * 2 ** 31)) })}
            >
              ↻
            </button>
          </span>
        </Field>

        <span className="railtail">
          <Toggle
            label="enhance"
            // honest state: with no prompt configured the job renders as-is
            // (the create path skips the rewrite), so the toggle reads off
            checked={enhanceOn && configured}
            disabled={running || !configured}
            title={
              configured
                ? `rewrite the prompt on ${o.enhance_target.model} before rendering`
                : `off — no enhancement prompt configured for ${resolved || 'this model'}; the job would render as-is (set one in settings)`
            }
            onChange={v => patch({ enhance: v })}
          />
        </span>
      </div>

      {/* the edit's source pictures: attach by button or by drag, one chip
          each, a mask of its own (radiance reads ONE, for the first picture) */}
      {canEdit && (
        <div
          className={`srcs${drag ? ' drag' : ''}${refs.length ? ' on' : ''}`}
          onDragOver={e => {
            e.preventDefault()
            setDrag(true)
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={e => {
            e.preventDefault()
            setDrag(false)
            void addPics(Array.from(e.dataTransfer?.files ?? []))
          }}
        >
          {refs.map((s, i) => (
            <figure key={s.key} className="src" title={`${s.name} · ${kibi(s.size)}${s.from ? ` · remixed from ${s.from}` : ''}`}>
              <SrcPreview src={s} />
              <figcaption>{i === 0 ? 'source' : `ref ${i + 1}`}</figcaption>
              <button type="button" title="remove" disabled={running} onClick={() => drop(s)}>
                ✕
              </button>
            </figure>
          ))}
          {mask && (
            <figure className="src mask" title={`${mask.name} · ${kibi(mask.size)} — transparent pixels may change${mask.from ? ` · remixed from ${mask.from}` : ''}`}>
              <SrcPreview src={mask} />
              <figcaption>mask</figcaption>
              <button type="button" title="remove" disabled={running} onClick={() => drop(mask)}>
                ✕
              </button>
            </figure>
          )}
          <div className="srcpick">
            <span className="pickrow">
              <button type="button" className="pick" disabled={running || refs.length >= SRC_MAX} onClick={() => fileIn.current?.click()} title={`png, jpeg or webp · up to ${SRC_MAX} · ≤16 MiB each · or drop them here`}>
                + picture
              </button>
              <button
                type="button"
                className="pick"
                disabled={running || !refs.length}
                onClick={() => maskIn.current?.click()}
                title={refs.length ? 'a mask: transparent pixels are what the model may change — it must be the first picture\u2019s exact size' : 'attach the picture the mask belongs to first'}
              >
                + mask
              </button>
              <input
                ref={fileIn}
                type="file"
                accept={SRC_ACCEPT}
                multiple
                hidden
                onChange={e => {
                  void addPics(Array.from(e.target.files ?? []))
                  e.target.value = ''
                }}
              />
              <input
                ref={maskIn}
                type="file"
                accept={SRC_MASK_ACCEPT}
                hidden
                onChange={e => {
                  void addMask(Array.from(e.target.files ?? []))
                  e.target.value = ''
                }}
              />
            </span>
            <span className="hint">
              {refs.length
                ? `${refs.length} picture${refs.length === 1 ? '' : 's'}${mask ? ' + mask' : ''}${need != null && big ? (over ? ` · ${need} rows — over every slot` : only ? ` · ${need} rows → ${seats.join('/') || 'the long-pass slot'} only` : ` · ${need} rows → ${seats.join('/') || 'any slot'}`) : ''}`
                : info?.edits === null
                  ? 'drop a picture in to edit — radiance has not confirmed this model reads them yet (its slot has never been up)'
                  : 'drop a picture in to edit — this model reads what you attach (png / jpeg / webp)'}
            </span>
          </div>
        </div>
      )}

      <textarea
        ref={ta}
        rows={2}
        value={draft.prompt}
        placeholder={kind === 'video' ? 'a red fox trotting through a snowy pine forest, snow crunching underfoot' : editing ? 'put a red scarf on the fox' : 'a red fox in fresh snow, morning light'}
        onChange={e => patch({ prompt: e.target.value })}
        onKeyDown={e => {
          if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
            e.preventDefault()
            if (canSubmit) void submit()
          }
        }}
      />
      <div className="cfoot">
        {over ? (
          <span className="note warn" title={`${overLabel} (the frame is ${rows} rows, a reference picture ${rm.ref_rows}, the prompt allowance ${rm.prompt_rows}) — create refuses it instead of queueing a job that spends a slot start to fail`}>
            {overLabel}
          </span>
        ) : (
          note && (
            <span className="note" title={note}>
              {resolved} · {note}
            </span>
          )
        )}
        {flash && <span className="flash">{flash} ✓</span>}
        {err && (
          <span className="banner err gen-err" title={err}>
            {err}
          </span>
        )}
        <button
          className="go"
          disabled={!canSubmit}
          onClick={() => void submit()}
          title={needPic ? 'the edit purpose needs a source picture — attach one' : '⌘/ctrl + ⏎'}
        >
          {running ? 'queueing…' : `queue ${refs.length ? 'edit' : kind ?? 'job'}`}
        </button>
      </div>
    </div>
  )
}

// ── the gallery ─────────────────────────────────────────────────────────────

function Thumb({ job, big }: { job: GenJob | GenJobCard; big?: boolean }) {
  const a = job.artifact
  // The file can die under a row: a record still in the store is protected (the
  // age GC skips its files), but a trimmed record or a hand `rm` leaves the URL
  // dead. Show that, not a broken image.
  const [gone, setGone] = useState(false)
  // A <video> the browser can't play (an odd codec, a dead file) — the poster
  // stands in when there is one. Never reported as "removed": the file may be
  // fine on the host (the download link still works).
  const [noplay, setNoplay] = useState(false)
  if (!a || gone)
    return (
      <span className="noimg">
        {big ? (gone ? 'artifact removed' : IN_FLIGHT.has(job.state) ? 'rendering…' : 'nothing rendered') : ''}
      </span>
    )
  // Big view: the player itself — the poster is a bonus, not a prerequisite
  // (a video radiance never gave a first frame to is still watchable).
  if (big && job.kind === 'video') {
    if (noplay)
      return a.thumb ? (
        <img className="media" src={a.thumb} alt="" title="this browser cannot play the file — showing its first frame" />
      ) : (
        <span className="noimg" title="the file may still be on the host — use the download link">
          can't play here
        </span>
      )
    return (
      <video
        className="media"
        src={a.url}
        poster={a.thumb ?? undefined}
        controls
        playsInline
        onError={() => setNoplay(true)}
      />
    )
  }
  // A video's thumbnail is best-effort; without one there is no image to show
  // (never the MP4 as an <img> — it would decode-fail and read as "removed").
  const src = job.kind === 'video' ? a.thumb : a.url
  if (!src)
    return (
      <span className="noimg" title={`${a.size / 1048576 < 0.1 ? '<0.1' : (a.size / 1048576).toFixed(1)} MiB — no thumbnail`}>
        '▶'
      </span>
    )
  return <img className="media" src={src} alt="" loading="lazy" onError={() => setGone(true)} />
}

/** The card's corner chip: the picture an EDIT worked from. A dead URL (age
 *  GC, a deleted record) drops the chip. */
function SrcChip({ job }: { job: GenJob | GenJobCard }) {
  const src = job.edit?.images?.[0]
  const [gone, setGone] = useState(false)
  if (!src || gone) return null
  return (
    <span
      className="jsrc"
      title={`edited from ${job.edit?.images.length} picture${job.edit && job.edit.images.length > 1 ? 's' : ''}${job.edit?.mask ? ' + mask' : ''}`}
    >
      <img src={src.url} alt="" loading="lazy" onError={() => setGone(true)} />
    </span>
  )
}

function JobCard({
  job,
  selected,
  onSel,
  onOpen,
  onRemove,
}: {
  job: GenJob | GenJobCard
  selected: boolean
  onSel: (on: boolean) => void
  onOpen: () => void
  onRemove: () => void
}) {
  const st = STATE[job.state]
  const inf = IN_FLIGHT.has(job.state)
  const ago = relTime(Date.now(), (inf ? job.created_at : (job.finished_at ?? job.created_at)) * 1000)
  return (
    <div
      className={`jc${selected ? ' sel' : ''}${inf ? ' inf' : ''}${job.state === 'failed' ? ' fail' : ''}`}
      title={job.error ?? undefined}
    >
      <div className="jthumb" onClick={onOpen} title="open">
        <Thumb job={job} />
        <label className="jcheck" onClick={e => e.stopPropagation()}>
          <input type="checkbox" checked={selected} onChange={e => onSel(e.target.checked)} />
        </label>
        <span className="jkind">{modeOf(job)}</span>
        <SrcChip job={job} />
        {inf &&
          (job.progress != null ? (
            <span className="jprog">
              <Meter pct={job.progress / 100} tone="load" />
              <em>{job.progress}%</em>
            </span>
          ) : (
            <span className="jbar" />
          ))}
        {/* the card opens on click — the actions must not also open it */}
        <span className="jacts" onClick={e => e.stopPropagation()}>
          {job.artifact && (
            <a href={job.artifact.url} download title="download">
              ↓
            </a>
          )}
          <button type="button" title="delete" onClick={onRemove}>
            ✕
          </button>
        </span>
      </div>
      <div className="jbody" onClick={onOpen}>
        <div className="jline">
          <span className="jid">{shortId(job.id)}</span>
          <span className={st.cls}>{st.label}</span>
        </div>
        {/* the human prompt only — the rewrite (when there is one) is the
            modal's: it is machine text, not what was typed */}
        <p className="jprompt" title={job.prompt}>
          {job.prompt}
        </p>
        <div className="jfoot">
          <span className="muted">{job.model}</span>
          <span className="muted">{job.error && inf ? job.error : ago}</span>
        </div>
      </div>
    </div>
  )
}

function Row({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="mrow">
      <span className="mk">{k}</span>
      <span className="mv">{children}</span>
    </div>
  )
}

function PromptBlock({ label, text, hint }: { label: string; text: string; hint?: string }) {
  return (
    <div className="pblock">
      <div className="ph">
        <span>{label}</span>
        {hint && <span className="muted">{hint}</span>}
        <CopyBtn text={text} />
      </div>
      <p className="pt">{text}</p>
    </div>
  )
}

function JobModal({
  job,
  o,
  tick,
  busy,
  onClose,
  onRemove,
  onRemix,
}: {
  job: GenJob | GenJobCard
  o: GenOverview
  /** the overview poll's `last` (a bump per successful tick, a re-served
   *  304 included) — the full-record fetch's retry clock */
  tick: number
  busy: boolean
  onClose: () => void
  onRemove: (j: GenJob | GenJobCard) => void
  onRemix: (j: GenJob | GenJobCard) => void
}) {
  // The page's records are card projections (clipped prompt, no params) —
  // the modal is the one place a clip would be seen, so a TERMINAL job takes
  // its full record, one GET. In-flight records arrive full (the overview's
  // active set the merge prefers) — use them directly, progress updates and
  // all. When one goes terminal the fetch takes over — the record the modal
  // stands on is then a clipped card, or (an off-page modal) the
  // open-snapshot: FULL, but still the IN-FLIGHT one (a full snapshot was
  // taken while live — terminal is sticky, so it can't be the finished
  // record) — the fetch carries the terminal transition: due even when the
  // standing record has params (skipping it would freeze the modal on
  // "running"); the standing record covers the beat before it lands.
  const [full, setFull] = useState<GenJob | null>(null)
  // The job 404'd (deleted mid-open, or the store trim took it) — the card
  // stands for good, no point re-asking. A blip is NOT that: the next tick
  // retries, so one failed fetch never leaves the clipped card on display
  // for the modal's whole life.
  const [gone, setGone] = useState(false)
  // A full live record in hand: the overview still lists the job in flight
  // (the source of truth — the record the modal stands on can be a stale
  // snapshot, and a stale in-flight state would skip the fetch, reading
  // "running" forever) AND the record is full (the card projections carry
  // no params — a card means the fetch is due, for the beat before the
  // overview catches up).
  const liveFull = o.active.some(a => a.id === job.id) && 'params' in job
  // the in-flight fetch's hold: `tick` re-runs this effect every poll, and a
  // response slower than the interval must not pile up a request per tick —
  // the clock retries a SETTLED failure only (the flag clears on settle, the
  // next tick re-fires)
  const inflight = useRef(false)
  useEffect(() => {
    if (liveFull || gone || full || inflight.current) return
    inflight.current = true
    let on = true
    void api<GenJob>(`/gen/jobs/${job.id}`)
      .then(r => {
        if (on) setFull(r)
      })
      .catch(e => {
        if (!on) return
        // a 404 (NotFound) is the only kind that keeps — the record is gone
        // (deleted mid-open, or the store's trim took it); a blip retries on
        // the next tick
        if (e instanceof NotFound) setGone(true)
      })
      .finally(() => {
        inflight.current = false
      })
    return () => {
      on = false
    }
  }, [job.id, liveFull, gone, full, tick])
  const j: GenJob | GenJobCard = liveFull ? job : (full ?? job)
  const params = paramsOf(j)
  const enhanced = enhancedOf(j)
  const st = STATE[j.state]
  const instruction = o.enhance.prompts[j.model] ?? ''
  const queueS = j.started_at != null ? j.started_at - j.created_at : null
  const runS = j.started_at != null && j.finished_at != null ? j.finished_at - j.started_at : null
  // the before/after strip: the edit's stored pictures (the mask last — it
  // qualifies the FIRST picture, it is not one)
  const inputs: { key: string; url: string; label: string; size: number; mask?: boolean }[] = [
    ...(j.edit?.images ?? []).map((s: GenEditInput, i: number) => ({
      key: `in${i}`,
      url: s.url,
      label: i === 0 ? 'source' : `ref ${i + 1}`,
      size: s.size,
    })),
    ...(j.edit?.mask ? [{ key: 'mask', url: j.edit.mask.url, label: 'mask', size: j.edit.mask.size, mask: true }] : []),
  ]
  const [show, setShow] = useState<string | null>(null)
  const shown = inputs.find(s => s.key === show)

  return (
    <Modal
      title={
        <>
          {j.id} <span className={st.cls}>{st.label}</span>
        </>
      }
      onClose={onClose}
      width={1000}
      footer={
        <>
          <button className="danger solid" disabled={busy} onClick={() => onRemove(j)}>
            delete
          </button>
          {j.artifact && (
            <a href={j.artifact.url} download>
              download
            </a>
          )}
          <button onClick={() => onRemix(j)} title="load this prompt + settings into the composer">
            remix
          </button>
        </>
      }
    >
      <div className="jm">
        <div className="jm-l">
          <div className="jm-media">
            {shown ? (
              <>
                <img className="media" src={shown.url} alt="" />
                <button type="button" className="back" onClick={() => setShow(null)}>
                  ← result
                </button>
              </>
            ) : (
              <Thumb job={j} big />
            )}
            {j.state === 'running' && j.progress != null && (
              <span className="jprog under">
                <Meter pct={j.progress / 100} tone="load" />
                <em>{j.progress}%</em>
              </span>
            )}
          </div>
          {/* an edit's before/after: the pictures it worked from, stored beside
              its result — flip the big view between them */}
          {inputs.length > 0 && (
            <div className="b4">
              <button type="button" className={shown ? '' : 'on'} onClick={() => setShow(null)} title="what the model made">
                {j.artifact?.url ? <img src={j.artifact.url} alt="" /> : <span className="noimg">—</span>}
                <em>after</em>
              </button>
              {inputs.map(s => (
                <button
                  key={s.key}
                  type="button"
                  className={show === s.key ? 'on' : ''}
                  onClick={() => setShow(shown === s ? null : s.key)}
                  title={`${s.label} · ${kibi(s.size)}${s.mask ? ' — transparent pixels are what the model may change' : ''}`}
                >
                  <img src={s.url} alt="" />
                  <em>{s.label}</em>
                </button>
              ))}
              <span className="muted b4hint">click to compare</span>
            </div>
          )}
          <PromptBlock label="prompt" text={j.prompt} hint={enhanced ? 'what you typed' : undefined} />
          {enhanced && <PromptBlock label="enhanced" text={enhanced} hint="what rendered" />}
          {instruction && (
            <details className="pblock">
              <summary>
                enhance instruction <span className="muted">{j.model}</span>
              </summary>
              <p className="pt muted">{instruction}</p>
              <CopyBtn text={instruction} />
            </details>
          )}
        </div>

        <div className="jm-r">
          <Row k="model">
            {j.model} <span className="muted">{j.edit ? 'image · edit' : j.kind}</span>
          </Row>
          <Row k="slot">
            {j.slot ?? <span className="muted">—</span>}
          </Row>
          <Row k="params">
            {Object.keys(params).length
              ? Object.entries(params)
                  .map(([k, v]) => `${k} ${v}`)
                  .join(' · ')
              : '—'}
          </Row>
          <Row k="enhance">
            {j.enhance ? (
              <>
                on <span className="muted">{o.enhance_target.model}</span>
              </>
            ) : (
              'off'
            )}
          </Row>
          <Row k="queued">
            {clockOf(j.created_at * 1000)}
            {queueS != null && <span className="muted"> · waited {dur(queueS)}</span>}
          </Row>
          <Row k={j.kind === 'video' ? 'rendered' : 'ran'}>
            {j.started_at ? (
              <>
                {clockOf(j.started_at * 1000)}
                {runS != null ? <span className="muted"> · {dur(runS)}</span> : <span className="muted"> · running</span>}
              </>
            ) : (
              '—'
            )}
          </Row>
          {j.artifact && (
            <Row k="artifact">
              {(j.artifact.size / 1048576).toFixed(1)} MiB <span className="muted">{j.artifact.mime}</span>
            </Row>
          )}
          {j.user && <Row k="by">{j.user}</Row>}
          {j.error && (
            <div className="merr">
              {j.error}
            </div>
          )}
        </div>
      </div>
    </Modal>
  )
}

// ── settings ────────────────────────────────────────────────────────────────

interface Form {
  purposes: Record<string, string>
  enhanceEnabled: boolean
  prompts: Record<string, string>
  enhanceInstance: string
  enhanceModel: string
  gcDays: string
}

// One form, two tabs. The PUT is all-or-nothing (the server rebuilds the file
// from the body), so both tabs save the same draft and stale prompt entries —
// keys matching no deployed model — must be copied or deleted here, because
// the server rejects the body while any of them is in it.
function SettingsModal({ o, busy, onSaved, onClose }: { o: GenOverview; busy: boolean; onSaved: () => void; onClose: () => void }) {
  const [tab, setTab] = useState<'gen' | 'prompts'>('gen')
  const [form, setForm] = useState<Form>({
    purposes: { ...o.purposes },
    enhanceEnabled: o.enhance.enabled,
    prompts: { ...o.enhance.prompts },
    enhanceInstance: o.enhance.instance,
    enhanceModel: o.enhance.model,
    gcDays: String(o.gc_days),
  })
  const [err, setErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const imageModels = Object.entries(o.models)
    .filter(([, m]) => m.kind === 'image')
    .map(([n]) => n)
  const videoModels = Object.entries(o.models)
    .filter(([, m]) => m.kind === 'video')
    .map(([n]) => n)
  // the "edit" purpose may name an UNPROBED model (null) — the server's rule;
  // one radiance says reads no pictures is not offered (the save would 400)
  const editModels = imageModels.filter(n => o.models[n].edits !== false)
  const genModels = [...imageModels, ...videoModels]
  const stale = Object.keys(form.prompts).filter(n => !genModels.includes(n))
  const setCount = genModels.filter(n => (form.prompts[n] ?? '').trim() !== '').length
  const [sel, setSel] = useState(genModels[0] ?? stale[0] ?? '')
  // The editor follows the highlight: the clicked row is what you edit — a
  // live model or a stale entry (you may want to read its text before
  // dropping it). The fallback to the first model is only for a click that
  // matches nothing (the fleet changed under an open dialog).
  const selStale = stale.includes(sel)
  const editing = selStale || genModels.includes(sel) ? sel : (genModels[0] ?? '')
  const [copyFrom, setCopyFrom] = useState('')
  const sources = Object.keys(form.prompts).filter(n => n !== editing && (form.prompts[n] ?? '').trim() !== '')
  const target = o.instances.find((i: GenInstance) => i.name === form.enhanceInstance)

  async function save() {
    setErr(null)
    setSaving(true)
    const body = {
      purposes: Object.fromEntries(Object.entries(form.purposes).filter(([, v]) => v)),
      enhance: {
        enabled: form.enhanceEnabled,
        prompts: form.prompts,
        instance: form.enhanceInstance,
        model: form.enhanceModel,
      },
      gc_days: Number(form.gcDays),
    }
    try {
      await api('/gen/settings', { method: 'PUT', body: JSON.stringify(body) })
      onSaved()
      onClose()
      return true
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e))
      return false
    } finally {
      setSaving(false)
    }
  }

  const blocked = stale.length > 0

  return (
    <Modal
      title={
        <>
          gen settings{' '}
          <Segmented
            value={tab}
            onChange={setTab}
            options={[
              { value: 'gen', label: 'general' },
              { value: 'prompts', label: 'prompts', title: `${setCount}/${genModels.length} models set${stale.length ? ` · ${stale.length} stale` : ''}`, count: stale.length ? stale.length : undefined },
            ]}
          />
        </>
      }
      onClose={onClose}
      width={820}
      footer={
        <>
          {blocked && (
            <span className="muted blocknote" title="the server rejects the save while any prompt key matches no deployed model">
              {stale.length} stale prompt {stale.length === 1 ? 'entry' : 'entries'} — copy or delete {stale.length === 1 ? 'it' : 'them'} first
            </span>
          )}
          <button onClick={onClose}>close</button>
          <button disabled={busy || saving || blocked} onClick={() => void save()}>
            {saving ? 'saving…' : 'save'}
          </button>
        </>
      }
    >
      {tab === 'gen' ? (
        <div className="fset">
          <FRow label="image purpose">
            <select
              value={form.purposes.image ?? ''}
              onChange={e => setForm({ ...form, purposes: { ...form.purposes, image: e.target.value } })}
            >
              <option value="">— none —</option>
              {imageModels.map(n => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </FRow>
          <FRow label="video purpose">
            <select
              value={form.purposes.video ?? ''}
              onChange={e => setForm({ ...form, purposes: { ...form.purposes, video: e.target.value } })}
            >
              <option value="">— none —</option>
              {videoModels.map(n => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </FRow>
          <FRow
            label="edit purpose"
            hint="what a request naming the “edit” purpose gets — a model radiance reports as reading pictures (unprobed ones stay pickable)"
          >
            <select
              value={form.purposes.edit ?? ''}
              onChange={e => setForm({ ...form, purposes: { ...form.purposes, edit: e.target.value } })}
            >
              <option value="">— none —</option>
              {editModels.map(n => (
                <option key={n} value={n}>
                  {n}
                  {o.models[n].edits === null ? ' — unprobed' : ''}
                </option>
              ))}
            </select>
          </FRow>
          <FRow label="enhance by default">
            <Toggle label="on" checked={form.enhanceEnabled} onChange={v => setForm({ ...form, enhanceEnabled: v })} />
          </FRow>
          <FRow label="enhance on">
            <select
              value={form.enhanceInstance}
              onChange={e => {
                const inst = o.instances.find(i => i.name === e.target.value)
                setForm({ ...form, enhanceInstance: e.target.value, enhanceModel: inst?.llms[0] ?? '' })
              }}
            >
              {o.instances.map(i => (
                <option key={i.name} value={i.name}>
                  {i.display} ({i.name}){i.running ? '' : ' — down'}
                </option>
              ))}
            </select>
            <select value={form.enhanceModel} onChange={e => setForm({ ...form, enhanceModel: e.target.value })}>
              {(target?.llms ?? []).map(m => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </FRow>
          <FRow label="delete artifacts after">
            <div className="unit">
              <input
                type="number"
                min="1"
                max="365"
                value={form.gcDays}
                onChange={e => setForm({ ...form, gcDays: e.target.value })}
              />
              <em>days</em>
            </div>
          </FRow>
          {err && <div className="banner err">{err}</div>}
        </div>
      ) : (
        <div className="ped">
          <div className="ped-list">
            {genModels.map(n => (
              <button key={n} type="button" className={n === editing ? 'on' : ''} onClick={() => setSel(n)}>
                <i className={stale.includes(n) ? 'stale' : (form.prompts[n] ?? '').trim() ? 'set' : ''} />
                <span>{n}</span>
              </button>
            ))}
            {genModels.length === 0 && <span className="muted">no gen models deployed</span>}
            {stale.length > 0 && (
              <div className="stale">
                <div className="stale-h">
                  <span className="muted">stale</span>
                  <button className="mini danger" onClick={() => setForm({ ...form, prompts: Object.fromEntries(Object.entries(form.prompts).filter(([k]) => !stale.includes(k))) })}>
                    delete all
                  </button>
                </div>
                {stale.map(n => (
                  <div key={n} className="stale-r">
                    <button type="button" className={n === editing ? 'on' : ''} onClick={() => setSel(n)} title={(form.prompts[n] ?? '').trim()}>
                      <span>{n}</span>
                    </button>
                    <button
                      className="mini danger"
                      title="drop this entry"
                      onClick={() => {
                        const next = { ...form.prompts }
                        delete next[n]
                        setForm({ ...form, prompts: next })
                        if (sel === n) setSel(genModels[0] ?? '')
                      }}
                    >
                      ✕
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
          <div className="ped-edit">
            {editing && (
              <>
                <div className="ped-h">
                  <code>{editing}</code>
                  {selStale && <span className="muted">stale — no deployed model by this name</span>}
                  {/* copy INTO a stale key is pointless (the save stays blocked
                      while it exists) — its text is a source, never a target */}
                  {!selStale && sources.length > 0 && (
                    <span className="ped-copy">
                      <select value={copyFrom} onChange={e => setCopyFrom(e.target.value)}>
                        <option value="">copy from…</option>
                        {sources.map(n => (
                          <option key={n} value={n}>
                            {n}
                            {stale.includes(n) ? ' (stale)' : ''}
                          </option>
                        ))}
                      </select>
                      <button
                        disabled={saving || !copyFrom}
                        title={`copy ${copyFrom || '…'} into ${editing} (replaces it)`}
                        onClick={() => {
                          const text = form.prompts[copyFrom] ?? ''
                          if (!editing || !copyFrom || !text.trim()) return
                          setForm({ ...form, prompts: { ...form.prompts, [editing]: text } })
                          setCopyFrom('')
                        }}
                      >
                        copy
                      </button>
                    </span>
                  )}
                </div>
                <textarea
                  rows={14}
                  value={form.prompts[editing] ?? ''}
                  placeholder={selStale ? '(stale entry — delete it, or copy its text into a current model)' : '(empty = no enhancement for this model)'}
                  onChange={e => setForm({ ...form, prompts: { ...form.prompts, [editing]: e.target.value } })}
                />
              </>
            )}
          </div>
        </div>
      )}
      {tab === 'prompts' && err && <div className="banner err">{err}</div>}
    </Modal>
  )
}

// ── the view ────────────────────────────────────────────────────────────────

type Filter = 'all' | 'active' | 'image' | 'video' | 'edit' | 'failed'

const PAGE = 24 // the gallery's window: one screenful of cards per transfer

type BatchResult = { id: string; ok: boolean; action?: string; error?: string }

/** The page's context, as the server applies it (filter + q — same haystack,
 *  same 200-char needle) — the in-flight merge honors it too, so the tabs and
 *  search filter everything on screen: "failed" never floats a render in
 *  flight into the results (nothing in flight can be failed), and neither
 *  does a search. */
function pageMatches(j: GenJob, filter: Filter, q: string): boolean {
  if (filter === 'image' && j.kind !== 'image') return false
  if (filter === 'video' && j.kind !== 'video') return false
  // "edit" is a modifier on the image class (its artifact is a picture too)
  if (filter === 'edit' && !j.edit) return false
  if (filter === 'failed') return false
  const needle = q.trim().toLowerCase().slice(0, 200)
  if (needle) {
    const hay = `${j.id} ${j.model} ${j.prompt} ${j.enhanced_prompt || ''}`.toLowerCase()
    if (!hay.includes(needle)) return false
  }
  return true
}

export default function Gen({ intervalMs, onTab }: { intervalMs: number; onTab?: (t: Tab) => void }) {
  const poll = usePoll(() => api<GenOverview>('/gen'), intervalMs)
  // The history page: cursor per depth in a stack (numbers only) — → pushes
  // the server's `next`, ← drops back to a previous one, and the same page
  // index always asks for the same records.
  const [pageIdx, setPageIdx] = useState(0)
  const cursors = useRef<number[]>([0])
  // Armed by older(), disarmed when a page load (or a context change) lands —
  // a → click before the current page's fetch has resolved would push a
  // cursor read from the PREVIOUS page's `next`, and the new page would just
  // duplicate it.
  const navigating = useRef(false)
  const [filter, setFilter] = useState<Filter>('all')
  const [qIn, setQIn] = useState('')
  const [q, setQ] = useState('')
  const page = usePoll(
    () =>
      api<GenPage>(
        `/gen/jobs?limit=${PAGE}&cursor=${cursors.current[pageIdx] ?? 0}&filter=${filter}&q=${encodeURIComponent(q)}`,
      ),
    intervalMs,
  )
  const pageRefresh = page.refresh
  const pageBooted = useRef(false)
  // A filter/search change starts a fresh walk from the newest page — the
  // cursor stack belongs to the previous filter context and is stale the
  // moment either changes. Declared BEFORE the refetch effect below: that
  // fetch must see the reset stack (and `?? 0` covers the one render before
  // pageIdx itself settles — a truncated stack reads as the newest page).
  useEffect(() => {
    cursors.current = [0]
    setPageIdx(0)
  }, [filter, q])
  // usePoll's first fetch covers the initial params; refetch immediately when
  // the user navigates / refilters / searches instead of waiting for the tick
  useEffect(() => {
    if (!pageBooted.current) {
      pageBooted.current = true
      return
    }
    void pageRefresh()
  }, [pageIdx, filter, q, pageRefresh])
  // older()'s guard lifts when the page's fetch lands (a `last` bump) or the
  // context changes — a fetch that fails just leaves → off until one succeeds
  useEffect(() => {
    navigating.current = false
  }, [page.last, filter, q])
  // the search is server-side (the whole store is searched, not the page) —
  // debounce the keystrokes so a search doesn't become a poll per character
  useEffect(() => {
    const t = window.setTimeout(() => setQ(qIn.trim()), 300)
    return () => window.clearTimeout(t)
  }, [qIn])

  const [busy, setBusy] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft>(loadDraft)
  // an edit's attached pictures — deliberately OUT of the persisted draft
  // (base64 would eat the localStorage quota); they survive a submit (the same
  // picture twice is a perfectly valid thing to want)
  const [srcs, setSrcs] = useState<Source[]>([])
  const [sel, setSel] = useState<Set<string>>(() => new Set())
  // what the selection carries, remembered per id: the MiB total and the
  // artifact count must survive paging (a job off the current page still
  // counts); refreshed from the record while it is on screen
  const [selMeta, setSelMeta] = useState<Record<string, { b: number; a: boolean }>>({})
  const [open, setOpen] = useState<string | null>(null)
  // the opened job's record at open time — the modal must not vanish when a
  // page change scrolls its record off screen (it re-lives while in-flight and
  // matching the filter/search: the active set is merged into every page)
  const openSnap = useRef<GenJob | GenJobCard | null>(null)
  const [settings, setSettings] = useState(false)
  const [ask, setAsk] = useState<{ ids: string[] } | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const composer = useRef<HTMLDivElement>(null)

  useEffect(() => localStorage.setItem(DRAFT_KEY, JSON.stringify(draft)), [draft])

  async function act(key: string, fn: () => Promise<unknown>) {
    setBusy(key)
    try {
      await fn()
      poll.refresh()
      return true
    } catch (e) {
      setNote(String(e instanceof Error ? e.message : e))
      return false
    } finally {
      setBusy(null)
    }
  }

  const o = poll.data
  // This page's window: the fetched records, with the in-flight set merged on
  // top (always fresh from the small poll, always worth showing) — through the
  // same filter/search the page was asked with, so the tabs and search filter
  // everything on screen, not just the fetched records.
  const jobs = useMemo(() => {
    const m = new Map<string, GenJob | GenJobCard>()
    for (const j of page.data?.jobs ?? []) m.set(j.id, j)
    for (const j of o?.active ?? []) if (pageMatches(j, filter, q)) m.set(j.id, j)
    return [...m.values()].sort((a, b) => Number(isLive(b)) - Number(isLive(a)) || b.created_at - a.created_at)
  }, [page.data, o, filter, q])

  // keep the selection's byte/artifact total current for what is on screen
  useEffect(() => {
    setSelMeta(m => {
      let changed = false
      const n = { ...m }
      for (const id of Object.keys(n))
        if (!sel.has(id)) {
          delete n[id]
          changed = true
        }
      for (const j of jobs) {
        if (!sel.has(j.id)) continue
        const v = { b: j.artifact?.size ?? 0, a: !!j.artifact }
        if (!n[j.id] || n[j.id].b !== v.b || n[j.id].a !== v.a) {
          n[j.id] = v
          changed = true
        }
      }
      return changed ? n : m
    })
  }, [jobs, sel])
  const selBytes = Object.values(selMeta).reduce((n, v) => n + v.b, 0)
  const selArts = Object.values(selMeta).filter(v => v.a).length
  // the live record wins while it is on this page (progress updates); the
  // snapshot keeps an off-page modal alive
  const openJob: GenJob | GenJobCard | null = open
    ? (jobs.find(j => j.id === open) ?? (openSnap.current?.id === open ? openSnap.current : null))
    : null

  function toggle(id: string, on: boolean) {
    setSel(s => {
      const n = new Set(s)
      if (on) n.add(id)
      else n.delete(id)
      return n
    })
  }

  function openJobModal(j: GenJob | GenJobCard) {
    openSnap.current = j
    setOpen(j.id)
  }

  function older() {
    if (navigating.current) return
    const nxt = page.data?.next
    if (nxt == null) return
    navigating.current = true
    if (cursors.current.length <= pageIdx + 1) cursors.current.push(nxt)
    setPageIdx(pageIdx + 1)
  }

  function loadRemix(j: GenJob) {
    // what the composer wants out of a full record: the prompt the modal
    // shows (the rewrite, when there is one — that is what rendered) + the
    // params, as the draft's string fields
    const p = j.params as { size?: string; seed?: number; seconds?: number }
    setDraft({
      model: j.model,
      prompt: j.enhanced_prompt || j.prompt,
      size: p.size ?? BLANK.size,
      seed: p.seed != null ? String(p.seed) : BLANK.seed,
      seconds: p.seconds != null ? String(p.seconds) : BLANK.seconds,
      enhance: j.enhance,
    })
    // an edit remixes its PICTURES too — stored under /gen/*, so pull them
    // back into the composer. A file that is gone is said, not silently
    // dropped: an edit without its picture is a different job.
    const refs = (j.edit?.images ?? []).map(s => fetchSource(s.url, false, s.mime))
    const mask = j.edit?.mask ? [fetchSource(j.edit.mask.url, true, j.edit.mask.mime)] : []
    if (refs.length + mask.length) {
      void Promise.all([...refs, ...mask])
        .then(setSrcs)
        .catch(e => {
          setSrcs([])
          setNote(`remix: its source picture is gone (${e instanceof Error ? e.message : String(e)}) — attach it again to repeat the edit`)
        })
    } else {
      setSrcs([])
    }
    setOpen(null)
    composer.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  function remix(j: GenJob | GenJobCard) {
    // A full record in hand — the live one off the poll, or the modal's
    // fetched one (terminal states are sticky, so it cannot have moved) —
    // remixes directly: no second GET for what the modal is already showing.
    // A card is clipped (prompt) and paramless: fetch the full record first.
    // A failed fetch NEVER falls back to the card — that would load the
    // 200-char clip into the composer as if it were the prompt — refuse with
    // a note instead (the modal closes; the card stands for a retry when the
    // job is still there).
    if ('params' in j) {
      loadRemix(j)
      return
    }
    void api<GenJob>(`/gen/jobs/${j.id}`)
      .then(loadRemix)
      .catch(e => {
        setOpen(null)
        setNote(`remix: ${e instanceof Error ? e.message : String(e)}`)
      })
  }

  async function remove(ids: string[]) {
    setAsk(null)
    // a plain `let res` would be narrowed to `never` here — TS doesn't track
    // the closure's assignment — so the result rides in a holder object
    const res: { r: { results: BatchResult[] } | null } = { r: null }
    // act() swallows a request-level failure into the error note — bail
    // before touching the selection, or a batch that never reached the server
    // reads as a successful one. Per-id failures are NOT thrown here: the
    // server's contract is that one bad id never aborts the rest, so the
    // selection and the note follow the per-id answers below
    const ok = await act(`rm:${ids.join(',')}`, async () => {
      res.r = await api<{ results: BatchResult[] }>('/gen/jobs/remove', {
        method: 'POST',
        body: JSON.stringify({ ids }),
      })
    })
    if (!ok || !res.r) return
    // Drop only the ids the server actually acted on — a failed one stays
    // selectable, so the retry names it instead of silently re-404ing it
    const done = new Set(res.r.results.filter(x => x.ok).map(x => x.id))
    if (done.size) {
      setSel(s => {
        const n = new Set(s)
        for (const id of done) n.delete(id)
        return n
      })
      if (open && done.has(open)) setOpen(null)
    }
    // the note says what actually happened, per the response (not a guess
    // from the current page — the batch may span pages)
    const c = res.r.results.filter(x => x.ok && x.action === 'canceled').length
    const d = res.r.results.filter(x => x.ok && x.action === 'deleted').length
    const rest = res.r.results.filter(x => x.ok && x.action === 'done').length
    const bad = res.r.results.length - c - d - rest
    const parts = [c && `${c} canceled`, d && `${d} deleted`, rest && `${rest} already done`, bad && `${bad} failed`].filter(Boolean)
    setNote(parts.join(' · ') || null)
    void pageRefresh()
  }

  if (poll.error) return <div className="banner err">gen: {poll.error}</div>
  if (page.error) return <div className="banner err">gen: {page.error}</div>
  if (!o) return <div className="banner">loading…</div>

  const liveIds = new Set(o.active.map(j => j.id))

  return (
    <div className="gen">
      <GenBar
        o={o}
        onSettings={() => setSettings(true)}
        onTab={onTab}
      />

      <div ref={composer}>
        <Composer
          o={o}
          draft={draft}
          setDraft={setDraft}
          srcs={srcs}
          setSrcs={setSrcs}
          running={busy !== null}
          onSubmit={async () => {
            setBusy('new')
            try {
              const r = await api<{ id: string }>('/gen/jobs', { method: 'POST', body: JSON.stringify(draftPayload(draft, o, srcs)) })
              poll.refresh()
              return r.id
            } finally {
              setBusy(null)
            }
          }}
        />
      </div>

      {note && (
        <div className="banner note-bar" onClick={() => setNote(null)}>
          {note} <span className="muted">✕</span>
        </div>
      )}

      <div className="card jobs">
        <div className="jobs-bar">
          <Segmented
            value={filter}
            onChange={setFilter}
            options={[
              { value: 'all', label: 'all', count: o.counts.all },
              { value: 'active', label: 'active', count: o.counts.active },
              { value: 'image', label: 'image', count: o.counts.image },
              { value: 'edit', label: 'edit', count: o.counts.edit },
              { value: 'video', label: 'video', count: o.counts.video },
              { value: 'failed', label: 'failed', count: o.counts.failed },
            ]}
          />
          <input
            className="search"
            placeholder="search"
            value={qIn}
            onChange={e => setQIn(e.target.value)}
            title="id, model, prompt — searches the whole history, not just this page"
          />
          <span className="sp" />
          <span className="pgnp">
            <button
              className="mini"
              disabled={pageIdx === 0}
              onClick={() => setPageIdx(i => Math.max(0, i - 1))}
              title="newer"
            >
              ←
            </button>
            <button className="mini" disabled={page.data?.next == null} onClick={older} title="older">
              →
            </button>
          </span>
          {sel.size > 0 ? (
            <span className="selbar">
              <b>{sel.size}</b> selected
              {selArts > 0 && <span className="muted">· {(selBytes / 1048576).toFixed(1)} MiB</span>}
              <button
                disabled={busy !== null || selArts === 0}
                onClick={() =>
                  void act('archive', async () => {
                    await download('/gen/jobs/archive', { ids: [...sel] }, 'panel-gen.zip')
                  })
                }
                title={selArts ? `the ${selArts} artifact${selArts === 1 ? '' : 's'} as one zip (plus a prompts.txt)` : 'none of these has an artifact'}
              >
                {busy === 'archive' ? 'zipping…' : 'download'}
              </button>
              <button className="danger" disabled={busy !== null} onClick={() => setAsk({ ids: [...sel] })}>
                delete
              </button>
              <button className="mini" title="clear selection" onClick={() => setSel(new Set())}>
                ✕
              </button>
            </span>
          ) : (
            <button
              className="mini selall"
              disabled={jobs.length === 0}
              title="every job on this page (the batch endpoints take any ids)"
              onClick={() => setSel(new Set(jobs.map(j => j.id)))}
            >
              select all
            </button>
          )}
        </div>

        <div className="jgrid">
          {jobs.map(j => (
            <JobCard
              key={j.id}
              job={j}
              selected={sel.has(j.id)}
              onSel={on => toggle(j.id, on)}
              onOpen={() => openJobModal(j)}
              onRemove={() => setAsk({ ids: [j.id] })}
            />
          ))}
          {jobs.length === 0 && (
            <div className="empty">{q || filter !== 'all' ? 'nothing matches' : 'queue something above'}</div>
          )}
        </div>
      </div>

      {openJob && (
        <JobModal
          key={openJob.id} // remount per job: the editor's state is that job's
          job={openJob}
          o={o}
          tick={poll.last ?? 0} // the full-record fetch's retry clock
          busy={busy !== null}
          onClose={() => setOpen(null)}
          onRemove={j => setAsk({ ids: [j.id] })}
          onRemix={remix}
        />
      )}

      {ask && (
        <ConfirmModal
          title={
            ask.ids.some(id => liveIds.has(id))
              ? 'cancel / delete these jobs?'
              : `delete ${ask.ids.length} job${ask.ids.length === 1 ? '' : 's'}?`
          }
          body={
            <div className="asklist">
              {ask.ids.map(id => {
                const j = jobs.find(x => x.id === id) ?? (openSnap.current?.id === id ? openSnap.current : null)
                return (
                  <div key={id}>
                    <code>{shortId(id)}</code>{' '}
                    <span className="muted">{j ? `${j.model} · ${j.prompt.slice(0, 60)}` : id}</span>
                  </div>
                )
              })}
              <p className="muted">
                in-flight jobs are canceled and stay in the history; finished ones lose their record and their files.
              </p>
            </div>
          }
          confirm={ask.ids.length === 1 ? 'delete' : `delete ${ask.ids.length}`}
          danger
          busy={busy !== null}
          onClose={() => setAsk(null)}
          onConfirm={() => void remove(ask.ids)}
        />
      )}

      {settings && <SettingsModal o={o} busy={busy !== null} onSaved={poll.refresh} onClose={() => setSettings(false)} />}
    </div>
  )
}

/** The composer draft + its attached pictures as the create endpoint wants
 *  them (the draft is UI state: strings, the settings-default sentinel, and
 *  fields the model may not take). Pictures in the body make it an EDIT. */
function draftPayload(draft: Draft, o: GenOverview, srcs: Source[]) {
  const { effective, kind } = resolveModel(o, draft.model)
  const params: Record<string, unknown> = {}
  if (draft.size.trim()) params.size = draft.size.trim()
  const seed = Number(draft.seed)
  if (draft.seed.trim() !== '' && Number.isFinite(seed)) params.seed = Math.trunc(seed)
  const seconds = Number(draft.seconds)
  // an emptied field omits seconds (radiance's default length applies) —
  // the same rule as seed; sending Number('') would 400 on 0
  if (kind === 'video' && draft.seconds.trim() !== '' && Number.isFinite(seconds)) params.seconds = seconds
  const enhance = draft.enhance ?? o.enhance.enabled
  const refs = srcs.filter(s => !s.mask)
  const mask = srcs.find(s => s.mask)
  const body: Record<string, unknown> = { model: effective, prompt: draft.prompt.trim(), params }
  if (refs.length) body.images = refs.map(s => s.dataUrl)
  // a mask belongs to the first picture; without one the server refuses it
  if (refs.length && mask) body.mask = mask.dataUrl
  if (draft.enhance !== null) body.enhance = enhance
  return body
}

/** Purpose key → the class it names. "edit" is an IMAGE model read through the
 *  picture path — the same artifact class. */
const PURPOSE_KIND: Record<string, 'image' | 'video'> = { image: 'image', video: 'video', edit: 'image' }

/** Purpose key or model name → what it resolves to. An empty selection falls
 *  back to the configured purposes (so the composer is useful before anyone
 *  touches the model field). */
function resolveModel(o: GenOverview, model: string) {
  const imageModels = Object.entries(o.models)
    .filter(([, m]) => m.kind === 'image')
    .map(([n]) => n)
  const videoModels = Object.entries(o.models)
    .filter(([, m]) => m.kind === 'video')
    .map(([n]) => n)
  // what the "edit" purpose could name — the server's rule: UNPROBED (null)
  // is not "refuses pictures"
  const editModels = imageModels.filter(n => o.models[n].edits !== false)
  // image → video → edit: an empty composer falls back to the plain-generation
  // models first (its default is never the model the operator pointed "edit"
  // at)
  const effective =
    model || o.purposes.image || o.purposes.video || o.purposes.edit || imageModels[0] || videoModels[0] || ''
  const purpose = effective in o.purposes ? effective : null
  const kind = purpose ? PURPOSE_KIND[purpose] ?? null : o.models[effective]?.kind ?? null
  const resolved = purpose ? o.purposes[purpose] : effective
  return { imageModels, videoModels, editModels, effective, kind, resolved, purpose }
}
