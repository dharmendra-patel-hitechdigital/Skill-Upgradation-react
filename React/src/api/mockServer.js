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

// The dashboard fixtures below mirror the real GET /dashboard/* responses field
// for field. They used to describe a storefront (revenue, orders, refunds),
// which meant offline development exercised a contract the backend never
// served — the shape looked fine and every real deployment then showed the
// wrong thing. Keep these in step with app/schemas/dashboard.py.
const COMPARISON = 'vs. previous 30 days'

const STATS = [
  { id: 'documents', label: 'Documents Processed', value: 128, format: 'number', delta: 12.5, trend: 'up', unit: null, comparison: COMPARISON },
  { id: 'pages', label: 'Pages Extracted', value: 964, format: 'number', delta: 8.2, trend: 'up', unit: null, comparison: COMPARISON },
  { id: 'data', label: 'Data Processed', value: 41.6, format: 'number', delta: -3.1, trend: 'down', unit: 'MB', comparison: COMPARISON },
  { id: 'success_rate', label: 'Success Rate', value: 96.1, format: 'percent', delta: 1.4, trend: 'up', unit: null, comparison: COMPARISON },
]

const VOLUME_SERIES = [
  { label: 'Jan', value: 42 },
  { label: 'Feb', value: 51 },
  { label: 'Mar', value: 48 },
  { label: 'Apr', value: 63 },
  { label: 'May', value: 59 },
  { label: 'Jun', value: 74 },
  { label: 'Jul', value: 69 },
  { label: 'Aug', value: 84 },
]

const SERIES_META = {
  title: 'Document Volume',
  subtitle: 'Documents uploaded per month, last 8 months',
  unit: 'documents',
  total: VOLUME_SERIES.reduce((sum, point) => sum + point.value, 0),
  year: new Date().getFullYear(),
}

const ACTIVITY = [
  { id: 412, user: 'Aarav Sharma', action: 'completed extraction on invoice-q3.pdf', amount: '248 KB', time: '2 min ago', type: 'completed', document_id: 87, filename: 'invoice-q3.pdf', document_type: 'invoice' },
  { id: 411, user: 'Mia Chen', action: 'uploaded lease-agreement.pdf', amount: '1.4 MB', time: '18 min ago', type: 'upload', document_id: 86, filename: 'lease-agreement.pdf', document_type: null },
  { id: 410, user: 'Liam Patel', action: 'failed to process scan-0042.png', amount: 'failed', time: '1 hr ago', type: 'failed', document_id: 85, filename: 'scan-0042.png', document_type: null },
  { id: 409, user: 'Sofia Rossi', action: 'queued a reprocess of receipt-aug.pdf', amount: '96 KB', time: '3 hrs ago', type: 'reprocess', document_id: 84, filename: 'receipt-aug.pdf', document_type: 'receipt' },
  { id: 408, user: 'Noah Kim', action: 'started processing contract-v2.pdf', amount: '512 KB', time: '5 hrs ago', type: 'processing', document_id: 83, filename: 'contract-v2.pdf', document_type: 'contract' },
]

const delay = (ms) => new Promise((r) => setTimeout(r, ms))

export function isMockEnabled(baseUrl) {
  return !baseUrl
}

export async function handleMockRequest(path, { method, body, token }) {
  await delay(600) // simulate latency

  // --- Auth ---
  if (path === '/auth/login' && method === 'POST') {
    // `username` is what the real backend's OAuth2 form expects, and what
    // auth.api.js therefore sends. `email` stays accepted so an older caller
    // does not silently fail here.
    const { username, email, password } = body || {}
    const identifier = username ?? email
    if (identifier?.trim().toLowerCase() === DEMO_USER.email && password === DEMO_PASSWORD) {
      // Mirrors the real response so auth.api.js can map both the same way.
      return {
        access_token: 'mock-jwt-token.' + btoa(identifier),
        refresh_token: 'mock-refresh-token.' + btoa(identifier),
        token_type: 'bearer',
        expires_in: 1800,
        user: DEMO_USER,
      }
    }
    throw new ApiError('Invalid email or password.', 401)
  }

  if (path === '/auth/register' && method === 'POST') {
    const { email, full_name: fullName, password } = body || {}
    if (email?.trim().toLowerCase() === DEMO_USER.email) {
      throw new ApiError('That email is already registered.', 409)
    }
    if (!password || password.length < 10) {
      throw new ApiError('Password must be at least 10 characters long.', 422)
    }
    // Returns the created profile and no tokens, exactly as the real endpoint
    // does, so the sign-in that follows is exercised offline too.
    return {
      ...DEMO_USER,
      id: 2,
      email: email.trim().toLowerCase(),
      full_name: fullName ?? null,
      role: 'user',
    }
  }

  // Everything below requires a valid token.
  if (!token) throw new ApiError('Not authenticated.', 401)

  // Matches the real backend: the profile lives under /users, not /auth, and
  // the user object is returned unwrapped.
  if (path === '/users/me' && method === 'GET') {
    return DEMO_USER
  }

  if (path === '/dashboard/stats' && method === 'GET') {
    return { stats: STATS, window_days: 30, generated_at: new Date().toISOString() }
  }

  // Still /revenue: the deployed bundle requests that path, so the real backend
  // serves the volume series from it too rather than breaking older clients.
  if (path === '/dashboard/revenue' && method === 'GET') {
    return { series: VOLUME_SERIES, meta: SERIES_META }
  }

  if (path === '/dashboard/activity' && method === 'GET') {
    return { activity: ACTIVITY }
  }

  throw new ApiError(`Mock endpoint not found: ${method} ${path}`, 404)
}
