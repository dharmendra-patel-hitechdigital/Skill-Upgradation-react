import { NavLink } from 'react-router-dom'
import { useAuth } from '../../hooks/useAuth.js'
import { isAdmin } from '../../lib/roles.js'

/**
 * Primary navigation.
 *
 * `to` entries are real routes rendered as NavLink, so the active state comes
 * from the router instead of a hard-coded `active: true` that lit up "Dashboard"
 * on every screen. Entries without a `to` are the placeholders this template
 * shipped with; they stay visibly disabled rather than pretending to navigate.
 */
const NAV = [
  { id: 'dashboard', label: 'Dashboard', icon: '◧', to: '/dashboard' },
  { id: 'documents', label: 'Documents', icon: '▤', to: '/documents' },
  { id: 'analytics', label: 'Analytics', icon: '◔', to: '/analytics' },
  { id: 'customers', label: 'Customers', icon: '◍' },
  { id: 'settings', label: 'Settings', icon: '⚙' },
]

export default function Sidebar({ open, onClose }) {
  const { user } = useAuth()

  return (
    <>
      <aside className={`sidebar ${open ? 'sidebar--open' : ''}`}>
        <div className="sidebar__brand">
          <span className="sidebar__logo">HI</span>
          <span className="sidebar__brand-name">Hitech</span>
        </div>

        <nav className="sidebar__nav">
          {NAV.map((item) =>
            item.to ? (
              <NavLink
                key={item.id}
                to={item.to}
                className={({ isActive }) =>
                  `sidebar__link ${isActive ? 'sidebar__link--active' : ''}`
                }
                onClick={onClose}
              >
                <span className="sidebar__icon" aria-hidden>{item.icon}</span>
                {item.label}
              </NavLink>
            ) : (
              <span
                key={item.id}
                className="sidebar__link sidebar__link--disabled"
                aria-disabled="true"
                title="Not available yet"
              >
                <span className="sidebar__icon" aria-hidden>{item.icon}</span>
                {item.label}
              </span>
            ),
          )}
        </nav>

        <div className="sidebar__footer">
          {isAdmin(user) ? (
            <>
              <p className="sidebar__footer-title">Administrator</p>
              <p className="sidebar__footer-text">
                Documents shows uploads from every user on this installation.
              </p>
            </>
          ) : (
            <>
              <p className="sidebar__footer-title">Need help?</p>
              <p className="sidebar__footer-text">Check our docs and guides.</p>
            </>
          )}
        </div>
      </aside>
      {open && <div className="sidebar__backdrop" onClick={onClose} />}
    </>
  )
}
