import { vi } from 'vitest'

/** Route a mocked global.fetch by URL substring, independent of call order --
 * needed because a page's own data-fetch effect and AuthProvider's `/v1/auth/me`
 * effect race (React fires a child's mount effect before its parent's), which
 * breaks any mock chained purely by call order. */
export function stubFetchByUrl(routes: Record<string, () => Promise<Response> | Response>) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = typeof input === 'string' ? input : input.toString()
    for (const [substring, handler] of Object.entries(routes)) {
      if (url.includes(substring)) {
        return Promise.resolve(handler())
      }
    }
    throw new Error(`stubFetchByUrl: no route matched ${url}`)
  })
}

export function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200, headers: { 'Content-Type': 'application/json' }, ...init,
  })
}
