import GaugeComponent from 'react-gauge-component'
import { api } from '../api'
import { usePoll } from '../usePoll'
import type { Gpu, Metrics } from '../types'

const mib = (v: number | null | undefined) =>
  v == null ? '—' : v >= 1024 ? `${(v / 1024).toFixed(1)} GiB` : `${v} MiB`

const temp = (v: number | null) => (v == null ? '—' : `${v.toFixed(0)}°C`)

function uptime(s: number | null): string {
  if (s == null) return '—'
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  return d > 0 ? `${d}d ${h}h` : h > 0 ? `${h}h ${m}m` : `${m}m`
}

function Dial({
  value,
  min,
  max,
  warn = 0.75,
  crit = 0.9,
}: {
  value: number | null
  min: number
  max: number
  warn?: number
  crit?: number
}) {
  if (value == null) return <div className="dial-na">n/a</div>
  const lim = (f: number) => min + (max - min) * f
  return (
    <GaugeComponent
      value={value}
      minValue={min}
      maxValue={max}
      arc={{
        padding: 0.02,
        subArcs: [
          { limit: lim(warn), color: '#3ddc84' },
          { limit: lim(crit), color: '#f6c344' },
          { color: '#ff5252' },
        ],
      }}
      pointer={{ type: 'arrow', elastic: true }}
      labels={{
        valueLabel: { hide: true }, // value shown in the figcaption instead
        tickLabels: {
          defaultTickValueConfig: { hide: true },
          defaultTickLineConfig: { hide: true },
        },
      }}
      style={{ width: 150, height: 90 }}
    />
  )
}

function GpuCard({ gpu }: { gpu: Gpu }) {
  return (
    <div className="card">
      <h3>{gpu.name}</h3>
      <div className="dials">
        <figure>
          <Dial value={gpu.temp_edge_c} min={0} max={110} />
          <figcaption>edge {temp(gpu.temp_edge_c)}</figcaption>
        </figure>
        {gpu.temp_junction_c != null && (
          <figure>
            <Dial value={gpu.temp_junction_c} min={0} max={120} />
            <figcaption>junction {temp(gpu.temp_junction_c)}</figcaption>
          </figure>
        )}
        {gpu.temp_mem_c != null && (
          <figure>
            <Dial value={gpu.temp_mem_c} min={0} max={110} />
            <figcaption>mem {temp(gpu.temp_mem_c)}</figcaption>
          </figure>
        )}
        <figure>
          <Dial value={gpu.util_pct} min={0} max={100} />
          <figcaption>util {gpu.util_pct ?? '—'}%</figcaption>
        </figure>
        {gpu.vram_total_mib != null && gpu.vram_total_mib > 0 && (
          <figure>
            <Dial value={gpu.vram_used_mib} min={0} max={gpu.vram_total_mib} />
            <figcaption>
              vram {mib(gpu.vram_used_mib)} / {mib(gpu.vram_total_mib)}
            </figcaption>
          </figure>
        )}
      </div>
      <div className="kv">
        <span>power: {gpu.power_w != null ? `${gpu.power_w.toFixed(1)} W` : '—'}</span>
        <span>fan: {gpu.fan_rpm != null ? `${gpu.fan_rpm} rpm` : '—'}</span>
      </div>
    </div>
  )
}

export default function Overview({ intervalMs }: { intervalMs: number }) {
  const { data, error } = usePoll(() => api<Metrics>('/metrics'), intervalMs)

  if (error) return <div className="banner err">metrics: {error}</div>
  if (!data) return <div className="banner">loading…</div>

  const ram = 'total_mib' in data.ram ? data.ram : undefined
  const disks = Array.isArray(data.disks) ? data.disks : []

  return (
    <>
      {data.gpus.length === 0 && <div className="banner">no AMD GPUs found in sysfs</div>}
      <div className="grid">
        {data.gpus.map((g) => (
          <GpuCard key={g.card} gpu={g} />
        ))}
      </div>
      <div className="grid">
        <div className="card">
          <h3>CPU</h3>
          <div className="dials">
            <figure>
              <Dial value={data.cpu_temp_c} min={0} max={100} />
              <figcaption>temp {temp(data.cpu_temp_c)}</figcaption>
            </figure>
          </div>
          <div className="kv">
            <span>load: {data.load.map((l) => l.toFixed(2)).join(' / ') || '—'}</span>
            <span>uptime: {uptime(data.uptime_s)}</span>
          </div>
        </div>
        <div className="card">
          <h3>RAM</h3>
          <div className="dials">
            <figure>
              <Dial value={ram?.used_mib ?? null} min={0} max={ram?.total_mib ?? 1} warn={0.8} crit={0.95} />
              <figcaption>
                {mib(ram?.used_mib ?? null)} / {mib(ram?.total_mib ?? null)}
              </figcaption>
            </figure>
          </div>
        </div>
        {disks.length === 0 ? (
          <div className="card">
            <h3>Disk</h3>
            <div className="kv">
              <span>n/a — nothing matched PANEL_DISK_MOUNTS</span>
            </div>
          </div>
        ) : (
          disks.map((d) => (
            <div className="card" key={d.mount}>
              <h3>{d.mount === '/' ? 'Disk' : `Disk ${d.mount}`}</h3>
              {d.error ? (
                <div className="kv err">
                  <span>⚠ {d.error}</span>
                </div>
              ) : (
                <>
                  <div className="dials">
                    <figure>
                      <Dial value={d.used_mib ?? null} min={0} max={d.total_mib ?? 1} warn={0.8} crit={0.9} />
                      <figcaption>
                        {mib(d.used_mib ?? null)} / {mib(d.total_mib ?? null)}
                      </figcaption>
                    </figure>
                  </div>
                  <div className="kv">
                    <span>free: {mib(d.avail_mib ?? null)}</span>
                  </div>
                </>
              )}
            </div>
          ))
        )}
      </div>
    </>
  )
}
