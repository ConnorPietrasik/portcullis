// Screenshots the panel UI (dev server + demo mock API) with headless Chromium.
// Node >= 22 (built-in fetch + WebSocket; no npm deps).
//
//   node tools/demo/shot.mjs [appUrl] [outDir] [options]
//
// Options
//   --tabs=Gen,Actions   which tabs to shoot (default: all six, in order)
//   --warm=35            warm-up seconds so the charts build history (0 to skip)
//   --pre=file.js        page script to run once after the warm-up, before the walk
//   --scenario=file.json shoot a scripted walk instead of the plain tab tour
//
// A scenario is a JSON array of steps, each `{ name, tab?, then?, wait?, full? }`
// — `tab` clicks a nav tab, `then` is a page script to run first (click a card,
// open a modal…), `wait` is ms to settle (default 1200), `full` captures the
// whole page. See scenarios/ for the shipped ones.
//
// Without a scenario: <tab>.png is the viewport (what the monitor shows) and
// <tab>-full.png the whole page, only for tabs taller than the viewport.
//
// 1920x1080 is the size the panel is viewed on (a second monitor).

import { spawn } from 'node:child_process'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { setTimeout as sleep } from 'node:timers/promises'

const argv = process.argv.slice(2)
const flags = Object.fromEntries(
  argv.filter(a => a.startsWith('--')).map(a => {
    const i = a.indexOf('=')
    return i < 0 ? [a.slice(2), true] : [a.slice(2, i), a.slice(i + 1)]
  }),
)
const positional = argv.filter(a => !a.startsWith('--'))

const APP = positional[0] ?? 'http://localhost:5173/'
const OUT = flags.out ?? positional[1] ?? 'tools/demo/screens'
const WARM_MS = Math.round(Number(flags.warm ?? 35) * 1000) // ~17 polls at 2 s
const VIEW_W = 1920
const VIEW_H = 1080
const DBUG_PORT = 9222

const DEFAULT_TABS = ['Overview', 'Services', 'GPUs', 'Gen', 'Actions', 'Settings']
const walk = flags.scenario
  ? JSON.parse(readFileSync(flags.scenario, 'utf8'))
  : String(flags.tabs ?? DEFAULT_TABS.join(','))
      .split(',')
      .map(t => t.trim())
      .filter(Boolean)
      .map(t => ({ name: t, tab: t }))

function log(...a) {
  console.log(new Date().toISOString().slice(11, 19), ...a)
}

// ── launch headless chromium with remote debugging ─────────────────────
const chrome = spawn('/usr/sbin/chromium', [
  '--headless=new',
  '--no-sandbox',
  '--disable-gpu',
  '--disable-dev-shm-usage',
  `--remote-debugging-port=${DBUG_PORT}`,
  `--window-size=${VIEW_W},${VIEW_H}`,
  '--hide-scrollbars',
  '--user-data-dir=/tmp/panel-demo-profile',
  'about:blank',
], { stdio: 'ignore' })

let targets = null
for (let i = 0; i < 60; i++) {
  await sleep(500)
  try {
    const r = await fetch(`http://127.0.0.1:${DBUG_PORT}/json/list`)
    targets = await r.json()
    const page = targets.find(t => t.type === 'page')
    if (page?.webSocketDebuggerUrl) break
    targets = null
  } catch {
    /* not up yet */
  }
}
if (!targets?.find(t => t.type === 'page')) {
  console.error('chromium did not open a debugging page')
  chrome.kill()
  process.exit(1)
}
const wsUrl = targets.find(t => t.type === 'page').webSocketDebuggerUrl

// ── minimal CDP client ──────────────────────────────────────────────────
const ws = new WebSocket(wsUrl)
await new Promise((res, rej) => {
  ws.onopen = res
  ws.onerror = () => rej(new Error('ws connect failed'))
})
let seq = 0
const pending = new Map()
const events = []
ws.onmessage = m => {
  const msg = JSON.parse(m.data)
  if (msg.id && pending.has(msg.id)) {
    pending.get(msg.id)(msg)
    pending.delete(msg.id)
  } else if (msg.method) events.push(msg)
}
const send = (method, params = {}) =>
  new Promise((res, rej) => {
    const id = ++seq
    pending.set(id, msg => (msg.error ? rej(new Error(`${method}: ${msg.error.message}`)) : res(msg.result)))
    ws.send(JSON.stringify({ id, method, params }))
  })

await send('Page.enable')
await send('Runtime.enable')
// Pin the exact viewport: headless=new reserves ~143 px of --window-size, so
// the flag alone gives 1920x937; the override gives the monitor's 1920x1080.
await send('Emulation.setDeviceMetricsOverride', { width: VIEW_W, height: VIEW_H, deviceScaleFactor: 1, mobile: false })
await send('Page.navigate', { url: APP })

// wait for the first paint + API polls to start building history
log(`loading ${APP} … warming up ${WARM_MS / 1000}s`)
for (let i = 0; i < 400 && !events.some(e => e.method === 'Page.loadEventFired'); i++) await sleep(250)
if (WARM_MS > 0) await sleep(WARM_MS)

const evalJs = expression => send('Runtime.evaluate', { expression, returnByValue: true })
const clickTab = name =>
  evalJs(`(() => {
     const b = [...document.querySelectorAll('nav button.tab')].find((x) => x.textContent.trim() === ${JSON.stringify(name)});
     if (!b) return 'not-found';
     b.click();
     return 'ok';
   })()`)
const runFile = path => evalJs(readFileSync(path, 'utf8'))

mkdirSync(OUT, { recursive: true })
const shot = async (name, full = false) => {
  const { data } = await send('Page.captureScreenshot', {
    format: 'png',
    ...(full ? { captureBeyondViewport: true } : {}),
  })
  const file = `${OUT}/${String(name).toLowerCase().replace(/\s+/g, '-')}${full ? '-full' : ''}.png`
  writeFileSync(file, Buffer.from(data, 'base64'))
  log(`saved ${file}`)
}

if (flags.pre) {
  const r = await runFile(flags.pre)
  log(`pre: ${JSON.stringify(r.result.value ?? null)}`)
}

for (const step of walk) {
  if (step.tab) {
    const r = await clickTab(step.tab)
    if (r.result.value !== 'ok') {
      console.error(`tab ${step.tab}: ${r.result.value}`)
      continue
    }
    // lazy chunk + one poll
    await sleep(step.wait ?? (step.tab === 'Overview' ? 1500 : 2000))
  }
  if (step.then) {
    const r = await evalJs(step.then)
    if (r.exceptionDetails) console.error(`then (${step.name}): ${r.exceptionDetails.text}`)
    // a step's return value is its assertion: `return 'ok'` when it just
    // clicked, or a diagnostic string when the thing it clicked refused
    // (steps that return nothing report nothing — no "undefined" noise)
    else if (r.result.value != null && r.result.value !== 'ok') log(`then (${step.name}): ${JSON.stringify(r.result.value)}`)
    await sleep(step.wait ?? 1200)
  }
  // the viewport frame always (that is what the monitor shows), and for a tab
  // taller than the monitor the whole page too — the plain tour, not a
  // scenario, decides that on its own
  const taller = (await evalJs('document.documentElement.scrollHeight')).result.value > VIEW_H
  const full = step.full ?? (!flags.scenario && taller)
  await shot(step.name, false)
  if (full) await shot(step.name, true)
}

ws.close()
chrome.kill()
log('done')
process.exit(0)
