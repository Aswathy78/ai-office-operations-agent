import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import {
  clearSessionTokens,
  exchangeOAuthCode,
  getCurrentUser,
  login as loginRequest,
  logout as logoutRequest,
  storeSessionTokens,
} from '../api/auth'
import {
  clearAccessToken,
  subscribeToUnauthorized,
} from '../api/client'
import type { User } from '../types/user'

interface AuthContextValue {
  user: User | null
  isAuthenticated: boolean
  authNotice: string
  signIn: (email: string, password: string) => Promise<void>
  /** Finish a Google / Microsoft sign-in with the one-time code from the redirect. */
  completeOAuthSignIn: (code: string) => Promise<void>
  signOut: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [authNotice, setAuthNotice] = useState('')

  const clearUser = useCallback(() => {
    clearAccessToken()
    clearSessionTokens()
    setUser(null)
  }, [])

  const handleUnauthorized = useCallback((message?: string) => {
    clearUser()
    // The server says why when it knows: inactivity, or an administrator ended the session.
    setAuthNotice(message || 'Your session has expired. Please sign in again.')
  }, [clearUser])

  useEffect(() => subscribeToUnauthorized(handleUnauthorized), [handleUnauthorized])

  const loadProfile = useCallback(async () => {
    const profile = await getCurrentUser()
    setUser({
      id: profile.id,
      name: profile.name,
      email: profile.email,
      role: profile.role,
      department: profile.team ?? '',
    })
  }, [])

  const signIn = useCallback(async (email: string, password: string) => {
    clearUser()
    setAuthNotice('')
    try {
      storeSessionTokens(await loginRequest(email, password))
      await loadProfile()
    } catch (error) {
      clearUser()
      throw error
    }
  }, [clearUser, loadProfile])

  const completeOAuthSignIn = useCallback(async (code: string) => {
    clearUser()
    setAuthNotice('')
    try {
      storeSessionTokens(await exchangeOAuthCode(code))
      await loadProfile()
    } catch (error) {
      clearUser()
      throw error
    }
  }, [clearUser, loadProfile])

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
      completeOAuthSignIn,
      signOut,
    }),
    [user, authNotice, signIn, completeOAuthSignIn, signOut],
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
