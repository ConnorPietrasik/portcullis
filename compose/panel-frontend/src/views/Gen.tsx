// Gen: the panel's own image/video queue (panel-api's gen section).
// Composer → one FIFO → the gen slots (the lane cards, assigned in the GPU
// view). The server is the arbiter — this view only renders and asks; the
// whole tab (slots, queue, jobs, settings) comes from one /api/gen poll.
//
// The `model` field accepts a purpose key (image/video — mapped in the
// settings below) or an exact model name; the server resolves it.
import { useEffect, useState } from 'react'
import { api } from '../api'
import { usePoll } from '../usePoll'
import { Meter } from '../ui'
import { relTime } from '../fmt'
import type { GenJob, GenOverview, GenSlot, GenState } from '../types'

const IN_FLIGHT: ReadonlySet<GenState> = new Set(['queued', 'starting', 'enhancing', 'loading', 'running'])

function stateBadge(state: GenState): string {
  if (state === 'running' || state === 'completed') return 'badge ok'
  if (state === 'failed') return 'badge err'
  if (state === 'canceled') return 'badge'
  return 'badge warn' // queued / starting / enhancing / loading
}

function Cards({ xs }: { xs: number[] | null }) {
  if (!xs || xs.length === 0) return <span className="muted">—</span>
  return (
    <span className="cardset">
      {xs.map((c) => (
        <span key={c} className="cardchip">
          {c}
        </span>
      ))}
    </span>
  )
}

// ── composer ──────────────────────────────────────────────────────────────

// Display-only per-model notes — what a deployed model can actually do, read
// off radiance's implementation (ref/radiance, arch/minimaxh3 for H3):
// chunk-quantized 24 fps timeline (7–20 chunks → 5.17–14.38 s, a request
// rounds UP to a chunk), always with sound, 32-px grid with a 256-px floor,
// the 1344×768 area cap, 4:1 max aspect, 8192-token prompt. The upstream
// model's "few frames" mode is not exposed there — radiance's H3 floor is
// 5.17 s. An annotation, not a second model registry: no discovery, no
// enforcement — unknown models get no note, and radiance's submit-time 400
// stays the authority (it names the model's own limits).
const MODEL_NOTES: Record<string, string> = {
  'minimax-h3-i8':
    'video 5.17–14.38 s (24 fps, 7–20 chunks; request rounds up to a chunk ≈ 0.71 s) · with sound · sides ×32, ≥ 256 px, ≤ 1344×768 area, aspect ≤ 4:1 · prompt ≤ 8192 tokens',
}

