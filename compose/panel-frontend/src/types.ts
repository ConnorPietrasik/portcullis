export interface Gpu {
  card: string
  name: string
  temp_edge_c: number | null
  temp_junction_c: number | null
  temp_mem_c: number | null
  util_pct: number | null
  vram_used_mib: number | null
  vram_total_mib: number | null
  power_w: number | null
  fan_rpm: number | null
}

export interface Ram {
  total_mib?: number
  used_mib?: number
  avail_mib?: number
}

export interface Disk {
  mount: string
  total_mib?: number
  used_mib?: number
  avail_mib?: number
  error?: string // set when the mount isn't usable (not mounted, missing, pseudo-fs)
}

export interface Metrics {
  ts: number
  gpus: Gpu[]
  cpu_temp_c: number | null
  ram: Ram | {}
  disks: Disk[]
  load: number[]
  uptime_s: number | null
}

export interface ContainerInfo {
  id: string
  name: string
  image: string
  state: string
  status: string
}

export interface ActionInfo {
  name: string
  summary: string
  arity: number
  patterns: string[]
  timeout_sec: number
  confirm: boolean
}

export interface ActionResult {
  ok: boolean
  exit_code: number
  stdout: string
  stderr: string
}
