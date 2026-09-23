import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { AuthProvider } from '../../context/AuthContext'
import { AdminConsole } from './AdminConsole'
import { stubFetchByUrl, jsonResponse } from '../../test-utils'

afterEach(() => cleanup())

function renderConsole() {
  render(
    <MemoryRouter>
      <AuthProvider>
        <AdminConsole />
      </AuthProvider>
    </MemoryRouter>,
  )
}

test('renders all four tab labels and defaults to the Providers tab', async () => {
  vi.stubGlobal('fetch', stubFetchByUrl({
    '/v1/auth/me': () => jsonResponse({ id: 1, email: 'admin@example.com', role: 'admin' }),
    '/v1/admin/config': () => jsonResponse({ config: [] }),
  }))

  renderConsole()

  await waitFor(() => expect(screen.getByRole('tab', { name: 'Providers' })).toBeInTheDocument())
  expect(screen.getByRole('tab', { name: 'Gate & Fields' })).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: 'Business Config' })).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: 'Submissions' })).toBeInTheDocument()
  expect(screen.getByRole('tab', { name: 'Providers' })).toHaveAttribute('aria-selected', 'true')
})

test('clicking a tab switches the selected tab', async () => {
  const { default: userEvent } = await import('@testing-library/user-event')
  vi.stubGlobal('fetch', stubFetchByUrl({
    '/v1/auth/me': () => jsonResponse({ id: 1, email: 'admin@example.com', role: 'admin' }),
    '/v1/admin/config': () => jsonResponse({ config: [] }),
    '/v1/admin/submissions': () => jsonResponse({ submissions: [], total: 0 }),
  }))
  renderConsole()
  await waitFor(() => expect(screen.getByRole('tab', { name: 'Submissions' })).toBeInTheDocument())

  await userEvent.setup().click(screen.getByRole('tab', { name: 'Submissions' }))

  expect(screen.getByRole('tab', { name: 'Submissions' })).toHaveAttribute('aria-selected', 'true')
  expect(screen.getByRole('tab', { name: 'Providers' })).toHaveAttribute('aria-selected', 'false')
})