function Composer({
  o,
  busy,
  onCreated,
}: {
  o: GenOverview
  busy: boolean
  onCreated: () => void
}) {
  const imageModels = Object.entries(o.models).filter(([, m]) => m.kind === 'image').map(([n]) => n)
  const videoModels = Object.entries(o.models).filter(([, m]) => m.kind === 'video').map(([n]) => n)
  const [model, setModel] = useState<string>('')
  // empty string → keep the purpose shortcut visible even when unconfigured
  const effective = model || o.purposes.image || o.purposes.video || imageModels[0] || videoModels[0] || ''
  const kind = effective in o.purposes ? (effective as 'image' | 'video') : o.models[effective]?.kind ?? null
  // the actual model behind the selection — a purpose key resolves to its mapped model; the
  // note and the enhancement prompts are keyed by model name, never by purpose
  const resolved = effective in o.purposes ? o.purposes[effective] : effective
  const note = resolved ? MODEL_NOTES[resolved] : undefined

  const [prompt, setPrompt] = useState('')
  const [enhance, setEnhance] = useState<boolean | null>(null) // null → settings default
  const [seconds, setSeconds] = useState('5')
  const [size, setSize] = useState('1344x768')
  const [seed, setSeed] = useState('-1')
  const [err, setErr] = useState<string | null>(null)

  const et = o.enhance_target
  const enhanceOn = enhance ?? o.enhance.enabled
  const enhanceConfigured = !!(resolved && o.enhance.prompts[resolved])

  async function submit() {
    if (!effective || !prompt.trim()) return
    setErr(null)
    const params: Record<string, unknown> = {}
    if (size.trim()) params.size = size.trim()
    const s = Number(seed)
    if (seed.trim() !== '' && Number.isFinite(s)) params.seed = Math.trunc(s)
    if (kind === 'video') {
      const sec = Number(seconds)
      if (Number.isFinite(sec)) params.seconds = sec
    }
    const body: Record<string, unknown> = { model: effective, prompt: prompt.trim(), params }
    if (enhance !== null) body.enhance = enhanceOn
    try {
      await api('/gen/jobs', { method: 'POST', body: JSON.stringify(body) })
      setPrompt('')
      onCreated()
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e))
    }
  }

  const canSubmit = !!effective && prompt.trim().length > 0 && !busy

  return (
    <div className="card gen-composer">
      <h3>
        new job <span className="muted">— one queue; jobs spread across the slots that host their model</span>
      </h3>
      <div className="gen-compose-grid">
        <label className="gen-field">
          <span>
            model <span className="muted">(purpose key or exact name)</span>
          </span>
          <select
            value={effective}
            onChange={(e) => {
              setModel(e.target.value)
              setEnhance(null) // an explicit on/off choice belongs to the previous model
            }}
            disabled={busy}
          >
            {o.purposes.image && (
              <option value="image" disabled={imageModels.length === 0}>
                purpose: image → {o.purposes.image}
              </option>
            )}
            {o.purposes.video && (
              <option value="video" disabled={videoModels.length === 0}>
                purpose: video → {o.purposes.video}
              </option>
            )}
            {imageModels.length > 0 && (
              <optgroup label="image models">
                {imageModels.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </optgroup>
            )}
            {videoModels.length > 0 && (
              <optgroup label="video models">
                {videoModels.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </optgroup>
            )}
          </select>
        </label>
        <label className="gen-field">
          <span>
            enhance prompt{' '}
            <span className="muted">
              (off-slot — {et.ok ? `${et.instance}'s ${et.model} rewrites it first` : 'no valid enhance target'}
              )
            </span>
          </span>
          <label className="inline">
            <input
              type="checkbox"
              checked={enhanceConfigured && enhanceOn}
              disabled={busy || !enhanceConfigured}
              title={
                enhanceConfigured
                  ? 'per-model prompt in the settings below'
                  : 'no enhancement prompt configured for this model — set one in the settings, or leave it off'
              }
              onChange={(e) => setEnhance(e.target.checked)}
            />{' '}
            <span className="muted">
              {enhanceConfigured ? (enhanceOn ? 'on' : 'off') : 'off (no prompt set)'}
            </span>
          </label>
        </label>
        {kind === 'video' && (
          <>
            <label className="gen-field">
              <span>
                seconds <span className="muted">(rounded up to the model's steps — its range lands in the job error if out of range)</span>
              </span>
              <input type="number" step="0.5" value={seconds} onChange={(e) => setSeconds(e.target.value)} disabled={busy} />
            </label>
            <label className="gen-field">
              <span>
                seed <span className="muted">(-1 = random)</span>
              </span>
              <input type="number" value={seed} onChange={(e) => setSeed(e.target.value)} disabled={busy} />
            </label>
          </>
        )}
        {kind === 'image' && (
          <label className="gen-field">
            <span>
              seed <span className="muted">(-1 = random)</span>
            </span>
            <input type="number" value={seed} onChange={(e) => setSeed(e.target.value)} disabled={busy} />
          </label>
        )}
        <label className="gen-field">
          <span>
            size <span className="muted">(WIDTHxHEIGHT — the model's own grid; its 400 names it if you miss)</span>
          </span>
          <input type="text" value={size} onChange={(e) => setSize(e.target.value)} disabled={busy} placeholder="1344x768" />
        </label>
        <label className="gen-field gen-prompt">
          <span>prompt</span>
          <textarea
            rows={3}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder={kind === 'video' ? 'a red fox trotting through a snowy pine forest, snow crunching underfoot' : 'a red fox in fresh snow, morning light'}
          />
        </label>
      </div>
      {note && <p className="muted gen-model-note">{note}</p>}
      <p className="muted gen-target-line" title={et.ok ? '' : et.reason}>
        enhance target: {et.instance} → {et.model}{' '}
        {et.ok ? (
          et.running ? (
            <span className="badge ok">running</span>
          ) : (
            <span className="badge">down — the engine starts it when a job needs it</span>
          )
        ) : (
          <span className="badge err">{et.reason || 'misconfigured'}</span>
        )}
      </p>
      {err && <div className="banner err gen-err">{err}</div>}
      <div className="btns">
        <button disabled={!canSubmit} onClick={() => void submit()}>
          queue {kind ? `a ${kind} job` : 'a job'}
        </button>
        <span className="muted">
          {o.queue.length > 0 && `${o.queue.length} queued — `}
          jobs start when a slot that hosts their model has a free lane; a drained slot yields its cards to a queued job
        </span>
      </div>
    </div>
  )
}

// ── slots (the lane) ──────────────────────────────────────────────────────

function SlotsCard({ o, busy, act }: { o: GenOverview; busy: boolean; act: (k: string, fn: () => Promise<unknown>) => void }) {
  return (
    <div className="card">
      <h3>
        slots <span className="muted">— the gen lane (cards assigned in the GPU view)</span>
      </h3>
      <table>
        <thead>
          <tr>
            <th>slot</th>
            <th>models</th>
            <th>cards</th>
            <th>state</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {o.slots.map((s: GenSlot) => (
            <tr key={s.name}>
              <td>
                {s.display} <span className="muted">{s.name}</span>
              </td>
              <td className="muted">
                {Object.entries(s.models)
                  .filter(([, k]) => k === 'image' || k === 'video' || k === 'llm')
                  .map(([n, k]) => (
                    <span key={n} className="gen-modelchip" title={k === 'llm' ? 'the enhancement LLM entry' : `${k} model`}>
                      {n}
                      <i>{k === 'llm' ? 'llm' : k}</i>
                    </span>
                  ))}
              </td>
              <td>
                <Cards xs={s.assigned} />
              </td>
              <td>
                <SlotStatus s={s} />
              </td>
              <td className="btns">
                {s.running ? (
                  <button
                    className="danger"
                    disabled={busy || s.status.busy}
                    title={s.status.busy ? 'a job is in flight on this slot' : 'stop (disabled while a job is in flight on this slot)'}
                    onClick={() =>
                      act(`gen-stop:${s.name}`, async () => {
                        if (
                          !window.confirm(
                            `stop ${s.display}? no job is in flight on it; the queue is unaffected (a queued job will start it again if it needs it)`,
                          )
                        )
                          return
                        await api('/gpus/stop', { method: 'POST', body: JSON.stringify({ service: s.name }) })
                      })
                    }
                  >
                    stop
                  </button>
                ) : (
                  <button
                    disabled={busy}
                    title={s.status.blockers.length ? s.status.blockers.join(' · ') : 'start (on its assigned cards when free, else the lowest free set)'}
                    onClick={() => act(`gen-start:${s.name}`, () => api('/gpus/start', { method: 'POST', body: JSON.stringify({ service: s.name }) }))}
                  >
                    start
                  </button>
                )}
              </td>
            </tr>
          ))}
          {o.slots.length === 0 && (
            <tr>
              <td colSpan={5} className="muted">
                no gen slots in the deployed compose (the gpu.private instances — see RUNBOOK → Gen)
              </td>
            </tr>
          )}
        </tbody>
      </table>
      <p className="muted">
        A slot keeps running after its in-flight jobs finish (lazy stop — the gen model stays
        resident, so the next job pays no disk load). It yields its cards only when (a) a queued
        job needs them — a drained slot stops automatically to free the lane, (b) you stop it
        here or in the GPU view, or (c) a reboot. Its gen model is the only entry — prompt
        enhancement runs off-slot, on the enhance target above.
      </p>
    </div>
  )
}

function SlotStatus({ s }: { s: GenSlot }) {
  const st = s.status
  if (s.running) {
    return (
      <span className="badge ok" title={st.busy ? 'a job is in flight' : 'idle — no job in flight'}>
        running{st.busy ? ' · busy' : ''}
      </span>
    )
  }
  if (st.state === 'blocked') {
    return (
      <span className="badge err" title={st.blockers.join(' · ')}>
        blocked
      </span>
    )
  }
  if (st.state === 'down-idle-stops') {
    return (
      <span className="badge warn" title={`start would stop: ${st.stops.join(', ')} (idle)`}>
        down · yields {st.stops.length}
      </span>
    )
  }
  return (
    <span className="badge" title={st.blockers.join(' · ') || 'cards free — start it (a queued job will)'}>
      down
    </span>
  )
}

// ── jobs ──────────────────────────────────────────────────────────────────

function ArtifactCell({ job }: { job: GenJob }) {
  const a = job.artifact
  // The link can die under a row: a record still in the store is protected
  // (the age GC skips its files; the DELETE endpoint takes record + files
  // together) — but a stale row (its record trimmed, then the file age-GC'd)
  // or a hand `rm` on the host leaves the file gone. Show that, not a broken
  // image.
  const [gone, setGone] = useState(false)
  if (!a) return <span className="muted">—</span>
  if (gone) return <span className="muted" title="the artifact file is gone (age GC, or the record predates it)">artifact removed</span>
  // A video's thumbnail is best-effort (its fetch never fails the job):
  // without one there is no image to render — a placeholder, never the MP4
  // as an <img> (it would fail to decode and read as "artifact removed").
  const thumb = job.kind === 'video' ? a.thumb : a.url
  return (
    <span className="gen-artifact">
      {thumb ? (
        <a href={a.url} target="_blank" rel="noreferrer" title={`${a.url} (${(a.size / 1048576).toFixed(1)} MiB)`}>
          <img className="gen-thumb" src={thumb} alt="" loading="lazy" onError={() => setGone(true)} />
        </a>
      ) : (
        <span
          className="gen-thumb gen-thumb-placeholder"
          title={`${a.url} (${(a.size / 1048576).toFixed(1)} MiB) — no thumbnail (the video's first-frame fetch is best-effort)`}
        >
          ▶
        </span>
      )}
      <a href={a.url} target="_blank" rel="noreferrer" className="muted">
        open
      </a>
    </span>
  )
}

function JobRow({
  job,
  busy,
  onDelete,
}: {
  job: GenJob
  busy: boolean
  onDelete: (j: GenJob) => void
}) {
  const inFlight = IN_FLIGHT.has(job.state)
  return (
    <tr>
      <td className="muted">{job.id.replace('job-', '')}</td>
      <td>
        {job.kind} <span className="muted">{job.model}</span>
        {job.enhanced_prompt && (
          <span className="muted" title={`enhanced: ${job.enhanced_prompt}`}>
            {' '}
            ✎
          </span>
        )}
      </td>
      <td className="gen-prompt-cell" title={job.prompt}>
        {job.prompt}
      </td>
      <td>
        <span className={stateBadge(job.state)}>{job.state}</span>
        {job.state === 'running' && job.progress != null && (
          <span className="gen-progress" title={`${job.progress}%`}>
            <Meter pct={job.progress / 100} tone="load" />
            <i>{job.progress}%</i>
          </span>
        )}
      </td>
      <td className="muted">{job.slot ?? '—'}</td>
      <td className="muted" title={job.error ?? ''}>
        {job.error ? <span className="gen-err-text" title={job.error}>{job.error.length > 40 ? job.error.slice(0, 40) + '…' : job.error}</span> : '—'}
      </td>
      <td>
        <ArtifactCell job={job} />
      </td>
      <td className="muted">
        {/* the job timestamps are epoch SECONDS (Python time.time()); relTime
            wants ms — feeding seconds straight in reads ~56 years "ago" */}
        {relTime(Date.now(), (inFlight ? job.created_at : job.finished_at ?? job.created_at) * 1000)}
      </td>
      <td className="btns">
        <button
          className="danger"
          disabled={busy}
          title={
            !inFlight
              ? 'delete the record + artifact'
              : job.state === 'queued'
                ? 'cancel (removes it from the queue; the record stays as canceled)'
                : 'cancel (drops the in-flight request — the slot aborts the work within a few seconds)'
          }
          onClick={() => onDelete(job)}
        >
          {inFlight ? 'cancel' : 'delete'}
        </button>
      </td>
    </tr>
  )
}

// ── settings ──────────────────────────────────────────────────────────────

interface SettingsForm {
  purposes: Record<string, string>
  enhanceEnabled: boolean
  prompts: Record<string, string>
  enhanceInstance: string
  enhanceModel: string
  gcDays: string
}

function SettingsCard({ o, busy, onSaved }: { o: GenOverview; busy: boolean; onSaved: () => void }) {
  const [form, setForm] = useState<SettingsForm | null>(null)
  const [err, setErr] = useState<string | null>(null)
  // seed once from the first poll; later polls never clobber a half-edited form
  useEffect(() => {
    if (!form) {
      setForm({
        purposes: { ...o.purposes },
        enhanceEnabled: o.enhance.enabled,
        prompts: { ...o.enhance.prompts },
        enhanceInstance: o.enhance.instance,
        enhanceModel: o.enhance.model,
        gcDays: String(o.gc_days),
      })
    }
  }, [o, form])
  if (!form) return null

  const imageModels = Object.entries(o.models).filter(([, m]) => m.kind === 'image').map(([n]) => n)
  const videoModels = Object.entries(o.models).filter(([, m]) => m.kind === 'video').map(([n]) => n)
  const genModels = [...imageModels, ...videoModels]
  const targetInstance = o.instances.find((i) => i.name === form.enhanceInstance)

  async function save() {
    if (!form) return
    setErr(null)
    const body: Record<string, unknown> = {
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
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e))
    }
  }

  return (
    <div className="card">
      <h3>
        settings <span className="muted">— purposes, enhancement prompts, artifact age GC</span>
      </h3>
      <div className="gen-settings">
        <label className="gen-field">
          <span>
            purpose <code>image</code> →
          </span>
          <select
            value={form.purposes.image ?? ''}
            onChange={(e) => setForm({ ...form, purposes: { ...form.purposes, image: e.target.value } })}
          >
            <option value="">— not configured —</option>
            {imageModels.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label className="gen-field">
          <span>
            purpose <code>video</code> →
          </span>
          <select
            value={form.purposes.video ?? ''}
            onChange={(e) => setForm({ ...form, purposes: { ...form.purposes, video: e.target.value } })}
          >
            <option value="">— not configured —</option>
            {videoModels.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label className="inline gen-field">
          <input
            type="checkbox"
            checked={form.enhanceEnabled}
            onChange={(e) => setForm({ ...form, enhanceEnabled: e.target.checked })}
          />{' '}
          <span>
            enhance by default <span className="muted">(per-model prompt required — a model without one skips)</span>
          </span>
        </label>
        <label className="gen-field">
          <span>
            enhance via instance <span className="muted">(any fleet instance with an llm entry)</span>
          </span>
          <select
            value={form.enhanceInstance}
            onChange={(e) => {
              const inst = o.instances.find((i) => i.name === e.target.value)
              setForm({
                ...form,
                enhanceInstance: e.target.value,
                enhanceModel: inst?.llms[0] ?? '',
              })
            }}
          >
            {o.instances.map((i) => (
              <option key={i.name} value={i.name}>
                {i.display} ({i.name}){i.running ? '' : ' — down'}
              </option>
            ))}
          </select>
        </label>
        <label className="gen-field">
          <span>
            … using its model <span className="muted">(the /v1 model name)</span>
          </span>
          <select value={form.enhanceModel} onChange={(e) => setForm({ ...form, enhanceModel: e.target.value })}>
            {(targetInstance?.llms ?? []).map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
        {genModels.map((n) => (
          <label key={n} className="gen-field">
            <span>
              enhancement prompt — <code>{n}</code>
            </span>
            <textarea
              rows={2}
              value={form.prompts[n] ?? ''}
              placeholder="(empty = no enhancement for this model)"
              onChange={(e) => setForm({ ...form, prompts: { ...form.prompts, [n]: e.target.value } })}
            />
          </label>
        ))}
        <label className="gen-field">
          <span>
            delete artifacts after (days)
          </span>
          <input
            type="number"
            min="1"
            max="365"
            value={form.gcDays}
            onChange={(e) => setForm({ ...form, gcDays: e.target.value })}
          />
        </label>
      </div>
      {err && <div className="banner err gen-err">{err}</div>}
      <div className="btns">
        <button disabled={busy} onClick={() => void save()}>
          save settings
        </button>
      </div>
    </div>
  )
}

// ── the view ──────────────────────────────────────────────────────────────

export default function Gen({ intervalMs }: { intervalMs: number }) {
  const poll = usePoll(() => api<GenOverview>('/gen'), intervalMs)
  const [busy, setBusy] = useState<string | null>(null)

  async function act(key: string, fn: () => Promise<unknown>) {
    setBusy(key)
    try {
      await fn()
      poll.refresh()
    } catch (e) {
      window.alert(String(e))
    }
    setBusy(null)
  }

  function onDelete(j: GenJob) {
    const label = IN_FLIGHT.has(j.state) ? `cancel ${j.id}?` : `delete ${j.id} and its artifact?`
    if (!window.confirm(label)) return
    void act(`gen-del:${j.id}`, () => api(`/gen/jobs/${j.id}`, { method: 'DELETE' }))
  }

  const data = poll.data
  if (poll.error) return <div className="banner err">gen: {poll.error}</div>
  if (!data) return <div className="banner">loading…</div>

  const active = data.jobs
    .filter((j) => IN_FLIGHT.has(j.state))
    .sort((a, b) => a.created_at - b.created_at)
  const recent = data.jobs.filter((j) => !IN_FLIGHT.has(j.state))

  return (
    <div className="gen-view">
      <Composer o={data} busy={busy !== null} onCreated={poll.refresh} />
      <div className="card">
        <h3>
          jobs <span className="muted">— {active.length} in flight · {recent.length} recent</span>
        </h3>
        <table>
          <thead>
            <tr>
              <th></th>
              <th>model</th>
              <th>prompt</th>
              <th>state</th>
              <th>slot</th>
              <th>error</th>
              <th>artifact</th>
              <th></th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {active.map((j) => (
              <JobRow key={j.id} job={j} busy={busy !== null} onDelete={onDelete} />
            ))}
            {recent.map((j) => (
              <JobRow key={j.id} job={j} busy={busy !== null} onDelete={onDelete} />
            ))}
            {active.length === 0 && recent.length === 0 && (
              <tr>
                <td colSpan={9} className="muted">
                  no jobs yet — queue one above
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <SlotsCard o={data} busy={busy !== null} act={act} />
      <SettingsCard o={data} busy={busy !== null} onSaved={poll.refresh} />
    </div>
  )
}
