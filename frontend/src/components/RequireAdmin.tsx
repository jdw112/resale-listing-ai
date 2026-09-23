import { Navigate } from 'react-router-dom'
import type { ReactNode } from 'react'
import { useAuth } from '../context/AuthContext'

export function RequireAdmin({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth()
  if (loading) {
    return <p>Loading...</p>
  }
  if (!user || user.role !== 'admin') {
    return <Navigate to="/" replace />
  }
  return <>{children}</>
}
