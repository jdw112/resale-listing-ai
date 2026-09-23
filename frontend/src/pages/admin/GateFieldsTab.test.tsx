import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GateFieldsTab } from './GateFieldsTab'
import { stubFetchByUrl, jsonResponse } from '../../test-utils'

afterEach(() => cleanup())

const CONFIG = [
  { key: 'gate.threshold', value: 0.6, default: 0.6, is_override: false, updated_by: null, updated_at: null },
  { key: 'gate.mandatory_fields', value: ['Brand', 'ProductName', 'InternalCategory'], default: ['Brand', 'ProductName', 'InternalCategory'], is_override: false, updated_by: null, updated_at: null },
  { key: 'gate.core_fields', value: ['Brand', 'ProductName'], default: ['Brand', 'ProductName'], is_override: false, updated_by: null, updated_at: null },
]

test('renders the threshold input and the two field-list textareas', async () => {
  vi.stubGlobal('fetch', stubFetchByUrl({ '/v1/admin/config': () => jsonResponse({ config: CONFIG }) }))

  render(<GateFieldsTab />)

  await waitFor(() => expect(screen.getByLabelText(/gate threshold/i)).toBeInTheDocument())
  expect(screen.getByLabelText(/gate threshold/i)).toHaveValue(0.6)
  expect(screen.getByLabelText(/mandatory fields/i)).toHaveValue('Brand, ProductName, InternalCategory')
})

test('saving the threshold submits a number, not a string', async () => {
  const fetchMock = stubFetchByUrl({
    '/v1/admin/config/gate.threshold': () =>
      jsonResponse({ key: 'gate.threshold', value: 0.75, is_override: true, updated_by: 1, updated_at: 'now' }),
    '/v1/admin/config': () => jsonResponse({ config: CONFIG }),
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<GateFieldsTab />)
  await waitFor(() => expect(screen.getByLabelText(/gate threshold/i)).toBeInTheDocument())
  const user = userEvent.setup()

  await user.clear(screen.getByLabelText(/gate threshold/i))
  await user.type(screen.getByLabelText(/gate threshold/i), '0.75')
  await user.click(screen.getByRole('button', { name: /save threshold/i }))

  await waitFor(() => {
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes('gate.threshold')) as
      | [RequestInfo | URL, RequestInit]
      | undefined
    expect(call).toBeTruthy()
    expect(JSON.parse((call![1] as RequestInit).body as string)).toEqual({ value: 0.75 })
  })
})
