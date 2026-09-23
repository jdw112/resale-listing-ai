import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { AuthProvider } from '../context/AuthContext'
import { RequireAdmin } from './RequireAdmin'
import { stubFetchByUrl, jsonResponse } from '../test-utils'

afterEach(() => cleanup())

function renderAt(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <AuthProvider>
        <Routes>
          <Route path="/admin" element={<RequireAdmin><p>Admin content</p></RequireAdmin>} />
          <Route path="/" element={<p>Home page</p>} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

test('renders children for an admin user', async () => {
  vi.stubGlobal('fetch', stubFetchByUrl({
    '/v1/auth/me': () => jsonResponse({ id: 1, email: 'admin@example.com', role: 'admin' }),
  }))

  renderAt('/admin')

  await waitFor(() => expect(screen.getByText('Admin content')).toBeInTheDocument())
})

test('redirects a submitter to home', async () => {
  vi.stubGlobal('fetch', stubFetchByUrl({
    '/v1/auth/me': () => jsonResponse({ id: 1, email: 'jane@example.com', role: 'submitter' }),
  }))

  renderAt('/admin')

  await waitFor(() => expect(screen.getByText('Home page')).toBeInTheDocument())
})

test('redirects an anonymous visitor to login-gated home (RequireAuth path)', async () => {
  vi.stubGlobal('fetch', stubFetchByUrl({
    '/v1/auth/me': () => new Response(null, { status: 401 }),
  }))

  renderAt('/admin')

  await waitFor(() => expect(screen.getByText('Home page')).toBeInTheDocument())
})
