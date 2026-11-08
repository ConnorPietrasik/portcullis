// ────────────────────────── history buffer (for trends) ──────────────────────────
// The panel polls every ~2 s, which is a free time-series: keeping the last few
// hundred samples turns "VRAM 21.4 GiB" into "VRAM has been climbing since the
// model loaded". Stored in sessionStorage so switching tabs (which unmounts the
// view) doesn't wipe the traces.

import { useEffect, useRef, useState } from 'react'
import type { Metrics } from './types'

export type Num = number | null

export interface GpuSample {
  util: Num
  edge: Num
  junction: Num
  mem: Num
  vramPct: Num
  vramUsed: Num
  power: Num
  fan: Num
  clock: Num
}

export interface DiskSample {
  usedPct: Num
  read: Num
  write: Num
}

export interface Sample {
  t: number
  cpuPct: Num
  cpuTemp: Num
  freqMhz: Num
  load1: Num
  load5: Num
  load15: Num
  ramUsed: Num
  ramCache: Num
  ramFree: Num
  ramTotal: Num
  swapUsed: Num
  swapTotal: Num
  rx: Num
  tx: Num
  gpus: Record<string, GpuSample>
  disks: Record<string, DiskSample>
}

const KEY = 'panel.history.v1'
const CAP = 400 // ≈ 13 min at 2 s — the charts show a window of this tail
const SAVE_EVERY_MS = 5000

function toSample(m: Metrics): Sample {
  const gpus: Record<string, GpuSample> = {}
  for (const g of m.gpus ?? []) {
    gpus[g.card] = {
      util: g.util_pct,
      edge: g.temp_edge_c,
      junction: g.temp_junction_c,
      mem: g.temp_mem_c,
      vramPct: g.vram_total_mib && g.vram_total_mib > 0 && g.vram_used_mib != null
        ? (g.vram_used_mib / g.vram_total_mib) * 100
        : null,
      vramUsed: g.vram_used_mib,
      power: g.power_w,
      fan: g.fan_rpm,
      clock: g.clock_mhz,
    }
  }
  const disks: Record<string, DiskSample> = {}
  for (const d of m.disks ?? []) {
    disks[d.mount] = {
      usedPct: d.total_mib && d.total_mib > 0 && d.used_mib != null ? (d.used_mib / d.total_mib) * 100 : null,
      read: d.read_bytes_s ?? null,
      write: d.write_bytes_s ?? null,
    }
  }
  return {
    t: Date.now(),
    cpuPct: m.cpu?.usage_pct ?? null,
    cpuTemp: m.cpu?.temp_c ?? null,
    freqMhz: m.cpu?.freq_mhz ?? null,
    load1: m.load?.[0] ?? null,
    load5: m.load?.[1] ?? null,
    load15: m.load?.[2] ?? null,
    ramUsed: m.ram?.used_mib ?? null,
    ramCache: m.ram?.cache_mib ?? null,
    ramFree: m.ram?.free_mib ?? null,
    ramTotal: m.ram?.total_mib ?? null,
    swapUsed: m.swap?.used_mib ?? null,
    swapTotal: m.swap?.total_mib ?? null,
    rx: m.net?.rx_bits_s ?? null,
    tx: m.net?.tx_bits_s ?? null,
    gpus,
    disks,
  }
}

function load(): Sample[] {
  try {
    const raw = sessionStorage.getItem(KEY)
    if (!raw) return []
    const arr = JSON.parse(raw)
    return Array.isArray(arr) ? (arr as Sample[]).slice(-CAP) : []
  } catch {
    return [] // private mode / quota / an older shape — trends just start over
  }
}

function save(hist: Sample[]) {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(hist))
  } catch {
    /* quota or disabled storage: trends are cosmetic, never worth an error banner */
  }
}

/** Rolling history of every polled sample; `m` is the latest /api/metrics body. */
export function useHistory(m: Metrics | null): Sample[] {
  const [hist, setHist] = useState<Sample[]>(load)
  const ref = useRef<Sample[]>(hist)
  const lastTs = useRef<Num>(null)
  ref.current = hist

  useEffect(() => {
    if (!m || m.ts === lastTs.current) return
    lastTs.current = m.ts
    setHist((prev) => [...prev, toSample(m)].slice(-CAP))
  }, [m])

  useEffect(() => {
    const t = window.setInterval(() => save(ref.current), SAVE_EVERY_MS)
    return () => {
      window.clearInterval(t)
      save(ref.current)
    }
  }, [])

  return hist
}

/** Last `n` samples — the window the charts and trends read. */
export const tail = (hist: Sample[], n: number): Sample[] => hist.slice(-n)

/** One metric as a series; a missing GPU/disk row reads as null (a gap, not a 0). */
export function seriesOf(hist: Sample[], pick: (s: Sample) => unknown): (number | null)[] {
  return hist.map((s) => {
    const v = pick(s)
    return typeof v === 'number' && Number.isFinite(v) ? v : null
  })
}
