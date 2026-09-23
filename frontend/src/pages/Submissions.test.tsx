import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { AuthProvider } from '../context/AuthContext'
import { Submissions } from './Submissions'
import { stubFetchByUrl, jsonResponse } from '../test-utils'

const THREADS_BODY = {
  threads: [
    {
      root: { submission_id: 'root-1', state: 'CLARIFICATION_REQUIRED', outcome: 'Clarification required', missing: ['ModelNumber/UPC'], product_key: 'widget-x', listing_sku: 'SKU-1', cost_cad: 0.42 },
      resubmissions: [
        { submission_id: 'resub-1', state: 'PUBLISHED', outcome: 'Accepted', missing: [], product_key: 'widget-x', listing_sku: 'SKU-1', cost_cad: 0.55 },
      ],
    },
  ],
}

describe('Submissions', () => {
  // This repo's vitest config does not enable `globals: true`, so
  // `@testing-library/react`'s automatic afterEach cleanup (which relies on a
  // global `afterEach`) never registers. Without an explicit cleanup call,
  // each test's rendered DOM lingers into the next test in this file.
  afterEach(() => {
    cleanup()
  })

  it('shows one row per thread with the latest outcome and item details, expandable to the history', async () => {
    vi.stubGlobal('fetch', stubFetchByUrl({
      '/v1/auth/me': () => new Response(null, { status: 401 }),
      '/v1/me/submissions': () => jsonResponse(THREADS_BODY),
    }))

    render(
      <MemoryRouter initialEntries={['/submissions']}>
        <AuthProvider>
          <Routes>
            <Route path="/submissions" element={<Submissions />} />
            <Route path="/submissions/:id" element={<p>result page</p>} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByText('Accepted')).toBeInTheDocument())
    // Latest version's item details surface in the row.
    expect(screen.getByText('widget-x')).toBeInTheDocument()
    expect(screen.getByText('SKU-1')).toBeInTheDocument()
    expect(screen.getByText('$0.55')).toBeInTheDocument()
    // Root (superseded) status is hidden until history is expanded.
    expect(screen.queryByText('Clarification required')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: '2 versions' }))

    await waitFor(() => expect(screen.getByText('Clarification required')).toBeInTheDocument())
    expect(screen.getByText(/Original/)).toBeInTheDocument()
  })
})
