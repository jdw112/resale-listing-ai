import { afterEach, expect, test, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { BusinessConfigTab } from './BusinessConfigTab'
import { stubFetchByUrl, jsonResponse } from '../../test-utils'

afterEach(() => cleanup())

const CONFIG = [
  {
    key: 'business.oversized_ups_ca',
    value: { longest_side_cm: 274.32, length_plus_girth_cm: 330, weight_kg: 68 },
    default: { longest_side_cm: 274.32, length_plus_girth_cm: 330, weight_kg: 68 },
    is_override: false, updated_by: null, updated_at: null,
  },
  {
    key: 'business.categories',
    value: { 'Smart Home': ['Cameras'] }, default: { 'Smart Home': ['Cameras'] },
    is_override: false, updated_by: null, updated_at: null,
  },
  {
    key: 'business.condition_scale',
    value: { provided_grades: ['New'], ai_suggested_grades: ['New'] },
    default: { provided_grades: ['New'], ai_suggested_grades: ['New'] },
    is_override: false, updated_by: null, updated_at: null,
  },
]

test('renders the three oversized-package number inputs with current values', async () => {
  vi.stubGlobal('fetch', stubFetchByUrl({ '/v1/admin/config': () => jsonResponse({ config: CONFIG }) }))

  render(<BusinessConfigTab />)

  await waitFor(() => expect(screen.getByLabelText(/longest side/i)).toBeInTheDocument())
  expect(screen.getByLabelText(/longest side/i)).toHaveValue(274.32)
  expect(screen.getByLabelText(/length \+ girth/i)).toHaveValue(330)
  expect(screen.getByLabelText(/weight/i)).toHaveValue(68)
})

test('saving oversized rules submits the three numbers as an object', async () => {
  const fetchMock = stubFetchByUrl({
    '/v1/admin/config/business.oversized_ups_ca': () =>
      jsonResponse({ key: 'business.oversized_ups_ca', value: { longest_side_cm: 200, length_plus_girth_cm: 330, weight_kg: 68 }, is_override: true, updated_by: 1, updated_at: 'now' }),
    '/v1/admin/config': () => jsonResponse({ config: CONFIG }),
  })
  vi.stubGlobal('fetch', fetchMock)
  render(<BusinessConfigTab />)
  await waitFor(() => expect(screen.getByLabelText(/longest side/i)).toBeInTheDocument())
  const user = userEvent.setup()

  await user.clear(screen.getByLabelText(/longest side/i))
  await user.type(screen.getByLabelText(/longest side/i), '200')
  await user.click(screen.getByRole('button', { name: /save oversized rules/i }))

  await waitFor(() => {
    const call = fetchMock.mock.calls.find(([url]) => String(url).includes('business.oversized_ups_ca')) as
      | [RequestInfo | URL, RequestInit]
      | undefined
    expect(call).toBeTruthy()
    const body = JSON.parse((call![1] as RequestInit).body as string)
    expect(body.value).toEqual({ longest_side_cm: 200, length_plus_girth_cm: 330, weight_kg: 68 })
  })
})

test('renders raw-JSON editors for categories and condition scale', async () => {
  vi.stubGlobal('fetch', stubFetchByUrl({ '/v1/admin/config': () => jsonResponse({ config: CONFIG }) }))

  render(<BusinessConfigTab />)

  await waitFor(() => expect(screen.getByLabelText(/categories/i)).toBeInTheDocument())
  expect(screen.getByLabelText(/categories/i)).toHaveValue(JSON.stringify(CONFIG[1].value, null, 2))
  expect(screen.getByLabelText(/condition scale/i)).toHaveValue(JSON.stringify(CONFIG[2].value, null, 2))
})
