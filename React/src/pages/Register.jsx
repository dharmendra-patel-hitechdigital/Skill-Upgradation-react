import { useState } from 'react'
import { Link, useNavigate, Navigate } from 'react-router-dom'
import { useAuth } from '../hooks/useAuth.js'
import { useDocumentTitle } from '../hooks/useDocumentTitle.js'
import { register as registerRequest } from '../api/auth.api.js'
import Button from '../components/ui/Button.jsx'
import Input from '../components/ui/Input.jsx'

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/

// Mirrors the server-side policy in app/schemas/user.py. Checking here as well
// is not duplication for its own sake: it turns a round-trip and a 422 into
// instant feedback under the field being typed. The server remains the
// authority - anything it rejects still surfaces in the alert.
const PASSWORD_MIN = 10
const HAS_LETTER = /[A-Za-z]/
const HAS_DIGIT = /\d/

export default function Register() {
  useDocumentTitle('Create account · Hitech')
  const { login, isAuthenticated } = useAuth()
  const navigate = useNavigate()

  const [form, setForm] = useState({
    fullName: '',
    email: '',
    password: '',
    confirm: '',
  })
  const [showPassword, setShowPassword] = useState(false)
  const [errors, setErrors] = useState({})
  const [submitError, setSubmitError] = useState('')
  const [loading, setLoading] = useState(false)

  if (isAuthenticated) return <Navigate to="/dashboard" replace />

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

    if (!form.password) {
      next.password = 'Password is required.'
    } else if (form.password.length < PASSWORD_MIN) {
      next.password = `Use at least ${PASSWORD_MIN} characters.`
    } else if (!HAS_LETTER.test(form.password)) {
      next.password = 'Include at least one letter.'
    } else if (!HAS_DIGIT.test(form.password)) {
      next.password = 'Include at least one digit.'
    } else if (form.password.trim() !== form.password) {
      next.password = 'Remove the space at the start or end.'
    }

    if (!form.confirm) next.confirm = 'Confirm your password.'
    else if (form.confirm !== form.password) next.confirm = 'Passwords do not match.'

    setErrors(next)
    return Object.keys(next).length === 0
  }

  async function handleSubmit(e) {
    e.preventDefault()
    if (!validate()) return
    setLoading(true)
    setSubmitError('')
    try {
      await registerRequest({
        email: form.email,
        fullName: form.fullName,
        password: form.password,
      })

      // Registration returns the profile but no tokens, so sign in to start a
      // session. If that second call fails the account still exists - send them
      // to the sign-in page rather than leaving them on a form that would now
      // fail with "email already registered".
      try {
        await login({ email: form.email, password: form.password })
        navigate('/dashboard', { replace: true })
      } catch {
        navigate('/login', {
          replace: true,
          state: { notice: 'Account created. Please sign in.' },
        })
      }
    } catch (err) {
      setSubmitError(err.message || 'Unable to create your account. Please try again.')
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
            Create your workspace to upload documents, track what the pipeline
            extracts, and share results with your team.
          </p>
          <ul className="auth__hero-points">
            <li>Automated document extraction</li>
            <li>Secure by design</li>
            <li>Built for teams</li>
          </ul>
        </div>
      </div>

      {/* Form panel */}
      <div className="auth__panel">
        <form className="auth__form" onSubmit={handleSubmit} noValidate>
          <h2 className="auth__title">Create your account</h2>
          <p className="auth__subtitle">It takes less than a minute.</p>

          {submitError && <div className="auth__alert">{submitError}</div>}

          <Input
            label="Full name"
            type="text"
            placeholder="Jane Doe"
            value={form.fullName}
            onChange={update('fullName')}
            error={errors.fullName}
            autoComplete="name"
            autoFocus
          />

          <Input
            label="Email"
            type="email"
            placeholder="you@hitech.com"
            value={form.email}
            onChange={update('email')}
            error={errors.email}
            autoComplete="email"
          />

          <Input
            label="Password"
            type={showPassword ? 'text' : 'password'}
            placeholder="••••••••••"
            value={form.password}
            onChange={update('password')}
            error={errors.password}
            autoComplete="new-password"
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

          <Input
            label="Confirm password"
            type={showPassword ? 'text' : 'password'}
            placeholder="••••••••••"
            value={form.confirm}
            onChange={update('confirm')}
            error={errors.confirm}
            autoComplete="new-password"
          />

          <p className="auth__policy">
            At least {PASSWORD_MIN} characters, including a letter and a digit.
          </p>

          <Button type="submit" loading={loading} fullWidth>
            Create account
          </Button>

          <p className="auth__switch">
            Already have an account? <Link to="/login">Sign in</Link>
          </p>
        </form>
      </div>
    </div>
  )
}
