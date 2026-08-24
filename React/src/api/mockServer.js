/**
 * In-memory mock backend.
 *
 * Lets the app run end-to-end with zero infrastructure. When you point
 * VITE_API_BASE_URL at a real server, this module is bypassed entirely
 * (see client.js) and you can delete it.
 */
import { ApiError } from './client.js'

// Mirrors the real GET /users/me: an integer id, `full_name` (not `name`), and
// the lower-case role from the server's enum. The previous fixture invented
// `name`, `avatar` and `Administrator`, none of which the API returns — so the
// Topbar and the admin gate worked offline and broke against the real backend.
const DEMO_USER = {
  id: 1,
  email: 'demo@hitech.com',
  full_name: 'Dharmendra Patel',
  role: 'admin',
  is_active: true,
  created_at: '2026-07-01T09:00:00Z',
  updated_at: '2026-08-20T14:12:00Z',
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

/**
 * Document fixtures, shaped exactly like GET /documents and /documents/{id}.
 *
 * One per lifecycle state, because the states are what the UI branches on:
 * `completed` carries an extraction, `failed` carries an `error` and no
 * extraction, and the two in-flight states carry neither.
 */
const OTHER_USER = {
  id: 2,
  email: 'aarav@hitech.com',
  full_name: 'Aarav Sharma',
}

const DOCUMENTS = [
  {
    id: 87,
    filename: 'invoice-q3.pdf',
    content_type: 'application/pdf',
    size_bytes: 254_016,
    checksum_sha256: 'b1946ac92492d2347c6235b4d2611184e2b1f7b0d3c4a5e6f708192a3b4c5d6e',
    status: 'completed',
    document_type: 'invoice',
    page_count: 3,
    attempt_count: 1,
    processing_duration_ms: 4820,
    created_at: '2026-08-24T09:41:12Z',
    updated_at: '2026-08-24T09:41:17Z',
    owner: DEMO_USER,
    error: null,
    extraction: {
      document_type: 'invoice',
      language: 'en',
      summary:
        'A commercial invoice from Acme Corporation covering three line items of ' +
        'consulting work delivered in Q3 2026. The total due is $1,240.50, payable ' +
        'within 30 days of the issue date. Payment instructions reference a bank ' +
        'transfer to account ending 4471.',
      confidence: 0.94,
      keywords: ['invoice', 'consulting', 'Q3 2026', 'net 30', 'Acme Corporation'],
      entities: [
        { text: 'Acme Corporation', type: 'organization', confidence: 0.97 },
        { text: 'Priya Raghunathan', type: 'person', confidence: 0.88 },
        { text: '$1,240.50', type: 'money', confidence: 0.96 },
        { text: '2026-09-23', type: 'date', confidence: 0.91 },
        { text: 'billing@acme.example', type: 'email', confidence: 0.99 },
      ],
      fields: [
        { key: 'invoice_number', value: 'INV-2026-0842', confidence: 0.98 },
        { key: 'invoice_total', value: '1240.50', confidence: 0.96 },
        { key: 'currency', value: 'USD', confidence: 0.99 },
        { key: 'issue_date', value: '2026-08-24', confidence: 0.93 },
        { key: 'due_date', value: '2026-09-23', confidence: 0.91 },
        { key: 'payment_terms', value: 'Net 30', confidence: 0.85 },
      ],
      warnings: [],
      text_preview:
        'ACME CORPORATION\n123 Industrial Way, Pune 411001\n\nINVOICE INV-2026-0842\n' +
        'Issued: 24 August 2026        Due: 23 September 2026\n\n' +
        'Description                          Qty     Rate      Amount\n' +
        'Platform architecture review           12   62.50      750.00\n' +
        'Pipeline migration support              6   57.75      346.50\n' +
        'On-call handover documentation          2   72.00      144.00\n\n' +
        'Subtotal                                            1,240.50\n' +
        'Total due (USD)                                     1,240.50\n',
      text_char_count: 4127,
      page_count: 3,
      ocr_provider: 'local',
      ocr_duration_ms: 610,
      analysis_provider: 'heuristic',
      analysis_model: 'rules-v1',
      analysis_duration_ms: 4210,
      prompt_tokens: null,
      completion_tokens: null,
    },
    events: [
      { event: 'uploaded', message: 'Received 254016 bytes as application/pdf.', created_at: '2026-08-24T09:41:12Z' },
      { event: 'processing_started', message: 'Pipeline claimed the document.', created_at: '2026-08-24T09:41:13Z' },
      { event: 'processing_completed', message: 'Classified as invoice with confidence 0.94.', created_at: '2026-08-24T09:41:17Z' },
    ],
  },
  {
    id: 86,
    filename: 'lease-agreement.pdf',
    content_type: 'application/pdf',
    size_bytes: 1_468_006,
    checksum_sha256: 'c2a5f8d1e0b7469a3c8d2e5f7a9b1c3d5e7f9a1b3c5d7e9f1a3b5c7d9e1f3a5b',
    status: 'processing',
    document_type: null,
    page_count: null,
    attempt_count: 1,
    processing_duration_ms: null,
    created_at: '2026-08-24T09:23:04Z',
    updated_at: '2026-08-24T09:23:05Z',
    owner: OTHER_USER,
    error: null,
    extraction: null,
    events: [
      { event: 'uploaded', message: 'Received 1468006 bytes as application/pdf.', created_at: '2026-08-24T09:23:04Z' },
      { event: 'processing_started', message: 'Pipeline claimed the document.', created_at: '2026-08-24T09:23:05Z' },
    ],
  },
  {
    id: 85,
    filename: 'scan-0042.png',
    content_type: 'image/png',
    size_bytes: 892_311,
    checksum_sha256: 'd3b6a9c2e1f8570b4d9e3f6a8b0c2d4e6f8a0b2c4d6e8f0a2b4c6d8e0f2a4b6c',
    status: 'failed',
    document_type: null,
    page_count: null,
    attempt_count: 2,
    processing_duration_ms: 1180,
    created_at: '2026-08-24T08:52:41Z',
    updated_at: '2026-08-24T08:52:43Z',
    owner: OTHER_USER,
    error: {
      code: 'no_text_layer',
      message:
        'No text could be extracted. This looks like a scanned image with no text ' +
        'layer — enable AWS Textract to process it.',
    },
    extraction: null,
    events: [
      { event: 'uploaded', message: 'Received 892311 bytes as image/png.', created_at: '2026-08-24T08:52:41Z' },
      { event: 'processing_started', message: 'Pipeline claimed the document.', created_at: '2026-08-24T08:52:42Z' },
      { event: 'processing_failed', message: 'No text layer found and no OCR provider configured.', created_at: '2026-08-24T08:52:43Z' },
    ],
  },
  {
    id: 84,
    filename: 'receipt-aug.pdf',
    content_type: 'application/pdf',
    size_bytes: 98_304,
    checksum_sha256: 'e4c7b0d3f2a9681c5e0f4a7b9c1d3e5f7a9b1c3d5e7f9a1b3c5d7e9f1a3b5c7d',
    status: 'pending',
    document_type: 'receipt',
    page_count: 1,
    attempt_count: 1,
    processing_duration_ms: null,
    created_at: '2026-08-24T07:15:00Z',
    updated_at: '2026-08-24T09:02:11Z',
    owner: DEMO_USER,
    error: null,
    extraction: null,
    events: [
      { event: 'uploaded', message: 'Received 98304 bytes as application/pdf.', created_at: '2026-08-24T07:15:00Z' },
      { event: 'reprocess_requested', message: 'Requested by user 1.', created_at: '2026-08-24T09:02:11Z' },
    ],
  },
]

/** Strip the heavy fields the real list endpoint does not return. */
function toListItem(document) {
  const { extraction, events, error, ...rest } = document
  return rest
}

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

  // --- Documents ---
  const [route, rawQuery] = path.split('?')
  const query = new URLSearchParams(rawQuery ?? '')

  if (route === '/documents' && method === 'GET') {
    let matches = DOCUMENTS.slice()

    const status = query.get('status')
    if (status) matches = matches.filter((d) => d.status === status)

    const type = query.get('document_type')
    if (type) matches = matches.filter((d) => d.document_type === type)

    const search = query.get('search')?.trim().toLowerCase()
    if (search) matches = matches.filter((d) => d.filename.toLowerCase().includes(search))

    const ownerEmail = query.get('owner_email')?.trim().toLowerCase()
    if (ownerEmail) {
      matches = matches.filter((d) => d.owner.email.toLowerCase().includes(ownerEmail))
    }

    const sortBy = query.get('sort_by') ?? 'created_at'
    const direction = query.get('sort_dir') === 'asc' ? 1 : -1
    matches.sort((a, b) => {
      const left = a[sortBy]
      const right = b[sortBy]
      if (left === right) return b.id - a.id // stable tie-break, as the API does
      return (left > right ? 1 : -1) * direction
    })

    const page = Number(query.get('page') ?? 1)
    const pageSize = Number(query.get('page_size') ?? 20)
    const total = matches.length
    const totalPages = Math.max(1, Math.ceil(total / pageSize))
    const start = (page - 1) * pageSize

    return {
      items: matches.slice(start, start + pageSize).map(toListItem),
      meta: {
        total,
        page,
        page_size: pageSize,
        total_pages: totalPages,
        has_next: page < totalPages,
        has_previous: page > 1,
      },
    }
  }

  if (route === '/documents/stats' && method === 'GET') {
    const counts = { pending: 0, processing: 0, completed: 0, failed: 0 }
    for (const document of DOCUMENTS) counts[document.status] += 1
    return counts
  }

  const documentMatch = route.match(/^\/documents\/(\d+)(\/[a-z]+)?$/)
  if (documentMatch) {
    const id = Number(documentMatch[1])
    const suffix = documentMatch[2] ?? ''
    const document = DOCUMENTS.find((candidate) => candidate.id === id)
    if (!document) throw new ApiError(`Document ${id} was not found.`, 404)

    if (suffix === '' && method === 'GET') return document

    if (suffix === '/text' && method === 'GET') {
      if (!document.extraction) {
        throw new ApiError('This document has not finished processing.', 409)
      }
      return {
        document_id: id,
        // The fixture has no megabyte-scale body, so the preview stands in for
        // the full text; the shape is what matters to the caller.
        text: document.extraction.text_preview,
        char_count: document.extraction.text_char_count,
        page_count: document.extraction.page_count,
        ocr_provider: document.extraction.ocr_provider,
      }
    }

    if (suffix === '/reprocess' && method === 'POST') {
      if (document.status === 'pending' || document.status === 'processing') {
        throw new ApiError(`This document is already '${document.status}'.`, 409)
      }
      // Mutates the fixture so the UI genuinely transitions, exactly as the real
      // endpoint resets the row to pending.
      document.status = 'pending'
      document.attempt_count += 1
      document.updated_at = new Date().toISOString()
      document.events = [
        ...document.events,
        {
          event: 'reprocess_requested',
          message: `Requested by user ${DEMO_USER.id}.`,
          created_at: document.updated_at,
        },
      ]
      return document
    }

    if (suffix === '' && method === 'DELETE') {
      DOCUMENTS.splice(DOCUMENTS.indexOf(document), 1)
      return { detail: `Document ${id} deleted.` }
    }
  }

  throw new ApiError(`Mock endpoint not found: ${method} ${path}`, 404)
}
