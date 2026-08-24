import { useEffect, useState } from 'react'
import { useApi } from '../hooks/useApi.js'
import { useDocumentTitle } from '../hooks/useDocumentTitle.js'
import { fetchAISettings, updateAISettings } from '../api/settings.api.js'
import { formatDateTime } from '../lib/format.js'
import Sidebar from '../components/layout/Sidebar.jsx'
import Topbar from '../components/layout/Topbar.jsx'
import Card from '../components/ui/Card.jsx'
import Button from '../components/ui/Button.jsx'
import Spinner from '../components/ui/Spinner.jsx'

/** Sentinel for "no override" — a radio value cannot be null. */
const USE_DEFAULT = '__default__'

export default function Settings() {
  useDocumentTitle('Settings · Hitech')
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [choice, setChoice] = useState(null)
  const [saving, setSaving] = useState(false)
  const [saveError, setSaveError] = useState(null)
  const [saved, setSaved] = useState(false)

  const settings = useApi(({ signal }) => fetchAISettings({ signal }))
  const data = settings.data

  // Seed the radio from the server once it answers, and re-seed after a save so
  // the form reflects what was actually stored rather than what was clicked.
  useEffect(() => {
    if (data) setChoice(data.selected ?? USE_DEFAULT)
  }, [data])

  const dirty = data != null && choice !== (data.selected ?? USE_DEFAULT)

  async function save() {
    setSaving(true)
    setSaveError(null)
    setSaved(false)
    try {
      await updateAISettings(choice === USE_DEFAULT ? null : choice)
      await settings.refetch()
      setSaved(true)
    } catch (err) {
      setSaveError(err)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="layout">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      <div className="layout__main">
        <Topbar onMenuClick={() => setSidebarOpen(true)} />

        <main className="content">
          <div className="content__header">
            <div>
              <h1 className="content__title">Settings</h1>
              <p className="content__subtitle">
                Runtime configuration for this installation. Administrators only.
              </p>
            </div>
          </div>

          {settings.error && (
            <Card className="content__error">
              <p>{settings.error.message}</p>
              <Button variant="danger" onClick={() => settings.refetch().catch(() => {})}>
                Try again
              </Button>
            </Card>
          )}

          {settings.loading && !data && (
            <div className="content__loading">
              <Spinner size={28} />
              <span>Loading settings…</span>
            </div>
          )}

          {data && (
            <Card className="panel">
              <div className="panel__head">
                <div>
                  <h3 className="panel__title">Document analysis engine</h3>
                  <p className="panel__meta">
                    Which AI engine reads uploaded documents and extracts their
                    fields.
                  </p>
                </div>
                <span className="pill">
                  Currently running: {data.effective}
                </span>
              </div>

              <fieldset className="choices" disabled={saving}>
                <legend className="sr-only">Analysis engine</legend>

                <label
                  className={`choice ${choice === USE_DEFAULT ? 'choice--on' : ''}`}
                >
                  <input
                    type="radio"
                    name="engine"
                    value={USE_DEFAULT}
                    checked={choice === USE_DEFAULT}
                    onChange={() => setChoice(USE_DEFAULT)}
                  />
                  <span className="choice__body">
                    <span className="choice__label">
                      Use the deployment default
                      <span className="pill pill--muted">{data.default}</span>
                    </span>
                    <span className="choice__desc">
                      Follow whatever <code>LLM_PROVIDER</code> is set to in this
                      environment. Choose this to stop overriding it.
                    </span>
                  </span>
                </label>

                {data.options.map((option) => (
                  <label
                    key={option.id}
                    className={
                      `choice ${choice === option.id ? 'choice--on' : ''} ` +
                      `${option.available ? '' : 'choice--off'}`
                    }
                  >
                    <input
                      type="radio"
                      name="engine"
                      value={option.id}
                      checked={choice === option.id}
                      /* An engine with no credentials is rejected server-side
                         anyway; disabling it here turns a 422 into a visible
                         reason. */
                      disabled={!option.available}
                      onChange={() => setChoice(option.id)}
                    />
                    <span className="choice__body">
                      <span className="choice__label">
                        {option.label}
                        {option.model && (
                          <span className="pill pill--muted">{option.model}</span>
                        )}
                      </span>
                      <span className="choice__desc">{option.description}</span>
                      {!option.available && (
                        <span className="choice__warn">
                          {option.unavailable_reason} Add it to the environment
                          (or AWS Secrets Manager) and redeploy — keys cannot be
                          set from this screen.
                        </span>
                      )}
                    </span>
                  </label>
                ))}
              </fieldset>

              {saveError && (
                <p className="choices__error">{saveError.message}</p>
              )}
              {saved && !dirty && (
                <p className="choices__ok">
                  Saved. This applies to the next document processed — anything
                  already in the pipeline finishes on the engine it started with,
                  and nothing is reprocessed automatically.
                </p>
              )}

              <div className="choices__actions">
                <Button loading={saving} disabled={!dirty} onClick={save}>
                  Save engine
                </Button>
                {dirty && (
                  <Button
                    variant="ghost"
                    className="btn--sm"
                    disabled={saving}
                    onClick={() => setChoice(data.selected ?? USE_DEFAULT)}
                  >
                    Cancel
                  </Button>
                )}
              </div>

              {data.updated_at && (
                <p className="panel__meta">
                  Last changed {formatDateTime(data.updated_at)}
                  {data.updated_by ? ` by ${data.updated_by}` : ''}.
                </p>
              )}
            </Card>
          )}
        </main>
      </div>
    </div>
  )
}
