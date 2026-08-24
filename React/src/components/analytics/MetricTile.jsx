import Card from '../ui/Card.jsx'

/**
 * A single headline figure.
 *
 * Deliberately *not* StatCard: that one shows a delta and a trend arrow, and
 * this endpoint reports a window rather than a comparison between two. Reusing
 * it would have meant inventing a delta to fill the slot.
 */
export default function MetricTile({ label, value, unit, hint, tone }) {
  return (
    <Card className={`tile ${tone ? `tile--${tone}` : ''}`}>
      <span className="tile__label">{label}</span>
      <span className="tile__value">
        {value}
        {unit && <span className="tile__unit">{unit}</span>}
      </span>
      {hint && <span className="tile__hint">{hint}</span>}
    </Card>
  )
}
