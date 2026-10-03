import { createContext, useContext, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { demoUsers } from '../mocks/mockUser'
import type { User, UserRole } from '../types/user'

interface AuthContextValue {
  user: User | null
  isAuthenticated: boolean
  signInAsDemo: (role: UserRole) => void
  signOut: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

const AUTH_STORAGE_KEY = 'ai-office-demo-user'

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(() => {
    const storedUser = sessionStorage.getItem(AUTH_STORAGE_KEY)

    if (!storedUser) {
      return null
    }

    try {
      return JSON.parse(storedUser) as User
    } catch {
      sessionStorage.removeItem(AUTH_STORAGE_KEY)
      return null
    }
  })

  const signInAsDemo = (role: UserRole) => {
    const demoUser = demoUsers[role]

    setUser(demoUser)
    sessionStorage.setItem(AUTH_STORAGE_KEY, JSON.stringify(demoUser))
  }

  const signOut = () => {
    setUser(null)
    sessionStorage.removeItem(AUTH_STORAGE_KEY)
  }

  const value = useMemo(
    () => ({
      user,
      isAuthenticated: Boolean(user),
      signInAsDemo,
      signOut,
    }),
    [user],
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