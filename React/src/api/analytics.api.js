/**
 * Analytics endpoints.
 *
 * Shares and percentiles are computed server-side, so nothing here does maths —
 * two clients rounding independently is how a "share of failures" ends up not
 * summing to 100% and the whole screen stops being trusted.
 */
import { api } from './client.js'

/** Selectable reporting windows. The API accepts 1–365. */
export const ANALYTICS_WINDOWS = [
  { days: 7, label: 'Last 7 days' },
  { days: 30, label: 'Last 30 days' },
  { days: 90, label: 'Last 90 days' },
]

/**
 * GET /analytics/documents -> the full analytics payload.
 *
 * `scope` in the response says whether the figures cover the whole installation
 * (admin) or just the caller's own uploads — worth surfacing, because the same
 * screen means different things to the two roles.
 */
export function fetchDocumentAnalytics(windowDays = 30, opts) {
  return api.get(`/analytics/documents?window_days=${windowDays}`, opts)
}
