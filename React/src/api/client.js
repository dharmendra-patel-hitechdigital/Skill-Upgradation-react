/**
 * Thin fetch wrapper used by every API module.
 *
 * - Reads the base URL from VITE_API_BASE_URL.
 * - Attaches the bearer token from localStorage automatically.
 * - Normalizes errors into a single `ApiError` shape.
 *
 * If no VITE_API_BASE_URL is configured, requests are routed to the built-in
 * mock backend (see ./mockServer.js) so the app is fully runnable offline.
 */
import { handleMockRequest, isMockEnabled } from './mockServer.js'

export const TOKEN_KEY = 'hitech.auth.token'

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.data = data
  }
}

const BASE_URL = import.meta.env.VITE_API_BASE_URL ?? ''

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

/**
 * @param {string} path   e.g. "/auth/login"
 * @param {object} [opts] { method, body, headers, signal }
 */
export async function request(path, { method = 'GET', body, headers = {}, signal } = {}) {
  const token = getToken()
  const finalHeaders = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...headers,
  }

  // Route to the in-memory mock backend when no real API is configured.
  if (isMockEnabled(BASE_URL)) {
    return handleMockRequest(path, { method, body, token })
  }

  let res
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      method,
      headers: finalHeaders,
      body: body ? JSON.stringify(body) : undefined,
      signal,
    })
  } catch (err) {
    if (err.name === 'AbortError') throw err
    throw new ApiError('Network error — please check your connection.', 0)
  }

  const isJson = res.headers.get('content-type')?.includes('application/json')
  const data = isJson ? await res.json() : await res.text()

  if (!res.ok) {
    const message = (data && data.message) || `Request failed (${res.status})`
    throw new ApiError(message, res.status, data)
  }

  return data
}

export const api = {
  get: (path, opts) => request(path, { ...opts, method: 'GET' }),
  post: (path, body, opts) => request(path, { ...opts, method: 'POST', body }),
  put: (path, body, opts) => request(path, { ...opts, method: 'PUT', body }),
  del: (path, opts) => request(path, { ...opts, method: 'DELETE' }),
}
