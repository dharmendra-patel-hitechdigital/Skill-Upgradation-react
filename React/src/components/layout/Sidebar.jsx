const NAV = [
  { id: 'dashboard', label: 'Dashboard', icon: '◧', active: true },
  { id: 'analytics', label: 'Analytics', icon: '◔' },
  { id: 'orders', label: 'Orders', icon: '▤' },
  { id: 'customers', label: 'Customers', icon: '◍' },
  { id: 'settings', label: 'Settings', icon: '⚙' },
]

export default function Sidebar({ open, onClose }) {
  return (
    <>
      <aside className={`sidebar ${open ? 'sidebar--open' : ''}`}>
        <div className="sidebar__brand">
          <span className="sidebar__logo">HI</span>
          <span className="sidebar__brand-name">Hitech</span>
        </div>

        <nav className="sidebar__nav">
          {NAV.map((item) => (
            <a
              key={item.id}
              href={`#${item.id}`}
              className={`sidebar__link ${item.active ? 'sidebar__link--active' : ''}`}
              onClick={onClose}
            >
              <span className="sidebar__icon" aria-hidden>{item.icon}</span>
              {item.label}
            </a>
          ))}
        </nav>

        <div className="sidebar__footer">
          <p className="sidebar__footer-title">Need help?</p>
          <p className="sidebar__footer-text">Check our docs and guides.</p>
        </div>
      </aside>
      {open && <div className="sidebar__backdrop" onClick={onClose} />}
    </>
  )
}
