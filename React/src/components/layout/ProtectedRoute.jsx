import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from '../../hooks/useAuth.js'
import { isAdmin } from '../../lib/roles.js'

/**
 * Gate a route on authentication, and optionally on the admin role.
 *
 * `requireAdmin` hides a screen a non-admin cannot use — it is not the security
 * boundary. Every admin-only endpoint enforces the role server-side on each
 * request, so editing the stored profile to say `admin` reveals an empty page
 * and a string of 403s, not data.
 *
 * A non-admin is sent to the dashboard rather than to /login: they *are* signed
 * in, and bouncing them to a login form for a page that is simply not theirs
 * reads as a broken session.
 */
export default function ProtectedRoute({ children, requireAdmin = false }) {
  const { isAuthenticated, user } = useAuth()
  const location = useLocation()

  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />
  }
  if (requireAdmin && !isAdmin(user)) {
    return <Navigate to="/dashboard" replace />
  }
  return children
}
