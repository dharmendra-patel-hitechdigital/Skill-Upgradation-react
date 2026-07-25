import Card from '../ui/Card.jsx'

function formatValue(value, format) {
  switch (format) {
    case 'currency':
      return '$' + value.toLocaleString('en-US')
    case 'percent':
      return value.toFixed(1) + '%'
    default:
      return value.toLocaleString('en-US')
  }
}

export default function StatCard({ stat }) {
  const positive = stat.trend === 'up'
  return (
    <Card className="stat-card">
      <div className="stat-card__head">
        <span className="stat-card__label">{stat.label}</span>
        <span className={`stat-card__delta stat-card__delta--${stat.trend}`}>
          {positive ? '▲' : '▼'} {Math.abs(stat.delta)}%
        </span>
      </div>
      <div className="stat-card__value">{formatValue(stat.value, stat.format)}</div>
      <div className="stat-card__foot">vs. last month</div>
    </Card>
  )
}
