import Card from '../ui/Card.jsx'
import {
  formatConfidence,
  formatDuration,
  humanize,
} from '../../lib/format.js'

/**
 * Everything the AI pipeline produced for one document.
 *
 * Renders each section only when it has content — an analyser that found no
 * entities should show nothing rather than an empty "Entities" heading that
 * reads like a rendering bug.
 *
 * `warnings` is shown *above* the summary on purpose: it carries the caveats
 * that decide whether the summary can be trusted at all (truncated input, an
 * unreadable page), so it must not sit at the bottom where it is missed.
 */
export default function ExtractionDetails({ extraction }) {
  const {
    summary,
    document_type: documentType,
    language,
    confidence,
    keywords = [],
    entities = [],
    fields = [],
    warnings = [],
    text_char_count: charCount,
    page_count: pageCount,
    ocr_provider: ocrProvider,
    ocr_duration_ms: ocrMs,
    analysis_provider: analysisProvider,
    analysis_model: analysisModel,
    analysis_duration_ms: analysisMs,
    prompt_tokens: promptTokens,
    completion_tokens: completionTokens,
  } = extraction

  return (
    <>
      {warnings.length > 0 && (
        <Card className="panel panel--warn">
          <h3 className="panel__title">Warnings from the analyser</h3>
          <ul className="panel__list">
            {warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </Card>
      )}

      <Card className="panel">
        <div className="panel__head">
          <h3 className="panel__title">AI analysis</h3>
          <div className="panel__pills">
            {documentType && <span className="pill">{humanize(documentType)}</span>}
            {language && <span className="pill">{language.toUpperCase()}</span>}
            <span className="pill pill--muted">
              Confidence {formatConfidence(confidence)}
            </span>
          </div>
        </div>

        <p className="panel__summary">{summary || 'The analyser produced no summary.'}</p>

        {keywords.length > 0 && (
          <div className="chips">
            {keywords.map((keyword) => (
              <span className="chip" key={keyword}>
                {keyword}
              </span>
            ))}
          </div>
        )}
      </Card>

      {fields.length > 0 && (
        <Card className="panel">
          <h3 className="panel__title">Extracted fields</h3>
          <div className="table-wrap">
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Field</th>
                  <th>Value</th>
                  <th className="table__num">Confidence</th>
                </tr>
              </thead>
              <tbody>
                {fields.map((field, index) => (
                  <tr key={`${field.key}-${index}`}>
                    <td className="table__key">{humanize(field.key)}</td>
                    <td>{field.value ?? '—'}</td>
                    <td className="table__num">{formatConfidence(field.confidence)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      {entities.length > 0 && (
        <Card className="panel">
          <h3 className="panel__title">Entities</h3>
          <div className="table-wrap">
            <table className="table table--compact">
              <thead>
                <tr>
                  <th>Text</th>
                  <th>Type</th>
                  <th className="table__num">Confidence</th>
                </tr>
              </thead>
              <tbody>
                {entities.map((entity, index) => (
                  <tr key={`${entity.text}-${index}`}>
                    <td>{entity.text}</td>
                    <td><span className="pill pill--muted">{humanize(entity.type)}</span></td>
                    <td className="table__num">{formatConfidence(entity.confidence)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}

      <Card className="panel">
        <h3 className="panel__title">How this was produced</h3>
        {/* Provenance, not decoration: "why is this summary poor?" is usually
            answered by which engine actually ran, and the token counts are what
            a spend question comes down to. */}
        <dl className="kv">
          <div className="kv__item">
            <dt>Text extraction</dt>
            <dd>{ocrProvider} · {formatDuration(ocrMs)}</dd>
          </div>
          <div className="kv__item">
            <dt>Analysis</dt>
            <dd>
              {analysisProvider}
              {analysisModel ? ` · ${analysisModel}` : ''} · {formatDuration(analysisMs)}
            </dd>
          </div>
          <div className="kv__item">
            <dt>Pages read</dt>
            <dd>{pageCount ?? '—'}</dd>
          </div>
          <div className="kv__item">
            <dt>Characters extracted</dt>
            <dd>{charCount?.toLocaleString('en-US') ?? '—'}</dd>
          </div>
          <div className="kv__item">
            <dt>Prompt tokens</dt>
            <dd>{promptTokens?.toLocaleString('en-US') ?? '—'}</dd>
          </div>
          <div className="kv__item">
            <dt>Completion tokens</dt>
            <dd>{completionTokens?.toLocaleString('en-US') ?? '—'}</dd>
          </div>
        </dl>
      </Card>

    </>
  )
}
