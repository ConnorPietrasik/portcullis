/** The poll's 304 — the gen endpoints' ETag says the body is unchanged:
 *  not an error, and there is no body. The browser normally never surfaces
 *  this to fetch() — its HTTP cache absorbs the 304 and re-serves the stored
 *  body as the 200 it first got (the wire transfer is what shrinks, the app
 *  code path is unchanged). This path is the defense for a 304 that DOES
 *  reach the app (a caching layer that passes it through, a non-browser
 *  client): usePoll keeps its data on it. A plain api() call (the modal's
 *  full-record fetch) never gets one: only the two gen polls answer
 *  conditionally. */
export class NotModified extends Error {}

/** The 404 — the record the fetch asked for does not exist (deleted,
 *  or the store's trim took it). A subclass so callers can tell "gone"
 *  from a blip without parsing the message (the Gen modal's gone-state). */
export class NotFound extends Error {}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    ...init,
  })
  if (res.status === 304) throw new NotModified()
  if (res.status === 404) {
    const body = await res.text().catch(() => '')
    throw new NotFound(`404 ${res.statusText}${body ? ` — ${body}` : ''}`)
  }
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    throw new Error(`${res.status} ${res.statusText}${body ? ` — ${body}` : ''}`)
  }
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

/**
 * POST for a file response (the gen batch download): the body is JSON, the
 * reply is bytes. The server's Content-Disposition names the file when it
 * sets one. A failed request carries a plain-text/JSON detail, so the same
 * "status — detail" error the JSON path throws is what the UI shows.
 */
export async function download(path: string, body: unknown, fallbackName = 'download'): Promise<void> {
  const res = await fetch(`/api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const text = await res.text().catch(() => '')
    let detail = text
    try {
      detail = String(JSON.parse(text).detail ?? text)
    } catch {
      /* not JSON — the text is the message */
    }
    throw new Error(`${res.status} ${res.statusText}${detail ? ` — ${detail.slice(0, 300)}` : ''}`)
  }
  const disp = res.headers.get('Content-Disposition') ?? ''
  const named = /filename="?([^";]+)"?/.exec(disp)?.[1]
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = named || fallbackName
  document.body.appendChild(a)
  a.click()
  a.remove()
  // the blob fetch starts after this handler returns — revoking synchronously
  // races the download (Firefox fails outright), so give it a beat
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
