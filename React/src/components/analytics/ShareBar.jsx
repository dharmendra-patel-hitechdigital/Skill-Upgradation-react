/**
 * Horizontal stacked bar for a set of parts that sum to a whole.
 *
 * Segments are sized by share of the total rather than by a caller-supplied
 * percentage, so the bar cannot disagree with the numbers in its own legend.
 * A zero-value segment renders no band but keeps its legend entry — "0 failed"
 * is information, and dropping the row would make the reader look for it.
 *
 * @param {{ segments: Array<{label: string, value: number, tone: string}> }} props
 */
export default function ShareBar({ segments, formatValue = (value) => value }) {
  const total = segments.reduce((sum, segment) => sum + (segment.value || 0), 0)

  return (
    <div className="sharebar">
      <div className="sharebar__track" role="img" aria-label={
        segments.map((s) => `${s.label}: ${s.value}`).join(', ')
      }>
        {total === 0 ? (
          <div className="sharebar__empty" />
        ) : (
          segments
            .filter((segment) => segment.value > 0)
            .map((segment) => (
              <div
                key={segment.label}
                className={`sharebar__seg sharebar__seg--${segment.tone}`}
                style={{ width: `${(segment.value / total) * 100}%` }}
                title={`${segment.label}: ${formatValue(segment.value)}`}
              />
            ))
        )}
      </div>

      <ul className="sharebar__legend">
        {segments.map((segment) => (
          <li className="sharebar__key" key={segment.label}>
            <span className={`sharebar__dot sharebar__dot--${segment.tone}`} aria-hidden />
            {segment.label}
            <strong>{formatValue(segment.value)}</strong>
            {total > 0 && (
              <span className="sharebar__pct">
                {Math.round((segment.value / total) * 100)}%
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
