import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { AuthProvider } from '../context/AuthContext'
import { Signup } from './Signup'

describe('Signup', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn())
  })

  it('signs up and navigates home on success', async () => {
    const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
    fetchMock
      .mockResolvedValueOnce({ ok: false, status: 401 }) // initial GET /v1/auth/me on mount
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ id: 1, email: 'jane@example.com', role: 'submitter' }),
      })

    render(
      <MemoryRouter initialEntries={['/signup']}>
        <AuthProvider>
          <Routes>
            <Route path="/signup" element={<Signup />} />
            <Route path="/" element={<p>home page</p>} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByLabelText('Email')).toBeInTheDocument())
    await userEvent.type(screen.getByLabelText('Email'), 'jane@example.com')
    await userEvent.type(screen.getByLabelText('Password'), 'hunter22')
    await userEvent.click(screen.getByRole('button', { name: 'Sign up' }))

    await waitFor(() => expect(screen.getByText('home page')).toBeInTheDocument())
  })

  it('shows an error message on duplicate email', async () => {
    const fetchMock = fetch as unknown as ReturnType<typeof vi.fn>
    fetchMock
      .mockResolvedValueOnce({ ok: false, status: 401 }) // initial GET /v1/auth/me
      .mockResolvedValueOnce({ ok: false, json: async () => ({ detail: "email already registered: 'jane@example.com'" }) })

    render(
      <MemoryRouter initialEntries={['/signup']}>
        <AuthProvider>
          <Routes>
            <Route path="/signup" element={<Signup />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByLabelText('Email')).toBeInTheDocument())
    await userEvent.type(screen.getByLabelText('Email'), 'jane@example.com')
    await userEvent.type(screen.getByLabelText('Password'), 'hunter22')
    await userEvent.click(screen.getByRole('button', { name: 'Sign up' }))

    await waitFor(() => expect(screen.getByText("email already registered: 'jane@example.com'")).toBeInTheDocument())
  })
})
