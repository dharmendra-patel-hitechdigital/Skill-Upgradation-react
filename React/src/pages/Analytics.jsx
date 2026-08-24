import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useApi } from '../hooks/useApi.js'
import { useDocumentTitle } from '../hooks/useDocumentTitle.js'
import { ANALYTICS_WINDOWS, fetchDocumentAnalytics } from '../api/analytics.api.js'
import { formatBytes, formatDuration, formatConfidence, humanize } from '../lib/format.js'
import Sidebar from '../components/layout/Sidebar.jsx'
import Topbar from '../components/layout/Topbar.jsx'
import MetricTile from '../components/analytics/MetricTile.jsx'
import ShareBar from '../components/analytics/ShareBar.jsx'
import DayBars from '../components/analytics/DayBars.jsx'
import Card from '../components/ui/Card.jsx'
import Button from '../components/ui/Button.jsx'
import Spinner from '../components/ui/Spinner.jsx'

const count = (value) => (value ?? 0).toLocaleString('en-US')

export default function Analytics() {
  useDocumentTitle('Analytics · Hitech')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [windowDays, setWindowDays] = useState(30)

  const report = useApi(
    ({ signal }) => fetchDocumentAnalytics(windowDays, { signal }),
    { deps: [windowDays] },
  )

  const data = report.data
  const totals = data?.totals
  const performance = data?.performance

  return (
    <div className="layout">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      <div className="layout__main">
        <Topbar onMenuClick={() => setSidebarOpen(true)} />

        <main className="content">
          <div className="content__header">
            <div>
              <h1 className="content__title">Document analytics</h1>
              <p className="content__subtitle">
                {data?.scope === 'installation'
                  ? 'Every document processed on this installation.'
                  : 'The documents you have uploaded.'}
              </p>
            </div>
            <div className="content__actions">
              {/* Window is a real query parameter, so each choice is a fresh
                  server-side aggregate rather than a client-side re-slice. */}
              <select
                className="toolbar__input"
                value={windowDays}
                onChange={(event) => setWindowDays(Number(event.target.value))}
                aria-label="Reporting window"
              >
                {ANALYTICS_WINDOWS.map((option) => (
                  <option key={option.days} value={option.days}>
                    {option.label}
                  </option>
                ))}
              </select>
              <Button
                variant="ghost"
                className="btn--sm"
                disabled={report.loading}
                onClick={() => report.refetch().catch(() => {})}
              >
                ↻ Refresh
              </Button>
            </div>
          </div>

          {report.error && (
            <Card className="content__error">
              <p>{report.error.message}</p>
              <Button variant="danger" onClick={() => report.refetch().catch(() => {})}>
                Try again
              </Button>
            </Card>
          )}

          {report.loading && !data && (
            <div className="content__loading">
              <Spinner size={28} />
              <span>Crunching {windowDays} days of documents…</span>
            </div>
          )}

          {data && (
            <>
              <section className="tile-grid">
                <MetricTile label="Documents" value={count(totals.documents)} />
                <MetricTile
                  label="Success rate"
                  value={totals.success_rate.toFixed(1)}
                  unit="%"
                  hint={`${count(totals.completed)} done · ${count(totals.failed)} failed`}
                  tone={totals.failed > 0 ? 'warn' : undefined}
                />
                <MetricTile label="Pages extracted" value={count(totals.pages)} />
                <MetricTile
                  label="Data processed"
                  value={formatBytes(totals.size_bytes)}
                />
                <MetricTile
                  label="Median pipeline time"
                  value={formatDuration(performance.p50_total_ms)}
                  hint={`p95 ${formatDuration(performance.p95_total_ms)}`}
                />
                <MetricTile
                  label="Repeated work"
                  value={count(totals.reprocessed)}
                  hint="documents processed more than once"
                  tone={totals.reprocessed > 0 ? 'warn' : undefined}
                />
              </section>

              <Card className="panel">
                <h3 className="panel__title">Outcome</h3>
                <ShareBar
                  formatValue={count}
                  segments={[
                    { label: 'Completed', value: totals.completed, tone: 'green' },
                    { label: 'Failed', value: totals.failed, tone: 'red' },
                    { label: 'In progress', value: totals.in_progress, tone: 'blue' },
                  ]}
                />
              </Card>

              {/* Failures first when there are any: it is the only panel here
                  that usually implies an action. */}
              {data.failures.length > 0 && (
                <Card className="panel panel--warn">
                  <h3 className="panel__title">
                    Why documents failed ({count(totals.failed)})
                  </h3>
                  <div className="table-wrap">
                    <table className="table table--compact">
                      <thead>
                        <tr>
                          <th>Error code</th>
                          <th className="table__num">Documents</th>
                          <th className="table__num">Share</th>
                          <th>Example message</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.failures.map((failure) => (
                          <tr key={failure.code}>
                            <td className="table__key">{humanize(failure.code)}</td>
                            <td className="table__num">{count(failure.documents)}</td>
                            <td className="table__num">{failure.share}%</td>
                            <td>{failure.example_message ?? '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <p className="panel__meta">
                    Shares are of failures, not of all documents.{' '}
                    <Link to="/documents?status=failed">See the failed documents →</Link>
                  </p>
                </Card>
              )}

              <Card className="panel">
                <h3 className="panel__title">What kind of documents arrive</h3>
                {data.by_type.length === 0 ? (
                  <p className="empty__text">Nothing classified yet in this window.</p>
                ) : (
                  <div className="table-wrap">
                    <table className="table table--compact">
                      <thead>
                        <tr>
                          <th>Type</th>
                          <th className="table__num">Documents</th>
                          <th>Share</th>
                          <th className="table__num">Avg confidence</th>
                          <th className="table__num">Avg pages</th>
                          <th className="table__num">Failed</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.by_type.map((entry) => (
                          <tr key={entry.document_type}>
                            <td className="table__key">{humanize(entry.document_type)}</td>
                            <td className="table__num">{count(entry.documents)}</td>
                            <td className="table__bar-cell">
                              <span className="minibar">
                                <span
                                  className="minibar__fill"
                                  style={{ width: `${entry.share}%` }}
                                />
                              </span>
                              <span className="table__sub">{entry.share}%</span>
                            </td>
                            <td className="table__num">
                              {formatConfidence(entry.avg_confidence)}
                            </td>
                            <td className="table__num">{entry.avg_pages ?? '—'}</td>
                            <td className="table__num">{count(entry.failed)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </Card>

              <Card className="panel">
                <h3 className="panel__title">Where pipeline time goes</h3>
                {performance.samples === 0 ? (
                  <p className="empty__text">No completed extractions in this window.</p>
                ) : (
                  <>
                    <ShareBar
                      formatValue={(value) => formatDuration(Math.round(value))}
                      segments={[
                        {
                          label: 'Text extraction',
                          value: Math.round(performance.avg_ocr_ms ?? 0),
                          tone: 'violet',
                        },
                        {
                          label: 'Analysis',
                          value: Math.round(performance.avg_analysis_ms ?? 0),
                          tone: 'blue',
                        },
                      ]}
                    />
                    <dl className="kv">
                      <div className="kv__item">
                        <dt>Median (p50)</dt>
                        <dd>{formatDuration(performance.p50_total_ms)}</dd>
                      </div>
                      <div className="kv__item">
                        <dt>95th percentile</dt>
                        <dd>{formatDuration(performance.p95_total_ms)}</dd>
                      </div>
                      <div className="kv__item">
                        <dt>Slowest</dt>
                        <dd>{formatDuration(performance.slowest_total_ms)}</dd>
                      </div>
                      <div className="kv__item">
                        <dt>Per page</dt>
                        <dd>
                          {formatDuration(
                            performance.avg_ms_per_page == null
                              ? null
                              : Math.round(performance.avg_ms_per_page),
                          )}
                        </dd>
                      </div>
                    </dl>
                    <p className="panel__meta">
                      Pipeline time only — it excludes time a document spent queued
                      behind other uploads. Based on {count(performance.samples)}{' '}
                      extraction{performance.samples === 1 ? '' : 's'}.
                    </p>
                  </>
                )}
              </Card>

              <Card className="panel">
                <h3 className="panel__title">Which engines actually ran</h3>
                {/* The AI layer degrades to the offline engines silently by
                    design, so this is the panel that reveals it happened. */}
                {data.providers.length === 0 ? (
                  <p className="empty__text">No extractions in this window.</p>
                ) : (
                  <div className="table-wrap">
                    <table className="table table--compact">
                      <thead>
                        <tr>
                          <th>Stage</th>
                          <th>Engine</th>
                          <th className="table__num">Documents</th>
                          <th className="table__num">Share of stage</th>
                        </tr>
                      </thead>
                      <tbody>
                        {data.providers.map((provider) => (
                          <tr key={`${provider.stage}-${provider.provider}`}>
                            <td>{humanize(provider.stage)}</td>
                            <td className="table__key">{provider.provider}</td>
                            <td className="table__num">{count(provider.documents)}</td>
                            <td className="table__num">{provider.share}%</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <p className="panel__meta">
                  {data.tokens.documents_with_tokens === 0
                    ? 'No tokens spent — every analysis ran on the built-in rule engine.'
                    : `${count(data.tokens.total_tokens)} tokens across ` +
                      `${count(data.tokens.documents_with_tokens)} documents ` +
                      `(${count(data.tokens.prompt_tokens)} prompt, ` +
                      `${count(data.tokens.completion_tokens)} completion).`}
                </p>
              </Card>

              <Card className="panel">
                <h3 className="panel__title">How confident the analyser was</h3>
                <div className="table-wrap">
                  <table className="table table--compact">
                    <tbody>
                      {data.confidence.map((bucket) => (
                        <tr key={bucket.label}>
                          <td className="table__key">{bucket.label}</td>
                          <td className="table__bar-cell">
                            <span className="minibar">
                              <span
                                className="minibar__fill"
                                style={{ width: `${bucket.share}%` }}
                              />
                            </span>
                          </td>
                          <td className="table__num">{count(bucket.documents)}</td>
                          <td className="table__num">{bucket.share}%</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <p className="panel__meta">
                  Anything in the lower two bands is worth a human check before the
                  extraction is used.
                </p>
              </Card>

              <Card className="panel">
                <h3 className="panel__title">Daily throughput</h3>
                <DayBars days={data.daily} />
                <div className="daybars__legend">
                  <span><i className="sharebar__dot sharebar__dot--green" /> Completed</span>
                  <span><i className="sharebar__dot sharebar__dot--blue" /> In progress</span>
                  <span><i className="sharebar__dot sharebar__dot--red" /> Failed</span>
                </div>
              </Card>

              {data.top_uploaders.length > 0 && (
                <Card className="panel">
                  <h3 className="panel__title">Busiest uploaders</h3>
                  <div className="table-wrap">
                    <table className="table table--compact">
                      <thead>
                        <tr>
                          <th>User</th>
                          <th className="table__num">Documents</th>
                          <th className="table__num">Failed</th>
                          <th aria-label="Actions" />
                        </tr>
                      </thead>
                      <tbody>
                        {data.top_uploaders.map((uploader) => (
                          <tr key={uploader.id}>
                            <td>
                              {uploader.full_name || '—'}
                              <span className="table__sub">{uploader.email}</span>
                            </td>
                            <td className="table__num">{count(uploader.documents)}</td>
                            <td className="table__num">{count(uploader.failed)}</td>
                            <td className="table__actions">
                              <Link
                                className="table__action"
                                to={`/documents?owner=${encodeURIComponent(uploader.email)}`}
                              >
                                View
                              </Link>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
              )}
            </>
          )}
        </main>
      </div>
    </div>
  )
}
