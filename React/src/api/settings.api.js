/**
 * Admin runtime settings.
 *
 * Only the *choice* of AI engine lives here. API keys are deployment secrets
 * (env vars, AWS Secrets Manager) and there is deliberately no endpoint to write
 * them — a key writable over HTTP is a key exfiltratable over HTTP.
 */
import { api } from './client.js'

/** GET /settings/ai -> { selected, effective, default, is_override, options, ... } */
export function fetchAISettings(opts) {
  return api.get('/settings/ai', opts)
}

/**
 * PUT /settings/ai -> the updated settings.
 *
 * `provider: null` clears the override and returns to the deployment default.
 * The field is sent explicitly rather than omitted, because the API treats an
 * absent field and an explicit null identically only if you send one.
 */
export function updateAISettings(provider, opts) {
  return api.put('/settings/ai', { provider: provider ?? null }, opts)
}
