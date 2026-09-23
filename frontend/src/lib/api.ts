const CSRF_COOKIE = 'csrf_token'

function getCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return match ? decodeURIComponent(match[1]) : null
}

export async function apiFetch(path: string, options: RequestInit = {}): Promise<Response> {
  const method = (options.method ?? 'GET').toUpperCase()
  const headers = new Headers(options.headers)
  if (!(options.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }
  if (method !== 'GET') {
    const csrfToken = getCookie(CSRF_COOKIE)
    if (csrfToken) {
      headers.set('X-CSRF-Token', csrfToken)
    }
  }
  return fetch(path, { ...options, headers, credentials: 'include' })
}
