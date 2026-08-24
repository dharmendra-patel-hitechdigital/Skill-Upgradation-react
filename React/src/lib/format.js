/** Display formatters shared by the document views. */

export function formatBytes(bytes) {
  if (bytes == null) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

export function formatDuration(ms) {
  if (ms == null) return '—'
  if (ms < 1000) return `${ms} ms`
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`
  return `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`
}

export function formatDateTime(iso) {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

/**
 * "identity_document" -> "Identity Document".
 *
 * The API returns machine-readable snake_case for types, statuses and event
 * names; every one of them reaches a label somewhere in the UI.
 */
export function humanize(value) {
  if (!value) return '—'
  return String(value)
    .split('_')
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

/** 0.94 -> "94%". Confidence is a 0..1 float server-side. */
export function formatConfidence(confidence) {
  if (confidence == null) return '—'
  return `${Math.round(confidence * 100)}%`
}

/** Truncate a hash for display while keeping it recognisable. */
export function shortChecksum(checksum) {
  if (!checksum) return '—'
  return `${checksum.slice(0, 12)}…`
}
