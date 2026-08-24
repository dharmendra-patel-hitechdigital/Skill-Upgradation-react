/**
 * Per-day throughput, with failures stacked on top of completions.
 *
 * Stacked rather than two series side by side: the question is "how much came in
 * and how much of it went wrong", and a stacked bar answers both from one
 * height. Failures sit on top so they are the part that catches the eye.
 *
 * Only every nth date is labelled — 90 rotated labels are unreadable, and a
 * label per bar is not what the axis is for.
 */
export default function DayBars({ days }) {
  if (days.length === 0) {
    return <p className="empty__text">No documents were uploaded in this window.</p>
  }

  const max = Math.max(...days.map((day) => day.documents), 1)
  // Aim for roughly 8 labels regardless of window length.
  const labelEvery = Math.max(1, Math.ceil(days.length / 8))

  return (
    <div className="daybars">
      {days.map((day, index) => {
        // `documents` includes pending/processing, so the remainder below the
        // completed+failed stack is work still in flight — shown as a lighter
        // band rather than silently rounded away.
        const inFlight = Math.max(0, day.documents - day.completed - day.failed)
        return (
          <div className="daybars__col" key={day.date}>
            <div
              className="daybars__stack"
              style={{ height: `${(day.documents / max) * 100}%` }}
              title={
                `${day.date}: ${day.documents} uploaded, ` +
                `${day.completed} completed, ${day.failed} failed` +
                (inFlight > 0 ? `, ${inFlight} in progress` : '')
              }
            >
              {day.failed > 0 && (
                <div
                  className="daybars__seg daybars__seg--failed"
                  style={{ flexBasis: `${(day.failed / day.documents) * 100}%` }}
                />
              )}
              {inFlight > 0 && (
                <div
                  className="daybars__seg daybars__seg--progress"
                  style={{ flexBasis: `${(inFlight / day.documents) * 100}%` }}
                />
              )}
              {day.completed > 0 && (
                <div
                  className="daybars__seg daybars__seg--done"
                  style={{ flexBasis: `${(day.completed / day.documents) * 100}%` }}
                />
              )}
            </div>
            <span className="daybars__label">
              {index % labelEvery === 0 ? day.date.slice(5) : ''}
            </span>
          </div>
        )
      })}
    </div>
  )
}
