import { api } from './client.js'

/** POST /auth/login -> { token, user } */
export function login(credentials) {
  return api.post('/auth/login', credentials)
}

/** GET /auth/me -> { user } */
export function fetchCurrentUser() {
  return api.get('/auth/me')
}
