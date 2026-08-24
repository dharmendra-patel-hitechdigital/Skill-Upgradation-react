import { Link } from 'react-router-dom'
import StatusBadge from './StatusBadge.jsx'
import { formatBytes, formatDateTime, formatDuration, humanize } from '../../lib/format.js'

/**
 * The document list itself.
 *
 * The `Uploaded by` column appears only for an administrator — for everyone
 * else every row would name the person reading the screen.
 *
 * Wrapped in an `overflow-x` scroller rather than collapsing columns on narrow
 * screens: an operator triaging a failed document needs the status and the
 * timing, and hiding either to fit a phone makes the table decorative.
 */
export default function DocumentTable({ documents, showOwner = false }) {
  if (documents.length === 0) {
    return (
      <div className="empty">
        <p className="empty__title">No documents match these filters</p>
        <p className="empty__text">
          Clear the filters, or upload a document to start the extraction pipeline.
        </p>
      </div>
    )
  }

  return (
    <div className="table-wrap">
      <table className="table">
        <thead>
          <tr>
            <th>Document</th>
            {showOwner && <th>Uploaded by</th>}
            <th>Type</th>
            <th>Status</th>
            <th className="table__num">Pages</th>
            <th className="table__num">Size</th>
            <th className="table__num">Processing</th>
            <th>Uploaded</th>
            <th aria-label="Actions" />
          </tr>
        </thead>
        <tbody>
          {documents.map((document) => (
            <tr key={document.id}>
              <td>
                <Link className="table__link" to={`/documents/${document.id}`}>
                  {document.filename}
                </Link>
                <span className="table__sub">{document.content_type}</span>
              </td>

              {showOwner && (
                <td>
                  {document.owner?.full_name || '—'}
                  <span className="table__sub">{document.owner?.email ?? '—'}</span>
                </td>
              )}

              <td>{document.document_type ? humanize(document.document_type) : '—'}</td>
              <td><StatusBadge status={document.status} /></td>
              <td className="table__num">{document.page_count ?? '—'}</td>
              <td className="table__num">{formatBytes(document.size_bytes)}</td>
              <td className="table__num">{formatDuration(document.processing_duration_ms)}</td>
              <td>{formatDateTime(document.created_at)}</td>
              <td className="table__actions">
                <Link className="table__action" to={`/documents/${document.id}`}>
                  View
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
