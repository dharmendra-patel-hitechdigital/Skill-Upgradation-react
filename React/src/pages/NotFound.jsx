import { Link } from 'react-router-dom'

export default function NotFound() {
  return (
    <div className="notfound">
      <p className="notfound__code">404</p>
      <h1 className="notfound__title">Page not found</h1>
      <p className="notfound__text">The page you're looking for doesn't exist or has moved.</p>
      <Link to="/dashboard" className="btn btn--primary">Back to dashboard</Link>
    </div>
  )
}
