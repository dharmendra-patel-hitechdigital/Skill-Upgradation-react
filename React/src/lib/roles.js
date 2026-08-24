/**
 * Role helpers.
 *
 * The backend's `UserRole` enum is lower-case (`admin` / `user`). Older mock
 * fixtures used the display spelling `Administrator`, so both are accepted here
 * rather than in each call site — an admin-only screen that silently fails to
 * appear because of letter case is a miserable thing to debug.
 *
 * This gates *presentation only*. Authorisation is enforced server-side on every
 * request, so editing your stored profile to say `admin` grants nothing.
 */
const ADMIN_ROLES = new Set(['admin', 'administrator'])

export function isAdmin(user) {
  const role = user?.role
  return typeof role === 'string' && ADMIN_ROLES.has(role.trim().toLowerCase())
}

/** Best available display name for a user, across both profile shapes. */
export function displayName(user) {
  return user?.full_name || user?.name || user?.email || 'there'
}

/** Initials for the avatar chip. */
export function initials(user) {
  const source = user?.full_name || user?.name || user?.email || '?'
  const parts = source.split(/[\s@._-]+/).filter(Boolean)
  if (parts.length === 0) return '?'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[1][0]).toUpperCase()
}
