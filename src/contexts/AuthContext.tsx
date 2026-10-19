import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { getCurrentUser, login as loginRequest, logout as logoutRequest } from '../api/auth'
import {
  clearAccessToken,
  setAccessToken,
  subscribeToUnauthorized,
} from '../api/client'
import type { User } from '../types/user'

interface AuthContextValue {
  user: User | null
  isAuthenticated: boolean
  authNotice: string
  signIn: (email: string, password: string) => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [authNotice, setAuthNotice] = useState('')

  const clearUser = useCallback(() => {
    clearAccessToken()
    setUser(null)
  }, [])

  const handleUnauthorized = useCallback(() => {
    clearUser()
    setAuthNotice('Your session has expired. Please sign in again.')
  }, [clearUser])

  useEffect(() => subscribeToUnauthorized(handleUnauthorized), [handleUnauthorized])

  const signIn = useCallback(async (email: string, password: string) => {
    clearUser()
    setAuthNotice('')
    try {
      const result = await loginRequest(email, password)
      setAccessToken(result.access_token)
      const profile = await getCurrentUser()
      setUser({
        id: profile.id,
        name: profile.name,
        email: profile.email,
        role: profile.role,
        department: profile.team ?? '',
      })
    } catch (error) {
      clearUser()
      throw error
    }
  }, [clearUser])

  const signOut = useCallback(async () => {
    try {
      await logoutRequest()
    } catch (error) {
      const detail = error instanceof Error ? ` ${error.message}` : ''
      setAuthNotice(`You were signed out locally, but the server could not confirm logout.${detail}`)
    } finally {
      clearUser()
    }
  }, [clearUser])

  const value = useMemo(
    () => ({
      user,
      isAuthenticated: Boolean(user),
      authNotice,
      signIn,
      signOut,
    }),
    [user, authNotice, signIn, signOut],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const context = useContext(AuthContext)

  if (!context) {
    throw new Error('useAuth must be used inside AuthProvider')
  }

  return context
}
