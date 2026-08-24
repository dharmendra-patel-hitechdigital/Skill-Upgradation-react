import { humanize } from '../../lib/format.js'

/**
 * Coloured pill for a document's processing state.
 *
 * `pending` and `processing` are deliberately different colours: "queued, not
 * started" and "running now" lead to different operator decisions, and a shared
 * neutral tone hides which one a stuck document is in.
 */
const TONES = {
  pending: 'amber',
  processing: 'blue',
  completed: 'green',
  failed: 'red',
}

export default function StatusBadge({ status }) {
  const tone = TONES[status] ?? 'slate'
  return (
    <span className={`badge badge--${tone}`}>
      {status === 'processing' && <span className="badge__pulse" aria-hidden />}
      {humanize(status)}
    </span>
  )
}
