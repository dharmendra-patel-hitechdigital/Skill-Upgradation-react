import Card from '../ui/Card.jsx'

/**
 * Lightweight bar chart rendered with plain divs — no chart dependency.
 *
 * The captions come from the response's `meta` rather than being hard-coded:
 * the server owns what the series actually measures, so the heading cannot drift
 * out of sync with the numbers under it. The literals below are only the
 * fallback for a response that carries no `meta`.
 */
export default function RevenueChart({ series = [], meta }) {
  const max = Math.max(...series.map((d) => d.value), 1)
  const unit = meta?.unit ?? ''

  return (
    <Card className="chart-card">
      <div className="chart-card__head">
        <div>
          <h3 className="chart-card__title">{meta?.title ?? 'Volume Overview'}</h3>
          <p className="chart-card__subtitle">
            {meta?.subtitle ?? 'Monthly totals'}
          </p>
        </div>
        <span className="chart-card__legend">{meta?.year ?? new Date().getFullYear()}</span>
      </div>

      <div className="chart">
        {series.map((d) => (
          <div className="chart__col" key={d.label}>
            <div className="chart__bar-wrap">
              <div
                className="chart__bar"
                style={{ height: `${(d.value / max) * 100}%` }}
                title={`${d.label}: ${d.value}${unit ? ` ${unit}` : ''}`}
              />
            </div>
            <span className="chart__label">{d.label}</span>
          </div>
        ))}
      </div>
    </Card>
  )
}
