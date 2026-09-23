import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AuthProvider } from './context/AuthContext'
import { App } from './App'

describe('App', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('shows the home placeholder at / once authenticated', async () => {
    ;(fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ok: true,
      json: async () => ({ id: 1, email: 'jane@example.com', role: 'submitter' }),
    })

    render(
      <MemoryRouter initialEntries={['/']}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByText('Acme Resale')).toBeInTheDocument())
  })

  it('redirects to /login at / when not authenticated', async () => {
    ;(fetch as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({ ok: false, status: 401 })

    render(
      <MemoryRouter initialEntries={['/']}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Log in' })).toBeInTheDocument())
  })
})
