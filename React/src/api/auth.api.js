/**
 * Auth endpoints.
 *
 * This module is the adapter between the backend's wire format and the shape
 * the rest of the app expects ({ token, user }). Keeping the translation here
 * means AuthContext and the pages never have to know that login is an OAuth2
 * form post returning snake_case token fields.
 */
import { api } from './client.js'

/**
 * POST /auth/login -> { token, refreshToken, expiresIn, user }
 *
 * Two things about the backend contract, both easy to get wrong:
 *
 * 1. It is an OAuth2 *password flow* form submission, not JSON. Sending JSON
 *    gets 422 "field required" for both fields.
 * 2. The email goes in a field literally named `username` - that name is fixed
 *    by the OAuth2 spec, not a choice this API made.
 */
export async function login({ email, password }) {
  const data = await api.postForm('/auth/login', { username: email, password })
  return {
    token: data.access_token,
    refreshToken: data.refresh_token,
    expiresIn: data.expires_in,
    user: data.user,
  }
}

/**
 * POST /auth/register -> { user }
 *
 * Plain JSON, unlike login - only the OAuth2 login endpoint is form-encoded.
 * Returns the created profile but no tokens, so the caller signs in afterwards
 * to get a session.
 *
 * The first account created on a fresh installation becomes the admin; every
 * one after that is a regular user.
 */
export async function register({ email, fullName, password }) {
  const user = await api.post('/auth/register', {
    email,
    full_name: fullName?.trim() || null,
    password,
  })
  return { user }
}

/**
 * GET /users/me -> { user }
 *
 * Note the path: the profile lives under /users, not /auth. The endpoint
 * returns the user object directly, so it is wrapped here to keep the
 * destructuring in AuthContext unchanged.
 */
export async function fetchCurrentUser() {
  const user = await api.get('/users/me')
  return { user }
}
