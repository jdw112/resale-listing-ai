import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { AuthProvider } from '../context/AuthContext'
import { Submit } from './Submit'
import { stubFetchByUrl, jsonResponse } from '../test-utils'

describe('Submit', () => {
  // This repo's vitest config does not enable `globals: true`, so
  // `@testing-library/react`'s automatic afterEach cleanup (which relies on a
  // global `afterEach`) never registers. Without an explicit cleanup call,
  // each test's rendered DOM lingers into the next test in this file, and
  // since every test renders near-identical "Submit an item" forms, later
  // queries (e.g. the "Submit" button) match multiple elements.
  afterEach(() => {
    cleanup()
  })

  it('uploads a photo, submits, and navigates to the result page', async () => {
    vi.stubGlobal('fetch', stubFetchByUrl({
      '/v1/auth/me': () => new Response(null, { status: 401 }),
      '/v1/me/config/condition-grades': () => jsonResponse({ grades: ['Open Box', 'New'] }),
      '/v1/me/uploads': () => jsonResponse({ uploads: [{ ref: 'abc123.jpg', filename: 'photo1.jpg' }] }),
      '/v1/me/submissions': () => jsonResponse({ submission_id: 'sub-1', status: 'RECEIVED' }),
    }))

    render(
      <MemoryRouter initialEntries={['/submit']}>
        <AuthProvider>
          <Routes>
            <Route path="/submit" element={<Submit />} />
            <Route path="/submissions/:id" element={<p>result page</p>} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByLabelText('Add photos')).toBeInTheDocument())

    const file = new File(['fake-bytes'], 'photo1.jpg', { type: 'image/jpeg' })
    await userEvent.upload(screen.getByLabelText('Add photos'), file)
    await waitFor(() => expect(screen.getByText('photo1.jpg')).toBeInTheDocument())

    await userEvent.selectOptions(screen.getByLabelText('Condition'), 'Open Box')
    await userEvent.click(screen.getByRole('button', { name: 'Submit item' }))

    await waitFor(() => expect(screen.getByText('result page')).toBeInTheDocument())
  })

  it('disables submit until at least one photo is added', async () => {
    vi.stubGlobal('fetch', stubFetchByUrl({
      '/v1/auth/me': () => new Response(null, { status: 401 }),
      '/v1/me/config/condition-grades': () => jsonResponse({ grades: ['Open Box'] }),
    }))

    render(
      <MemoryRouter initialEntries={['/submit']}>
        <AuthProvider>
          <Submit />
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByRole('button', { name: 'Submit item' })).toBeDisabled())
  })

  it('shows an error message when submission creation fails', async () => {
    vi.stubGlobal('fetch', stubFetchByUrl({
      '/v1/auth/me': () => new Response(null, { status: 401 }),
      '/v1/me/config/condition-grades': () => jsonResponse({ grades: ['Open Box'] }),
      '/v1/me/uploads': () => jsonResponse({ uploads: [{ ref: 'abc123.jpg', filename: 'photo1.jpg' }] }),
      '/v1/me/submissions': () => jsonResponse({ detail: 'invalid image reference' }, { status: 422 }),
    }))

    render(
      <MemoryRouter initialEntries={['/submit']}>
        <AuthProvider>
          <Submit />
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByLabelText('Add photos')).toBeInTheDocument())
    const file = new File(['fake-bytes'], 'photo1.jpg', { type: 'image/jpeg' })
    await userEvent.upload(screen.getByLabelText('Add photos'), file)
    await waitFor(() => expect(screen.getByText('photo1.jpg')).toBeInTheDocument())
    await userEvent.selectOptions(screen.getByLabelText('Condition'), 'Open Box')
    await userEvent.click(screen.getByRole('button', { name: 'Submit item' }))

    await waitFor(() => expect(screen.getByText('invalid image reference')).toBeInTheDocument())
  })
})
