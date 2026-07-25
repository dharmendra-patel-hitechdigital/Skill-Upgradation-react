import { useState } from 'react'
import { useNavigate, useLocation, Navigate } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth.js'
import { useDocumentTitle } from '../hooks/useDocumentTitle.js'
import Button from '../components/ui/Button.jsx'
import Input from '../components/ui/Input.jsx'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

export default function Login() {
  useDocumentTitle('Sign in · Hitech')
  const { login, isAuthenticated } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const from = location.state?.from?.pathname || '/dashboard'

  const [form, setForm] = useState({ email: '', password: '' })
  const [showPassword, setShowPassword] = useState(false)
  const [errors, setErrors] = useState({})
  const [submitError, setSubmitError] = useState('')
  const [loading, setLoading] = useState(false)

  // Already signed in? Skip the form.
  if (isAuthenticated) return <Navigate to={from} replace />

  function update(field) {
    return (e) => {
      setForm((f) => ({ ...f, [field]: e.target.value }))
      setErrors((prev) => ({ ...prev, [field]: undefined }))
      setSubmitError('')
    }
  }

  function validate() {
    const next = {}
    if (!form.email) next.email = 'Email is required.'
    else if (!EMAIL_RE.test(form.email)) next.email = 'Enter a valid email address.'
    if (!form.password) next.password = 'Password is required.'
    setErrors(next)
    return Object.keys(next).length === 0
  }

  async function handleSubmit(e) {
    e.preventDefault()
    if (!validate()) return
    setLoading(true)
    setSubmitError('')
    try {
      await login(form)
      navigate(from, { replace: true })
    } catch (err) {
      setSubmitError(err.message || 'Unable to sign in. Please try again.')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="auth">
      {/* Brand / marketing panel */}
      <div className="auth__hero">
        <div className="auth__hero-content">
          <span className="auth__logo">HI</span>
          <h1 className="auth__hero-title">Hitech</h1>
          <p className="auth__hero-text">
            Sign in to your workspace to track revenue, monitor activity, and grow
            with confidence.
          </p>
          <ul className="auth__hero-points">
            <li>Real-time analytics</li>
            <li>Secure by design</li>
            <li>Built for teams</li>
          </ul>
        </div>
      </div>

      {/* Form panel */}
      <div className="auth__panel">
        <form className="auth__form" onSubmit={handleSubmit} noValidate>
          <h2 className="auth__title">Welcome back</h2>
          <p className="auth__subtitle">Please enter your details to sign in.</p>

          {submitError && <div className="auth__alert">{submitError}</div>}

          <Input
            label="Email"
            type="email"
            placeholder="you@hitech.com"
            value={form.email}
            onChange={update('email')}
            error={errors.email}
            autoComplete="email"
            autoFocus
          />

          <Input
            label="Password"
            type={showPassword ? 'text' : 'password'}
            placeholder="••••••••"
            value={form.password}
            onChange={update('password')}
            error={errors.password}
            autoComplete="current-password"
            trailing={
              <button
                type="button"
                className="auth__toggle"
                onClick={() => setShowPassword((v) => !v)}
                aria-label={showPassword ? 'Hide password' : 'Show password'}
              >
                {showPassword ? 'Hide' : 'Show'}
              </button>
            }
          />

          <div className="auth__row">
            <label className="auth__remember">
              <input type="checkbox" /> Remember me
            </label>
            <a href="#reset" className="auth__forgot">Forgot password?</a>
          </div>

          <Button type="submit" loading={loading} fullWidth>
            Sign in
          </Button>

          <p className="auth__hint">
            Demo credentials — <code>demo@hitech.com</code> / <code>password123</code>
          </p>
        </form>
      </div>
    </div>
  )
}
