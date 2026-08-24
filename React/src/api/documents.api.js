/**
 * Document endpoints.
 *
 * Like auth.api.js, this module is the adapter between the backend's wire
 * format and what the pages want. Two things it deliberately absorbs:
 *
 * 1. Query-string assembly, so pages pass a plain filters object and never
 *    hand-build URLs (an empty filter must be *absent*, not `&status=`, which
 *    the API would reject as an invalid enum value).
 * 2. The `{items, meta}` page envelope, kept as-is because the pager needs the
 *    metadata — it is only given a stable shape when the response is empty.
 */
import { api } from './client.js'

/** Processing states, in lifecycle order. Mirrors DocumentStatus server-side. */
export const DOCUMENT_STATUSES = ['pending', 'processing', 'completed', 'failed']

/** Classifications the analyser can assign. Mirrors DocumentKind server-side. */
export const DOCUMENT_TYPES = [
  'invoice',
  'receipt',
  'contract',
  'resume',
  'report',
  'letter',
  'form',
  'identity_document',
  'bank_statement',
  'other',
]

function buildQuery(params) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    // Skip empty values rather than sending them: `?status=` is a 422 from the
    // enum validator, and `?search=` would be a pointless filter.
    if (value === undefined || value === null || value === '') continue
    query.set(key, String(value))
  }
  const encoded = query.toString()
  return encoded ? `?${encoded}` : ''
}

/**
 * GET /documents -> { items, meta }
 *
 * Administrators receive every user's documents; everyone else receives their
 * own. That scope is decided by the server from the caller's role, so there is
 * no parameter here to widen it.
 *
 * @param {{ page?: number, pageSize?: number, status?: string,
 *           documentType?: string, search?: string, ownerEmail?: string,
 *           sortBy?: string, sortDir?: 'asc'|'desc' }} [filters]
 */
export async function fetchDocuments(filters = {}, opts) {
  const {
    page = 1,
    pageSize = 20,
    status,
    documentType,
    search,
    ownerEmail,
    sortBy,
    sortDir,
  } = filters

  const path = `/documents${buildQuery({
    page,
    page_size: pageSize,
    status,
    document_type: documentType,
    search,
    owner_email: ownerEmail,
    sort_by: sortBy,
    sort_dir: sortDir,
  })}`

  const data = await api.get(path, opts)
  return { items: data.items ?? [], meta: data.meta ?? null }
}

/**
 * POST /documents — multipart upload, field name `file`.
 *
 * Resolves to `{ document, deduplicated }`:
 *
 * * **202** — accepted. The record comes back as `pending`; extraction runs in
 *   the background, so the caller must poll (the detail page does).
 * * **200** — this exact file, byte for byte, was already uploaded by this user.
 *   The *original* record is returned and nothing is reprocessed. Worth telling
 *   the user about: otherwise re-uploading looks like it silently did nothing.
 *
 * @param {File} file
 */
export async function uploadDocument(file, opts) {
  const formData = new FormData()
  // The field name is fixed by the endpoint's `file: UploadFile = File(...)`.
  formData.append('file', file, file.name)

  const { status, data } = await api.upload('/documents', formData, opts)
  return { document: data, deduplicated: status === 200 }
}

/**
 * GET /health/providers -> the effective upload limits and active AI engines.
 *
 * Read for `max_upload_mb` / `accepted_types` so the picker enforces the same
 * limits the server does. Hard-coding them client-side means a server config
 * change silently turns a friendly "too large" into a raw 413.
 */
export async function fetchUploadLimits(opts) {
  const data = await api.get('/health/providers', opts)
  return {
    maxUploadMb: data.max_upload_mb ?? null,
    acceptedTypes: data.accepted_types ?? [],
  }
}

/** GET /documents/stats -> { [status]: count } */
export function fetchDocumentStats(opts) {
  return api.get('/documents/stats', opts)
}

/**
 * GET /documents/{id} -> the full record.
 *
 * `extraction` is null while `status` is pending/processing, and `error`
 * explains a failure. `events` is the processing audit trail.
 */
export function fetchDocument(id, opts) {
  return api.get(`/documents/${id}`, opts)
}

/**
 * GET /documents/{id}/text -> { text, char_count, ... }
 *
 * Separate from the detail call on purpose: extracted text can be megabytes, so
 * the detail response carries only a preview and this is fetched on demand.
 */
export function fetchDocumentText(id, opts) {
  return api.get(`/documents/${id}/text`, opts)
}

/** POST /documents/{id}/reprocess -> the record, back in `pending`. */
export function reprocessDocument(id, opts) {
  return api.post(`/documents/${id}/reprocess`, undefined, opts)
}

/** DELETE /documents/{id} — removes the record, its extraction, and the blob. */
export function deleteDocument(id, opts) {
  return api.del(`/documents/${id}`, opts)
}

/** POST /documents/{id}/ask -> a grounded answer, with `answer_found`. */
export function askDocument(id, question, opts) {
  return api.post(`/documents/${id}/ask`, { question }, opts)
}
