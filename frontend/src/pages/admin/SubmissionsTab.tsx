import { useEffect, useState } from 'react'
import { fetchAdminSubmissions, adminHeroImageUrl } from '../../lib/adminApi'
import type { AdminSubmission } from '../../lib/adminApi'

const POLL_INTERVAL_MS = 10_000
const PAGE_SIZE = 50

// Hero cell: a "Review" link to the rejected image when the AI website hero was
// flagged and an image exists; a plain flag when it was flagged but nothing was
// generated to inspect (e.g. no usable product photo); otherwise a dash. The
// generation prompt, when present, is the link's tooltip.
function renderHeroReview(s: AdminSubmission) {
  if (s.website_hero_review !== 'Yes') {
    return '—'
  }
  const tooltip = s.website_hero_prompt ?? undefined
  if (s.website_hero_rejected_url) {
    return (
      <a href={adminHeroImageUrl(s.submission_id)} target="_blank" rel="noreferrer" title={tooltip}>
        Review
      </a>
    )
  }
  return <span title={tooltip}>Review (no image)</span>
}

export function SubmissionsTab() {
  const [submissions, setSubmissions] = useState<AdminSubmission[]>([])
  const [total, setTotal] = useState(0)
  const [statusFilter, setStatusFilter] = useState('')
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  // A filter change should return to page 1.
  useEffect(() => {
    setOffset(0)
  }, [statusFilter])

  useEffect(() => {
    let cancelled = false

    async function load() {
      try {
        const result = await fetchAdminSubmissions({
          ...(statusFilter ? { status: statusFilter } : {}),
          limit: PAGE_SIZE,
          offset,
        })
        if (!cancelled) {
          setError(null)
          setSubmissions(result.submissions)
          setTotal(result.total)
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'failed to load submissions')
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    load()
    const interval = setInterval(load, POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      clearInterval(interval)
    }
  }, [statusFilter, offset])

  if (loading) {
    return <p>Loading...</p>
  }

  return (
    <div>
      <h2>Submissions</h2>
      {error && <p role="alert">{error}</p>}
      <label htmlFor="status-filter">Filter by status</label>
      <select id="status-filter" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
        <option value="">All</option>
        <option value="Accepted">Accepted</option>
        <option value="Accepted with unknowns">Accepted with unknowns</option>
        <option value="Clarification required">Clarification required</option>
        <option value="Rejected">Rejected</option>
      </select>
      <p>{total} total</p>
      <table>
        <thead>
          <tr>
            <th>Submission ID</th><th>State</th><th>Status</th><th>Product</th><th>SKU</th><th>Cost (CAD)</th><th>Processed</th><th>Hero</th>
          </tr>
        </thead>
        <tbody>
          {submissions.map((s) => (
            <tr key={s.submission_id}>
              <td>{s.submission_id}</td>
              <td>{s.state}</td>
              <td>{s.status ?? '—'}</td>
              <td>{s.product_key ?? '—'}</td>
              <td>{s.sku ?? '—'}</td>
              <td>{s.cost_cad != null ? s.cost_cad.toFixed(2) : '—'}</td>
              <td>{s.processed_at ?? '—'}</td>
              <td>{renderHeroReview(s)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div>
        <button type="button" onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))} disabled={offset === 0}>
          Previous
        </button>
        <button type="button" onClick={() => setOffset(offset + PAGE_SIZE)} disabled={offset + PAGE_SIZE >= total}>
          Next
        </button>
      </div>
    </div>
  )
}
