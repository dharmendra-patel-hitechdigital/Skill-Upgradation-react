import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useApi } from '../hooks/useApi.js'
import { useDocumentTitle } from '../hooks/useDocumentTitle.js'
import {
  deleteDocument,
  fetchDocument,
  fetchDocumentText,
  reprocessDocument,
} from '../api/documents.api.js'
import {
  formatBytes,
  formatDateTime,
  formatDuration,
  humanize,
  shortChecksum,
} from '../lib/format.js'
import Sidebar from '../components/layout/Sidebar.jsx'
import Topbar from '../components/layout/Topbar.jsx'
import StatusBadge from '../components/documents/StatusBadge.jsx'
import ExtractionDetails from '../components/documents/ExtractionDetails.jsx'
import Card from '../components/ui/Card.jsx'
import Button from '../components/ui/Button.jsx'
import Spinner from '../components/ui/Spinner.jsx'

/** How often to re-poll while the pipeline is still working on this document. */
const POLL_MS = 3000
const IN_FLIGHT = new Set(['pending', 'processing'])

export default function DocumentDetail() {
  const { id } = useParams()
  const navigate = useNavigate()

  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [fullText, setFullText] = useState(null)
  const [textLoading, setTextLoading] = useState(false)
  const [action, setAction] = useState(null) // 'reprocess' | 'delete'
  const [actionError, setActionError] = useState(null)

  const document = useApi(({ signal }) => fetchDocument(id, { signal }), { deps: [id] })
  const record = document.data
  const status = record?.status

  useDocumentTitle(record ? `${record.filename} · Hitech` : 'Document · Hitech')

  // Upload returns 202 and the pipeline finishes asynchronously, so a detail
  // page opened straight after an upload would otherwise sit on "pending"
  // forever until the user hit reload.
  const refetch = document.refetch
  const refetchRef = useRef(refetch)
  refetchRef.current = refetch

  useEffect(() => {
    if (!IN_FLIGHT.has(status)) return undefined
    const timer = setInterval(() => {
      refetchRef.current().catch(() => {})
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [status])

  const loadFullText = useCallback(async () => {
    setTextLoading(true)
    try {
      const data = await fetchDocumentText(id)
      setFullText(data.text)
    } catch (err) {
      setActionError(err)
    } finally {
      setTextLoading(false)
    }
  }, [id])

  async function handleReprocess() {
    setAction('reprocess')
    setActionError(null)
    try {
      await reprocessDocument(id)
      // Drop any text from the previous run: it is about to be replaced.
      setFullText(null)
      await refetch()
    } catch (err) {
      setActionError(err)
    } finally {
      setAction(null)
    }
  }

  async function handleDelete() {
    // Irreversible — it removes the record, the extraction and the stored file.
    if (!window.confirm(`Delete "${record?.filename}"? This cannot be undone.`)) return
    setAction('delete')
    setActionError(null)
    try {
      await deleteDocument(id)
      navigate('/documents', { replace: true })
    } catch (err) {
      setActionError(err)
      setAction(null)
    }
  }

  return (
    <div className="layout">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      <div className="layout__main">
        <Topbar onMenuClick={() => setSidebarOpen(true)} />

        <main className="content">
          <Link className="backlink" to="/documents">
            ← All documents
          </Link>

          {document.loading && !record && (
            <div className="content__loading">
              <Spinner size={28} />
              <span>Loading document…</span>
            </div>
          )}

          {document.error && (
            <Card className="content__error">
              <p>{document.error.message}</p>
              <Button variant="danger" onClick={() => refetch().catch(() => {})}>
                Try again
              </Button>
            </Card>
          )}

          {record && (
            <>
              <div className="content__header">
                <div>
                  <h1 className="content__title">{record.filename}</h1>
                  <p className="content__subtitle">
                    Uploaded by {record.owner?.full_name || record.owner?.email} ·{' '}
                    {formatDateTime(record.created_at)}
                  </p>
                </div>
                <div className="content__actions">
                  <StatusBadge status={record.status} />
                  <Button
                    variant="ghost"
                    className="btn--sm"
                    loading={action === 'reprocess'}
                    disabled={IN_FLIGHT.has(record.status) || action !== null}
                    onClick={handleReprocess}
                    title={
                      IN_FLIGHT.has(record.status)
                        ? 'Already queued — nothing to retry yet'
                        : 'Run the pipeline again'
                    }
                  >
                    ↻ Reprocess
                  </Button>
                  <Button
                    variant="danger"
                    className="btn--sm"
                    loading={action === 'delete'}
                    disabled={action !== null}
                    onClick={handleDelete}
                  >
                    Delete
                  </Button>
                </div>
              </div>

              {actionError && (
                <Card className="content__error">
                  <p>{actionError.message}</p>
                </Card>
              )}

              {record.error && (
                <Card className="panel panel--error">
                  <h3 className="panel__title">Processing failed</h3>
                  <p className="panel__summary">{record.error.message}</p>
                  <p className="panel__meta">Error code: {record.error.code}</p>
                </Card>
              )}

              <Card className="panel">
                <h3 className="panel__title">Document</h3>
                <dl className="kv">
                  <div className="kv__item">
                    <dt>Type</dt>
                    <dd>{record.document_type ? humanize(record.document_type) : '—'}</dd>
                  </div>
                  <div className="kv__item">
                    <dt>Pages</dt>
                    <dd>{record.page_count ?? '—'}</dd>
                  </div>
                  <div className="kv__item">
                    <dt>Size</dt>
                    <dd>{formatBytes(record.size_bytes)}</dd>
                  </div>
                  <div className="kv__item">
                    <dt>Content type</dt>
                    <dd>{record.content_type}</dd>
                  </div>
                  <div className="kv__item">
                    <dt>Processing time</dt>
                    <dd>{formatDuration(record.processing_duration_ms)}</dd>
                  </div>
                  <div className="kv__item">
                    <dt>Attempts</dt>
                    <dd>{record.attempt_count}</dd>
                  </div>
                  <div className="kv__item">
                    <dt>Uploaded by</dt>
                    <dd>{record.owner?.email ?? '—'}</dd>
                  </div>
                  <div className="kv__item">
                    <dt>Last updated</dt>
                    <dd>{formatDateTime(record.updated_at)}</dd>
                  </div>
                  <div className="kv__item">
                    {/* The dedup key: the same file uploaded twice returns the
                        original record, and this is how you confirm that. */}
                    <dt>Checksum (SHA-256)</dt>
                    <dd title={record.checksum_sha256}>
                      {shortChecksum(record.checksum_sha256)}
                    </dd>
                  </div>
                </dl>
              </Card>

              {IN_FLIGHT.has(record.status) && (
                <Card className="panel panel--info">
                  <div className="panel__inline">
                    <Spinner size={18} />
                    <span>
                      Extraction is {record.status}. This page refreshes itself every{' '}
                      {POLL_MS / 1000} seconds.
                    </span>
                  </div>
                </Card>
              )}

              {record.extraction && <ExtractionDetails extraction={record.extraction} />}

              {record.extraction && (
                <Card className="panel">
                  <div className="panel__head">
                    <h3 className="panel__title">Extracted text</h3>
                    {fullText === null && (
                      <Button
                        variant="ghost"
                        className="btn--sm"
                        loading={textLoading}
                        onClick={loadFullText}
                      >
                        Load full text (
                        {record.extraction.text_char_count?.toLocaleString('en-US')} chars)
                      </Button>
                    )}
                  </div>
                  {/* The detail response carries only a preview — full OCR text
                      can be megabytes, so it is fetched only when asked for. */}
                  <pre className="text-block">
                    {fullText ?? record.extraction.text_preview}
                  </pre>
                </Card>
              )}

              {record.events?.length > 0 && (
                <Card className="panel">
                  <h3 className="panel__title">Processing history</h3>
                  <ol className="timeline">
                    {record.events.map((event, index) => (
                      <li className="timeline__item" key={`${event.event}-${index}`}>
                        <span className="timeline__dot" aria-hidden />
                        <div>
                          <p className="timeline__event">{humanize(event.event)}</p>
                          {event.message && (
                            <p className="timeline__message">{event.message}</p>
                          )}
                          <span className="timeline__time">
                            {formatDateTime(event.created_at)}
                          </span>
                        </div>
                      </li>
                    ))}
                  </ol>
                </Card>
              )}
            </>
          )}
        </main>
      </div>
    </div>
  )
}
