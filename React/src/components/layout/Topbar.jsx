import { useState } from 'react'
import { useAuth } from '../../hooks/useAuth.js'
import { displayName, initials, isAdmin } from '../../lib/roles.js'

export default function Topbar({ onMenuClick }) {
  const { user, logout } = useAuth()
  const [menuOpen, setMenuOpen] = useState(false)

  return (
    <header className="topbar">
      <div className="topbar__left">
        <button className="topbar__menu-btn" onClick={onMenuClick} aria-label="Open menu">
          ☰
        </button>
        <div className="topbar__search">
          <span className="topbar__search-icon" aria-hidden>⌕</span>
          <input type="search" placeholder="Search…" aria-label="Search" />
        </div>
      </div>

      <div className="topbar__right">
        <button className="topbar__icon-btn" aria-label="Notifications">
          <span aria-hidden>◔</span>
          <span className="topbar__badge" />
        </button>

        <div className="topbar__user">
          {/* Derived, not read from a field: the real /users/me returns
              `full_name`, with no `avatar` or `name` to fall back on. */}
          <button className="topbar__avatar" onClick={() => setMenuOpen((v) => !v)}>
            {user?.avatar ?? initials(user)}
          </button>
          {menuOpen && (
            <div className="topbar__dropdown" onMouseLeave={() => setMenuOpen(false)}>
              <div className="topbar__dropdown-head">
                <strong>{displayName(user)}</strong>
                <span>{user?.email}</span>
                {isAdmin(user) && <span className="pill pill--muted">Administrator</span>}
              </div>
              <button className="topbar__dropdown-item" onClick={logout}>
                Sign out
              </button>
            </div>
          )}
        </div>
      </div>
    </header>
  )
}
