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
 * @param {object} [opts] { method, body, headers, signal, form, withStatus }
 *
 * `form: true` sends the body as application/x-www-form-urlencoded instead of
 * JSON. Only /auth/login needs it: the backend implements the OAuth2 password
 * flow via FastAPI's OAuth2PasswordRequestForm, which reads form fields and
 * rejects a JSON payload with 422 "field required".
 *
 * A `FormData` body is passed through untouched and **without** a Content-Type
 * header — the browser has to set it itself, because only it knows the multipart
 * boundary it generated. Setting `multipart/form-data` by hand omits the
 * boundary and the server rejects the request as malformed.
 *
 * `withStatus: true` resolves to `{ status, data }` instead of just the body.
 * Needed where a success code carries meaning: the upload endpoint answers
 * **202** for "accepted, now processing" and **200** for "this exact file was
 * already uploaded, here is the original", and those want different messages.
 */
export async function request(
  path,
  { method = 'GET', body, headers = {}, signal, form = false, withStatus = false } = {},
) {
  const token = getToken()
  const isMultipart = typeof FormData !== 'undefined' && body instanceof FormData

  const finalHeaders = {
    ...(isMultipart
      ? {}
      : { 'Content-Type': form ? 'application/x-www-form-urlencoded' : 'application/json' }),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...headers,
  }

  // Route to the in-memory mock backend when no real API is configured.
  if (isMockEnabled(BASE_URL)) {
    const result = await handleMockRequest(path, { method, body, token })
    // The mock signals a dedup hit the same way the real API does, so the
    // caller's branching is identical in both modes.
    return withStatus
      ? { status: result?.__status ?? 200, data: stripInternal(result) }
      : stripInternal(result)
  }

  let requestBody
  if (body === undefined || body === null) requestBody = undefined
  else if (isMultipart) requestBody = body
  else if (form) requestBody = new URLSearchParams(body).toString()
  else requestBody = JSON.stringify(body)

  let res
  try {
    res = await fetch(`${BASE_URL}${path}`, {
      method,
      headers: finalHeaders,
      body: requestBody,
      signal,
    })
  } catch (err) {
    if (err.name === 'AbortError') throw err
    throw new ApiError('Network error — please check your connection.', 0)
  }

  const isJson = res.headers.get('content-type')?.includes('application/json')
  const data = isJson ? await res.json() : await res.text()

  if (!res.ok) {
    // The backend wraps failures as { error: { code, message, details } }; the
    // mock server and FastAPI's own handlers use flatter shapes. Check all of
    // them, otherwise every real error surfaces as "Request failed (422)" and
    // the actual reason is only visible in devtools.
    const message =
      data?.error?.message ||
      data?.detail ||
      data?.message ||
      `Request failed (${res.status})`
    throw new ApiError(message, res.status, data)
  }

  return withStatus ? { status: res.status, data } : data
}

/** Remove the mock backend's status marker so callers never see it. */
function stripInternal(result) {
  if (!result || typeof result !== 'object' || !('__status' in result)) return result
  const { __status, ...rest } = result
  return rest
}

export const api = {
  get: (path, opts) => request(path, { ...opts, method: 'GET' }),
  post: (path, body, opts) => request(path, { ...opts, method: 'POST', body }),
  postForm: (path, body, opts) => request(path, { ...opts, method: 'POST', body, form: true }),
  // `formData` must be a FormData instance; request() detects it and lets the
  // browser set the multipart Content-Type with its boundary.
  upload: (path, formData, opts) =>
    request(path, { ...opts, method: 'POST', body: formData, withStatus: true }),
  put: (path, body, opts) => request(path, { ...opts, method: 'PUT', body }),
  del: (path, opts) => request(path, { ...opts, method: 'DELETE' }),
}
