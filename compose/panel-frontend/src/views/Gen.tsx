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
// The in-flight set is merged into whatever page is showing, so a long video
// keeps its progress bar on every page, and every mutation just re-polls.
//
// The `model` field accepts a purpose key (image/video — mapped in the
// settings) or an exact model name; the server resolves it.
import { useEffect, useMemo, useRef, useState } from 'react'
import type { Tab } from '../App'
import { api, download } from '../api'
import { usePoll } from '../usePoll'
import { ConfirmModal, CopyBtn, Field, FRow, Meter, Modal, Segmented, Toggle } from '../ui'
import { clockOf, dur, relTime } from '../fmt'
import type { GenInstance, GenJob, GenOverview, GenPage, GenSlot, GenState } from '../types'

const IN_FLIGHT: ReadonlySet<GenState> = new Set(['queued', 'starting', 'enhancing', 'loading', 'running'])
const isLive = (j: GenJob) => IN_FLIGHT.has(j.state)

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

// What a deployed model can actually do, read off radiance's implementation
// (ref/radiance). A tooltip, not a second model registry: no discovery, no
// enforcement — radiance's submit-time 400 stays the authority, and unknown
// models get no note.
const MODEL_NOTES: Record<string, string> = {
  'Minimax-H3-T2V':
    '5.17–14.38 s (24 fps, request rounds up to a ≈0.71 s chunk) · with sound · sides ×32, ≥256 px, ≤1344×768 area, aspect ≤4:1 · prompt ≤8192 tokens',
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

/** A frame that fits one model is often illegal on the other (1536×1152 is a
 *  fine Krea2 4:3 and over H3's 1344×768 area), so switching kind retunes the
 *  size: keep it if it is a frame of the new class, else take the new class's
 *  preset for the same aspect, else its 16:9. Null = leave it alone. */
function retune(kind: 'image' | 'video' | null, size: string): string | null {
  if (!kind) return null
  const list = SIZES[kind]
  if (list.some(p => p.size === size.trim())) return null
  const a = aspect(size)
  const same =
    a == null
      ? undefined
      : list.find(p => {
          const [w, h] = p.size.split('x').map(Number)
          return Math.abs(w / h - a) < 1e-6
        })
  return (same ?? list.find(p => p.ratio === '16:9') ?? list[0]).size
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
  running,
  onSubmit,
}: {
  o: GenOverview
  draft: Draft
  setDraft: (d: Draft) => void
  running: boolean
  onSubmit: () => Promise<string | null>
}) {
  const [err, setErr] = useState<string | null>(null)
  const [flash, setFlash] = useState<string | null>(null)
  const ta = useRef<HTMLTextAreaElement>(null)

  const { imageModels, videoModels, effective, kind, resolved } = resolveModel(o, draft.model)
  const note = resolved ? MODEL_NOTES[resolved] : undefined
  const configured = !!(resolved && o.enhance.prompts[resolved])
  const enhanceOn = draft.enhance ?? o.enhance.enabled

  const patch = (p: Partial<Draft>) => setDraft({ ...draft, ...p })

  async function submit() {
    if (!effective || !draft.prompt.trim()) return
    setErr(null)
    setFlash(null)
    try {
      const id = await onSubmit()
      setFlash(id ? `queued ${shortId(id)}` : 'queued')
      window.setTimeout(() => setFlash(null), 4000)
      ta.current?.focus()
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e))
    }
  }

  const canSubmit = !!effective && draft.prompt.trim().length > 0 && !running

  return (
    <div className="card gen-comp">
      <div className="rail">
        <Field label="model" className="w-model">
          <select
            value={effective}
            onChange={e => {
              const next = e.target.value
              // a purpose key and a model name can be the same class (image →
              // Krea2): only a real class change retunes the frame
              const nk = resolveModel(o, next).kind
              const size = nk && nk !== kind ? retune(nk, draft.size) : null
              patch({ model: next, enhance: null, ...(size ? { size } : {}) })
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
            {imageModels.length > 0 && (
              <optgroup label="image models">
                {imageModels.map(n => (
                  <option key={n} value={n}>
                    {n}
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

        <Field label="size" className="w-size" title="WxH — the chips are the frames each model accepts">
          <input value={draft.size} onChange={e => patch({ size: e.target.value })} placeholder="1344x768" disabled={running} />
        </Field>

        {/* its own field, not satellites of the input: the label row stays one
            line and a custom size simply leaves every chip off */}
        <Field label="aspect" className="w-aspect">
          <span className="ratios">
            {(SIZES[kind ?? 'image'] ?? SIZES.image).map(p => (
              <button
                key={p.size}
                type="button"
                className={`mini${draft.size === p.size ? ' on' : ''}`}
                title={p.size}
                onClick={() => patch({ size: p.size })}
              >
                {p.ratio}
              </button>
            ))}
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

      <textarea
        ref={ta}
        rows={2}
        value={draft.prompt}
        placeholder={kind === 'video' ? 'a red fox trotting through a snowy pine forest, snow crunching underfoot' : 'a red fox in fresh snow, morning light'}
        onChange={e => patch({ prompt: e.target.value })}
        onKeyDown={e => {
          if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
            e.preventDefault()
            if (canSubmit) void submit()
          }
        }}
      />
      <div className="cfoot">
        {note && (
          <span className="note" title={note}>
            {resolved} · {note}
          </span>
        )}
        {flash && <span className="flash">{flash} ✓</span>}
        {err && (
          <span className="banner err gen-err" title={err}>
            {err}
          </span>
        )}
        <button className="go" disabled={!canSubmit} onClick={() => void submit()} title="⌘/ctrl + ⏎">
          {running ? 'queueing…' : `queue ${kind ?? 'job'}`}
        </button>
      </div>
    </div>
  )
}

// ── the gallery ─────────────────────────────────────────────────────────────

function Thumb({ job, big }: { job: GenJob; big?: boolean }) {
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

function JobCard({
  job,
  selected,
  onSel,
  onOpen,
  onRemove,
}: {
  job: GenJob
  selected: boolean
  onSel: (on: boolean) => void
  onOpen: () => void
  onRemove: () => void
}) {
  const st = STATE[job.state]
  const inf = IN_FLIGHT.has(job.state)
  const shown = job.enhanced_prompt || job.prompt
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
        <span className="jkind">{job.kind}</span>
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
          <button type="button" title={inf ? 'cancel' : 'delete'} onClick={onRemove}>
            ✕
          </button>
        </span>
      </div>
      <div className="jbody" onClick={onOpen}>
        <div className="jline">
          <span className="jid">{shortId(job.id)}</span>
          <span className={st.cls}>{st.label}</span>
        </div>
        <p className="jprompt" title={shown}>
          {shown}
          {job.enhanced_prompt && <em title="enhanced prompt — the rewrite is what rendered"> ✎</em>}
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
  busy,
  onClose,
  onRemove,
  onRemix,
}: {
  job: GenJob
  o: GenOverview
  busy: boolean
  onClose: () => void
  onRemove: (j: GenJob) => void
  onRemix: (j: GenJob) => void
}) {
  const st = STATE[job.state]
  const instruction = o.enhance.prompts[job.model] ?? ''
  const queueS = job.started_at != null ? job.started_at - job.created_at : null
  const runS = job.started_at != null && job.finished_at != null ? job.finished_at - job.started_at : null

  return (
    <Modal
      title={
        <>
          {job.id} <span className={st.cls}>{st.label}</span>
        </>
      }
      onClose={onClose}
      width={1000}
      footer={
        <>
          <button className="danger" disabled={busy} onClick={() => onRemove(job)}>
            {IN_FLIGHT.has(job.state) ? 'cancel' : 'delete'}
          </button>
          {job.artifact && (
            <a href={job.artifact.url} download>
              download
            </a>
          )}
          <button onClick={() => onRemix(job)} title="load this prompt + settings into the composer">
            remix
          </button>
        </>
      }
    >
      <div className="jm">
        <div className="jm-l">
          <div className="jm-media">
            <Thumb job={job} big />
            {job.state === 'running' && job.progress != null && (
              <span className="jprog under">
                <Meter pct={job.progress / 100} tone="load" />
                <em>{job.progress}%</em>
              </span>
            )}
          </div>
          <PromptBlock label="prompt" text={job.prompt} hint={job.enhanced_prompt ? 'what you typed' : undefined} />
          {job.enhanced_prompt && <PromptBlock label="enhanced" text={job.enhanced_prompt} hint="what rendered" />}
          {instruction && (
            <details className="pblock">
              <summary>
                enhance instruction <span className="muted">{job.model}</span>
              </summary>
              <p className="pt muted">{instruction}</p>
              <CopyBtn text={instruction} />
            </details>
          )}
        </div>

        <div className="jm-r">
          <Row k="model">
            {job.model} <span className="muted">{job.kind}</span>
          </Row>
          <Row k="slot">
            {job.slot ?? <span className="muted">—</span>}
          </Row>
          <Row k="params">
            {Object.keys(job.params).length
              ? Object.entries(job.params)
                  .map(([k, v]) => `${k} ${v}`)
                  .join(' · ')
              : '—'}
          </Row>
          <Row k="enhance">
            {job.enhance ? (
              <>
                on <span className="muted">{o.enhance_target.model}</span>
              </>
            ) : (
              'off'
            )}
          </Row>
          <Row k="queued">
            {clockOf(job.created_at * 1000)}
            {queueS != null && <span className="muted"> · waited {dur(queueS)}</span>}
          </Row>
          <Row k={job.kind === 'video' ? 'rendered' : 'ran'}>
            {job.started_at ? (
              <>
                {clockOf(job.started_at * 1000)}
                {runS != null ? <span className="muted"> · {dur(runS)}</span> : <span className="muted"> · running</span>}
              </>
            ) : (
              '—'
            )}
          </Row>
          {job.artifact && (
            <Row k="artifact">
              {(job.artifact.size / 1048576).toFixed(1)} MiB <span className="muted">{job.artifact.mime}</span>
            </Row>
          )}
          {job.user && <Row k="by">{job.user}</Row>}
          {job.error && (
            <div className="merr">
              {job.error}
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

type Filter = 'all' | 'active' | 'image' | 'video' | 'failed'

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
  const [sel, setSel] = useState<Set<string>>(() => new Set())
  // what the selection carries, remembered per id: the MiB total and the
  // artifact count must survive paging (a job off the current page still
  // counts); refreshed from the record while it is on screen
  const [selMeta, setSelMeta] = useState<Record<string, { b: number; a: boolean }>>({})
  const [open, setOpen] = useState<string | null>(null)
  // the opened job's record at open time — the modal must not vanish when a
  // page change scrolls its record off screen (it re-lives while in-flight and
  // matching the filter/search: the active set is merged into every page)
  const openSnap = useRef<GenJob | null>(null)
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
    const m = new Map<string, GenJob>()
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
  const openJob: GenJob | null = open
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

  function openJobModal(j: GenJob) {
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

  function remix(j: GenJob) {
    const p = j.params as { size?: string; seed?: number; seconds?: number }
    setDraft({
      model: j.model,
      prompt: j.enhanced_prompt || j.prompt,
      size: p.size ?? BLANK.size,
      seed: p.seed != null ? String(p.seed) : BLANK.seed,
      seconds: p.seconds != null ? String(p.seconds) : BLANK.seconds,
      enhance: j.enhance,
    })
    setOpen(null)
    composer.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
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
          running={busy !== null}
          onSubmit={async () => {
            setBusy('new')
            try {
              const r = await api<{ id: string }>('/gen/jobs', { method: 'POST', body: JSON.stringify(draftPayload(draft, o)) })
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
                    <span className="muted">{j ? `${j.model} · ${(j.enhanced_prompt || j.prompt).slice(0, 60)}` : id}</span>
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

/** The composer draft as the create endpoint wants it (the draft is UI state:
 *  strings, the settings-default sentinel, and fields the model may not take). */
function draftPayload(draft: Draft, o: GenOverview) {
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
  const body: Record<string, unknown> = { model: effective, prompt: draft.prompt.trim(), params }
  if (draft.enhance !== null) body.enhance = enhance
  return body
}

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
  const effective = model || o.purposes.image || o.purposes.video || imageModels[0] || videoModels[0] || ''
  const kind = effective in o.purposes ? (effective as 'image' | 'video') : o.models[effective]?.kind ?? null
  const resolved = effective in o.purposes ? o.purposes[effective] : effective
  return { imageModels, videoModels, effective, kind, resolved }
}
