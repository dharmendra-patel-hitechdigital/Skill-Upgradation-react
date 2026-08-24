import { useEffect, useMemo, useState } from 'react'
import { useAuth } from '../hooks/useAuth.js'
import { useApi } from '../hooks/useApi.js'
import { useDocumentTitle } from '../hooks/useDocumentTitle.js'
import { fetchDocuments } from '../api/documents.api.js'
import { isAdmin } from '../lib/roles.js'
import Sidebar from '../components/layout/Sidebar.jsx'
import Topbar from '../components/layout/Topbar.jsx'
import DocumentToolbar from '../components/documents/DocumentToolbar.jsx'
import DocumentTable from '../components/documents/DocumentTable.jsx'
import Card from '../components/ui/Card.jsx'
import Button from '../components/ui/Button.jsx'
import Spinner from '../components/ui/Spinner.jsx'

const PAGE_SIZE = 20

const EMPTY_FILTERS = {
  search: '',
  ownerEmail: '',
  status: '',
  documentType: '',
  sortBy: 'created_at',
  sortDir: 'desc',
}

/** Free-text inputs debounce; selects apply immediately. */
const TEXT_DEBOUNCE_MS = 350

export default function Documents() {
  useDocumentTitle('Documents · Hitech')
  const { user } = useAuth()
  const admin = isAdmin(user)

  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [draft, setDraft] = useState(EMPTY_FILTERS)
  const [applied, setApplied] = useState(EMPTY_FILTERS)
  const [page, setPage] = useState(1)

  // Debounce only what is typed. Without this, every keystroke in the filename
  // box is a request, and the responses can land out of order.
  useEffect(() => {
    const timer = setTimeout(() => setApplied(draft), TEXT_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [draft])

  // Any filter change invalidates the current page number: staying on page 4 of
  // a freshly narrowed result set shows an empty table that looks like an error.
  useEffect(() => {
    setPage(1)
  }, [applied])

  const query = useMemo(
    () => ({
      ...applied,
      // Sending owner_email as a regular user is harmless (the server still
      // scopes to them) but it is a filter that can only return nothing, so it
      // is dropped rather than silently emptying their list.
      ownerEmail: admin ? applied.ownerEmail : '',
      page,
      pageSize: PAGE_SIZE,
    }),
    [applied, admin, page],
  )

  const documents = useApi(({ signal }) => fetchDocuments(query, { signal }), {
    deps: [query],
  })

  const items = documents.data?.items ?? []
  const meta = documents.data?.meta ?? null

  return (
    <div className="layout">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      <div className="layout__main">
        <Topbar onMenuClick={() => setSidebarOpen(true)} />

        <main className="content">
          <div className="content__header">
            <div>
              <h1 className="content__title">Documents</h1>
              <p className="content__subtitle">
                {admin
                  ? 'Every document processed on this installation, across all users.'
                  : 'Every document you have uploaded, with its extraction results.'}
              </p>
            </div>
          </div>

          {documents.error && (
            <Card className="content__error">
              <p>{documents.error.message}</p>
              <Button variant="danger" onClick={() => documents.refetch().catch(() => {})}>
                Try again
              </Button>
            </Card>
          )}

          <Card className="panel">
            <DocumentToolbar
              value={draft}
              onChange={setDraft}
              showOwner={admin}
              total={meta?.total}
              busy={documents.loading}
              onRefresh={() => documents.refetch().catch(() => {})}
            />

            {documents.loading ? (
              <div className="content__loading">
                <Spinner size={24} />
                <span>Loading documents…</span>
              </div>
            ) : (
              <DocumentTable documents={items} showOwner={admin} />
            )}
          </Card>

          {meta && meta.total_pages > 1 && (
            <div className="pager">
              <Button
                variant="ghost"
                className="btn--sm"
                disabled={!meta.has_previous || documents.loading}
                onClick={() => setPage((current) => Math.max(1, current - 1))}
              >
                ← Previous
              </Button>
              <span className="pager__label">
                Page {meta.page} of {meta.total_pages}
              </span>
              <Button
                variant="ghost"
                className="btn--sm"
                disabled={!meta.has_next || documents.loading}
                onClick={() => setPage((current) => current + 1)}
              >
                Next →
              </Button>
            </div>
          )}
        </main>
      </div>
    </div>
  )
}
