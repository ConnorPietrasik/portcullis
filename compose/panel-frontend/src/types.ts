// Shape of /api/* (see panel-api/main.py). Every sysfs/proc read can fail, so
// every reading is nullable and the UI must render "—" rather than assume a 0.

export interface Gpu {
  card: string
  /** ROCR device index — the card's position in the KFD topology order
   *  (node id, host CPU node skipped), what ROCR_VISIBLE_DEVICES and the
   *  GPU pool's tile indices count in. The sysfs card name is NOT this
   *  (a non-GPU drm device can take card0). */
  rocr_index: number
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
  /** panel.category compose label — the Services view's group (null/"" → Other) */
  category?: string | null
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
  /** panel.category compose label (panel-do list-services) — same group as the container list */
  category?: string | null
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

// ── GPU pool (panel tab "GPUs"; see panel-api/llm_gpus.py) ──────────────

export interface GpuPoolCard {
  index: number
  /** the instance holding this card right now (live container env), or null */
  holder: string | null
}

export interface LlmGpuInstance {
  name: string
  display: string
  /** cards this instance needs (llm.cards label, else --tp) */
  cards_needed: number
  /** the ledger's assignment (intent) — null when never assigned */
  assigned: number[] | null
  /** cards its up-container actually pins (live) — drift vs assigned = a failed apply or hand-run compose */
  holding: number[] | null
  state: string
  running: boolean
  autostart: boolean
}

export interface GpuPool {
  /** the pool size (host card count from /sys) */
  cards: number
  pool: GpuPoolCard[]
  instances: LlmGpuInstance[]
  autostart: string[]
  warnings: string[]
}

export interface ActionResult {
  ok: boolean
  exit_code: number
  stdout: string
  stderr: string
}

// ── auto-update (panel-do update-* actions; see panel-api /api/update*) ───────
// A before→after row of the LAST update, classified by panel-api.
export type UpdateChange =
  | 'major' // a deliberate line edit landed: the leading version integer moved
  | 'minor' // same major, new version — a patch/minor release floated in
  | 'moved' // registry image without a version label (radiance/searxng `latest`)
  | 'rebuilt' // locally built image (panel-web) — a new build, no upstream
  | 'unchanged'
  | 'removed' // the service no longer has a container after the update
  | 'changed' // no comparable version on one side

export interface UpdateRow {
  service: string
  pre_image: string
  pre_tags: string
  pre_version: string | null
  post_image: string | null
  post_version: string | null
  state: string
  change: UpdateChange
}

export interface UpdateReport {
  /** the raw state file (null before the first update) */
  state: {
    snapshot?: string
    status?: string
    repo_root?: string
    repo_head?: string
    services?: Record<string, { image_id: string; rollback_image: string; image_tags: string }>
  } | null
  /** the daily timer's state (null when the unit isn't installed) */
  timer_enabled: boolean | null
  services: UpdateRow[]
  /** ANSI-stripped tail of /opt/panel/.update-run.log (the in-progress view) */
  log_tail: string
}

/** One changed line of what the NEXT update would apply. */
export interface PreviewChange {
  kind: 'image' | 'base' | 'npm' | 'pip'
  name: string
  old: string
  new: string
  change: UpdateChange
}

export interface UpdatePreview {
  repo_root: string
  deployed_head: string | null
  clone_head: string | null
  deployed_at: string | null
  status: string | null
  commits: string[]
  /** the FULL pending-commit count (`commits` is capped at the last 30) */
  commit_count: number
  /** `git status --porcelain` — non-empty ⇒ the auto-update skips */
  dirty: string[]
  /** set when the clone is a git checkout whose status cannot be read —
   *  the auto-update REFUSES it (fail closed); only "run anyway" applies it */
  git_error: string | null
  changes: PreviewChange[]
}
