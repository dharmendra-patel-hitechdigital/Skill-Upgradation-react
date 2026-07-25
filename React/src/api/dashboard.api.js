import { api } from './client.js'

/** GET /dashboard/stats -> { stats } */
export function fetchStats(opts) {
  return api.get('/dashboard/stats', opts)
}

/** GET /dashboard/revenue -> { series } */
export function fetchRevenue(opts) {
  return api.get('/dashboard/revenue', opts)
}

/** GET /dashboard/activity -> { activity } */
export function fetchActivity(opts) {
  return api.get('/dashboard/activity', opts)
}
