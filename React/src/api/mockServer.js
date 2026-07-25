/**
 * In-memory mock backend.
 *
 * Lets the app run end-to-end with zero infrastructure. When you point
 * VITE_API_BASE_URL at a real server, this module is bypassed entirely
 * (see client.js) and you can delete it.
 */
import { ApiError } from './client.js'

const DEMO_USER = {
  id: 'usr_001',
  name: 'Dharmendra Patel',
  email: 'demo@hitech.com',
  role: 'Administrator',
  avatar: 'DP',
}

const DEMO_PASSWORD = 'password123'

const STATS = [
  { id: 'revenue', label: 'Total Revenue', value: 84230, format: 'currency', delta: 12.5, trend: 'up' },
  { id: 'users', label: 'Active Users', value: 2841, format: 'number', delta: 8.2, trend: 'up' },
  { id: 'orders', label: 'New Orders', value: 1259, format: 'number', delta: -3.1, trend: 'down' },
  { id: 'conversion', label: 'Conversion Rate', value: 4.7, format: 'percent', delta: 1.4, trend: 'up' },
]

const REVENUE_SERIES = [
  { label: 'Jan', value: 42 },
  { label: 'Feb', value: 51 },
  { label: 'Mar', value: 48 },
  { label: 'Apr', value: 63 },
  { label: 'May', value: 59 },
  { label: 'Jun', value: 74 },
  { label: 'Jul', value: 69 },
  { label: 'Aug', value: 84 },
]

const ACTIVITY = [
  { id: 1, user: 'Aarav Sharma', action: 'placed an order', amount: '$1,240', time: '2 min ago', type: 'order' },
  { id: 2, user: 'Mia Chen', action: 'upgraded to Pro plan', amount: '$49', time: '18 min ago', type: 'upgrade' },
  { id: 3, user: 'Liam Patel', action: 'requested a refund', amount: '$320', time: '1 hr ago', type: 'refund' },
  { id: 4, user: 'Sofia Rossi', action: 'created an account', amount: '', time: '3 hr ago', type: 'signup' },
  { id: 5, user: 'Noah Kim', action: 'placed an order', amount: '$890', time: '5 hr ago', type: 'order' },
]

const delay = (ms) => new Promise((r) => setTimeout(r, ms))

export function isMockEnabled(baseUrl) {
  return !baseUrl
}

export async function handleMockRequest(path, { method, body, token }) {
  await delay(600) // simulate latency

  // --- Auth ---
  if (path === '/auth/login' && method === 'POST') {
    const { email, password } = body || {}
    if (email?.trim().toLowerCase() === DEMO_USER.email && password === DEMO_PASSWORD) {
      return { token: 'mock-jwt-token.' + btoa(email), user: DEMO_USER }
    }
    throw new ApiError('Invalid email or password.', 401)
  }

  // Everything below requires a valid token.
  if (!token) throw new ApiError('Not authenticated.', 401)

  if (path === '/auth/me' && method === 'GET') {
    return { user: DEMO_USER }
  }

  if (path === '/dashboard/stats' && method === 'GET') {
    return { stats: STATS }
  }

  if (path === '/dashboard/revenue' && method === 'GET') {
    return { series: REVENUE_SERIES }
  }

  if (path === '/dashboard/activity' && method === 'GET') {
    return { activity: ACTIVITY }
  }

  throw new ApiError(`Mock endpoint not found: ${method} ${path}`, 404)
}
