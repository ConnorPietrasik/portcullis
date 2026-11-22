// Screenshots the panel UI (dev server + demo mock API) with headless Chromium.
// Node >= 22 (built-in fetch + WebSocket; no npm deps).
//
//   node tools/demo/shot.mjs [appUrl] [outDir]
//
// Loads the app, lets the polls build a few minutes of chart history, then
// clicks each tab and captures at 1920x1080 — the size the panel is viewed on
// (second monitor). <tab>.png is the viewport (what the screen shows);
// <tab>-full.png is the full page, only for tabs taller than the viewport.

import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { setTimeout as sleep } from 'node:timers/promises'

const APP = process.argv[2] ?? 'http://localhost:5173/'
const OUT = process.argv[3] ?? 'tools/demo/screens'
const TABS = ['Overview', 'Services', 'GPUs', 'Gen', 'Actions', 'Settings']
const WARMUP_MS = 35_000 // ~17 polls at 2 s — enough for traces + a net spike
const VIEW_W = 1920
const VIEW_H = 1080
const DBUG_PORT = 9222

function log(...a) { console.log(new Date().toISOString().slice(11, 19), ...a) }

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
    const page = targets.find((t) => t.type === 'page')
    if (page?.webSocketDebuggerUrl) break
    targets = null
  } catch { /* not up yet */ }
}
if (!targets?.find((t) => t.type === 'page')) {
  console.error('chromium did not open a debugging page')
  chrome.kill()
  process.exit(1)
}
const wsUrl = targets.find((t) => t.type === 'page').webSocketDebuggerUrl

// ── minimal CDP client ──────────────────────────────────────────────────
const ws = new WebSocket(wsUrl)
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws connect failed')) })
let seq = 0
const pending = new Map()
const events = []
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data)
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id) }
  else if (msg.method) events.push(msg)
}
const send = (method, params = {}) => new Promise((res, rej) => {
  const id = ++seq
  pending.set(id, (msg) => (msg.error ? rej(new Error(`${method}: ${msg.error.message}`)) : res(msg.result)))
  ws.send(JSON.stringify({ id, method, params }))
})

await send('Page.enable')
await send('Runtime.enable')
// Pin the exact viewport: headless=new reserves ~143 px of --window-size, so
// the flag alone gives 1920x937; the override gives the monitor's 1920x1080.
await send('Emulation.setDeviceMetricsOverride', { width: VIEW_W, height: VIEW_H, deviceScaleFactor: 1, mobile: false })
await send('Page.navigate', { url: APP })

// wait for the first paint + API polls to start building history
log(`loading ${APP} … warming up ${WARMUP_MS / 1000}s`)
for (let i = 0; i < 400 && !events.some((e) => e.method === 'Page.loadEventFired'); i++) await sleep(250)
await sleep(WARMUP_MS)

const evalJs = (expression) => send('Runtime.evaluate', { expression, returnByValue: true })
const clickTab = (name) => evalJs(
  `(() => {
     const b = [...document.querySelectorAll('nav button.tab')].find((x) => x.textContent.trim() === ${JSON.stringify(name)});
     if (!b) return 'not-found';
     b.click();
     return 'ok';
   })()`,
)

mkdirSync(OUT, { recursive: true })
const shot = async (name, full = false) => {
  const { data } = await send('Page.captureScreenshot', {
    format: 'png',
    ...(full ? { captureBeyondViewport: true } : {}),
  })
  const file = `${OUT}/${name.toLowerCase()}${full ? '-full' : ''}.png`
  writeFileSync(file, Buffer.from(data, 'base64'))
  log(`saved ${file}`)
}

for (const tab of TABS) {
  const r = await clickTab(tab)
  if (r.result.value !== 'ok') { console.error(`tab ${tab}: ${r.result.value}`); continue }
  await sleep(tab === 'Overview' ? 1500 : 2500) // lazy chunks + one poll
  await shot(tab) // the viewport — what the monitor shows
  const h = (await evalJs(`document.documentElement.scrollHeight`)).result.value
  if (h > VIEW_H) await shot(tab, true) // the overflow, for the full page
}

ws.close()
chrome.kill()
log('done')
process.exit(0)
