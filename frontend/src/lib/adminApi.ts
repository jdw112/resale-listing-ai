import { apiFetch } from './api'

export interface AdminConfigEntry {
  key: string
  value: unknown
  default: unknown
  is_override: boolean
  updated_by: number | null
  updated_at: string | null
}

export interface AdminSubmission {
  submission_id: string
  state: string
  status: string | null
  product_key: string | null
  sku: string | null
  cost_cad: number | null
  processed_at: string | null
  // AI website-hero review signals (present once the images stage has run)
  website_hero_review: string | null
  website_hero_rejected_url: string | null
  website_hero_prompt: string | null
}

// Same-origin admin proxy that streams the QC-rejected hero image; the session
// cookie rides along automatically on a plain link/navigation.
export function adminHeroImageUrl(submissionId: string): string {
  return `/v1/admin/hero-image/${encodeURIComponent(submissionId)}`
}

async function parseErrorDetail(res: Response, fallback: string): Promise<string> {
  try {
    const body = await res.json()
    return body.detail ?? fallback
  } catch {
    return fallback
  }
}

export async function fetchAdminConfig(): Promise<AdminConfigEntry[]> {
  const res = await apiFetch('/v1/admin/config')
  if (!res.ok) {
    throw new Error(await parseErrorDetail(res, 'failed to load config'))
  }
  const body = await res.json()
  return body.config
}

export async function updateAdminConfig(key: string, value: unknown): Promise<AdminConfigEntry> {
  const res = await apiFetch(`/v1/admin/config/${encodeURIComponent(key)}`, {
    method: 'PUT', body: JSON.stringify({ value }),
  })
  if (!res.ok) {
    throw new Error(await parseErrorDetail(res, 'failed to update config'))
  }
  return res.json()
}

export async function deleteAdminConfig(key: string): Promise<void> {
  const res = await apiFetch(`/v1/admin/config/${encodeURIComponent(key)}`, { method: 'DELETE' })
  if (!res.ok) {
    throw new Error(await parseErrorDetail(res, 'failed to reset config'))
  }
}

export async function fetchAdminSubmissions(
  params: { status?: string; limit?: number; offset?: number } = {},
): Promise<{ submissions: AdminSubmission[]; total: number }> {
  const query = new URLSearchParams()
  if (params.status) query.set('status', params.status)
  if (params.limit) query.set('limit', String(params.limit))
  if (params.offset) query.set('offset', String(params.offset))
  const qs = query.toString()
  const res = await apiFetch(`/v1/admin/submissions${qs ? `?${qs}` : ''}`)
  if (!res.ok) {
    throw new Error(await parseErrorDetail(res, 'failed to load submissions'))
  }
  return res.json()
}
