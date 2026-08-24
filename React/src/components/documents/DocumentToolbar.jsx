import { DOCUMENT_STATUSES, DOCUMENT_TYPES } from '../../api/documents.api.js'
import { humanize } from '../../lib/format.js'
import Button from '../ui/Button.jsx'

/**
 * Filter controls for the document list.
 *
 * Purely presentational: it reports edits upward through `onChange` and holds
 * no state of its own, so the page stays the single source of truth for what is
 * currently applied (and for the debounce on the text inputs).
 *
 * `showOwner` is driven by the caller's role. A regular user's list is already
 * restricted to their own uploads server-side, so an owner filter there would
 * be a control that can only ever narrow a list of one person's files to zero.
 */
export default function DocumentToolbar({
  value,
  onChange,
  showOwner = false,
  total,
  busy = false,
  onRefresh,
}) {
  function update(patch) {
    onChange({ ...value, ...patch })
  }

  return (
    <div className="toolbar">
      <div className="toolbar__row">
        <div className="toolbar__field toolbar__field--grow">
          <label className="toolbar__label" htmlFor="doc-search">
            Filename
          </label>
          <input
            id="doc-search"
            className="toolbar__input"
            type="search"
            placeholder="Search by filename…"
            value={value.search}
            onChange={(event) => update({ search: event.target.value })}
          />
        </div>

        {showOwner && (
          <div className="toolbar__field toolbar__field--grow">
            <label className="toolbar__label" htmlFor="doc-owner">
              Uploaded by
            </label>
            <input
              id="doc-owner"
              className="toolbar__input"
              type="search"
              placeholder="Email contains…"
              value={value.ownerEmail}
              onChange={(event) => update({ ownerEmail: event.target.value })}
            />
          </div>
        )}

        <div className="toolbar__field">
          <label className="toolbar__label" htmlFor="doc-status">
            Status
          </label>
          <select
            id="doc-status"
            className="toolbar__input"
            value={value.status}
            onChange={(event) => update({ status: event.target.value })}
          >
            <option value="">All statuses</option>
            {DOCUMENT_STATUSES.map((status) => (
              <option key={status} value={status}>
                {humanize(status)}
              </option>
            ))}
          </select>
        </div>

        <div className="toolbar__field">
          <label className="toolbar__label" htmlFor="doc-type">
            Type
          </label>
          <select
            id="doc-type"
            className="toolbar__input"
            value={value.documentType}
            onChange={(event) => update({ documentType: event.target.value })}
          >
            <option value="">All types</option>
            {DOCUMENT_TYPES.map((type) => (
              <option key={type} value={type}>
                {humanize(type)}
              </option>
            ))}
          </select>
        </div>

        <div className="toolbar__field">
          <label className="toolbar__label" htmlFor="doc-sort">
            Sort
          </label>
          <select
            id="doc-sort"
            className="toolbar__input"
            value={`${value.sortBy}:${value.sortDir}`}
            onChange={(event) => {
              const [sortBy, sortDir] = event.target.value.split(':')
              update({ sortBy, sortDir })
            }}
          >
            <option value="created_at:desc">Newest first</option>
            <option value="created_at:asc">Oldest first</option>
            <option value="filename:asc">Filename A–Z</option>
            <option value="filename:desc">Filename Z–A</option>
            <option value="size_bytes:desc">Largest first</option>
            <option value="updated_at:desc">Recently updated</option>
          </select>
        </div>
      </div>

      <div className="toolbar__meta">
        <span className="toolbar__count">
          {total == null ? 'Loading…' : `${total.toLocaleString('en-US')} document${total === 1 ? '' : 's'}`}
        </span>
        <Button variant="ghost" className="btn--sm" onClick={onRefresh} disabled={busy}>
          ↻ Refresh
        </Button>
      </div>
    </div>
  )
}
