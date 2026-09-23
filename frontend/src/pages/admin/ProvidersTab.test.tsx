import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { ProvidersTab } from './ProvidersTab'
import { stubFetchByUrl, jsonResponse } from '../../test-utils'

afterEach(() => cleanup())

const CONFIG = [
  { key: 'provider_stack.vision_provider', value: 'openai', default: 'openai', is_override: false, updated_by: null, updated_at: null },
  { key: 'provider_stack.copy_provider', value: 'openai', default: 'openai', is_override: false, updated_by: null, updated_at: null },
  { key: 'provider_stack.web_search_provider', value: 'openai_web_search', default: 'openai_web_search', is_override: false, updated_by: null, updated_at: null },
  { key: 'provider_stack.image_process_provider', value: 'photoroom_clipdrop', default: 'photoroom_clipdrop', is_override: false, updated_by: null, updated_at: null },
  { key: 'provider_stack.json_repair_provider', value: 'openai', default: 'claude', is_override: true, updated_by: 1, updated_at: '2026-08-21T00:00:00Z' },
]

test('renders one selector per provider seam with its current value', async () => {
  vi.stubGlobal('fetch', stubFetchByUrl({ '/v1/admin/config': () => jsonResponse({ config: CONFIG }) }))

  render(<ProvidersTab />)

  await waitFor(() => expect(screen.getByLabelText(/vision provider/i)).toBeInTheDocument())
  expect(screen.getByLabelText(/json repair provider/i)).toHaveValue('openai')
  expect(screen.getByText(/overridden/i)).toBeInTheDocument()
})

test('changing a selector saves the override', async () => {
  const fetchMock = stubFetchByUrl({
    '/v1/admin/config/provider_stack.vision_provider': () =>
      jsonResponse({ key: 'provider_stack.vision_provider', value: 'gemini', is_override: true, updated_by: 1, updated_at: 'now' }),
    '/v1/admin/config': () => jsonResponse({ config: CONFIG }),
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<ProvidersTab />)
  await waitFor(() => expect(screen.getByLabelText(/vision provider/i)).toBeInTheDocument())

  await userEvent.setup().selectOptions(screen.getByLabelText(/vision provider/i), 'gemini')

  await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
    expect.stringContaining('/v1/admin/config/provider_stack.vision_provider'),
    expect.objectContaining({ method: 'PUT' }),
  ))
})
