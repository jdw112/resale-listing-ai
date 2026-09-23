import { createContext, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { apiFetch } from '../lib/api'

export interface User {
  id: number
  email: string
  role: string
}

interface AuthContextValue {
  user: User | null
  loading: boolean
  login: (email: string, password: string) => Promise<void>
  signup: (email: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined)

async function parseErrorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json()
    return body.detail ?? fallback
  } catch {
    return fallback
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    apiFetch('/v1/auth/me')
      .then((res) => (res.ok ? res.json() : null))
      .then(setUser)
      .catch(() => setUser(null))
      .finally(() => setLoading(false))
  }, [])

  async function login(email: string, password: string) {
    const res = await apiFetch('/v1/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) })
    if (!res.ok) {
      throw new Error(await parseErrorDetail(res, 'login failed'))
    }
    setUser(await res.json())
  }

  async function signup(email: string, password: string) {
    const res = await apiFetch('/v1/auth/register', { method: 'POST', body: JSON.stringify({ email, password }) })
    if (!res.ok) {
      throw new Error(await parseErrorDetail(res, 'signup failed'))
    }
    setUser(await res.json())
  }

  async function logout() {
    await apiFetch('/v1/auth/logout', { method: 'POST' })
    setUser(null)
  }

  return <AuthContext.Provider value={{ user, loading, login, signup, logout }}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth must be used within an AuthProvider')
  }
  return ctx
}
