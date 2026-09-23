import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { AuthProvider } from '../context/AuthContext'
import { SubmissionResult } from './SubmissionResult'
import { stubFetchByUrl, jsonResponse } from '../test-utils'

describe('SubmissionResult', () => {
  // This repo's vitest config does not enable `globals: true`, so
  // `@testing-library/react`'s automatic afterEach cleanup (which relies on a
  // global `afterEach`) never registers. Without an explicit cleanup call,
  // each test's rendered DOM lingers into the next test in this file.
  afterEach(() => {
    cleanup()
  })

  it('shows the clarification-answer form for a Clarification required outcome, and submits it', async () => {
    // '/v1/me/submissions/sub-1/answer' is listed first: stubFetchByUrl matches by
    // substring in insertion order, and it's a superstring of the plain-GET route
    // below, so the more specific route must be checked first or every POST to
    // .../answer would incorrectly match the plain GET route instead.
    vi.stubGlobal('fetch', stubFetchByUrl({
      '/v1/auth/me': () => new Response(null, { status: 401 }),
      '/v1/me/submissions/sub-1/answer': () => jsonResponse({ submission_id: 'sub-2', status: 'RECEIVED' }),
      '/v1/me/submissions/sub-1': () => jsonResponse({
        submission_id: 'sub-1', state: 'CLARIFICATION_REQUIRED', outcome: 'Clarification required',
        missing: ['ModelNumber/UPC'], product: null, listing: null,
      }),
    }))

    render(
      <MemoryRouter initialEntries={['/submissions/sub-1']}>
        <AuthProvider>
          <Routes>
            <Route path="/submissions/:id" element={<SubmissionResult />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByLabelText('ModelNumber/UPC')).toBeInTheDocument())

    await userEvent.type(screen.getByLabelText('ModelNumber/UPC'), 'XYZ123')
    await userEvent.click(screen.getByRole('button', { name: 'Submit answer' }))

    await waitFor(() => expect(screen.getByText('Thanks — reprocessing your answer.')).toBeInTheDocument())
  })

  it('shows the full product record for a Published outcome, no answer form', async () => {
    vi.stubGlobal('fetch', stubFetchByUrl({
      '/v1/auth/me': () => new Response(null, { status: 401 }),
      '/v1/me/submissions/sub-3': () => jsonResponse({
        submission_id: 'sub-3', state: 'PUBLISHED', outcome: 'Accepted', missing: [],
        product: { Brand: { value: 'Acme' } }, listing: { SKU: { value: 'RL-ABCD-123' } },
      }),
    }))

    render(
      <MemoryRouter initialEntries={['/submissions/sub-3']}>
        <AuthProvider>
          <Routes>
            <Route path="/submissions/:id" element={<SubmissionResult />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByText('Acme')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('RL-ABCD-123')).toBeInTheDocument())
    expect(screen.getByText('Brand')).toBeInTheDocument()
    expect(screen.getByText('SKU')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Submit answer' })).not.toBeInTheDocument()
  })

  it('shows an error message when the answer submission fails', async () => {
    // Same specific-route-first ordering rationale as the first test above.
    vi.stubGlobal('fetch', stubFetchByUrl({
      '/v1/auth/me': () => new Response(null, { status: 401 }),
      '/v1/me/submissions/sub-1/answer': () =>
        jsonResponse({ detail: 'submission is not awaiting clarification' }, { status: 409 }),
      '/v1/me/submissions/sub-1': () => jsonResponse({
        submission_id: 'sub-1', state: 'CLARIFICATION_REQUIRED', outcome: 'Clarification required',
        missing: ['ModelNumber/UPC'], product: null, listing: null,
      }),
    }))

    render(
      <MemoryRouter initialEntries={['/submissions/sub-1']}>
        <AuthProvider>
          <Routes>
            <Route path="/submissions/:id" element={<SubmissionResult />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByLabelText('ModelNumber/UPC')).toBeInTheDocument())
    await userEvent.type(screen.getByLabelText('ModelNumber/UPC'), 'XYZ123')
    await userEvent.click(screen.getByRole('button', { name: 'Submit answer' }))

    await waitFor(() => expect(screen.getByText('submission is not awaiting clarification')).toBeInTheDocument())
  })
})
