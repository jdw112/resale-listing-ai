import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { AuthProvider, useAuth } from './AuthContext'
import { RequireAuth } from '../components/RequireAuth'

function Probe() {
  const { user, loading } = useAuth()
  if (loading) return <p>loading</p>
  return <p>{user ? `logged in as ${user.email}` : 'not logged in'}</p>
}

describe('AuthProvider', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('loads the current user from GET /v1/auth/me on mount', async () => {
    ;(fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({ id: 1, email: 'jane@example.com', role: 'submitter' }),
    })

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )

    await waitFor(() => expect(screen.getByText('logged in as jane@example.com')).toBeInTheDocument())
  })

  it('shows not logged in when GET /v1/auth/me returns 401', async () => {
    ;(fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: false, status: 401 })

    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>,
    )

    await waitFor(() => expect(screen.getByText('not logged in')).toBeInTheDocument())
  })
})

describe('RequireAuth', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('redirects an unauthenticated visitor to /login', async () => {
    ;(fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: false, status: 401 })

    render(
      <MemoryRouter initialEntries={['/protected']}>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<p>login page</p>} />
            <Route
              path="/protected"
              element={
                <RequireAuth>
                  <p>secret content</p>
                </RequireAuth>
              }
            />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByText('login page')).toBeInTheDocument())
  })
})
