import { createContext, useCallback, useEffect, useMemo, useState } from 'react'
import { login as loginRequest, fetchCurrentUser } from '../api/auth.api.js'
import { getToken, setToken } from '../api/client.js'

export const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [initializing, setInitializing] = useState(true)

  // On first mount, restore the session if a token is present.
  useEffect(() => {
    let cancelled = false

    async function restore() {
      if (!getToken()) {
        setInitializing(false)
        return
      }
      try {
        const { user } = await fetchCurrentUser()
        if (!cancelled) setUser(user)
      } catch {
        setToken(null) // token expired / invalid
      } finally {
        if (!cancelled) setInitializing(false)
      }
    }

    restore()
    return () => {
      cancelled = true
    }
  }, [])

  const login = useCallback(async (credentials) => {
    const { token, user } = await loginRequest(credentials)
    setToken(token)
    setUser(user)
    return user
  }, [])

  const logout = useCallback(() => {
    setToken(null)
    setUser(null)
  }, [])

  const value = useMemo(
    () => ({ user, isAuthenticated: !!user, initializing, login, logout }),
    [user, initializing, login, logout],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}
