// Views are lazy chunks: only the active tab's code ships to the browser. The
// Overview draws its own bars and traces in SVG/CSS, so it is a normal-sized
// chunk now — no chart library in the bundle at all.
import { Suspense, lazy, useState } from 'react'

const Overview = lazy(() => import('./views/Overview'))
const Services = lazy(() => import('./views/Services'))
const Gpus = lazy(() => import('./views/Gpus'))
const Actions = lazy(() => import('./views/Actions'))
const Settings = lazy(() => import('./views/Settings'))

const TABS = ['Overview', 'Services', 'GPUs', 'Actions', 'Settings'] as const
type Tab = (typeof TABS)[number]

const INTERVAL_KEY = 'panel.intervalMs'
const DEFAULT_INTERVAL = 2000

function loadInterval(): number {
  const v = Number(localStorage.getItem(INTERVAL_KEY))
  return Number.isFinite(v) && v >= 500 ? v : DEFAULT_INTERVAL
}

export default function App() {
  const [tab, setTab] = useState<Tab>('Overview')
  const [intervalMs, setIntervalMs] = useState<number>(loadInterval)

  const changeInterval = (ms: number) => {
    localStorage.setItem(INTERVAL_KEY, String(ms))
    setIntervalMs(ms)
  }

  return (
    <div className="app">
      <header>
        <h1>panel</h1>
        <nav>
          {TABS.map((t) => (
            <button key={t} className={t === tab ? 'tab active' : 'tab'} onClick={() => setTab(t)}>
              {t}
            </button>
          ))}
        </nav>
      </header>
      <main>
        <Suspense fallback={<div className="banner">loading…</div>}>
          {tab === 'Overview' && <Overview intervalMs={intervalMs} />}
          {tab === 'Services' && <Services intervalMs={intervalMs} />}
          {tab === 'GPUs' && <Gpus intervalMs={intervalMs} />}
          {tab === 'Actions' && <Actions />}
          {tab === 'Settings' && <Settings intervalMs={intervalMs} onChange={changeInterval} />}
        </Suspense>
      </main>
      <footer>refresh: {(intervalMs / 1000).toFixed(1)}s — set in Settings</footer>
    </div>
  )
}
