import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import { SubmissionsTab } from './SubmissionsTab'
import { stubFetchByUrl, jsonResponse } from '../../test-utils'

afterEach(() => cleanup())

function row(overrides: Record<string, unknown> = {}) {
  return {
    submission_id: 'sub-1', state: 'DONE', status: 'Accepted', product_key: 'PK-1',
    sku: 'SKU-1', cost_cad: 1.5, processed_at: '2026-09-01T00:00:00Z',
    website_hero_review: null, website_hero_rejected_url: null, website_hero_prompt: null,
    ...overrides,
  }
}

function stubSubmissions(submissions: unknown[]) {
  vi.stubGlobal('fetch', stubFetchByUrl({
    '/v1/admin/submissions': () => jsonResponse({ submissions, total: submissions.length }),
  }))
}

test('flagged row links Review to the hero-image proxy with the prompt as tooltip', async () => {
  stubSubmissions([row({
    submission_id: 'sub-flagged', website_hero_review: 'Yes',
    website_hero_rejected_url: 'file:///x/website_hero_rejected.jpg',
    website_hero_prompt: 'the exact hero prompt',
  })])

  render(<SubmissionsTab />)

  const link = await screen.findByRole('link', { name: /review/i })
  expect(link).toHaveAttribute('href', '/v1/admin/hero-image/sub-flagged')
  expect(link).toHaveAttribute?.('target', '_blank')
  expect(link).toHaveAttribute('title', 'the exact hero prompt')
})

test('non-flagged row shows a dash and no review link', async () => {
  stubSubmissions([row()])

  render(<SubmissionsTab />)

  await waitFor(() => expect(screen.getByText('sub-1')).toBeInTheDocument())
  expect(screen.queryByRole('link', { name: /review/i })).not.toBeInTheDocument()
})

test('flagged row with no rejected image shows a plain flag, not a link', async () => {
  stubSubmissions([row({
    submission_id: 'sub-noimg', website_hero_review: 'Yes',
    website_hero_rejected_url: null, website_hero_prompt: 'prompt here',
  })])

  render(<SubmissionsTab />)

  const cell = await screen.findByText(/review \(no image\)/i)
  expect(cell).toBeInTheDocument()
  expect(within(cell.closest('tr') as HTMLElement).queryByRole('link')).not.toBeInTheDocument()
})
