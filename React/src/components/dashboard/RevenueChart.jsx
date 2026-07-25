import Card from '../ui/Card.jsx'

/** Lightweight bar chart rendered with plain divs — no chart dependency. */
export default function RevenueChart({ series = [] }) {
  const max = Math.max(...series.map((d) => d.value), 1)

  return (
    <Card className="chart-card">
      <div className="chart-card__head">
        <div>
          <h3 className="chart-card__title">Revenue Overview</h3>
          <p className="chart-card__subtitle">Monthly revenue, in thousands</p>
        </div>
        <span className="chart-card__legend">2026</span>
      </div>

      <div className="chart">
        {series.map((d) => (
          <div className="chart__col" key={d.label}>
            <div className="chart__bar-wrap">
              <div
                className="chart__bar"
                style={{ height: `${(d.value / max) * 100}%` }}
                title={`${d.label}: ${d.value}k`}
              />
            </div>
            <span className="chart__label">{d.label}</span>
          </div>
        ))}
      </div>
    </Card>
  )
}
