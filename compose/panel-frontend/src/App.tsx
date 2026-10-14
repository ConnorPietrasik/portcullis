import { useState } from 'react'
import Overview from './views/Overview'
import Services from './views/Services'
import Actions from './views/Actions'
import Settings from './views/Settings'

const TABS = ['Overview', 'Services', 'Actions', 'Settings'] as const
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
        {tab === 'Overview' && <Overview intervalMs={intervalMs} />}
        {tab === 'Services' && <Services intervalMs={intervalMs} />}
        {tab === 'Actions' && <Actions />}
        {tab === 'Settings' && <Settings intervalMs={intervalMs} onChange={changeInterval} />}
      </main>
      <footer>refresh: {(intervalMs / 1000).toFixed(1)}s — set in Settings</footer>
    </div>
  )
}
