// Shape of /api/* (see panel-api/main.py). Every sysfs/proc read can fail, so
// every reading is nullable and the UI must render "—" rather than assume a 0.

export interface Gpu {
  card: string
  name: string
  driver: string
  pci_id: string
  pci_slot: string
  temp_edge_c: number | null
  temp_junction_c: number | null
  temp_mem_c: number | null
  util_pct: number | null
  vram_used_mib: number | null
  vram_total_mib: number | null
  power_w: number | null
  /** The card's own power limit — W is meaningless without it. */
  power_cap_w: number | null
  fan_rpm: number | null
  clock_mhz: number | null
  mem_clock_mhz: number | null
}

export interface Host {
  hostname?: string
  kernel?: string
}

export interface Cpu {
  temp_c: number | null
  cores: number
  model: string
  freq_mhz: number | null
  /** Δ of /proc/stat between two polls — null on the first sample. */
  usage_pct: number | null
  procs_running: number | null
  procs_blocked: number | null
}

export interface Ram {
  total_mib?: number
  /** total − MemAvailable: memory nothing can reclaim. */
  used_mib?: number
  avail_mib?: number
  free_mib?: number
  /** reclaimable page cache/buffers — NOT the same thing as used. */
  cache_mib?: number
}

export interface Swap {
  total_mib?: number
  used_mib?: number
}

export interface Disk {
  mount: string
  device: string
  fstype: string
  total_mib?: number
  used_mib?: number
  avail_mib?: number
  read_bytes_s?: number
  write_bytes_s?: number
  /** the drive's own sensor, when the kernel has one (NVMe always does) */
  temp_c?: number
  error?: string // set when the mount isn't usable (not mounted, missing, pseudo-fs)
}

/** One hwmon chip, reduced to its hottest reading (see sensors() in panel-api). */
export interface Sensor {
  chip: string
  device: string
  label: string
  temp_c: number
}

export interface Net {
  ifaces?: string[]
  /** bits/s — links are quoted in bits, disks in bytes. */
  rx_bits_s?: number
  tx_bits_s?: number
}

export interface Metrics {
  ts: number
  host: Host
  gpus: Gpu[]
  cpu: Cpu
  /** All fields optional: an unreadable /proc/meminfo yields an empty object. */
  ram: Ram
  swap: Swap
  disks: Disk[]
  load: number[]
  uptime_s: number | null
  net: Net
  sensors: Sensor[]
}

export interface ContainerInfo {
  id: string
  name: string
  /** compose service name (container label) — joins /api/stack-services */
  service?: string
  image: string
  state: string
  status: string
}

/** A service defined in the deployed compose, with no container required. */
export interface StackService {
  name: string
  image: string
  display: string
  restart: string
}

export interface ActionInfo {
  name: string
  summary: string
  arity: number
  patterns: string[]
  timeout_sec: number
  confirm: boolean
}

export interface LLMFleetEntry {
  name: string
  display: string
  state: string
}

export interface LLMSettings {
  order: string[]
  status: Record<string, 'up' | 'down' | 'unknown'>
  active: string | null
  fleet: LLMFleetEntry[]
  probe_interval_s: number
}

export interface ActionResult {
  ok: boolean
  exit_code: number
  stdout: string
  stderr: string
}
